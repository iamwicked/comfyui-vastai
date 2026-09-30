"""The stage engine: the loop every ComfyUI stage shares (design §A13, §M4, §C6.1; Req 9.5-9.11, 10.3-10.10,
10.13-10.16, 11.2, 11.10, 21.3).

    run_stage(stage, week, *, ..., dry_run=False) -> exit status   render or finish, from the files on disk
    execute(run, *, ..., dry_run=False) -> exit status             any StageRun (the stage-neutral core)
    disk_files(output_dir) -> jobs.Files                            the Files a builder reads on disk
    recorded_durations(root), median(values), estimate(...)         the dry-run estimate (Req 9.6)

run_stage takes the week lock (locks.ProcessLock on weeks/<W>/.lock; exit 4 with the holder's PID, before
the Manifest is read or written), reads the Persona_File, the Content_Calendar, the Prompt_Sheet and the
Manifest (an unreadable Manifest: exit 2, the file untouched), checks the Persona_File and every Instagram
Post against the Instagram_Allowlist, and hands a StageRun to execute. Its `build` is the stage's builder of
jobs.py, called again with the current Manifest after every done Job, so an Edit Slide whose Slide-1 Pick
has just been rendered becomes ready inside the same run.

execute:
1. builds the Jobs; a dry run writes one filled prompt per Job to submit into
   Output_Root/dry-run/<stage>-<scope>-<UTC stamp>/, reports the counts and the estimate per
   Render_Template, runs the preflight and reports it, then exits 0 or 2; it writes no Manifest.
2. runs the preflight of task 19 (exit 2 on any failure, the Manifest untouched), then, for render, asks
   Ollama to unload its models (a refusal or silence is a warning).
3. loops until no Job is queued, running or ready. Each pass classifies every Job:
   waiting (an input file is missing: recorded `waiting`), done (its key is done and every recorded
   output exists and is over 0 bytes), attach (its key is `submitted`: its prompt id is polled like
   any in-flight one, so a prompt ComfyUI still has is waited for and never sent again), or ready.
   Ready Jobs are submitted while fewer than MAX_IN_FLIGHT are in flight: the inputs are uploaded, the
   record `submitted` with a fresh UUID4 is written through write_atomic, and only then is the prompt
   POSTed (a kill between the two leaves a record the next run attaches to or re-checks). A 400 with
   node_errors fails the Job with ComfyUI's message, not resubmitted in this run.
   Every Timing.poll_s: GET /queue, and GET /history/<id> for each in-flight id not in the queue.
   Success checks each output (exists, over 0 bytes, and for an `edit` Job the size-table size) and
   takes duration_s from the execution_start and execution_success timestamps; an execution error or
   interruption fails the Job with exception_message. A Job seen running past its timeout_s gets
   POST /interrupt and fails "timed out after N s" once history shows it or Timing.interrupt_grace_s
   has passed. A Job absent from queue and history twice in a row is resubmitted while it has been
   resubmitted fewer than MAX_RESUBMITS times in this run, else failed "lost by ComfyUI".
   A ComfyUI that does not answer (Timing.silence_s per call) is retried for Timing.silence_budget_s
   without any status change, then the stage exits 2 naming the URL.
   Every status change is written to the Manifest before the next submission.
4. prints the summary: each failed Job with its error, each waiting Job with the input it lacks (by Post,
   Slide and Candidate), the Posts waiting for planning or review, invalid Picks, Slides waiting for a
   Pick with each Candidate's file, and the Instagram Posts the allowlist refused.

Exit statuses (errors.Exit): 0 no Job failed (waiting Jobs included); 3 a Job failed, an Instagram Post
was refused or a Pick is invalid (Req 11.10); 2 an environment, preflight or connectivity failure; 4 the
lock is held; 1 invalid input (a Persona_File without a stills Character_LoRA among them); 130 Ctrl-C, which
submits nothing more and leaves the in-flight Jobs recorded `submitted` for the next run to attach to.

I/O happens only at the edges: the ComfyUI and Ollama clients, the clock, the sleep and the wall clock are
passed in, so the tests run the loop against the fakes with the shortened Timing.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import math
import os
import stat
import sys
import time
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, TextIO

from influencer import env
from influencer import jobs as J
from influencer import preflight as PF
from influencer.atomic import write_atomic
from influencer.comfy import ERROR, INTERRUPTED, SUCCESS, UPLOAD_SUBFOLDER, ComfyClient, History, Rejected
from influencer.errors import EnvironmentFailure, Exit, Problem, ValidationError, format_problems
from influencer.http import HttpError, TimedOut, Unreachable
from influencer.locks import LockHeld, ProcessLock
from influencer.manifest import (
    DONE,
    FAILED,
    FILE_NAME as MANIFEST_NAME,
    SUBMITTED,
    WAITING,
    JobRecord,
    Manifest,
    Output,
    load_manifest,
    read_manifest,
    write_manifest,
)
from influencer.ollama import OllamaClient
from influencer.timing import DEFAULT_TIMING, Timing

MAX_IN_FLIGHT = 2  # Jobs submitted and not finished, at most (§A13)
ABSENT_CHECKS = 2  # absent from queue and history this many polls in a row: lost (checked, then re-checked once)
MAX_RESUBMITS = 2  # a lost Job is resubmitted while it has been resubmitted fewer times in this run (Req 10.5)
INTERRUPTED_EXIT = 130  # Ctrl-C, as a shell reports SIGINT
LOCK_NAME = ".lock"  # weeks/<W>/.lock
DRY_RUN_FOLDER = "dry-run"
READY = "ready"  # a Job state beside the Manifest statuses
BUILDERS: Mapping[str, Callable[..., J.Build]] = {J.RENDER: J.build_render, J.FINISH: J.build_finish}


class ComfyUISilent(EnvironmentFailure):
    """ComfyUI gave no answer for Timing.silence_budget_s after the preflight (Req 10.15): exit 2."""

    def __init__(self, url: str, budget: float, error: Exception) -> None:
        self.url = url
        super().__init__(
            f"ComfyUI at {url} gave no answer for {budget:g} s ({error}); no Job's status was changed, and the "
            "next run attaches to the Jobs recorded as submitted"
        )


# ---- clocks, files and small helpers ----


@dataclass(frozen=True)
class Clock:
    """The time sources of a run: monotonic for timeouts, sleep between polls, wall for the Manifest's
    timestamps and the dry-run stamp. Tests pass their own."""

    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    wall: Callable[[], float] = time.time


def utc_stamp(at: float) -> str:
    """2026-09-28T10:02:11Z, the Manifest's timestamp form."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def disk_files(output_dir: str | os.PathLike[str]) -> J.Files:
    """The Files a builder uses on disk; output_dir is ComfyUI's output folder. image_size reads the image
    header with Pillow."""

    def size(path: Path) -> int | None:
        try:
            info = os.stat(path)
        except OSError:
            return None
        return info.st_size if stat.S_ISREG(info.st_mode) else None

    def read(path: Path) -> bytes:
        return Path(path).read_bytes()

    def image_size(path: Path) -> tuple[int, int]:
        from PIL import Image  # the render and finish commands need Pillow (their import guard checks it)

        with Image.open(path) as image:
            return int(image.size[0]), int(image.size[1])

    return J.Files(Path(output_dir), size, read, image_size)


def _outputs_present(record: JobRecord, files: J.Files) -> bool:
    return bool(record.outputs) and all(files.present(files.output_dir / output.path) for output in record.outputs)


def _limited(client: Any, **limits: float) -> Any:
    """A shallow copy of a client whose Timing has these limits (the connection settings are shared)."""
    view = copy.copy(client)
    view.timing = dataclasses.replace(client.timing, **limits)
    return view


def job_name(job: J.Job) -> str:
    """How the summary names a Job: Post, Slide and Candidate, then its id."""
    if job.post is None:
        return job.id
    parts = [f"Post {job.post}"]
    if job.slide is not None:
        parts.append(f"Slide {job.slide}")
    if job.candidate is not None:
        parts.append(f"Candidate {job.candidate}")
    return f"{', '.join(parts)} ({job.id})"


def _size(size: tuple[int, int]) -> str:
    return f"{size[0]}×{size[1]}"


# ---- the Manifest in memory ----


class Ledger:
    """The Manifest a stage run keeps: records unique by job_key, waiting records unique by id, old records
    kept (§M4). save() replaces the file atomically (write_manifest)."""

    def __init__(self, path: Path, manifest: Manifest) -> None:
        self.path = Path(path)
        self.kind, self.scope = manifest.kind, manifest.scope
        self.records: list[JobRecord] = list(manifest.jobs)
        self.saves = 0

    @property
    def manifest(self) -> Manifest:
        return Manifest(self.kind, self.scope, tuple(self.records))

    def keyed(self, key: str) -> JobRecord | None:
        return next((record for record in self.records if record.job_key == key), None)

    def put(self, record: JobRecord) -> bool:
        """Adds or replaces record; True when the Manifest changed. A keyed record replaces the one with its
        job_key and drops a waiting record of its id; a waiting record replaces the waiting one of its id."""
        before = list(self.records)
        if record.status == WAITING:
            same = lambda other: other.status == WAITING and other.id == record.id  # noqa: E731
        else:
            same = lambda other: other.job_key == record.job_key  # noqa: E731
            self.records = [other for other in self.records if not (other.status == WAITING and other.id == record.id)]
        for index, other in enumerate(self.records):
            if same(other):
                self.records[index] = record
                break
        else:
            self.records.append(record)
        return self.records != before

    def save(self) -> None:
        write_manifest(self.path, self.manifest)
        self.saves += 1


def lacks(job: J.Job, files: J.Files) -> str | None:
    """What a Job waits for: its builder's waiting_for, else the first input file that is missing or empty."""
    if job.waiting_for is not None:
        return job.waiting_for
    for name, path in job.inputs.items():
        if not files.present(path):
            return f"the {name} file {path}"
    return None


def job_state(job: J.Job, manifest: Manifest, files: J.Files) -> tuple[str, str | None, str | None]:
    """(state, key, what it lacks): WAITING, DONE (key done and every output on disk, Req 10.3), SUBMITTED
    (a record to attach to, Req 10.13) or READY. Reads the input files for the key."""
    reason = lacks(job, files)
    if reason is not None:
        return WAITING, None, reason
    key = job.key(files.read)
    record = next((item for item in manifest.jobs if item.job_key == key), None)
    if record is not None and record.status == DONE and _outputs_present(record, files):
        return DONE, key, None
    if record is not None and record.status == SUBMITTED and record.prompt_id:
        return SUBMITTED, key, None
    return READY, key, None


# ---- what a stage run needs ----


@dataclass
class StageRun:
    """One stage over one scope, resolved: execute() needs nothing else from the disk.

    build(manifest) makes the Jobs from the current Manifest (called again after each done Job). root is the
    Influencer_Root, whose Manifests the dry-run estimate reads. refused maps each Instagram Post the
    allowlist refused to its Problems; notes are printed as warnings before the stage starts."""

    stage: str
    scope: str
    root: Path
    output_root: Path
    manifest_path: Path
    manifest: Manifest
    build: Callable[[Manifest], J.Build]
    files: J.Files
    config: Mapping[str, str | None] = field(default_factory=dict)
    refused: Mapping[str, Sequence[Problem]] = field(default_factory=dict)
    notes: Sequence[str] = ()
    unload_ollama: bool = False


@dataclass
class _Flight:
    job: J.Job
    key: str
    prompt_id: str
    started: float | None = None  # monotonic, when first seen running
    interrupted: float | None = None  # monotonic, when POST /interrupt was sent
    absent: int = 0  # polls in a row that found it neither queued nor in history


# ---- the loop (task 20.4, 20.5) ----


class _Loop:
    def __init__(self, run: StageRun, ledger: Ledger, comfy: ComfyClient, timing: Timing, clock: Clock,
                 err: TextIO) -> None:  # fmt: skip
        self.run = run
        self.ledger = ledger
        self.timing = timing
        self.comfy = _limited(comfy, request_s=min(comfy.timing.request_s, timing.silence_s))
        self.url = comfy.loopback.url
        self.clock = clock
        self.err = err
        self.build = J.Build(())
        self.flights: dict[str, _Flight] = {}
        self.failed: dict[str, tuple[J.Job, str]] = {}  # key -> (Job, error), failed in this run
        self.done_here: set[str] = set()  # keys that became done in this run
        self.done_before: set[str] = set()  # keys found done, skipped without a submission
        self.waiting: list[tuple[J.Job, str]] = []
        self.ready: list[tuple[J.Job, str]] = []
        self.resubmits: dict[str, int] = {}
        self.attached: set[str] = set()
        self.silent_since: float | None = None
        self.dirty = True

    # -- driving --

    def loop(self) -> None:
        while True:
            if self.dirty:
                self.build = self.run.build(self.ledger.manifest)
                self.dirty = False
            self.classify()
            if self.silent_since is None:
                self.submit()
            if not self.flights and not self.ready:
                return
            self.clock.sleep(self.timing.poll_s)
            self.poll()

    def _stamp(self) -> str:
        return utc_stamp(self.clock.wall())

    # -- ComfyUI calls and silence (Req 10.15) --

    def _call(self, transient: tuple[type[BaseException], ...], action: Callable[[], Any]) -> tuple[bool, Any]:
        """(True, the answer), or (False, None) after a transient failure, which counts toward the silence
        budget; past the budget, ComfyUISilent. Other errors propagate."""
        try:
            value = action()
        except transient as error:
            self._silent(error)  # type: ignore[arg-type]
            return False, None
        self.silent_since = None
        return True, value

    def _silent(self, error: Exception) -> None:
        now = self.clock.monotonic()
        if self.silent_since is None:
            self.silent_since = now
            print(f"warning: ComfyUI at {self.url} did not answer ({error}); retrying for up to "
                  f"{self.timing.silence_budget_s:g} s without changing any Job", file=self.err)  # fmt: skip
        if now - self.silent_since >= self.timing.silence_budget_s:
            raise ComfyUISilent(self.url, self.timing.silence_budget_s, error)

    # -- classifying (Req 10.3, 10.4, 10.13) --

    def classify(self) -> None:
        self.waiting, self.ready = [], []
        changed = False
        seen: set[str] = set()
        manifest = self.ledger.manifest
        for job in self.build.jobs:
            try:
                state, key, reason = job_state(job, manifest, self.run.files)
            except OSError as error:  # an input vanished while its bytes were read
                state, key, reason = WAITING, None, f"a readable input file ({error})"
            if state == WAITING:
                assert reason is not None
                self.waiting.append((job, reason))
                changed |= self.ledger.put(self._record(job, WAITING, waiting_for=reason))
                continue
            assert key is not None
            if key in seen or key in self.flights or key in self.failed:
                continue
            seen.add(key)
            if state == DONE:
                if key not in self.done_here:
                    self.done_before.add(key)
            elif state == SUBMITTED and key not in self.attached:
                record = self.ledger.keyed(key)
                assert record is not None and record.prompt_id is not None
                self.attached.add(key)
                self.flights[key] = _Flight(job, key, record.prompt_id)  # attach: polled, never re-sent
            else:
                self.ready.append((job, key))
        if changed:
            self.ledger.save()

    def _record(self, job: J.Job, status: str, **values: Any) -> JobRecord:
        return JobRecord(job.id, job.stage, job.template_name, status, post=job.post, slide=job.slide,
                         candidate=job.candidate, seed=job.seed, **values)  # fmt: skip

    # -- submitting (Req 10.6, 10.9) --

    def submit(self) -> None:
        while self.ready and len(self.flights) < MAX_IN_FLIGHT and self.silent_since is None:
            job, key = self.ready.pop(0)
            self._post(job, key, resubmit=False)

    def _post(self, job: J.Job, key: str, *, resubmit: bool) -> None:
        """Uploads the inputs, records `submitted` with a new UUID4 (written), then POSTs the prompt."""
        try:
            prompt, uploads = job.prompt(self.run.files.read)
        except OSError as error:
            self._fail_unsent(job, key, f"cannot read an input file: {error}")
            return
        for name, data in uploads.items():
            folder, _, base = name.rpartition("/")
            try:
                ok, _ = self._call((TimedOut, Unreachable),
                                   lambda: self.comfy.upload(base, data, subfolder=folder or UPLOAD_SUBFOLDER))  # fmt: skip
            except HttpError as error:
                self._fail_unsent(job, key, f"ComfyUI refused the upload of {name}: {error}")
                return
            if not ok:
                return  # silent: the Job keeps its status and is tried again
        count = self.resubmits.get(key, 0) + (1 if resubmit else 0)
        self.resubmits[key] = count
        prompt_id = str(uuid.uuid4())
        self.ledger.put(self._record(job, SUBMITTED, job_key=key, prompt_id=prompt_id, resubmits=count,
                                     submitted_at=self._stamp()))  # fmt: skip
        self.ledger.save()  # before POST /prompt: a kill between the two leaves a record to re-check
        flight = _Flight(job, key, prompt_id)
        self.flights[key] = flight
        try:
            self._call((TimedOut, Unreachable), lambda: self.comfy.submit(prompt, prompt_id))
        except Rejected as rejected:
            self._fail(flight, str(rejected))
        except HttpError as error:
            self._fail(flight, f"ComfyUI did not take the prompt: {error}")
        # a silent submit leaves the flight: the polls find the prompt, or find it lost and resubmit it

    def _fail_unsent(self, job: J.Job, key: str, error: str) -> None:
        self.ledger.put(self._record(job, FAILED, job_key=key, error=error, finished_at=self._stamp()))
        self.ledger.save()
        self.failed[key] = (job, error)

    def _fail(self, flight: _Flight, error: str) -> None:
        record = self.ledger.keyed(flight.key)
        assert record is not None
        self.ledger.put(dataclasses.replace(record, status=FAILED, error=error, outputs=(), finished_at=self._stamp()))
        self.ledger.save()
        self.flights.pop(flight.key, None)
        self.failed[flight.key] = (flight.job, error)

    def _done(self, flight: _Flight, outputs: Sequence[Output], duration: float | None) -> None:
        record = self.ledger.keyed(flight.key)
        assert record is not None
        self.ledger.put(dataclasses.replace(record, status=DONE, outputs=tuple(outputs), duration_s=duration,
                                            error=None, finished_at=self._stamp()))  # fmt: skip
        self.ledger.save()
        self.flights.pop(flight.key, None)
        self.done_here.add(flight.key)
        self.dirty = True  # rebuild: dependent Jobs may be ready now

    # -- polling (Req 10.5, 10.7, 10.13) --

    def poll(self) -> None:
        if not self.flights and self.silent_since is None:
            return
        ok, queue = self._call((HttpError,), self.comfy.queue)
        if not ok:
            return
        for flight in list(self.flights.values()):
            if not self._poll_one(flight, queue):
                return  # a silent call: try again at the next poll

    def _poll_one(self, flight: _Flight, queue: Any) -> bool:
        now = self.clock.monotonic()
        if flight.prompt_id in queue.running and flight.started is None:
            flight.started = now
            record = self.ledger.keyed(flight.key)
            if record is not None and self.ledger.put(dataclasses.replace(record, started_at=self._stamp())):
                self.ledger.save()
        if flight.started is not None and flight.interrupted is None and now - flight.started > flight.job.timeout_s:
            ok, _ = self._call((HttpError,), lambda: self.comfy.interrupt(flight.prompt_id))
            if not ok:
                return False
            flight.interrupted = self.clock.monotonic()
        if flight.prompt_id in queue:
            flight.absent = 0
            self._interrupt_grace(flight)
            return True
        ok, history = self._call((HttpError,), lambda: self.comfy.history(flight.prompt_id))
        if not ok:
            return False
        if history is None or history.outcome is None:
            if flight.interrupted is not None:
                self._interrupt_grace(flight)
                return True
            if history is None:
                flight.absent += 1
                if flight.absent >= ABSENT_CHECKS:
                    self._lost(flight)
            return True
        self._outcome(flight, history)
        return True

    def _timed_out(self, flight: _Flight) -> str:
        return f"timed out after {flight.job.timeout_s} s"

    def _interrupt_grace(self, flight: _Flight) -> None:
        if flight.interrupted is not None and self.clock.monotonic() - flight.interrupted >= self.timing.interrupt_grace_s:
            self._fail(flight, self._timed_out(flight))

    def _lost(self, flight: _Flight) -> None:
        if self.resubmits.get(flight.key, 0) < MAX_RESUBMITS:
            self.flights.pop(flight.key, None)
            self._post(flight.job, flight.key, resubmit=True)
            return
        self._fail(flight, f"lost by ComfyUI: not queued, not running and not in its history, after "
                           f"{MAX_RESUBMITS} resubmits")  # fmt: skip

    def _outcome(self, flight: _Flight, history: History) -> None:
        if flight.interrupted is not None:
            self._fail(flight, self._timed_out(flight))
            return
        if history.outcome == ERROR:
            self._fail(flight, history.error or "ComfyUI reported an execution error without a message")
            return
        if history.outcome == INTERRUPTED:
            self._fail(flight, "interrupted in ComfyUI")
            return
        assert history.outcome == SUCCESS
        files = self.run.files
        outputs = []
        for item in history.outputs:
            if item.type != "output":
                continue
            relative = f"{item.subfolder}/{item.filename}" if item.subfolder else item.filename
            size = files.size(files.output_dir / relative)
            if size is None or size <= 0:
                self._fail(flight, f"ComfyUI reported the output {relative}, which is missing or empty")
                return
            outputs.append(Output(relative, size))
        if not outputs:
            self._fail(flight, "ComfyUI reported success but no output file")
            return
        expected = flight.job.expect_size
        if expected is not None:  # the §C7 output check of an edit Job
            for output in outputs:
                try:
                    found = files.image_size(files.output_dir / output.path)
                except Exception as error:  # noqa: BLE001 - Pillow raises several kinds for a bad file
                    self._fail(flight, f"the output {output.path} is not a readable image: {error}")
                    return
                if tuple(found) != tuple(expected):
                    self._fail(flight, f"the output {output.path} is {_size(found)}, but {flight.job.template_name} "
                                       f"renders {_size(expected)} for this Post's aspect")  # fmt: skip
                    return
        self._done(flight, outputs, _duration(history))


def _duration(history: History) -> float | None:
    """execution_success minus execution_start, in seconds, so queue time is not counted (§M4)."""
    start, end = history.events.get("execution_start"), history.events.get("execution_success")
    if start is None or end is None:
        return None
    return max(0.0, (end - start) / 1000)


# ---- the dry run (task 20.6) ----


def recorded_durations(root: str | os.PathLike[str]) -> dict[str, list[float]]:
    """duration_s of every done Job, per Render_Template, across weeks/*/manifest.json, grid/manifest.json
    and dataset/*-refined/.manifest.json under root; an unreadable Manifest is skipped."""
    root = Path(root)
    paths = [*sorted((root / "weeks").glob(f"*/{MANIFEST_NAME}")), root / "grid" / MANIFEST_NAME,
             *sorted((root / "dataset").glob("*-refined/.manifest.json"))]  # fmt: skip
    found: dict[str, list[float]] = {}
    for path in paths:
        if not path.is_file():
            continue
        try:
            manifest = read_manifest(path)
        except ValidationError:
            continue
        for record in manifest.jobs:
            if record.status == DONE and record.duration_s is not None:
                found.setdefault(record.template, []).append(record.duration_s)
    return found


def median(values: Iterable[float]) -> Fraction | None:
    """The median, exactly; for an even count the mean of the two middle values; None for no values."""
    ordered = sorted(Fraction(value) for value in values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def estimate(durations: Iterable[float], jobs: int) -> str:
    """`N min`: the median duration times the Jobs to submit, rounded up to whole minutes; `unknown`
    without a recorded duration (Req 9.6)."""
    middle = median(durations)
    if middle is None:
        return "unknown"
    return f"{math.ceil(middle * jobs / 60)} min"


def _dry_folder(output_root: Path, stage: str, scope: str, at: float) -> Path:
    base = output_root / DRY_RUN_FOLDER
    base.mkdir(parents=True, exist_ok=True)
    name = f"{stage}-{scope}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(at))}"
    for number in range(1, 1000):
        folder = base / (name if number == 1 else f"{name}-{number}")
        try:
            folder.mkdir()
            return folder
        except FileExistsError:
            continue
    raise EnvironmentFailure(f"{base}: no free dry-run folder name for {name}")


def _dry_run(run: StageRun, build: J.Build, comfy: ComfyClient, timing: Timing, clock: Clock, started: float,
             out: TextIO) -> tuple[int, list[J.Job]]:  # fmt: skip
    counts: dict[str, dict[str, int]] = {}
    submit: list[J.Job] = []
    keep: list[J.Job] = []  # the Jobs the preflight checks: to submit or waiting
    seen: set[str] = set()
    for job in build.jobs:
        state, key, _ = job_state(job, run.manifest, run.files)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        row = counts.setdefault(job.template_name, {"submit": 0, DONE: 0, WAITING: 0})
        if state == DONE:
            row[DONE] += 1
            continue
        keep.append(job)
        if state == WAITING:
            row[WAITING] += 1
        else:
            row["submit"] += 1
            submit.append(job)
    folder = _dry_folder(run.output_root, run.stage, run.scope, clock.wall())
    for job in submit:
        prompt, _ = job.prompt(run.files.read)
        text = json.dumps(prompt, indent=2, ensure_ascii=False) + "\n"
        write_atomic(folder / (job.id.replace("/", "_") + ".json"), text)
    durations = recorded_durations(run.root)
    print(f"{run.stage} {run.scope}: dry run; {len(submit)} prompt files in {folder}", file=out)
    for template, row in counts.items():
        guess = estimate(durations.get(template, ()), row["submit"])
        print(f"  {template}: {row['submit']} to submit, {row[DONE]} done, {row[WAITING]} waiting; "
              f"estimate {guess}", file=out)  # fmt: skip
    report = PF.run(J.preflight_input(keep, run.output_root, config=run.config), comfy=comfy, timing=timing,
                    started=started, clock=clock.monotonic)  # fmt: skip
    print(report.text(), file=out)
    return (Exit.OK if report.ok else Exit.ENVIRONMENT), keep


# ---- the summary (task 20.7) ----


def summary_lines(run: StageRun, build: J.Build, loop: _Loop | None) -> list[str]:
    """The closing summary (Req 10.8, 9.11, 11.8, 11.10, 21.3)."""
    lines = []
    if loop is not None:
        done = len(loop.done_here) + len(loop.done_before)
        lines.append(f"{run.stage} {run.scope}: {done} done ({len(loop.done_before)} already done), "
                     f"{len(loop.failed)} failed, {len(loop.waiting)} waiting")  # fmt: skip
        for job, error in loop.failed.values():
            lines.append(f"failed: {job_name(job)}: {error}")
        for job, reason in loop.waiting:
            lines.append(f"waiting: {job_name(job)}: needs {reason}")
    for post, reason in build.planning.items():
        lines.append(f"waiting for planning: Post {post!r} {reason}")
    for post in build.review:
        lines.append(f"waiting for review: {J.review_reason(post)}")
    for pick in build.invalid_picks:
        lines.append(f"invalid Pick: {pick.message}")
    for wait in build.pick_waits:
        rendered = "; ".join(f"Candidate {number}: {path if path is not None else 'not rendered yet'}"
                             for number, path in wait.candidates)  # fmt: skip
        lines.append(f"waiting for a Pick: Post {wait.post!r}, Slide {wait.slide}: {rendered}")
    for post, problems in run.refused.items():
        for problem in problems:
            lines.append(f"refused: Post {post!r}: {problem}")
    for post in build.left_calendar:
        lines.append(f"note: the Prompt_Sheet entry of Post {post!r} matches no Post of the calendar; it is kept")
    return lines


def exit_status(run: StageRun, build: J.Build, loop: _Loop) -> int:
    """3 when a Job failed, an Instagram Post was refused or a Pick is invalid; else 0 (Req 10.14)."""
    return Exit.FAILED if loop.failed or run.refused or build.invalid_picks else Exit.OK


# ---- the Ollama unload (Req 9.7, 9.10) ----


def unload_ollama(ollama: OllamaClient, timing: Timing, out: TextIO, err: TextIO) -> None:
    """Asks Ollama to unload every loaded model; a refusal or a silence of Timing.silence_s is a warning."""
    view = _limited(ollama, request_s=min(ollama.timing.request_s, timing.silence_s))
    try:
        names = view.unload_all()
    except EnvironmentFailure as error:
        print(f"warning: could not unload the Ollama models: Ollama at {ollama.url}: {error}; the stage continues",
              file=err)  # fmt: skip
        return
    if names:
        print(f"unloaded the Ollama models {', '.join(names)}", file=out)


# ---- the core ----


def execute(run: StageRun, *, comfy: ComfyClient, ollama: OllamaClient | None = None, timing: Timing | None = None,
            clock: Clock | None = None, started: float | None = None, dry_run: bool = False,
            out: TextIO | None = None, err: TextIO | None = None) -> int:  # fmt: skip
    """Steps 1-4 of the module docstring for one StageRun; returns the exit status. The caller holds the
    scope's lock. A ValidationError from the builder (a missing Character_LoRA) propagates."""
    timing = timing or DEFAULT_TIMING
    clock = clock or Clock()
    out = out or sys.stdout
    err = err or sys.stderr
    started = clock.monotonic() if started is None else started
    for note in run.notes:
        print(f"warning: {note}", file=err)
    build = run.build(run.manifest)  # NoCharacterLora stops the stage here, before any submission
    if dry_run:
        status, _ = _dry_run(run, build, comfy, timing, clock, started, out)
        for line in summary_lines(run, build, None):
            print(line, file=out)
        return status
    keep = [job for job in build.jobs if job_state(job, run.manifest, run.files)[0] != DONE]
    report = PF.run(J.preflight_input(keep, run.output_root, config=run.config), comfy=comfy, timing=timing,
                    started=started, clock=clock.monotonic)  # fmt: skip
    if not report.ok:
        print(f"{run.stage}: {report.text()}", file=err)
        return Exit.ENVIRONMENT
    if run.unload_ollama and ollama is not None:
        unload_ollama(ollama, timing, out, err)
    ledger = Ledger(run.manifest_path, run.manifest)
    loop = _Loop(run, ledger, comfy, timing, clock, err)
    try:
        loop.loop()
    except KeyboardInterrupt:
        left = sum(1 for record in ledger.records if record.status == SUBMITTED)
        print(f"{run.stage}: interrupted; no new Job was submitted, {left} Job(s) stay recorded as submitted, and "
              "the next run attaches to them", file=err)  # fmt: skip
        return INTERRUPTED_EXIT
    except ComfyUISilent as silent:
        print(f"{run.stage}: {silent}", file=err)
        for line in summary_lines(run, loop.build, loop):
            print(line, file=out)
        return Exit.ENVIRONMENT
    for line in summary_lines(run, loop.build, loop):
        print(line, file=out)
    return exit_status(run, loop.build, loop)


# ---- render and finish from the files on disk ----


def instagram_refusals(calendar: Any, allowlist: Any, path: str) -> dict[str, list[Problem]]:
    """Each Instagram Post whose own look list leaves the allowlist, with its Problems (Req 21.3). A Post
    without its own list uses the Persona_File's stacks, which check_persona has checked."""
    from influencer.allowlist import post_problems

    refused: dict[str, list[Problem]] = {}
    for post in calendar.posts:
        if post.looks is None:
            continue
        problems = post_problems(post.id, post.looks, allowlist, platform=post.platform, path=path)
        if problems:
            refused[post.id] = problems
    return refused


def _week_run(stage: str, week: str, paths: env.Paths, root: Path, folder: Path, *, only: Collection[str] | None,
              templates: Mapping[str, Any] | None, allowlist: Any) -> StageRun:  # fmt: skip
    from influencer import allowlist as A
    from influencer.calendar_io import find_calendar, read_calendar
    from influencer.persona import read_persona
    from influencer.prompt_sheet import FILE_NAME as SHEET_NAME
    from influencer.prompt_sheet import read_prompt_sheet
    from influencer.templates import load_template

    persona = read_persona(root / env.PERSONA_FILE, root)
    allowed = allowlist if allowlist is not None else A.read_allowlist(paths.config_dir, persona)
    A.check_persona(persona, allowed)  # Req 21.6: exit 1 before any request
    calendar_path = find_calendar(root, week)
    result = read_calendar(calendar_path, persona, root)
    if result.calendar is None:
        raise ValidationError(result.all_problems())
    calendar = result.calendar
    notes = [f"{problem}" for problems in result.rejected.values() for problem in problems]
    sheet_path = folder / SHEET_NAME
    sheet = read_prompt_sheet(sheet_path) if sheet_path.exists() else None
    manifest_path = folder / MANIFEST_NAME
    manifest = load_manifest(manifest_path, kind="week", scope=week)  # ValidationError: see run_stage
    chosen = dict(templates) if templates is not None else {
        name: load_template(name) for name in J.template_names(stage, persona)
    }  # fmt: skip
    files = disk_files(paths.comfyui_dir / "output")
    refused = instagram_refusals(calendar, allowed, str(calendar_path))
    builder = BUILDERS[stage]
    wanted = None if only is None else frozenset(only)

    def build(current: Manifest) -> J.Build:
        made = builder(week, calendar, persona, sheet, current, chosen, files)
        jobs = tuple(job for job in made.jobs
                     if job.post not in refused and (wanted is None or job.post in wanted))  # fmt: skip
        return dataclasses.replace(made, jobs=jobs)

    return StageRun(
        stage=stage, scope=week, root=root, output_root=paths.output_root(persona.slug), manifest_path=manifest_path,
        manifest=manifest, build=build, files=files, config=PF.read_config(paths.config_dir), refused=refused,
        notes=notes, unload_ollama=stage == J.RENDER,
    )  # fmt: skip


class _UnreadableManifest(Exception):
    def __init__(self, error: ValidationError) -> None:
        super().__init__(str(error))
        self.error = error


def run_stage(stage: str, week: str, *, persona: str | None = None, home: str | os.PathLike[str] | None = None,
              environ: Mapping[str, str] | None = None, dry_run: bool = False, only: Collection[str] | None = None,
              comfy: ComfyClient | None = None, ollama: OllamaClient | None = None, timing: Timing | None = None,
              clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
              wall: Callable[[], float] = time.time, templates: Mapping[str, Any] | None = None,
              allowlist: Any = None, out: TextIO | None = None, err: TextIO | None = None) -> int:  # fmt: skip
    """The render or finish stage for one week, from the files on disk (see the module docstring); returns the
    exit status. persona is the slug (--persona), home the Influencer home (--home); environ defaults to
    os.environ. comfy and ollama default to the clients for COMFYUI_URL and OLLAMA_URL; templates to the
    stage's Render_Templates; allowlist to the one read from CONFIG_DIR/models.list. clock, sleep and wall
    are the time sources (Clock)."""
    if stage not in BUILDERS:
        raise ValueError(f"run_stage runs {', '.join(BUILDERS)}, not {stage!r}")
    from influencer.calendar_io import check_week, find_calendar, week_folder

    out = out or sys.stdout
    err = err or sys.stderr
    timing = timing or DEFAULT_TIMING
    times = Clock(clock, sleep, wall)
    started = clock()
    try:
        week = check_week(week)
        paths = env.paths(environ, home=home)
        root = paths.influencer_root(persona)
        folder = week_folder(root, week)
        find_calendar(root, week)  # a missing week folder or calendar is reported before the lock is taken
        with ProcessLock(folder / LOCK_NAME):
            try:
                run = _week_run(stage, week, paths, root, folder, only=only, templates=templates, allowlist=allowlist)
            except ValidationError as error:
                if any(problem.path == str(folder / MANIFEST_NAME) for problem in error.problems):
                    raise _UnreadableManifest(error) from None
                raise
            comfy = comfy or ComfyClient.from_env(environ, timing=timing)
            if run.unload_ollama and not dry_run and ollama is None:
                ollama = OllamaClient.from_env(environ, timing=timing)
            return execute(run, comfy=comfy, ollama=ollama, timing=timing, clock=times, started=started,
                           dry_run=dry_run, out=out, err=err)  # fmt: skip
    except KeyboardInterrupt:  # before the loop (the preflight, the unload): nothing was submitted
        print(f"{stage}: interrupted before any Job was submitted", file=err)
        return INTERRUPTED_EXIT
    except LockHeld as held:
        print(f"{stage}: {held}; no Job was submitted and the Manifest was not written", file=err)
        return Exit.LOCKED
    except _UnreadableManifest as unreadable:  # Req 10.16: no Job, the file left as it was
        print(f"{stage}: the Manifest cannot be read, so no Job was submitted and the file was left unchanged:\n"
              f"{format_problems(unreadable.error.problems)}", file=err)  # fmt: skip
        return Exit.ENVIRONMENT
    except ValidationError as error:
        print(format_problems(error.problems), file=err)
        return Exit.USAGE
    except EnvironmentFailure as error:
        print(f"{stage}: {error}", file=err)
        return Exit.ENVIRONMENT
