"""The export stage: every Post of a week graded and exported, one Post at a time (design §C8, §M9;
Req 13.11-13.15).

    run_export(week, *, persona, home, external, ...) -> exit status      `influencer.sh export --week W`
    prepare(week, paths, root, folder, ...) -> ExportRun                  the opening steps, under the lock
    export_post(run, post, hooks) -> PostResult                           one Post: sources, staging, status

Opening steps (task 23.1), in order: the week lock (weeks/<W>/.lock; exit 4 with the holder's PID), the
Persona_File, the Content_Calendar with its one-file check, the Prompt_Sheet, the Manifest, then
grade.load_grade, so an invalid LUT or grain strength exits 1 before any folder is created. Then the
`--external post-id=path` values are checked, every one of them before anything is written, and
registered in weeks/<W>/external.yaml (§M9: one `post-id: /absolute/path` entry per reel Post, the path
itself, not a copy).

Sources. A Slide's finished image is jobs.finished_image of the Slide's finish Job as jobs.build_finish
builds it now, from the current Pick bytes, Persona_File and Prompt_Sheet entry, so an image finished from
an earlier Pick is never exported (Req 12.5). A reel Post's source is its registered video, and only when
the Post is not in mode `external`, the done Reel Job's output (rendered_reel; no Phase 1 run makes one).

Staging (task 23.2). Every file of a Post goes into <post folder>/.export-tmp-<pid>/ first; only when all
of them are written are they moved into place with os.replace, and each <post-id>_NN.jpg|mp4 this run did
not produce is removed. Any read or write error removes the temp folder and leaves the earlier exports as
they were. A stale .export-tmp-<pid> of a PID that no longer runs is removed when the Post starts; one of a
live PID is left alone and reported.

Reels stay in Phase 3: a reel Post gets its source and a status, never an encode here. With ffmpeg missing
it waits for ffmpeg; with ffmpeg present it waits for the Phase 3 encoder, which Hooks.encode_reel plugs in.

Statuses (task 23.5): `exported` when every Slide, or the Reel, has its file; `failed` when not exported
and the Manifest records a failed Job of the Post or one of its files failed; `waiting` otherwise. The run
prints each Post's line, the three counts, the absolute Output_Root and the stop-the-Instance reminder, and
exits 0 (no Post failed), 3 (a Post failed), 1 (an invalid Persona_File, calendar, LUT, grain strength or
--external), 2 (an unreadable Manifest, an environment failure) or 4 (the lock is held).

Seams for task 24: Hooks.stage_extras writes the QC_Sheet and Publish_Sheet into the temp folder, so they
move into place with the Post's exports, and Hooks.after writes the week index from the PostResults.
run_export uses default_hooks() (export/qc.py, publish.py, index.py) unless given hooks; Hooks.start runs
the Pillow check before the Posts.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from influencer import env, ids
from influencer import jobs as J
from influencer.atomic import write_atomic
from influencer.calendar_io import Calendar, Post
from influencer.errors import EnvironmentFailure, Exit, Problem, ValidationError, format_problems
from influencer.locks import LockHeld, ProcessLock
from influencer.manifest import DONE, FAILED as JOB_FAILED
from influencer.manifest import FILE_NAME as MANIFEST_NAME
from influencer.manifest import Manifest

STAGE = "export"
LOCK_NAME = ".lock"  # weeks/<W>/.lock, the lock every week stage takes
EXTERNAL_FILE = "external.yaml"  # weeks/<W>/external.yaml (§M9)
TEMP_PREFIX = ".export-tmp-"
EXPORTED, FAILED, WAITING = "exported", "failed", "waiting"
STATUSES = (EXPORTED, FAILED, WAITING)
REMINDER = "Stop the Instance in the Vast console when you are done"
EXTERNAL_OPTION = "--external"
IMAGE_EXT, REEL_EXT = ".jpg", ".mp4"
_TEMP_RE = re.compile(re.escape(TEMP_PREFIX) + r"([0-9]{1,10})")


# ---- what a run holds ----


@dataclass(frozen=True)
class Source:
    """One file a Post exports: its number NN (a Slide, or 1 for a Reel), the source path, or None with
    the reason it is missing."""

    number: int
    path: Path | None
    reason: str | None = None


@dataclass(frozen=True)
class Staged:
    """A file this run wrote into the temp folder: its final name, its number and its flags."""

    name: str
    number: int
    flags: tuple[str, ...] = ()


@dataclass
class ExportRun:
    """Everything prepare() loaded; export_post needs nothing else from the disk but the source files."""

    week: str
    root: Path
    folder: Path  # weeks/<W>
    output_root: Path
    persona: Any
    calendar: Calendar
    sheet: Any
    manifest: Manifest
    grading: Any
    build: J.Build | None  # None when the finish builder refuses the Persona_File (no stills Character_LoRA)
    build_problem: str | None
    files: J.Files
    externals: dict[str, str]
    notes: list[str] = field(default_factory=list)
    unreadable: tuple[str, str] | None = None  # (path, error) of a Pick the finish builder could not read


@dataclass(frozen=True)
class PostResult:
    """One Post's outcome. files are the exported files in upload order; missing names each missing Slide
    or the Reel with its reason; error is (path, message) of a failed read or write; failed_jobs the
    Manifest's failed Jobs of the Post; flags per exported number; notes the warnings of the Post."""

    post: Post
    folder: Path
    status: str
    files: tuple[Path, ...] = ()
    missing: tuple[tuple[str, str], ...] = ()
    error: tuple[str, str] | None = None
    failed_jobs: tuple[tuple[str, str], ...] = ()
    flags: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    extras: tuple[Path, ...] = ()
    notes: tuple[str, ...] = ()

    def lines(self) -> list[str]:
        """The Post's summary lines (Req 13.14, 13.15)."""
        name = f"Post {self.post.id!r}"
        lines = [f"warning: {name}: {note}" for note in self.notes]
        if self.status == EXPORTED:
            shown = ", ".join(path.name for path in self.files)
            lines.append(f"exported: {name}: {shown} in {self.folder}")
            for number, flags in sorted(self.flags.items()):
                for flag in flags:
                    lines.append(f"  {flag}: {self.post.id}_{number:02d}")
            return lines
        if self.error is not None:
            lines.append(f"failed: {name}: {self.error[0]}: {self.error[1]}; its earlier exports are unchanged")
        for job, error in self.failed_jobs:
            lines.append(f"failed: {name}: the Job {job} failed: {error or 'no error recorded'}")
        if self.missing:
            what = ", ".join(item for item, _ in self.missing)
            label = "failed" if self.status == FAILED else "waiting"
            lines.append(f"{label}: {name}: missing {what}; its earlier exports are unchanged")
            lines += [f"  {item}: {reason}" for item, reason in self.missing]
        return lines


StageExtras = Callable[[ExportRun, Post, Path, Sequence[Staged]], Sequence[str]]
After = Callable[[ExportRun, Sequence[PostResult]], None]
EncodeReel = Callable[[ExportRun, Post, Path, Path], None]
Start = Callable[[ExportRun], None]


@dataclass(frozen=True)
class Hooks:
    """The seams of the later tasks. stage_extras(run, post, temp, staged) writes more files into the
    temp folder and returns their names (task 24: the QC_Sheet and the Publish_Sheet); after(run,
    results) runs once every Post is done (task 24: the week index); encode_reel(run, post, source,
    dest) writes a Reel's MP4 (Phase 3); start(run) runs before the Posts and may add run.notes (task 24:
    the Pillow check). None skips a seam; without encode_reel a reel Post waits."""

    stage_extras: StageExtras | None = None
    after: After | None = None
    encode_reel: EncodeReel | None = None
    start: Start | None = None


def default_hooks() -> Hooks:
    """The hooks run_export uses when given none: the QC_Sheet and Publish_Sheet of every exported Post
    and the week index (task 24). With a Pillow older than 10.1 the start hook reports its version and
    the QC_Sheets are left out."""
    from influencer.export import index, publish, qc

    problem = qc.pillow_problem()

    def start(run: ExportRun) -> None:
        if problem is not None:
            run.notes.append(problem)

    def stage_extras(run: ExportRun, post: Post, temp: Path, staged: Sequence[Staged]) -> list[str]:
        names = [] if problem is not None else [qc.write_qc(run, post, temp, staged)]
        return [*names, publish.write_publish(run, post, temp, staged)]

    return Hooks(stage_extras=stage_extras, after=index.write_index, start=start)


def post_status(exported: bool, failed_jobs: bool, error: bool) -> str:
    """Req 15.8: exported when every file was exported; else failed when the Manifest records a failed Job
    of the Post or one of its files failed; else waiting."""
    if exported:
        return EXPORTED
    return FAILED if failed_jobs or error else WAITING


class _Failed(Exception):
    """A read or write of a Post failed: the path and the error (Req 13.15)."""

    def __init__(self, path: str | os.PathLike[str], error: BaseException | str) -> None:
        super().__init__(path, error)
        self.path = os.fspath(path)
        self.message = error if isinstance(error, str) else _describe(error)


def _describe(error: BaseException) -> str:
    if isinstance(error, OSError) and error.strerror:
        return error.strerror
    return str(error) or type(error).__name__


# ---- external videos (task 23.3, §M9) ----


def read_externals(path: Path) -> dict[str, str]:
    """external.yaml as {post id: absolute path}; {} when the file does not exist. A file that is not a
    mapping of Post ids to absolute paths is a ValidationError naming each bad entry."""
    from influencer.yamlio import load_yaml

    if not os.path.lexists(path):
        return {}
    data = load_yaml(path)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValidationError([Problem(str(path), None, None, "is not a mapping of reel Post ids to video paths")])
    problems, found = [], {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str) or not os.path.isabs(value):
            problems.append(Problem(str(path), str(key), None, f"{value!r} is not an absolute video path"))
        else:
            found[key] = value
    if problems:
        raise ValidationError(problems)
    return found


def check_externals(given: Sequence[tuple[str, str]], calendar: Calendar) -> dict[str, str]:
    """The --external values as {post id: absolute path}, in the order given. Every unknown or non-reel
    Post id, repeated id, missing path, non-file and unreadable path is one Problem naming the value; any
    Problem is a ValidationError, raised before anything is written (Req 13.11)."""
    posts = {post.id: post for post in calendar.posts}
    problems: list[Problem] = []
    found: dict[str, str] = {}
    seen: set[str] = set()

    def bad(post_id: str, path: str, reason: str) -> None:
        problems.append(Problem("command line", EXTERNAL_OPTION, None, f"{post_id}={path}: {reason}"))

    for post_id, path in given:
        post = posts.get(post_id)
        if post_id in seen:
            bad(post_id, path, f"Post {post_id!r} is given more than once")
            continue
        seen.add(post_id)
        if post is None:
            bad(post_id, path, f"the calendar of {calendar.week} has no Post {post_id!r}")
            continue
        if post.type != "reel":
            bad(post_id, path, f"Post {post_id!r} is a {post.type} Post; only a reel Post takes a video")
            continue
        absolute = os.path.abspath(os.path.expanduser(path))
        if not os.path.lexists(absolute) or not os.path.exists(absolute):
            bad(post_id, path, f"{absolute} does not exist")
            continue
        if not os.path.isfile(absolute):
            bad(post_id, path, f"{absolute} is not a regular file")
            continue
        try:
            with open(absolute, "rb") as handle:
                handle.read(1)
        except OSError as error:
            bad(post_id, path, f"{absolute} cannot be read: {_describe(error)}")
            continue
        found[post_id] = absolute
    if problems:
        raise ValidationError(problems)
    return found


def register_externals(path: Path, current: Mapping[str, str], given: Mapping[str, str]) -> dict[str, str]:
    """current with given's entries set, written to path through write_atomic when anything changed."""
    from influencer.yamlio import dump_yaml

    merged = dict(current)
    merged.update(given)
    if merged != dict(current):
        write_atomic(path, dump_yaml(merged))
    return merged


# ---- sources (task 23.1) ----


def _job_errors(run: ExportRun, post: Post) -> tuple[tuple[str, str], ...]:
    return tuple((record.id, record.error or "") for record in run.manifest.jobs
                 if record.post == post.id and record.status == JOB_FAILED)  # fmt: skip


def slide_sources(run: ExportRun, post: Post) -> list[Source]:
    """Each Slide's finished image, or why it has none. A Pick that cannot be read while its key is
    computed is a _Failed with its path."""
    build = run.build
    if run.unreadable is not None:
        raise _Failed(*run.unreadable)
    if build is None:
        return [Source(n, None, run.build_problem) for n in range(1, post.slides + 1)]
    if post.id in build.planning:
        reason = f"waits for planning: Post {post.id!r} {build.planning[post.id]}"
        return [Source(n, None, reason) for n in range(1, post.slides + 1)]
    if post.id in build.review:
        return [Source(n, None, J.review_reason(post.id)) for n in range(1, post.slides + 1)]
    finish = {job.slide: job for job in build.jobs if job.post == post.id and job.stage == J.FINISH}
    invalid = {pick.slide: pick.message for pick in build.invalid_picks if pick.post == post.id}
    sources = []
    for number in range(1, post.slides + 1):
        job = finish.get(number)
        if job is None:
            reason = invalid.get(number, "has no finish Job")
            sources.append(Source(number, None, f"has no finished image: invalid Pick ({reason})"))
            continue
        if job.waiting_for is not None:
            sources.append(Source(number, None, f"has no finished image: its finish Job waits for {job.waiting_for}"))
            continue
        try:
            found = J.finished_image(job, run.manifest, run.files)
        except OSError as error:
            where = error.filename or job.inputs.get("pick_image") or run.files.output_dir
            raise _Failed(where, error) from None
        if found is None:
            reason = f"has no finished image from its current Pick; run finish --week {run.week}"
            sources.append(Source(number, None, reason))
        else:
            sources.append(Source(number, Path(found)))
    return sources


def rendered_reel(run: ExportRun, post: Post) -> Path | None:
    """The first output of the done Reel Job record of post whose outputs are all on disk. The reels
    builder of Phase 3 adds the current-key check that finished_image makes for a Slide."""
    reel = ids.reel_id(post.id)
    for record in reversed(run.manifest.jobs):
        if record.id != reel or record.status != DONE or not record.outputs:
            continue
        if all(run.files.present(run.files.output_dir / output.path) for output in record.outputs):
            return run.files.output_dir / record.outputs[0].path
    return None


def reel_source(run: ExportRun, post: Post) -> Source:
    """The registered video, then (not in mode external) the rendered Reel (Req 13.11, 13.14)."""
    registered = run.externals.get(post.id)
    if registered is not None:
        if os.path.isfile(registered):
            return Source(1, Path(registered))
        return Source(1, None, f"the registered video {registered} no longer exists; register one with "
                               f"{EXTERNAL_OPTION} {post.id}=PATH")  # fmt: skip
    if post.mode == "external":
        return Source(1, None, f"has no user-given video; register one with {EXTERNAL_OPTION} {post.id}=PATH")
    rendered = rendered_reel(run, post)
    if rendered is not None:
        return Source(1, rendered)
    return Source(1, None, f"has neither a rendered Reel nor a user-given video ({EXTERNAL_OPTION} {post.id}=PATH)")


# ---- staging (task 23.2) ----


def post_folder(run: ExportRun, post: Post) -> Path:
    """The Post's §M5 folder: <Output_Root>/<W>/<post> or <Output_Root>/<W>/other/<post>."""
    return run.output_root / ids.post_folder(run.week, post.id, post.platform)


def export_name(run: ExportRun, post: Post, number: int, ext: str) -> str:
    """<post-id>_NN.jpg or .mp4."""
    return Path(ids.export_path(run.week, post.id, post.platform, number, ext)).name


def _export_re(post: Post) -> re.Pattern[str]:
    return re.compile(re.escape(post.id) + r"_([0-9]{2,3})\.(?:jpg|mp4)")


def pid_alive(pid: int) -> bool:
    """True while a process of that PID runs (one of another user counts)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OverflowError:
        return False
    return True


def clear_stale_temps(folder: Path) -> list[str]:
    """Removes each .export-tmp-<pid> of folder whose PID no longer runs (or is this process's, left by an
    earlier process of the same PID); returns a note for each one of a live PID, which is left alone."""
    notes: list[str] = []
    try:
        entries = sorted(folder.iterdir())
    except FileNotFoundError:
        return notes
    except OSError as error:
        return [f"cannot list {folder} for stale temp folders: {_describe(error)}"]
    for entry in entries:
        match = _TEMP_RE.fullmatch(entry.name)
        if match is None or not entry.is_dir() or entry.is_symlink():
            continue
        pid = int(match.group(1))
        if pid != os.getpid() and pid_alive(pid):
            notes.append(f"{entry} belongs to PID {pid}, which is still running; it was left alone")
            continue
        shutil.rmtree(entry, ignore_errors=True)
        if entry.exists():
            notes.append(f"could not remove the stale temp folder {entry}")
    return notes


def _write(path: Path, data: bytes) -> None:
    try:
        with open(path, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise _Failed(path, error) from None


def _stage_images(run: ExportRun, post: Post, sources: Sequence[Source], temp: Path) -> list[Staged]:
    from PIL import Image

    from influencer.export import images

    staged = []
    for source in sources:
        assert source.path is not None
        try:
            image = images.export_slide(source.path, post, run.week, source.number, run.grading)
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            raise _Failed(source.path, error) from None
        name = export_name(run, post, source.number, IMAGE_EXT)
        _write(temp / name, image.data)
        staged.append(Staged(name, source.number, image.flags))
    return staged


def _stage_reel(run: ExportRun, post: Post, source: Source, temp: Path, hooks: Hooks) -> list[Staged]:
    assert source.path is not None and hooks.encode_reel is not None
    name = export_name(run, post, 1, REEL_EXT)
    try:
        hooks.encode_reel(run, post, source.path, temp / name)
    except _Failed:
        raise
    except (OSError, ValueError, RuntimeError) as error:
        raise _Failed(source.path, error) from None
    if not (temp / name).is_file():
        raise _Failed(temp / name, "the Reel encoder wrote no file")
    return [Staged(name, 1)]


def stage_and_replace(run: ExportRun, post: Post, folder: Path, sources: Sequence[Source],
                      hooks: Hooks) -> tuple[list[Staged], list[Path]]:  # fmt: skip
    """Writes every file of the Post into folder/.export-tmp-<pid>/, then moves them into place and drops
    the stale NN files; raises _Failed with the temp folder removed and the old exports unchanged."""
    temp = folder / f"{TEMP_PREFIX}{os.getpid()}"
    try:
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(temp):
                shutil.rmtree(temp)
            temp.mkdir()
        except OSError as error:
            raise _Failed(error.filename or temp, error) from None
        if post.type == "reel":
            staged = _stage_reel(run, post, sources[0], temp, hooks)
        else:
            staged = _stage_images(run, post, sources, temp)
        extras: list[str] = []
        if hooks.stage_extras is not None:
            try:
                extras = list(hooks.stage_extras(run, post, temp, staged))
            except _Failed:
                raise
            except (OSError, ValueError, RuntimeError) as error:  # an unreadable image, a failing ffmpeg
                raise _Failed(getattr(error, "filename", None) or temp, error) from None
        produced = {item.name for item in staged}
        pattern = _export_re(post)
        try:
            stale = [entry for entry in folder.iterdir()
                     if pattern.fullmatch(entry.name) and entry.name not in produced and not entry.is_dir()]  # fmt: skip
            for name in [*(item.name for item in staged), *extras]:
                os.replace(temp / name, folder / name)
            for entry in stale:
                entry.unlink()
        except OSError as error:
            raise _Failed(error.filename or folder, error) from None
        return staged, [folder / name for name in extras]
    finally:
        shutil.rmtree(temp, ignore_errors=True)


# ---- one Post (tasks 23.2-23.5) ----


def export_post(run: ExportRun, post: Post, hooks: Hooks | None = None) -> PostResult:
    """One Post's sources, staging and status; never raises for the Post's own files."""
    from influencer.export import video

    hooks = hooks or Hooks()
    folder = post_folder(run, post)
    notes = tuple(clear_stale_temps(folder))
    failed_jobs = _job_errors(run, post)

    def outcome(missing: Sequence[Source] = (), error: _Failed | None = None) -> PostResult:
        status = post_status(False, bool(failed_jobs), error is not None)
        items = tuple((_item(post, source.number), source.reason or "") for source in missing)
        where = None if error is None else (error.path, error.message)
        return PostResult(post, folder, status, missing=items, error=where, failed_jobs=failed_jobs, notes=notes)

    try:
        sources = [reel_source(run, post)] if post.type == "reel" else slide_sources(run, post)
    except _Failed as failed:
        return outcome(error=failed)
    missing = [source for source in sources if source.path is None]
    if missing:
        return outcome(missing)
    if post.type == "reel":
        if not video.ffmpeg_available():
            reason = f"waits for ffmpeg ({video.discover()}); the still images export without it"
            return outcome([Source(1, None, reason)])
        if hooks.encode_reel is None:
            return outcome([Source(1, None, "has its source, and waits for the Reel encode of the reels export "
                                            "(Phase 3)")])  # fmt: skip
    try:
        staged, extras = stage_and_replace(run, post, folder, sources, hooks)
    except _Failed as failed:
        return outcome(error=failed)
    flags = {item.number: item.flags for item in staged if item.flags}
    files = tuple(folder / item.name for item in staged)
    return PostResult(post, folder, post_status(True, bool(failed_jobs), False), files=files, failed_jobs=failed_jobs, flags=flags,
                      extras=tuple(extras), notes=notes)  # fmt: skip


def _item(post: Post, number: int) -> str:
    return "the Reel" if post.type == "reel" else f"Slide {number}"


# ---- the opening steps (task 23.1) ----


class _UnreadableManifest(Exception):
    def __init__(self, error: ValidationError) -> None:
        super().__init__(str(error))
        self.error = error


def prepare(week: str, paths: env.Paths, root: Path, folder: Path, *, external: Sequence[tuple[str, str]] = (),
            templates: Mapping[str, Any] | None = None) -> ExportRun:  # fmt: skip
    """The opening steps under the lock: every read and check, then the external.yaml registration, the
    run's only write before the Posts. A ValidationError is exit 1; an unreadable Manifest raises
    _UnreadableManifest."""
    from influencer.calendar_io import find_calendar, read_calendar
    from influencer.engine import disk_files
    from influencer.grade import load_grade
    from influencer.manifest import load_manifest
    from influencer.persona import read_persona
    from influencer.prompt_sheet import FILE_NAME as SHEET_NAME
    from influencer.prompt_sheet import read_prompt_sheet
    from influencer.templates import load_template

    persona = read_persona(root / env.PERSONA_FILE, root)
    calendar_path = find_calendar(root, week)
    result = read_calendar(calendar_path, persona, root)
    if result.calendar is None:
        raise ValidationError(result.all_problems())
    calendar = result.calendar
    notes = [f"{problem}" for problems in result.rejected.values() for problem in problems]
    sheet_path = folder / SHEET_NAME
    sheet = read_prompt_sheet(sheet_path) if sheet_path.exists() else None
    manifest_path = folder / MANIFEST_NAME
    try:
        manifest = load_manifest(manifest_path, kind="week", scope=week)
    except ValidationError as error:
        raise _UnreadableManifest(error) from None
    grading = load_grade(persona, root)  # an invalid LUT or grain strength stops here, nothing written
    external_path = folder / EXTERNAL_FILE
    current = read_externals(external_path)
    given = check_externals(external, calendar)
    files = disk_files(paths.comfyui_dir / "output")
    build: J.Build | None = None
    problem: str | None = None
    chosen = dict(templates) if templates is not None else {
        name: load_template(name) for name in J.template_names(J.FINISH, persona)
    }  # fmt: skip
    unreadable: tuple[str, str] | None = None
    try:
        build = J.build_finish(week, calendar, persona, sheet, manifest, chosen, files)
    except J.NoCharacterLora as error:
        problem = f"has no finished image: {format_problems(error.problems)}"
    except OSError as error:  # a Pick whose header Pillow cannot read while the finish Jobs are built
        unreadable = (os.fspath(error.filename or files.output_dir), _describe(error))
        problem = "a Pick cannot be read"
    externals = register_externals(external_path, current, given)
    return ExportRun(week, root, folder, paths.output_root(persona.slug), persona, calendar, sheet, manifest,
                     grading, build, problem, files, externals, notes, unreadable)  # fmt: skip


# ---- the stage (task 23.5) ----


def counts(results: Sequence[PostResult]) -> dict[str, int]:
    return {status: sum(1 for result in results if result.status == status) for status in STATUSES}


def exit_status(results: Sequence[PostResult]) -> int:
    """3 when a Post failed, else 0 (waiting Posts included)."""
    return Exit.FAILED if any(result.status == FAILED for result in results) else Exit.OK


def export_week(run: ExportRun, hooks: Hooks | None = None, *, out: TextIO | None = None,
                err: TextIO | None = None) -> int:  # fmt: skip
    """Every Post of run in calendar order, then the summary; returns the exit status."""
    out = out or sys.stdout
    err = err or sys.stderr
    hooks = hooks or Hooks()
    if hooks.start is not None:
        hooks.start(run)
    for note in run.notes:
        print(f"warning: {note}", file=err)
    results = [export_post(run, post, hooks) for post in run.calendar.posts]
    if hooks.after is not None:
        try:
            hooks.after(run, results)
        except OSError as error:  # the week index; every Post's own files are already in place
            print(f"warning: {error.filename or run.output_root}: {_describe(error)}; the week index was not "
                  f"written", file=err)  # fmt: skip
    for result in results:
        for line in result.lines():
            print(line, file=out)
    total = counts(results)
    print(f"{STAGE} {run.week}: {total[EXPORTED]} exported, {total[FAILED]} failed, {total[WAITING]} waiting",
          file=out)  # fmt: skip
    print(f"Output_Root: {run.output_root.absolute()}", file=out)
    print(REMINDER, file=out)
    return exit_status(results)


def run_export(week: str, *, persona: str | None = None, home: str | os.PathLike[str] | None = None,
               environ: Mapping[str, str] | None = None, external: Sequence[tuple[str, str]] = (),
               templates: Mapping[str, Any] | None = None, hooks: Hooks | None = None,
               out: TextIO | None = None, err: TextIO | None = None) -> int:  # fmt: skip
    """The export stage for one week (see the module docstring); returns the exit status. persona is the
    slug (--persona), home the Influencer home (--home), external the --external (post id, path) pairs;
    environ defaults to os.environ, templates to the finish builder's Render_Templates."""
    from influencer.calendar_io import check_week, find_calendar, week_folder

    out = out or sys.stdout
    err = err or sys.stderr
    try:
        week = check_week(week)
        paths = env.paths(environ, home=home)
        root = paths.influencer_root(persona)
        folder = week_folder(root, week)
        find_calendar(root, week)  # a missing week folder or calendar is reported before the lock is taken
        with ProcessLock(folder / LOCK_NAME):
            run = prepare(week, paths, root, folder, external=external, templates=templates)
            return export_week(run, hooks if hooks is not None else default_hooks(), out=out, err=err)
    except LockHeld as held:
        print(f"{STAGE}: {held}; nothing was exported", file=err)
        return Exit.LOCKED
    except _UnreadableManifest as unreadable:
        print(f"{STAGE}: the Manifest cannot be read, so nothing was exported and the file was left unchanged:\n"
              f"{format_problems(unreadable.error.problems)}", file=err)  # fmt: skip
        return Exit.ENVIRONMENT
    except ValidationError as error:
        print(format_problems(error.problems), file=err)
        print(f"{STAGE}: nothing was exported", file=err)
        return Exit.USAGE
    except EnvironmentFailure as error:
        print(f"{STAGE}: {error}", file=err)
        return Exit.ENVIRONMENT
