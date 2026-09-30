"""Tests of the stage engine, scripts/influencer/engine.py (tasks 20.4-20.7, 20.9; design §A13, §M4, §C6.1;
Req 9.5-9.11, 10.3-10.10, 10.13-10.15, 11.2, 12.4, 21.3).

Every test runs run_stage end to end against the fake ComfyUI and Ollama, with a Persona_File, calendar and
Prompt_Sheet written into a temp Influencer_Root and the shortened Timing:
- resume after a kill: attach to a queued prompt with no second POST, re-check then resubmit an unknown one
- a forgotten prompt resubmitted twice then "lost by ComfyUI"; a 400 rejection; a timeout interrupt
- a silent ComfyUI past the retry budget (exit 2), Ctrl-C (exit 130), a held lock (exit 4)
- the dry run: its folder, an untouched Manifest, `unknown` and a minute estimate
- a deleted output re-rendered, a missing input waiting, 4:5 Edit sizes and the edit output check, the
  finish upscale factors, the crash-safe submit order, the Ollama unload and the closing summary
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from fakes.comfy import FOREVER, FORGET, REJECT, Plan
from helpers.harness import REPO, SHORT_TIMING, TempEnv

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import engine as E  # noqa: E402
from influencer import jobs as J  # noqa: E402
from influencer.allowlist import Allowlist, character_names  # noqa: E402
from influencer.calendar_io import Calendar, Post, print_calendar_yaml  # noqa: E402
from influencer.comfy import ComfyClient  # noqa: E402
from influencer.errors import Exit  # noqa: E402
from influencer.locks import ProcessLock  # noqa: E402
from influencer.manifest import DONE, FAILED, SUBMITTED, WAITING, JobRecord, Manifest, read_manifest, write_manifest  # noqa: E402
from influencer.ollama import OllamaClient  # noqa: E402
from influencer.persona import LoraRef, print_persona  # noqa: E402
from influencer.preflight import required_files  # noqa: E402
from influencer.prompt_sheet import Entry, PromptSheet, Slide, print_prompt_sheet  # noqa: E402
from influencer.templates import load_template  # noqa: E402
from strategies_jobs import BASE_PERSONA  # noqa: E402

WEEK = "2026-W40"
PERSONA = dataclasses.replace(BASE_PERSONA, slug="test-persona")
LOOK = "looks/film-grain.safetensors"
ALLOW = Allowlist(frozenset({LOOK}), character_names(PERSONA))
STILL_34, EDIT_34, EDIT_45 = (1440, 1920), (864, 1152), (896, 1120)
EDIT_SIZE_NODE, UPSCALE_NODE = "7", "13"  # edit.json's ImageScale node, finish.json's upscaler


def single(ident: str = "mon-cafe", **fields: object) -> Post:
    return Post(ident, "single", "2026-09-28", location="cafe", outfit="cream", **fields)


def carousel(ident: str = "tue-street", **fields: object) -> Post:
    fields.setdefault("slides", 2)
    return Post(ident, "carousel", "2026-09-29", location="street", outfit="coat", **fields)


def complete_object_info(jobs: list[J.Job]) -> dict:
    """An /object_info reporting every class and input the Jobs' prompts use."""
    info: dict = {}
    for job in jobs:
        for node in job.preview_prompt().values():
            entry = info.setdefault(node["class_type"], {"input": {"required": {}, "optional": {}}, "output": []})
            for name in node["inputs"]:
                entry["input"]["required"][name] = ["*", {}]
    return info


@dataclass
class Result:
    code: int
    out: str
    err: str


class World:
    """A temp Influencer_Root with the persona, a week's calendar and Prompt_Sheet, and the fakes."""

    def __init__(self, env: TempEnv, comfy, ollama) -> None:
        self.env, self.comfy, self.ollama = env, comfy, ollama
        self.root = env.influencer_root
        self.folder = self.root / "weeks" / WEEK
        self.manifest_path = self.folder / "manifest.json"
        self.output_dir = env.comfyui_dir / "output"
        self.calendar: Calendar | None = None
        self.sheet: PromptSheet | None = None

    def write(self, *posts: Post, entries: dict[str, tuple[bool, dict[int, object]] | None] | None = None) -> None:
        """The persona, the calendar of posts and a Prompt_Sheet: every Post approved without Picks unless
        entries gives (approved, {slide: pick}) or None (no entry) for it. The fake then reports every
        class, input and model file of the render and finish Jobs."""
        (self.root / "refs").mkdir(parents=True, exist_ok=True)
        (self.root / "refs" / "hero.png").write_bytes(b"hero")
        (self.root / "grade").mkdir(exist_ok=True)
        (self.root / "grade" / "film.cube").write_text("cube")
        (self.root / "persona.yaml").write_text(print_persona(PERSONA))
        self.folder.mkdir(parents=True, exist_ok=True)
        self.calendar = Calendar(WEEK, posts)
        (self.folder / "calendar.yaml").write_text(print_calendar_yaml(self.calendar))
        rows = []
        for post in posts:
            state = (entries or {}).get(post.id, (True, {}))
            if state is None:
                continue
            approved, picks = state
            slides = tuple(Slide(f"scene {n}", "alt", picks.get(n)) for n in range(1, (post.slides or 1) + 1))
            rows.append(Entry(post.id, approved=approved, slides=slides))
        self.sheet = PromptSheet(WEEK, tuple(rows))
        (self.folder / "prompts.yaml").write_text(print_prompt_sheet(self.sheet))
        self.ready()

    def templates(self) -> dict:
        return {name: load_template(name) for name in J.template_names(J.FINISH, PERSONA)}

    def build(self, stage: str = J.RENDER, manifest: Manifest | None = None) -> J.Build:
        builder = J.build_render if stage == J.RENDER else J.build_finish
        manifest = manifest or Manifest("week", WEEK)
        return builder(WEEK, self.calendar, PERSONA, self.sheet, manifest, self.templates(), E.disk_files(self.output_dir))

    def ready(self) -> None:
        jobs = [*self.build(J.RENDER).jobs, *self.build(J.FINISH).jobs]
        self.comfy.object_info = complete_object_info(jobs)
        for item, _ in required_files(J.preflight_input(jobs, self.output_dir)):
            self.comfy.models.setdefault(item.folder, []).append(item.name)

    def run(self, stage: str = J.RENDER, *, templates=None, sleep=time.sleep, dry_run=False, ollama=None,
            comfy=None) -> Result:  # fmt: skip
        out, err = io.StringIO(), io.StringIO()
        code = E.run_stage(
            stage, WEEK, dry_run=dry_run, comfy=comfy or ComfyClient.from_env(timing=SHORT_TIMING),
            ollama=ollama or OllamaClient.from_env(timing=SHORT_TIMING), timing=SHORT_TIMING, sleep=sleep,
            templates=templates, allowlist=ALLOW, out=out, err=err,
        )  # fmt: skip
        return Result(code, out.getvalue(), err.getvalue())

    def records(self) -> dict[str, JobRecord]:
        """The Manifest's records by id (the last one of an id wins)."""
        return {record.id: record for record in read_manifest(self.manifest_path).jobs}

    def posts(self) -> list:
        return self.comfy.calls("POST", "/prompt")


@pytest.fixture
def world(temp_env, comfy_fake, ollama_fake, monkeypatch) -> World:
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10**12, 0, 10**12))
    return World(temp_env, comfy_fake, ollama_fake)


def submitted_record(job: J.Job, key: str, prompt_id: str) -> JobRecord:
    return JobRecord(job.id, job.stage, job.template_name, SUBMITTED, post=job.post, slide=job.slide,
                     candidate=job.candidate, job_key=key, seed=job.seed, prompt_id=prompt_id,
                     submitted_at="2026-09-28T10:00:00Z")  # fmt: skip


def the_job(world: World, ident: str, stage: str = J.RENDER) -> J.Job:
    return {job.id: job for job in world.build(stage, read_manifest(world.manifest_path)
                                               if world.manifest_path.exists() else None).jobs}[ident]  # fmt: skip


# ---- resume after a kill (Req 10.13, 10.5) ----


def test_a_submitted_record_still_queued_is_attached_without_a_second_post(world: World) -> None:
    world.write(single())
    job = the_job(world, "render/mon-cafe/s01/c1")
    prompt, _ = job.prompt(Path.read_bytes)
    key = job.key(Path.read_bytes)
    prompt_id = world.comfy.enqueue(prompt, plan=Plan(run_s=0.3))
    write_manifest(world.manifest_path, Manifest("week", WEEK, (submitted_record(job, key, prompt_id),)))
    result = world.run()
    assert result.code == Exit.OK, result.err
    assert world.posts() == []  # attached, never sent again
    record = world.records()[job.id]
    assert (record.status, record.prompt_id, record.resubmits) == (DONE, prompt_id, 0)
    assert record.outputs and (world.output_dir / record.outputs[0].path).stat().st_size == record.outputs[0].bytes
    assert record.duration_s is not None and record.duration_s >= 0.3 - 0.01


def test_a_submitted_record_unknown_to_comfyui_is_rechecked_once_then_resubmitted(world: World) -> None:
    world.write(single())
    job = the_job(world, "render/mon-cafe/s01/c1")
    stale = str(uuid.uuid4())  # written, then killed before POST /prompt: ComfyUI never saw it
    write_manifest(world.manifest_path,
                   Manifest("week", WEEK, (submitted_record(job, job.key(Path.read_bytes), stale),)))  # fmt: skip
    result = world.run()
    assert result.code == Exit.OK, result.err
    assert len(world.comfy.calls("GET", f"/history/{stale}")) == 2  # checked, then re-checked once
    assert len(world.posts()) == 1
    record = world.records()[job.id]
    assert record.status == DONE and record.prompt_id != stale and record.resubmits == 1


def test_the_submitted_record_is_on_disk_before_post_prompt(world: World) -> None:
    world.write(single())
    seen = []

    class Watching(ComfyClient):
        def submit(self, graph, prompt_id=None):
            records = {r.prompt_id: r.status for r in read_manifest(world.manifest_path).jobs}
            seen.append(records.get(prompt_id))
            return super().submit(graph, prompt_id)

    result = world.run(comfy=Watching(world.comfy.url, timing=SHORT_TIMING))
    assert result.code == Exit.OK, result.err
    assert seen == [SUBMITTED]


# ---- lost, rejected, timed out (Req 10.5, 10.6, 10.7) ----


def test_a_forgotten_prompt_is_resubmitted_twice_then_failed_lost(world: World) -> None:
    world.write(single())
    world.comfy.script(Plan(FORGET), Plan(FORGET), Plan(FORGET))
    result = world.run()
    assert result.code == Exit.FAILED
    assert len(world.posts()) == 3
    record = world.records()["render/mon-cafe/s01/c1"]
    assert record.status == FAILED and record.error.startswith("lost by ComfyUI") and record.resubmits == 2
    assert "failed: Post mon-cafe, Slide 1, Candidate 1 (render/mon-cafe/s01/c1): lost by ComfyUI" in result.out


def test_a_rejected_prompt_fails_with_comfyuis_message_and_is_not_resubmitted(world: World) -> None:
    world.write(single())
    world.comfy.script(Plan(REJECT, message="Value not in list: lora_name"))
    result = world.run()
    assert result.code == Exit.FAILED
    assert len(world.posts()) == 1
    record = world.records()["render/mon-cafe/s01/c1"]
    assert record.status == FAILED and "Value not in list: lora_name" in record.error


def test_a_running_job_past_its_timeout_is_interrupted_and_failed(world: World) -> None:
    world.write(single())
    world.comfy.default_plan = Plan(FOREVER)
    templates = {name: dataclasses.replace(t, timeout_s=1) for name, t in world.templates().items()}
    result = world.run(templates=templates)
    assert result.code == Exit.FAILED
    record = world.records()["render/mon-cafe/s01/c1"]
    assert world.comfy.interrupts == [record.prompt_id]
    assert (record.status, record.error) == (FAILED, "timed out after 1 s")
    assert record.started_at is not None


# ---- silence, Ctrl-C, the lock (Req 10.15, 10.10) ----


def test_a_silent_comfyui_is_retried_for_the_budget_then_exits_2_with_the_url(world: World) -> None:
    world.write(single())
    world.comfy.default_plan = Plan(run_s=30)

    def sleep(seconds: float) -> None:
        world.comfy.pause()  # after the submit: every later request gets no answer
        time.sleep(seconds)

    begun = time.monotonic()
    result = world.run(sleep=sleep)
    assert result.code == Exit.ENVIRONMENT
    assert time.monotonic() - begun >= SHORT_TIMING.silence_budget_s
    assert f"ComfyUI at {world.comfy.url} gave no answer" in result.err
    assert world.records()["render/mon-cafe/s01/c1"].status == SUBMITTED  # no status changed


def test_ctrl_c_exits_130_with_the_queued_jobs_still_submitted(world: World) -> None:
    world.write(single(candidates=2))
    world.comfy.default_plan = Plan(run_s=30)

    def sleep(seconds: float) -> None:
        raise KeyboardInterrupt  # SIGINT during the loop's first wait

    result = world.run(sleep=sleep)
    assert result.code == E.INTERRUPTED_EXIT == 130
    records = world.records()
    assert {records[f"render/mon-cafe/s01/c{n}"].status for n in (1, 2)} == {SUBMITTED}
    assert {records[f"render/mon-cafe/s01/c{n}"].prompt_id for n in (1, 2)} == set(world.comfy.prompt_ids)
    assert len(world.posts()) == 2


def test_a_held_lock_exits_4_with_the_holders_pid(world: World) -> None:
    world.write(single())
    with ProcessLock(world.folder / ".lock"):
        result = world.run()
    assert result.code == Exit.LOCKED
    assert f"PID {os.getpid()}" in result.err
    assert world.posts() == [] and not world.manifest_path.exists()


# ---- the dry run (Req 9.5, 9.6, 9.9) ----


def _dry_folder(world: World) -> Path:
    [folder] = (world.env.output_root / "dry-run").iterdir()
    return folder


def test_a_dry_run_writes_its_folder_and_estimates_unknown_without_history(world: World) -> None:
    world.write(single(candidates=2))
    result = world.run(dry_run=True)
    assert result.code == Exit.OK, result.out + result.err
    folder = _dry_folder(world)
    assert folder.name.startswith(f"render-{WEEK}-")
    names = sorted(path.name for path in folder.iterdir())
    assert names == ["render_mon-cafe_s01_c1.json", "render_mon-cafe_s01_c2.json"]
    job = the_job(world, "render/mon-cafe/s01/c1")
    assert json.loads((folder / names[0]).read_text()) == job.prompt(Path.read_bytes)[0]
    assert "still: 2 to submit, 0 done, 0 waiting; estimate unknown" in result.out
    assert "preflight: every check passed" in result.out
    assert world.posts() == [] and not world.manifest_path.exists()


def test_a_dry_run_leaves_the_manifest_byte_identical_and_estimates_minutes(world: World) -> None:
    world.write(single(candidates=2))
    other = world.root / "weeks" / "2026-W39"
    other.mkdir(parents=True)
    durations = [JobRecord(f"render/old/s01/c{n}", "render", "still", DONE, job_key=f"{n:064x}", duration_s=d)
                 for n, d in ((1, 50.0), (2, 81.0))]  # fmt: skip
    write_manifest(other / "manifest.json", Manifest("week", "2026-W39", tuple(durations)))
    unrelated = JobRecord("render/gone/s01/c1", "render", "still", FAILED, job_key="f" * 64, error="old")
    write_manifest(world.manifest_path, Manifest("week", WEEK, (unrelated,)))
    before = world.manifest_path.read_bytes()
    world.comfy.object_info = {}  # the preflight fails, and the files and report are still written
    result = world.run(dry_run=True)
    assert result.code == Exit.ENVIRONMENT
    assert world.manifest_path.read_bytes() == before
    assert len(list(_dry_folder(world).iterdir())) == 2
    # the median of 50 and 81 is 65.5 s; times 2 Jobs is 131 s, rounded up to 3 minutes
    assert "still: 2 to submit, 0 done, 0 waiting; estimate 3 min" in result.out
    assert "preflight: " in result.out and "failed check" in result.out
    assert world.posts() == []


def test_estimate_and_median() -> None:
    assert E.median([]) is None and E.estimate([], 3) == "unknown"
    assert E.median([3.0, 1.0, 2.0]) == 2 and E.median([50.0, 81.0]) == 65.5
    assert E.estimate([60.0], 2) == "2 min" and E.estimate([61.0], 1) == "2 min" and E.estimate([30.0], 0) == "0 min"


# ---- done, waiting and the output checks (Req 10.3, 10.4, 11.2, 12.4) ----


def test_a_done_job_whose_output_was_deleted_is_submitted_again(world: World) -> None:
    world.write(single())
    assert world.run().code == Exit.OK
    record = world.records()["render/mon-cafe/s01/c1"]
    (world.output_dir / record.outputs[0].path).unlink()
    again = world.run()
    assert again.code == Exit.OK and len(world.posts()) == 2
    assert world.records()["render/mon-cafe/s01/c1"].status == DONE
    unchanged = world.run()  # every output present: nothing is submitted (Req 10.3)
    assert unchanged.code == Exit.OK and len(world.posts()) == 2
    assert "1 done (1 already done)" in unchanged.out


def test_a_job_whose_input_is_missing_stays_waiting_while_the_others_run(world: World) -> None:
    world.write(single(), single("wed-street"))
    world.comfy.default_plan = Plan(size=STILL_34)
    assert world.run().code == Exit.OK
    pick = world.records()["render/mon-cafe/s01/c1"].outputs[0].path
    (world.output_dir / pick).unlink()  # mon-cafe's Pick is gone; wed-street's is there
    result = world.run(J.FINISH)
    assert result.code == Exit.OK, result.err  # waiting Jobs do not fail the stage
    records = world.records()
    assert records["finish/wed-street/s01"].status == DONE
    waiting = records["finish/mon-cafe/s01"]
    assert waiting.status == WAITING and waiting.job_key is None and "render/mon-cafe/s01/c1" in waiting.waiting_for
    assert "waiting: Post mon-cafe, Slide 1 (finish/mon-cafe/s01): needs the Pick of Slide 1" in result.out


def _graph(world: World, record: JobRecord) -> dict:
    return world.comfy.prompt(record.prompt_id).graph


def test_a_4_5_carousel_edit_job_carries_896_by_1120_and_a_wrong_size_output_fails(world: World) -> None:
    world.write(carousel(aspect="4:5"))
    world.comfy.script(Plan(size=(1536, 1920)), Plan(size=EDIT_34))  # the edit output comes back 864×1152
    result = world.run()
    assert result.code == Exit.FAILED
    records = world.records()
    assert records["render/tue-street/s01/c1"].status == DONE
    edit = records["render/tue-street/s02/c1"]  # became ready in the same run, after Slide 1 was done
    inputs = _graph(world, edit)[EDIT_SIZE_NODE]["inputs"]
    assert (inputs["width"], inputs["height"]) == EDIT_45
    assert edit.status == FAILED and "864×1152" in edit.error and "896×1120" in edit.error


def test_a_carousel_renders_in_one_run_and_finish_fills_upscale_3_and_2(world: World) -> None:
    world.write(carousel())
    world.ollama.add_model("qwen3:8b")
    world.ollama.loaded["qwen3:8b"] = time.time()
    world.comfy.script(Plan(size=STILL_34), Plan(size=EDIT_34))
    rendered = world.run()
    assert rendered.code == Exit.OK, rendered.out + rendered.err
    assert world.ollama.loaded == {}  # unloaded before the first Job (Req 9.7)
    assert len(world.posts()) == 2  # Slide 2's Edit Job waited for Slide 1 and ran in the same run
    finished = world.run(J.FINISH)
    assert finished.code == Exit.OK, finished.out + finished.err
    records = world.records()
    assert _graph(world, records["finish/tue-street/s01"])[UPSCALE_NODE]["inputs"]["upscale"] == 2  # 1440×1920
    assert _graph(world, records["finish/tue-street/s02"])[UPSCALE_NODE]["inputs"]["upscale"] == 3  # 864×1152


def test_an_ollama_that_refuses_the_unload_is_a_warning(world: World, closed_port: int) -> None:
    world.write(single())
    ollama = OllamaClient(f"http://127.0.0.1:{closed_port}", timing=SHORT_TIMING)
    result = world.run(ollama=ollama)
    assert result.code == Exit.OK
    assert f"warning: could not unload the Ollama models: Ollama at http://127.0.0.1:{closed_port}" in result.err


# ---- the summary and the exit statuses (Req 10.8, 10.14, 9.11, 11.10, 21.3) ----


def test_the_summary_names_every_waiting_invalid_and_refused_post(world: World) -> None:
    bad_look = (LoraRef("looks/not-installed.safetensors", 0.5),)
    world.write(
        single("mon-cafe"), single("tue-plan"), single("wed-review"), single("thu-pick", candidates=2),
        single("fri-look", looks=bad_look), carousel("sat-wait", candidates=2),
        entries={"tue-plan": None, "wed-review": (False, {}), "thu-pick": (True, {1: 5})},
    )  # fmt: skip
    result = world.run()
    assert result.code == Exit.FAILED  # an invalid Pick and a refused Instagram Post, though no Job failed
    out = result.out
    assert "waiting for planning: Post 'tue-plan'" in out
    assert "waiting for review: Post 'wed-review' waits for review" in out
    assert "invalid Pick: Post 'thu-pick', Slide 1:" in out
    assert "refused: Post 'fri-look':" in out and "looks/not-installed.safetensors" in out
    assert "waiting: Post sat-wait, Slide 2, Candidate 1 (render/sat-wait/s02/c1): needs a Pick for Slide 1" in out
    assert "waiting for a Pick: Post 'sat-wait', Slide 1: Candidate 1: " in out
    posted = {record.post for record in world.records().values() if record.status == DONE}
    assert posted == {"mon-cafe", "thu-pick", "sat-wait"}  # fri-look was refused, the others gated


# ---- the render and finish commands (design §C6) ----


@pytest.fixture
def cli(world: World, monkeypatch):
    """The command line, with the stage's allowlist in CONFIG_DIR/models.list and the short Timing."""
    from influencer import __main__ as cli

    (world.env.config_dir / "models.list").write_text(
        "lora|film-grain.safetensors|hf|owner/looks::film-grain.safetensors|loras/looks|wf:influencer\n"
    )
    monkeypatch.setattr(E, "DEFAULT_TIMING", SHORT_TIMING)
    return cli


def test_render_dry_run_only_writes_the_named_posts_prompt_files(world: World, cli, capsys) -> None:
    world.write(single(), single("wed-street"))
    assert cli.main(["render", "--week", WEEK, "--dry-run", "--only", "wed-street"]) == Exit.OK, capsys.readouterr()
    assert sorted(path.name for path in _dry_folder(world).iterdir()) == ["render_wed-street_s01_c1.json"]
    assert world.posts() == [] and not world.manifest_path.exists()


def test_render_then_finish_run_the_stages_on_comfyui(world: World, cli, capsys) -> None:
    world.write(single())
    world.comfy.default_plan = Plan(size=STILL_34)
    assert cli.main(["render", "--week", WEEK]) == Exit.OK, capsys.readouterr()
    assert cli.main(["finish", "--week", WEEK, "--dry-run"]) == Exit.OK, capsys.readouterr()
    assert len(world.posts()) == 1  # the finish dry run submitted nothing
    assert cli.main(["finish", "--week", WEEK]) == Exit.OK, capsys.readouterr()
    assert {world.records()[ident].status for ident in ("render/mon-cafe/s01/c1", "finish/mon-cafe/s01")} == {DONE}


def test_finish_without_a_calendar_exits_1_and_render_under_a_held_lock_exits_4(world: World, cli, capsys) -> None:
    world.write(single())
    assert cli.main(["finish", "--week", "2026-W41"]) == Exit.USAGE
    assert "2026-W41" in capsys.readouterr().err
    with ProcessLock(world.folder / ".lock"):
        assert cli.main(["render", "--week", WEEK]) == Exit.LOCKED
    assert f"PID {os.getpid()}" in capsys.readouterr().err
    assert world.posts() == [] and not world.manifest_path.exists()
