"""The Manifest: the JSON file of Job records a stage keeps (design §M4; Req 10.9, 10.12, 10.16).

A week's Jobs are recorded in weeks/<W>/manifest.json (kind `week`, scope the Week_Id), the Test_Grid's in
grid/manifest.json (kind `grid`) and a refined Dataset's in dataset/<v>-refined/.manifest.json (kind
`refine`). A record holds the Job's identity (`id`, `stage`, `template`, `post`, `slide`, `candidate`),
its `job_key`, `status`, `seed`, ComfyUI `prompt_id` and `resubmits`, the three timestamps, `duration_s`,
`outputs` and `error` or `waiting_for`. A grid record (stage `grid`, id `grid/<checkpoint>/<strength>/p<NN>`)
adds `checkpoint`, `strength` and the Test_Grid `prompt` number; a refine record has id
`refine/<image base name>`.

`status` is waiting, submitted, done or failed. Records are unique by `job_key`; a waiting record has
`job_key: null` and is unique by `id`. Old records stay, so an `id` may come back with another `job_key`.
`outputs[].path` is relative to ComfyUI's output folder.

print_manifest writes JSON with ensure_ascii=False, so an error message keeps its non-ASCII text, and
read_manifest reads back an equal Manifest (Req 10.12). A lone surrogate, which UTF-8 cannot hold (a file
name ComfyUI decoded with surrogateescape, say), is written as its \\uXXXX escape; the file stays UTF-8
and reads back the same, except that a high surrogate directly followed by a low one reads back as the one
character the pair encodes. The Printer refuses a Manifest the Reader would reject, so a written file
always reads.

write_manifest replaces the file through atomic.write_atomic on every status change, so a process killed
at any moment leaves the old file or the new one, both readable (Req 10.9). load_manifest gives a stage
its Manifest: a new, empty one when no file exists, and a ValidationError naming the path and the error
when the file exists but cannot be read. The caller then has no Manifest to write, submits no Job and
leaves the file exactly as it was (Req 10.16).
"""
from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from influencer.atomic import write_atomic
from influencer.errors import Problem, ValidationError, format_problems

FORMAT = "influencer-manifest/1"
FILE_NAME = "manifest.json"
KINDS = ("week", "grid", "refine")
WAITING, SUBMITTED, DONE, FAILED = STATUSES = ("waiting", "submitted", "done", "failed")
GRID_STAGE = "grid"
SEED_MAX = 4_294_967_295
JOB_KEY_RE = re.compile(r"[0-9a-f]{64}")

MANIFEST_KEYS = ("format", "kind", "scope", "jobs")
RECORD_KEYS = (
    "id", "stage", "template", "post", "slide", "candidate", "job_key", "status", "seed", "prompt_id",
    "resubmits", "submitted_at", "started_at", "finished_at", "duration_s", "outputs", "error", "waiting_for",
)
GRID_KEYS = ("checkpoint", "strength", "prompt")
OUTPUT_KEYS = ("path", "bytes")

_SURROGATE = re.compile("[\ud800-\udfff]")
_SHOWN_MAX = 60


# ---- the model ----


def _lists_as_tuples(obj: object, *names: str) -> None:
    for name in names:
        value = getattr(obj, name)
        if isinstance(value, list):
            object.__setattr__(obj, name, tuple(value))


@dataclass(frozen=True)
class Output:
    """One output file of a Job."""

    path: str  # relative to ComfyUI's output folder
    bytes: int


@dataclass(frozen=True)
class JobRecord:
    """One Job's record; outputs passed as a list are stored as a tuple."""

    id: str  # for example render/tue-street/s01/c2
    stage: str
    template: str
    status: str  # one of STATUSES
    post: str | None = None
    slide: int | None = None
    candidate: int | None = None
    job_key: str | None = None  # 64 lowercase hex digits; None exactly while waiting
    seed: int | None = None  # 0 to SEED_MAX
    prompt_id: str | None = None  # ComfyUI's prompt id once submitted
    resubmits: int = 0
    submitted_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None  # execution_success minus execution_start, queue time not counted
    outputs: tuple[Output, ...] = ()
    error: str | None = None
    waiting_for: str | None = None  # the input image a waiting Job lacks
    checkpoint: str | None = None  # grid records only, like strength and prompt
    strength: float | None = None
    prompt: int | None = None  # the Test_Grid prompt number

    def __post_init__(self) -> None:
        _lists_as_tuples(self, "outputs")


@dataclass(frozen=True)
class Manifest:
    """A Manifest; jobs passed as a list are stored as a tuple."""

    kind: str  # one of KINDS
    scope: str  # what it covers: for kind week, the Week_Id
    jobs: tuple[JobRecord, ...] = ()

    def __post_init__(self) -> None:
        _lists_as_tuples(self, "jobs")


# ---- Reader, Printer and writing ----


def read_manifest(path: str | os.PathLike[str]) -> Manifest:
    """The Manifest at path. A file that cannot be read, is not UTF-8 JSON or is not a valid Manifest raises
    one ValidationError holding every Problem, each naming the path."""
    name = os.fspath(path)
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        reason = f"cannot read the file: {error.strerror or error}"
        raise ValidationError([Problem(name, None, None, reason)]) from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        line = data.count(b"\n", 0, error.start) + 1
        reason = f"not UTF-8 text: byte 0x{data[error.start]:02x} at byte offset {error.start}"
        raise ValidationError([Problem(name, None, line, reason)]) from None
    try:
        loaded = json.loads(text, parse_constant=_refuse_constant)
    except json.JSONDecodeError as error:
        reason = f"not valid JSON (column {error.colno}): {error.msg}"
        raise ValidationError([Problem(name, None, error.lineno, reason)]) from None
    except (ValueError, RecursionError) as error:  # NaN, an integer too long to convert, deep nesting
        raise ValidationError([Problem(name, None, None, f"not valid JSON: {error}")]) from None
    found: list[Problem] = []
    manifest = _manifest(loaded, name, found)
    if found or manifest is None:
        raise ValidationError(found)
    return manifest


def load_manifest(path: str | os.PathLike[str], *, kind: str, scope: str) -> Manifest:
    """The Manifest a stage uses: read_manifest(path), or a new, empty Manifest of kind and scope when
    nothing exists at path. A file that exists but cannot be read raises read_manifest's ValidationError
    and is left exactly as it was (Req 10.16)."""
    if not os.path.lexists(path):
        return Manifest(kind, scope)
    return read_manifest(path)


def print_manifest(manifest: Manifest) -> str:
    """manifest as JSON text that read_manifest reads back equal; raises ValueError for a Manifest the
    Reader would reject."""
    data = {
        "format": FORMAT,
        "kind": manifest.kind,
        "scope": manifest.scope,
        "jobs": [_record_data(record) for record in manifest.jobs],
    }
    found: list[Problem] = []
    _manifest(data, "the Manifest to print", found)
    if found:
        raise ValueError("an invalid Manifest cannot be printed:\n" + format_problems(found))
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    return _SURROGATE.sub(lambda match: f"\\u{ord(match.group()):04x}", text) + "\n"


def write_manifest(path: str | os.PathLike[str], manifest: Manifest) -> None:
    """Replaces the file at path with manifest atomically: a reader sees the old file or the new one."""
    write_atomic(path, print_manifest(manifest))


def _record_data(record: JobRecord) -> dict[str, object]:
    data: dict[str, object] = {
        "id": record.id, "stage": record.stage, "template": record.template,
        "post": record.post, "slide": record.slide, "candidate": record.candidate,
    }
    if record.stage == GRID_STAGE or (record.checkpoint, record.strength, record.prompt) != (None, None, None):
        data.update(checkpoint=record.checkpoint, strength=record.strength, prompt=record.prompt)
    data.update(
        job_key=record.job_key, status=record.status, seed=record.seed, prompt_id=record.prompt_id,
        resubmits=record.resubmits, submitted_at=record.submitted_at, started_at=record.started_at,
        finished_at=record.finished_at, duration_s=record.duration_s,
        outputs=[{"path": output.path, "bytes": output.bytes} for output in record.outputs],
        error=record.error, waiting_for=record.waiting_for,
    )
    return data


def _refuse_constant(name: str) -> object:
    raise ValueError(f"{name} is not a number JSON allows")


# ---- checking the loaded JSON ----

Check = Callable[[object], "str | None"]


def _manifest(data: object, path: str, found: list[Problem]) -> Manifest | None:
    def add(field: str | None, reason: str) -> None:
        found.append(Problem(path, field, None, reason))

    if not isinstance(data, dict):
        add(None, f"holds {_what(data)}; a Manifest is a JSON object with the keys {_listed(MANIFEST_KEYS)}")
        return None
    for key in data:
        if key not in MANIFEST_KEYS:
            add(key, f"unknown key; a Manifest has the keys {_listed(MANIFEST_KEYS)}")
    for key in MANIFEST_KEYS:
        if key not in data:
            add(key, "missing")
    if "format" in data and data["format"] != FORMAT:
        add("format", f"is {_shown(data['format'])}; this version reads {FORMAT}")
    kind, scope, jobs = data.get("kind"), data.get("scope"), data.get("jobs", [])
    if "kind" in data and not (isinstance(kind, str) and kind in KINDS):
        add("kind", f"is {_shown(kind)}; a Manifest's kind is {_listed(KINDS, 'or')}")
    if "scope" in data and not _name(scope):
        add("scope", f"must be non-empty text, not {_shown(scope)}")
    records: list[JobRecord] = []
    if not isinstance(jobs, list):
        add("jobs", f"must be a list of job records, not {_shown(jobs)}")
    else:
        for number, item in enumerate(jobs, 1):
            record = _record(item, number, add)
            if record is not None:
                records.append(record)
        _check_unique(jobs, add)
    if found:
        return None
    return Manifest(kind, scope, tuple(records))  # type: ignore[arg-type]


def _record(item: object, number: int, add: Callable[[str | None, str], None]) -> JobRecord | None:
    where = f"jobs[{number}]"
    if not isinstance(item, dict):
        add(where, f"holds {_what(item)}; a job record is a JSON object")
        return None
    label = _label(item)
    bad_keys: list[str] = []

    def bad(key: str, reason: str) -> None:
        bad_keys.append(key)
        add(f"{where}.{key}", label + reason)

    grid = item.get("stage") == GRID_STAGE
    checks = _GRID_CHECKS if grid else _CHECKS
    for key in item:
        if key in checks or key == "outputs":
            continue
        if key in GRID_KEYS:
            bad(key, f"only a grid record (stage {GRID_STAGE!r}) has {key}")
        else:
            bad(key, "unknown key")
    for key in (*RECORD_KEYS, *(GRID_KEYS if grid else ())):
        if key not in item:
            bad(key, "missing; a grid record has checkpoint, strength and prompt" if key in GRID_KEYS else "missing")
    for key, check in checks.items():
        if key in item and (reason := check(item[key])) is not None:
            bad(key, reason)
    outputs = _outputs(item.get("outputs", []), bad)
    status, job_key = item.get("status"), item.get("job_key")
    if status == WAITING and job_key is not None:
        bad("job_key", "must be null in a waiting record: a waiting Job has no Job_Key yet")
    elif status in STATUSES and status != WAITING and "job_key" in item and job_key is None:
        bad("job_key", f"is null, but a {status} record needs its Job_Key")
    if bad_keys:
        return None
    values = {key: item[key] for key in checks}
    for key in ("duration_s", "strength"):
        if values.get(key) is not None:
            values[key] = float(values[key])  # type: ignore[arg-type]
    return JobRecord(outputs=outputs, **values)  # type: ignore[arg-type]


def _outputs(value: object, bad: Callable[[str, str], None]) -> tuple[Output, ...]:
    if not isinstance(value, list):
        bad("outputs", f"must be a list of {{path, bytes}} objects, not {_shown(value)}")
        return ()
    outputs = []
    for number, item in enumerate(value, 1):
        at = f"outputs[{number}]"
        if not isinstance(item, dict):
            bad(at, f"holds {_what(item)}; an output is a JSON object with path and bytes")
            continue
        problems = 0
        for key in item:
            if key not in OUTPUT_KEYS:
                bad(f"{at}.{key}", "unknown key; an output has path and bytes")
                problems += 1
        for key, check in (("path", _output_path), ("bytes", _whole_from(0))):
            reason = "missing" if key not in item else check(item[key])
            if reason is not None:
                bad(f"{at}.{key}", reason)
                problems += 1
        if not problems:
            outputs.append(Output(item["path"], item["bytes"]))
    return tuple(outputs)


def _check_unique(jobs: list, add: Callable[[str | None, str], None]) -> None:
    """Records are unique by job_key; waiting records, whose job_key is null, are unique by id."""
    keys: dict[str, int] = {}
    waiting: dict[str, int] = {}
    for number, item in enumerate(jobs, 1):
        if not isinstance(item, dict):
            continue
        ident, key = item.get("id"), item.get("job_key")
        if isinstance(key, str) and JOB_KEY_RE.fullmatch(key):
            if key in keys:
                first = keys[key]
                named = _named(jobs[first - 1])
                other = f"jobs[{first}] ({named})" if named else f"jobs[{first}]"
                reason = f"{other} has the same job_key; records are unique by job_key"
                add(f"jobs[{number}].job_key", _label(item) + reason)
            else:
                keys[key] = number
        if item.get("status") == WAITING and _name(ident):
            if ident in waiting:
                reason = f"jobs[{waiting[ident]}] is a waiting record with the same id"
                add(f"jobs[{number}].id", f"{_label(item)}{reason}; waiting records are unique by id")
            else:
                waiting[ident] = number  # type: ignore[index]


# ---- small checks and messages ----


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _name(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def _named(item: dict) -> str:
    """The record's name in a message, job '<id>', or the empty string when it has no id."""
    ident = item.get("id")
    return f"job {ident!r}" if _name(ident) else ""


def _label(item: dict) -> str:
    named = _named(item)
    return f"{named}: " if named else ""


def _text(value: object) -> str | None:
    return None if isinstance(value, str) else f"must be text, not {_shown(value)}"


def _non_empty(value: object) -> str | None:
    return None if _name(value) else f"must be non-empty text, not {_shown(value)}"


def _optional(check: Check) -> Check:
    return lambda value: None if value is None else check(value)


def _whole_from(low: int, high: int | None = None) -> Check:
    bounds = f"from {low}" if high is None else f"from {low} to {high}"

    def check(value: object) -> str | None:
        if _whole(value) and value >= low and (high is None or value <= high):  # type: ignore[operator]
            return None
        return f"must be a whole number {bounds}, not {_shown(value)}"

    return check


def _status(value: object) -> str | None:
    if isinstance(value, str) and value in STATUSES:
        return None
    return f"{_shown(value)} is not a status; a status is {_listed(STATUSES, 'or')}"


def _job_key(value: object) -> str | None:
    if isinstance(value, str) and JOB_KEY_RE.fullmatch(value):
        return None
    return f"must be a Job_Key of 64 lowercase hex digits, or null; not {_shown(value)}"


def _seconds(value: object) -> str | None:
    if _finite(value) and value >= 0:  # type: ignore[operator]
        return None
    return f"must be a number of seconds from 0, not {_shown(value)}"


def _number(value: object) -> str | None:
    return None if _finite(value) else f"must be a number, not {_shown(value)}"


def _output_path(value: object) -> str | None:
    if not _name(value):
        return f"must be a file path relative to ComfyUI's output folder, not {_shown(value)}"
    text: str = value  # type: ignore[assignment]
    if text.startswith("/"):
        return f"{_shown(text)} is absolute; it must be relative to ComfyUI's output folder"
    if ".." in text.split("/"):
        return f"{_shown(text)} leaves ComfyUI's output folder through '..'"
    if "\x00" in text:
        return f"{_shown(text)} holds a NUL character"
    return None


_CHECKS: dict[str, Check] = {
    "id": _non_empty,
    "stage": _non_empty,
    "template": _non_empty,
    "status": _status,
    "post": _optional(_non_empty),
    "slide": _optional(_whole_from(1)),
    "candidate": _optional(_whole_from(1)),
    "job_key": _optional(_job_key),
    "seed": _optional(_whole_from(0, SEED_MAX)),
    "prompt_id": _optional(_text),
    "resubmits": _whole_from(0),
    "submitted_at": _optional(_text),
    "started_at": _optional(_text),
    "finished_at": _optional(_text),
    "duration_s": _optional(_seconds),
    "error": _optional(_text),
    "waiting_for": _optional(_text),
}
_GRID_CHECKS: dict[str, Check] = {**_CHECKS, "checkpoint": _non_empty, "strength": _number, "prompt": _whole_from(1)}


def _listed(items: tuple[str, ...], last: str = "and") -> str:
    return ", ".join(items[:-1]) + f" {last} {items[-1]}" if len(items) > 1 else "".join(items)


def _what(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, dict):
        return "an object"
    if isinstance(value, list):
        return "a list"
    return _shown(value)


def _shown(value: object) -> str:
    """value as JSON text for a message, cut to 60 characters."""
    try:
        shown = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        shown = repr(value)
    return shown if len(shown) <= _SHOWN_MAX else shown[: _SHOWN_MAX - 3] + "..."
