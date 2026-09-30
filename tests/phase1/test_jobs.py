"""Tests of the Job builders, scripts/influencer/jobs.py (tasks 20.1-20.3, 20.8; design §A13, §M5,
Property 28, Decision 27; Req 9.11, 10.3, 10.4, 10.11, 11.2, 11.7-11.10, 12.1, 12.4, 12.5).

- Property 28: the render and finish builders produce Jobs only for approved Posts, and the gate the
  reels builder will share reports every other Post as waiting for planning or review
- unit tests: the Job shape and key, 4:5 Edit sizes, the finish upscale factor, the LoRA lists, sharp
  decoding, a missing Character_LoRA, an invalid Pick, a stale finished image, `other` output prefixes
"""
from __future__ import annotations

import dataclasses
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import ids  # noqa: E402
from influencer import jobs as J  # noqa: E402
from influencer.calendar_io import STILL_TYPES, Calendar, Post  # noqa: E402
from influencer.manifest import DONE, JobRecord, Manifest, Output  # noqa: E402
from influencer.persona import CharacterLoras, LoraRef  # noqa: E402
from influencer.preflight import PreflightInput, required_files  # noqa: E402
from influencer.prompt_sheet import Entry, PromptSheet, Slide  # noqa: E402
from influencer.templates import load_template  # noqa: E402
from strategies_calendar import calendars  # noqa: E402
from strategies_jobs import APPROVED, BASE_PERSONA, MISSING, UNAPPROVED, MemoryFiles, approval_sheets, job_persona  # noqa: E402

WEEK = "2026-W40"
TEMPLATES = {name: load_template(name) for name in ("still", "still-sharp", "edit", "finish")}
EMPTY = Manifest("week", WEEK)


def post(ident: str = "tue-street", kind: str = "carousel", **fields: object) -> Post:
    fields.setdefault("location", "cafe")
    fields.setdefault("outfit", "cream")
    if kind == "carousel":
        fields.setdefault("slides", 3)
    return Post(ident, kind, "2026-09-29", **fields)


def sheet_for(*posts: Post, picks: dict[tuple[str, int], object] | None = None, approved: bool = True) -> PromptSheet:
    picks = picks or {}
    entries = [
        Entry(p.id, approved=approved,
              slides=tuple(Slide(f"scene {n}", "alt", picks.get((p.id, n))) for n in range(1, p.slides + 1)))
        for p in posts
    ]  # fmt: skip
    return PromptSheet(WEEK, tuple(entries))


def render(*posts: Post, persona=BASE_PERSONA, manifest=EMPTY, memory=None, sheet=None) -> J.Build:
    memory = memory or MemoryFiles()
    return J.build_render(WEEK, Calendar(WEEK, posts), persona, sheet or sheet_for(*posts), manifest, TEMPLATES,
                          memory.files)  # fmt: skip


def finish(*posts: Post, persona=BASE_PERSONA, manifest=EMPTY, memory=None, sheet=None) -> J.Build:
    memory = memory or MemoryFiles()
    return J.build_finish(WEEK, Calendar(WEEK, posts), persona, sheet or sheet_for(*posts), manifest, TEMPLATES,
                          memory.files)  # fmt: skip


def by_id(build: J.Build) -> dict[str, J.Job]:
    return {job.id: job for job in build.jobs}


def done(job: J.Job, memory: MemoryFiles, data: bytes, size: tuple[int, int], key: str | None = None) -> JobRecord:
    """A done record of job with one output file of data, written into memory."""
    relative = f"{job.output_prefix}_00001_.png"
    memory.add(relative, data, size)
    return JobRecord(job.id, job.stage, job.template_name, DONE, post=job.post, slide=job.slide,
                     candidate=job.candidate, job_key=key or job.key(memory.files.read), seed=job.seed,
                     outputs=(Output(relative, len(data)),))  # fmt: skip


def with_records(*records: JobRecord) -> Manifest:
    return Manifest("week", WEEK, records)


# ---- Property 28 ----


@st.composite
def _gate_cases(draw: st.DrawFn):
    persona = draw(job_persona())
    calendar = draw(calendars(persona, week=WEEK))
    sheet, states = draw(approval_sheets(calendar))
    return persona, calendar, sheet, states


# Feature: ai-influencer-pipeline, Property 28: Approval gate
@settings(deadline=None)
@given(_gate_cases())
def test_property_28_approval_gate(case) -> None:
    """**Validates: Requirements 9.11**"""
    persona, calendar, sheet, states = case
    memory = MemoryFiles()
    for types, builder in ((STILL_TYPES, J.build_render), (STILL_TYPES, J.build_finish), (J.REEL_TYPES, None)):
        posts = [p for p in calendar.posts if p.type in types]
        want = {state: {p.id for p in posts if states[p.id] == state} for state in (MISSING, UNAPPROVED, APPROVED)}
        gate = J.approval_gate(calendar, sheet, types)
        assert {p.id for p, _ in gate.approved} == want[APPROVED]
        assert set(gate.planning) == want[MISSING]
        assert set(gate.review) == want[UNAPPROVED]
        if builder is None:
            continue  # the reels builder arrives with Phase 3; it uses this same gate
        build = builder(WEEK, calendar, persona, sheet, EMPTY, TEMPLATES, memory.files)
        assert {job.post for job in build.jobs} == want[APPROVED]  # every approved Post has a Job
        assert set(build.planning) == want[MISSING] and set(build.review) == want[UNAPPROVED]
    assert memory.reads == []  # an empty Manifest names no Pick, so no file was read


# ---- the Job (20.1) ----


def test_still_job_shape_key_and_timeout() -> None:
    p = post("mon-cafe", "single")
    job = by_id(render(p))["render/mon-cafe/s01/c1"]
    assert job.template_name == "still" and job.stage == "render" and job.timeout_s == 900
    assert job.seed == ids.seed_for(WEEK, "mon-cafe", 1, 1) == job.values["seed"]
    assert job.output_prefix == f"influencer/mara-lind/{WEEK}/mon-cafe/render/mon-cafe_s01_c1"
    assert job.values["output_prefix"] == job.output_prefix
    assert (job.values["width"], job.values["height"]) == (1440, 1920)
    assert job.inputs == {} and job.waiting_for is None
    prompt, uploads = job.prompt(lambda path: b"")
    assert uploads == {} and job.key(lambda path: b"") == ids.job_key(prompt, {})
    assert job.key(lambda path: b"") == by_id(render(p))[job.id].key(lambda path: b"")  # rebuilt, same key


def test_other_platform_output_prefixes() -> None:
    p = post("fri-dinner", "carousel", platform="other", slides=2)
    memory = MemoryFiles()
    slide1 = by_id(render(p))["render/fri-dinner/s01/c1"]
    manifest = with_records(done(slide1, memory, b"still", (1440, 1920)))
    for job in [*render(p, manifest=manifest, memory=memory).jobs, *finish(p, manifest=manifest, memory=memory).jobs]:
        assert job.output_prefix.startswith(f"influencer/mara-lind/{WEEK}/other/fri-dinner/{job.stage}/fri-dinner_s0")
    instagram = by_id(render(post("fri-dinner", "carousel", slides=2)))["render/fri-dinner/s02/c1"]
    assert "/other/" not in instagram.output_prefix


# ---- the render builder (20.2) ----


def test_still_loras_character_first_then_looks() -> None:
    p = post("mon-cafe", "single")
    assert by_id(render(p))["render/mon-cafe/s01/c1"].values["loras"] == [
        {"name": "characters/mara-lind/mara-v1.safetensors", "strength": 0.9},
        {"name": "looks/film-grain.safetensors", "strength": 0.5},
    ]
    own = post("mon-cafe", "single", looks=(LoraRef("looks/own.safetensors", -0.3),))
    assert [e["name"] for e in by_id(render(own))["render/mon-cafe/s01/c1"].values["loras"]] == [
        "characters/mara-lind/mara-v1.safetensors", "looks/own.safetensors"]  # fmt: skip
    empty = post("mon-cafe", "single", looks=())  # an empty list overrides the Persona_File's stack
    assert by_id(render(empty))["render/mon-cafe/s01/c1"].values["loras"] == [
        {"name": "characters/mara-lind/mara-v1.safetensors", "strength": 0.9}]  # fmt: skip
    other = post("mon-cafe", "single", platform="other")  # looks.stills.other is empty
    assert len(by_id(render(other))["render/mon-cafe/s01/c1"].values["loras"]) == 1


def test_still_sharp_selected_by_sharp_decode() -> None:
    p = post("mon-cafe", "story")
    sharp = dataclasses.replace(BASE_PERSONA, sharp_decode=True)
    assert {job.template_name for job in render(p, persona=sharp).jobs} == {"still-sharp"}
    assert {job.template_name for job in render(p).jobs} == {"still"}
    assert by_id(render(p))["render/mon-cafe/s01/c1"].values["width"] == 1080  # 9:16 crops to 1080


def test_carousel_edit_jobs_wait_then_take_the_slide_one_pick_at_4_5() -> None:
    p = post(aspect="4:5")
    memory = MemoryFiles()
    first = by_id(render(p, memory=memory))
    assert [first[f"render/tue-street/s0{n}/c1"].template_name for n in (1, 2, 3)] == ["still", "edit", "edit"]
    waiting = first["render/tue-street/s02/c1"]
    assert waiting.waiting_for is not None and "render/tue-street/s01/c1" in waiting.waiting_for
    assert waiting.inputs == {}
    manifest = with_records(done(first["render/tue-street/s01/c1"], memory, b"slide-1", (1536, 1920)))
    for slide in (2, 3):
        edit = by_id(render(p, manifest=manifest, memory=memory))[f"render/tue-street/s0{slide}/c1"]
        assert edit.waiting_for is None
        assert (edit.values["width"], edit.values["height"]) == (896, 1120) == edit.expect_size
        assert edit.inputs["reference_image"] == memory.output_dir / manifest.jobs[0].outputs[0].path
        prompt, uploads = edit.prompt(memory.files.read)
        assert list(uploads.values()) == [b"slide-1"]
        assert prompt["6"]["inputs"]["image"] == ids.upload_name(b"slide-1", ".png")


def test_edit_jobs_carry_no_loras() -> None:
    p = post()
    memory = MemoryFiles()
    slide1 = by_id(render(p))["render/tue-street/s01/c1"]
    manifest = with_records(done(slide1, memory, b"x", (1440, 1920)))
    edit = by_id(render(p, manifest=manifest, memory=memory))["render/tue-street/s02/c1"]
    assert "loras" not in edit.values
    prompt, _ = edit.prompt(memory.files.read)
    assert all(int(node_id) < 900 for node_id in prompt)  # no LoRA loader was inserted


def test_new_slide_one_pick_gives_new_edit_key() -> None:
    p = post()
    memory = MemoryFiles()
    slide1 = by_id(render(p))["render/tue-street/s01/c1"]
    manifest = with_records(done(slide1, memory, b"first", (1440, 1920)))
    before = by_id(render(p, manifest=manifest, memory=memory))["render/tue-street/s02/c1"].key(memory.files.read)
    memory.data[memory.output_dir / manifest.jobs[0].outputs[0].path] = b"other"
    rebuilt = by_id(render(p, manifest=manifest, memory=memory))
    # the Slide-1 record's key no longer matches the file, but the Still Job's key has no inputs, so the
    # record still names the Pick; the Edit Job's key follows the new bytes
    assert rebuilt["render/tue-street/s02/c1"].key(memory.files.read) != before


def test_missing_character_lora_stops_the_stage() -> None:
    bare = dataclasses.replace(BASE_PERSONA, character_loras=CharacterLoras())
    for builder in (render, finish):
        with pytest.raises(J.NoCharacterLora) as caught:
            builder(post("mon-cafe", "single"), persona=bare)
        assert caught.value.problems[0].field == "character_loras.stills"


def test_invalid_pick_reported_while_other_jobs_build() -> None:
    bad, good = post("tue-street", candidates=2), post("mon-cafe", "single")
    sheet = sheet_for(bad, good, picks={("tue-street", 1): 5})
    build = render(bad, good, sheet=sheet)
    assert [(i.post, i.slide, i.value, i.valid) for i in build.invalid_picks] == [("tue-street", 1, 5, (1, 2))]
    assert "5" in build.invalid_picks[0].message and "1, 2" in build.invalid_picks[0].message
    assert set(by_id(build)) == {"render/tue-street/s01/c1", "render/tue-street/s01/c2", "render/mon-cafe/s01/c1"}
    finished = finish(bad, good, sheet=sheet)
    assert "finish/tue-street/s01" not in by_id(finished) and "finish/tue-street/s02" in by_id(finished)
    assert len(finished.invalid_picks) == 1


def test_waiting_for_a_pick_reports_each_rendered_candidate() -> None:
    p = post(candidates=2, slides=2)
    memory = MemoryFiles()
    c2 = by_id(render(p))["render/tue-street/s01/c2"]
    manifest = with_records(done(c2, memory, b"c2", (1440, 1920)))
    build = render(p, manifest=manifest, memory=memory)
    assert build.pick_waits == (J.PickWait("tue-street", 1, ((1, None), (2, memory.output_dir / manifest.jobs[0].outputs[0].path))),)
    assert all(by_id(build)[f"render/tue-street/s02/c{c}"].waiting_for.startswith("a Pick") for c in (1, 2))


# ---- the finish builder and the gate (20.3) ----


def test_finish_upscale_2_for_a_still_pick_and_3_for_an_edit_pick() -> None:
    p = post(slides=2)
    memory = MemoryFiles()
    still = by_id(render(p))["render/tue-street/s01/c1"]
    manifest = with_records(done(still, memory, b"still", (1440, 1920)))
    edit = by_id(render(p, manifest=manifest, memory=memory))["render/tue-street/s02/c1"]
    manifest = with_records(*manifest.jobs, done(edit, memory, b"edit", (864, 1152)))
    jobs = by_id(finish(p, manifest=manifest, memory=memory))
    assert jobs["finish/tue-street/s01"].values["upscale"] == 2
    assert jobs["finish/tue-street/s02"].values["upscale"] == 3
    for slide in (1, 2):
        job = jobs[f"finish/tue-street/s0{slide}"]
        assert job.waiting_for is None and job.template_name == "finish" and job.candidate is None
        assert job.values["character_lora"] == "characters/mara-lind/mara-v1.safetensors"
        assert set(job.inputs) == {"pick_image"}
        job.prompt(memory.files.read)  # fills without a FillError


def test_finish_waits_without_a_pick_file() -> None:
    job = by_id(finish(post("mon-cafe", "single")))["finish/mon-cafe/s01"]
    assert job.waiting_for is not None and "render/mon-cafe/s01/c1" in job.waiting_for
    assert "upscale" not in job.values and job.preview_prompt()["13"]["inputs"]["upscale"] == 2


def test_finished_image_with_a_stale_key_does_not_count() -> None:
    p = post("mon-cafe", "single")
    memory = MemoryFiles()
    still = by_id(render(p))["render/mon-cafe/s01/c1"]
    base = [done(still, memory, b"pick", (1440, 1920))]
    job = by_id(finish(p, manifest=with_records(*base), memory=memory))["finish/mon-cafe/s01"]
    stale = done(job, memory, b"finished-from-an-earlier-pick", (2880, 3840), key="0" * 64)
    assert J.finished_image(job, with_records(*base, stale), memory.files) is None
    current = done(job, memory, b"finished", (2880, 3840))
    assert J.finished_image(job, with_records(*base, stale, current), memory.files) is not None
    memory.data.pop(memory.output_dir / current.outputs[0].path)  # a deleted output is not done either
    assert J.finished_image(job, with_records(*base, current), memory.files) is None


def test_gate_reports_planning_and_review() -> None:
    a, b, c = post("a-post", "single"), post("b-post", "single"), post("c-post", "single")
    sheet = PromptSheet(WEEK, (Entry("b-post", approved=False, slides=(Slide("s", "a"),)),
                               Entry("c-post", approved=True, slides=(Slide("s", "a"),))))  # fmt: skip
    build = render(a, b, c, sheet=sheet)
    assert set(build.planning) == {"a-post"} and build.review == ("b-post",)
    assert {job.post for job in build.jobs} == {"c-post"}
    assert set(J.approval_gate(Calendar(WEEK, (a,)), None, STILL_TYPES).planning) == {"a-post"}


def test_preflight_input_from_jobs() -> None:
    p = post()
    build = finish(p)
    request = J.preflight_input([*render(p).jobs, *build.jobs], REPO)
    assert isinstance(request, PreflightInput) and len(request.prompts) == 3 + 3
    names = {(item.folder, item.name) for item, _ in required_files(request)}
    assert ("loras", "characters/mara-lind/mara-v1.safetensors") in names
    assert ("diffusion_models", "qwen_image_edit_2511_bf16.safetensors") in names
