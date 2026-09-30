"""Tests of the Manifest, scripts/influencer/manifest.py (task 10, design §M4; Req 10.9, 10.12, 10.16).

- Property 27: a printed Manifest of every Job kind and status reads back equal
- the design's example reads; an error message keeps its non-ASCII text, and a lone surrogate reads back
- two records sharing a job_key, two waiting records sharing an id, an unknown status and a grid record
  without checkpoint are each reported with the record's id; the Printer refuses such a Manifest
- a Manifest that exists but cannot be read is reported with its path and the error and left unchanged; a
  missing one starts empty
- a write leaves no temp file, and a write interrupted before os.replace leaves the previous Manifest
  readable and unchanged
"""
from __future__ import annotations

import dataclasses
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import given

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer.errors import ValidationError  # noqa: E402
from influencer.manifest import (  # noqa: E402
    JobRecord,
    Manifest,
    Output,
    load_manifest,
    print_manifest,
    read_manifest,
    write_manifest,
)
from strategies_state import manifests  # noqa: E402

KEY_A, KEY_B = "a" * 64, "b" * 64

RECORD = JobRecord(
    "render/tue-street/s01/c1", "render", "still", "submitted", post="tue-street", slide=1, candidate=1,
    job_key=KEY_A, seed=7, prompt_id="5d0b3c8e-4f7a-4c1e-9a55-2b1f0c7d9e11", submitted_at="2026-09-28T10:02:11Z",
)
MANIFEST = Manifest("week", "2026-W40", [RECORD])


def write(folder: Path, data: bytes | str | dict, name: str = "manifest.json") -> Path:
    path = folder / name
    if isinstance(data, dict):
        data = json.dumps(data, indent=2)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def names(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


# ---- Property 27 ----


# Feature: ai-influencer-pipeline, Property 27: Manifest round trip
# **Validates: Requirements 10.12**
@given(manifest=manifests())
def test_manifest_round_trip(manifest):
    with tempfile.TemporaryDirectory() as folder:
        back = read_manifest(write(Path(folder), print_manifest(manifest)))
    assert back == manifest


# ---- Reader and Printer ----

DESIGN_EXAMPLE = """\
{
  "format": "influencer-manifest/1",
  "kind": "week",
  "scope": "2026-W40",
  "jobs": [
    {
      "id": "render/tue-street/s01/c2",
      "stage": "render", "template": "still", "post": "tue-street", "slide": 1, "candidate": 2,
      "job_key": "9f2cJOBKEY", "status": "done", "seed": 3141592653,
      "prompt_id": "5d0b3c8e-4f7a-4c1e-9a55-2b1f0c7d9e11", "resubmits": 0,
      "submitted_at": "2026-09-28T10:02:11Z", "started_at": "2026-09-28T10:02:12Z", "finished_at": "2026-09-28T10:02:58Z",
      "duration_s": 46.3,
      "outputs": [{"path": "influencer/mara-lind/2026-W40/tue-street/render/tue-street_s01_c2_00001_.png", "bytes": 5123456}],
      "error": null, "waiting_for": null
    }
  ]
}
""".replace("9f2cJOBKEY", "9f2c" + "0" * 60)  # the design writes "9f2c…(64 hex)"


def test_the_design_example_reads(tmp_path):
    assert read_manifest(write(tmp_path, DESIGN_EXAMPLE)) == Manifest(
        "week",
        "2026-W40",
        [
            JobRecord(
                "render/tue-street/s01/c2", "render", "still", "done", post="tue-street", slide=1, candidate=2,
                job_key="9f2c" + "0" * 60, seed=3141592653, prompt_id="5d0b3c8e-4f7a-4c1e-9a55-2b1f0c7d9e11",
                resubmits=0, submitted_at="2026-09-28T10:02:11Z", started_at="2026-09-28T10:02:12Z",
                finished_at="2026-09-28T10:02:58Z", duration_s=46.3,
                outputs=[
                    Output("influencer/mara-lind/2026-W40/tue-street/render/tue-street_s01_c2_00001_.png", 5123456)
                ],
            )
        ],
    )


def test_an_error_message_keeps_its_non_ascii_text_line_breaks_and_quotes(tmp_path):
    error = 'Prompt outputs failed validation: "Café" — 😀\nline 2 \\ end\u2028'
    failed = dataclasses.replace(RECORD, status="failed", error=error)
    text = print_manifest(Manifest("week", "2026-W40", [failed]))
    assert '"error": "Prompt outputs failed validation: \\"Café\\" — 😀\\nline 2 \\\\ end\u2028"' in text
    assert read_manifest(write(tmp_path, text)).jobs[0].error == error


def test_a_lone_surrogate_is_written_as_an_escape_and_reads_back(tmp_path):
    failed = dataclasses.replace(RECORD, status="failed", error="cannot open b\udcff.png")
    text = print_manifest(Manifest("week", "2026-W40", [failed]))
    assert '"error": "cannot open b\\udcff.png"' in text
    assert read_manifest(write(tmp_path, text)).jobs[0] == failed


def record(**fields: object) -> dict:
    """A valid record as JSON data, with fields changed or added."""
    data = {
        "id": "render/tue-street/s01/c1", "stage": "render", "template": "still", "post": "tue-street",
        "slide": 1, "candidate": 1, "job_key": KEY_A, "status": "done", "seed": 7, "prompt_id": "p",
        "resubmits": 0, "submitted_at": None, "started_at": None, "finished_at": None, "duration_s": 46.3,
        "outputs": [{"path": "influencer/tue-street_s01_c1_00001_.png", "bytes": 1}], "error": None,
        "waiting_for": None,
    }
    data.update(fields)
    return data


def grid_record_without_checkpoint() -> dict:
    data = record(
        id="grid/lora-000002000/0.8/p01", stage="grid", post=None, slide=None, candidate=None,
        checkpoint="lora-000002000.safetensors", strength=0.8, prompt=1,
    )
    del data["checkpoint"]
    return data


def waiting(ident: str) -> dict:
    return record(id=ident, status="waiting", job_key=None, prompt_id=None, outputs=[], waiting_for="the Pick of s01")


REJECTED = {
    "two records sharing a job_key": (
        [record(id="render/tue-street/s01/c1"), record(id="render/tue-street/s02/c1")],
        "jobs[2].job_key",
        "job 'render/tue-street/s02/c1': jobs[1] (job 'render/tue-street/s01/c1') has the same job_key",
    ),
    "two waiting records sharing an id": (
        [waiting("render/tue-street/s02/c1"), waiting("render/tue-street/s02/c1")],
        "jobs[2].id",
        "job 'render/tue-street/s02/c1': jobs[1] is a waiting record with the same id",
    ),
    "an unknown status": (
        [record(status="queued")],
        "jobs[1].status",
        "job 'render/tue-street/s01/c1': \"queued\" is not a status; a status is waiting, submitted, done or failed",
    ),
    "a grid record without checkpoint": (
        [grid_record_without_checkpoint()],
        "jobs[1].checkpoint",
        "job 'grid/lora-000002000/0.8/p01': missing; a grid record has checkpoint, strength and prompt",
    ),
}


@pytest.mark.parametrize(("jobs", "field", "reason"), REJECTED.values(), ids=REJECTED.keys())
def test_a_bad_record_is_reported_with_its_id(tmp_path, jobs, field, reason):
    path = write(tmp_path, {"format": "influencer-manifest/1", "kind": "week", "scope": "2026-W40", "jobs": jobs})
    with pytest.raises(ValidationError) as caught:
        read_manifest(path)
    assert [(problem.path, problem.field) for problem in caught.value.problems] == [(str(path), field)]
    assert caught.value.problems[0].reason.startswith(reason)


def test_a_valid_manifest_may_repeat_an_id_across_job_keys():
    old = dataclasses.replace(RECORD, status="done")
    again = dataclasses.replace(RECORD, job_key=KEY_B)
    pending = JobRecord(RECORD.id, "render", "still", "waiting", waiting_for="the Pick of s01")
    manifest = Manifest("week", "2026-W40", [old, again, pending])
    assert '"job_key": null' in print_manifest(manifest)


def test_the_printer_refuses_a_manifest_the_reader_would_reject():
    twice = Manifest("week", "2026-W40", [RECORD, dataclasses.replace(RECORD, id="render/tue-street/s02/c1")])
    with pytest.raises(ValueError, match=r"jobs\[2\]\.job_key: job 'render/tue-street/s02/c1'"):
        print_manifest(twice)


# ---- loading, and the unreadable-file branch ----

UNREADABLE = {
    "not JSON": (b'{\n  "format": "influencer-manifest/1",\n  "kind": }\n', 3, "not valid JSON (column 11)"),
    "not UTF-8": (b'{"format": "influencer-manifest/1\xff"}', 1, "not UTF-8 text: byte 0xff"),
    "another format": (
        json.dumps({"format": "influencer-manifest/2", "kind": "week", "scope": "2026-W40", "jobs": []}).encode(),
        None,
        'is "influencer-manifest/2"; this version reads influencer-manifest/1',
    ),
}


@pytest.mark.parametrize(("content", "line", "reason"), UNREADABLE.values(), ids=UNREADABLE.keys())
def test_an_unreadable_manifest_is_reported_with_its_path_and_left_unchanged(tmp_path, content, line, reason):
    path = write(tmp_path, content)
    before = path.stat()
    with pytest.raises(ValidationError) as caught:
        load_manifest(path, kind="week", scope="2026-W40")
    (problem,) = caught.value.problems
    assert (problem.path, problem.line) == (str(path), line)
    assert reason in problem.reason
    assert str(caught.value).startswith(f"{path}: ")
    assert path.read_bytes() == content
    assert path.stat().st_mtime_ns == before.st_mtime_ns
    assert names(tmp_path) == ["manifest.json"]


def test_a_missing_manifest_starts_empty_and_writes_nothing(tmp_path):
    assert load_manifest(tmp_path / "manifest.json", kind="week", scope="2026-W40") == Manifest("week", "2026-W40")
    assert names(tmp_path) == []


# ---- atomic writes ----


def test_a_write_leaves_no_temp_file(tmp_path):
    path = tmp_path / "manifest.json"
    write_manifest(path, Manifest("week", "2026-W40"))  # creates the file
    write_manifest(path, MANIFEST)  # replaces it
    assert names(tmp_path) == ["manifest.json"]
    assert load_manifest(path, kind="week", scope="2026-W40") == MANIFEST


def test_a_write_interrupted_before_os_replace_leaves_the_previous_manifest(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    write_manifest(path, MANIFEST)
    before = path.read_bytes()
    done = dataclasses.replace(RECORD, status="done", duration_s=46.3, outputs=[Output("influencer/x.png", 5)])

    def interrupted(source: object, target: object) -> None:
        raise OSError("interrupted before os.replace")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", interrupted)
        with pytest.raises(OSError, match="interrupted before os.replace"):
            write_manifest(path, Manifest("week", "2026-W40", [done]))
    assert path.read_bytes() == before
    assert read_manifest(path) == MANIFEST
    assert names(tmp_path) == ["manifest.json"]
