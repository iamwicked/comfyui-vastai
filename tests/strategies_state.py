"""Hypothesis strategies for the week's two state files (task 10, design Properties 11 and 27).

- prompt_sheets(): a valid Prompt_Sheet with texts() in every text value, and Picks that are null, a
  Candidate number, or a value PromptSheet.check reports
- job_records(): a valid Manifest job record of every Job kind and status, texts() in `error` and
  `waiting_for`
- manifests(): st.builds(Manifest, jobs=st.lists(job_records())), records unique as the Manifest requires

Kept apart from strategies.py, which other tasks extend meanwhile; texts() comes from there.
"""
from __future__ import annotations

import sys
from datetime import datetime

from hypothesis import strategies as st

from helpers.harness import REPO
from strategies import texts

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer.manifest import (  # noqa: E402
    GRID_STAGE,
    KINDS,
    SEED_MAX,
    STATUSES,
    WAITING,
    JobRecord,
    Manifest,
    Output,
)
from influencer.prompt_sheet import Entry, Planned, PromptSheet, Slide  # noqa: E402

# ---- Prompt_Sheet ----


def picks() -> st.SearchStrategy[object]:
    """A recorded pick: null, a Candidate number, or a value that is not one (0, 5, "2", true, any text)."""
    return st.one_of(st.none(), st.integers(1, 4), st.integers(), st.booleans(), texts())


def prompt_sheets() -> st.SearchStrategy[PromptSheet]:
    slides = st.builds(Slide, scene=texts(), alt=texts(), pick=picks())
    planned = st.builds(Planned, model=texts(), at=texts(), attempts=st.integers(1, 10))
    entries = st.builds(
        Entry,
        post=texts(min_size=1),
        approved=st.booleans(),
        needs_manual_prompt=st.booleans(),
        review=st.lists(texts(), max_size=3),
        planned=st.none() | planned,
        slides=st.lists(slides, max_size=4),
        caption=st.none() | texts(),
        hashtags=st.lists(texts(), max_size=4),
        motion=st.none() | texts(),
        raw_response=st.none() | texts(),
    )
    return st.builds(
        PromptSheet,
        week=texts(min_size=1),
        entries=st.lists(entries, max_size=3, unique_by=lambda entry: entry.post),
    )


# ---- Manifest ----

# Every Job kind as (stage, template). Render, finish and reel records name their Post; render and finish
# records their Slide, render records their Candidate; grid records add checkpoint, strength and prompt.
JOB_KINDS = (
    ("render", "still"),
    ("render", "edit"),
    ("finish", "finish"),
    ("reels", "reel"),
    ("reels", "ref_reel"),
    (GRID_STAGE, "still"),
    ("refine", "refine"),
)

_SLUGS = st.from_regex(r"[a-z][a-z0-9]{0,7}(-[a-z0-9]{1,6}){0,2}", fullmatch=True)
_JOB_KEYS = st.binary(min_size=32, max_size=32).map(bytes.hex)
_TIMES = st.none() | st.datetimes(min_value=datetime(2024, 1, 1), max_value=datetime(2100, 1, 1)).map(
    lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ")
)
_SEGMENTS = _SLUGS | texts(min_size=1, single_line=True).filter(
    lambda segment: "/" not in segment and "\x00" not in segment and segment != ".."
)
# Relative to ComfyUI's output folder, as the Manifest requires.
_OUTPUT_PATHS = st.lists(_SEGMENTS, min_size=1, max_size=4).map("/".join)
_OUTPUTS = st.lists(st.builds(Output, path=_OUTPUT_PATHS, bytes=st.integers(0, 2**40)), max_size=3)


@st.composite
def job_records(draw: st.DrawFn) -> JobRecord:
    """A valid job record: any Job kind and status, a job_key exactly when not waiting."""
    stage, template = draw(st.sampled_from(JOB_KINDS))
    status = draw(st.sampled_from(STATUSES))
    post = slide = candidate = checkpoint = strength = prompt = None
    if stage in ("render", "finish", "reels"):
        post = draw(_SLUGS)
    if stage in ("render", "finish"):
        slide = draw(st.integers(1, 20))
    if stage == "render":
        candidate = draw(st.integers(1, 4))
    if stage == GRID_STAGE:
        checkpoint = draw(_SLUGS) + ".safetensors"
        strength = draw(st.floats(-4, 4, allow_nan=False))
        prompt = draw(st.integers(1, 99))
    usual_id = {
        "render": f"render/{post}/s{slide or 0:02d}/c{candidate}",
        "finish": f"finish/{post}/s{slide or 0:02d}",
        "reels": f"reels/{post}",
        GRID_STAGE: f"grid/{checkpoint}/{strength}/p{prompt or 0:02d}",
        "refine": f"refine/{draw(_SLUGS)}",
    }[stage]
    waiting = status == WAITING
    return JobRecord(
        id=draw(st.just(usual_id) | texts(min_size=1)),
        stage=stage,
        template=template,
        status=status,
        post=post,
        slide=slide,
        candidate=candidate,
        job_key=None if waiting else draw(_JOB_KEYS),
        seed=draw(st.none() | st.integers(0, SEED_MAX)),
        prompt_id=None if waiting else draw(st.none() | st.uuids().map(str)),
        resubmits=draw(st.integers(0, 3)),
        submitted_at=draw(_TIMES),
        started_at=draw(_TIMES),
        finished_at=draw(_TIMES),
        duration_s=draw(st.none() | st.floats(0, 1e5, allow_nan=False)),
        outputs=draw(_OUTPUTS),
        error=draw(st.none() | texts()),
        waiting_for=draw(st.none() | texts()),
        checkpoint=checkpoint,
        strength=strength,
        prompt=prompt,
    )


def record_identity(record: JobRecord) -> tuple[str, str]:
    """What no two records of a Manifest share: the job_key, or a waiting record's id."""
    return ("id", record.id) if record.job_key is None else ("job_key", record.job_key)


def manifests() -> st.SearchStrategy[Manifest]:
    return st.builds(
        Manifest,
        kind=st.sampled_from(KINDS),
        scope=texts(min_size=1),
        jobs=st.lists(job_records(), max_size=6, unique_by=record_identity),
    )
