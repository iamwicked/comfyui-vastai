"""The plan stage (task 21; design §A14, §M3; Req 6.1-6.5, 6.11, 6.13-6.17, 21.3, 21.4, 21.6).

- Property 15: the validator accepts a JSON value exactly when the shape rules and the Safety_Guard pass
- Property 16: the merge never changes an entry it was not asked to write
- the stage's branches against the fake Ollama with the shortened Timing: four failed attempts, the
  Safety_Guard and raw_response, a missing model, an unreachable Ollama, the 600 s budget, a stop mid-run,
  the model per platform, a refused Instagram Post, --replan over a full sheet, the lock, and no ComfyUI
"""
from __future__ import annotations

import dataclasses
import io
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fakes.ollama import Answer
from helpers.harness import REPO, SHORT_TIMING, TempEnv, run_bounded

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import planner as PL  # noqa: E402
from influencer.allowlist import Allowlist, character_names  # noqa: E402
from influencer.calendar_io import Calendar, Post, print_calendar_yaml  # noqa: E402
from influencer.errors import Exit  # noqa: E402
from influencer.locks import ProcessLock  # noqa: E402
from influencer.ollama import OllamaClient  # noqa: E402
from influencer.persona import Captions, LoraRef, print_persona  # noqa: E402
from influencer.prompt_sheet import Entry, Planned, PromptSheet, Slide, print_prompt_sheet, read_prompt_sheet  # noqa: E402
from strategies import texts  # noqa: E402
from strategies_jobs import BASE_PERSONA  # noqa: E402
from strategies_persona import GUARD  # noqa: E402
from strategies_planning import TERM_CARRIERS, labelled_responses, outcomes, plan_posts, plan_responses, prompt_sheets  # noqa: E402

WEEK = "2026-W40"
DISCLOSURE = "AI-generated character, not a real person."
PERSONA = dataclasses.replace(
    BASE_PERSONA, slug="test-persona", captions=Captions(disclosure=DISCLOSURE, flagged_terms=("cure",))
)
LOOK = "looks/film-grain.safetensors"
ALLOW = Allowlist(frozenset({LOOK}), character_names(PERSONA))
QWEN, DOLPHIN = "qwen3:8b", "dolphin3:8b"
TERM = TERM_CARRIERS[0]


def single(ident: str = "mon-cafe", **fields: object) -> Post:
    return Post(ident, "single", "2026-09-28", location="cafe", outfit="cream", **fields)


def carousel(ident: str = "tue-street", **fields: object) -> Post:
    fields.setdefault("slides", 2)
    return Post(ident, "carousel", "2026-09-29", location="street", outfit="coat", **fields)


def reel(ident: str = "wed-reel", **fields: object) -> Post:
    return Post(ident, "reel", "2026-09-30", mode="external", duration=8, **fields)


def response(slides: int, *, caption: str = "Tuesdays are for the long way round.", hashtags=("#slowliving",),
             motion: str | None = None, scene: str = "she stops at the bakery window") -> dict:  # fmt: skip
    value: dict[str, Any] = {
        "slides": [{"scene": f"{scene} {n}", "alt": f"A woman at a window, view {n}."} for n in range(1, slides + 1)],
        "caption": caption,
        "hashtags": list(hashtags),
    }
    if motion is not None:
        value["motion"] = motion
    return value


def answer(value: object) -> Answer:
    return Answer(content=json.dumps(value))


@dataclass
class Result:
    code: int
    out: str
    err: str


class World:
    """A temp Influencer_Root with the persona and a week's calendar and Prompt_Sheet, and the fake Ollama."""

    def __init__(self, env: TempEnv, ollama) -> None:
        self.env, self.ollama = env, ollama
        self.root = env.influencer_root
        self.folder = self.root / "weeks" / WEEK
        self.sheet_path = self.folder / "prompts.yaml"
        ollama.add_model(QWEN, ("completion", "thinking"))
        ollama.add_model(DOLPHIN, ("completion",))

    def write(self, *posts: Post, sheet: PromptSheet | None = None) -> None:
        (self.root / "refs").mkdir(parents=True, exist_ok=True)
        (self.root / "refs" / "hero.png").write_bytes(b"hero")
        (self.root / "grade").mkdir(exist_ok=True)
        (self.root / "grade" / "film.cube").write_text("cube")
        (self.root / "persona.yaml").write_text(print_persona(PERSONA))
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / "calendar.yaml").write_text(print_calendar_yaml(Calendar(WEEK, posts)))
        if sheet is not None:
            self.sheet_path.write_text(print_prompt_sheet(sheet))

    def run(self, *, replan=(), ollama: OllamaClient | None = None) -> Result:
        out, err = io.StringIO(), io.StringIO()
        client = ollama or OllamaClient.from_env(timing=SHORT_TIMING)
        code = PL.run_plan(WEEK, replan=replan, ollama=client, timing=SHORT_TIMING, allowlist=ALLOW, out=out,
                           err=err)  # fmt: skip
        return Result(code, out.getvalue(), err.getvalue())

    def sheet(self) -> PromptSheet:
        return read_prompt_sheet(self.sheet_path)

    def chats(self) -> list[dict]:
        return [request.json() for request in self.ollama.calls("POST", "/api/chat")]


@pytest.fixture
def world(temp_env, ollama_fake) -> World:
    return World(temp_env, ollama_fake)


def old_entry(post: str, count: int, **fields: object) -> Entry:
    values: dict[str, Any] = dict(
        approved=True, review=("cure",), planned=Planned(QWEN, "2026-09-20T08:00:00Z", 1),
        slides=tuple(Slide(f"old scene {n}", f"old alt {n}", 1) for n in range(1, count + 1)),
        caption="old caption", hashtags=("#old",),
    )  # fmt: skip
    values.update(fields)
    return Entry(post, **values)


# ---- Property 15 ----

JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | texts(max_size=20),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(texts(max_size=10), inner, max_size=4),
    max_leaves=12,
)
SHAPED = st.fixed_dictionaries(
    {"slides": st.lists(st.dictionaries(st.sampled_from(("scene", "alt")), JSON), max_size=4),
     "caption": JSON, "hashtags": JSON},
    optional={"motion": JSON},
)  # fmt: skip


def all_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, item in value.items() for text in (key, *all_strings(item))]
    if isinstance(value, list):
        return [text for item in value for text in all_strings(item)]
    return []


def filled(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def accepted(value: object, slides: int, is_reel: bool) -> bool:
    """Property 15's statement, written out."""
    if not isinstance(value, dict) or not isinstance(value.get("slides"), list) or len(value["slides"]) != slides:
        return False
    if not all(isinstance(item, dict) and filled(item.get("scene")) and filled(item.get("alt")) for item in value["slides"]):
        return False
    tags = value.get("hashtags")
    if not filled(value.get("caption")) or not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return False
    if is_reel and not filled(value.get("motion")):
        return False
    return not any(GUARD.check_text(text) for text in all_strings(value))


# Feature: ai-influencer-pipeline, Property 15: Plan responses are validated exactly
@given(st.data())
def test_plan_responses_are_validated_exactly(data) -> None:
    """**Validates: Requirements 6.13, 6.5**"""
    post = data.draw(plan_posts())
    is_reel = post.type == "reel"
    value, fault = data.draw(st.one_of(labelled_responses(post), plan_responses(post).map(lambda v: (v, "?")),
                                       JSON.map(lambda v: (v, "?")), SHAPED.map(lambda v: (v, "?"))))  # fmt: skip
    rule = PL.check_response(value, post.slides, is_reel, GUARD)
    assert (rule is None) == accepted(value, post.slides, is_reel), (value, rule)
    if fault is None:
        assert rule is None
    elif fault != "?":
        assert rule is not None, fault


def test_validation_order_and_reasons() -> None:
    post = carousel(slides=2)
    good = json.dumps(response(2))
    ok = PL.validate_attempt(good, post, PERSONA, elapsed=0.1, budget=1.0, guard=GUARD)
    assert ok.plan is not None and ok.plan.caption.endswith("\n" + DISCLOSURE) and ok.clean
    late = PL.validate_attempt("{", post, PERSONA, elapsed=2.0, budget=1.0, guard=GUARD)
    assert late.rule.startswith(PL.BUDGET)
    assert PL.validate_attempt("{", post, PERSONA, elapsed=0, budget=1, guard=GUARD).rule.startswith(PL.PARSING)
    unsafe = json.dumps(response(2, caption=f"hello {TERM}"))
    verdict = PL.validate_attempt(unsafe, post, PERSONA, elapsed=0, budget=1, guard=GUARD)
    assert verdict.rule.startswith(PL.SAFETY) and not verdict.clean and TERM not in verdict.rule
    many = json.dumps(response(2, hashtags=[f"#t{n}" for n in range(6)]))
    assert PL.validate_attempt(many, post, PERSONA, elapsed=0, budget=1, guard=GUARD).rule.startswith(PL.LIMITS)
    other = dataclasses.replace(post, platform="other")
    assert PL.validate_attempt(many, other, PERSONA, elapsed=0, budget=1, guard=GUARD).plan is not None
    flagged = PL.validate_attempt(json.dumps(response(2, caption="A cure for Mondays.")), post, PERSONA, elapsed=0,
                                  budget=1, guard=GUARD)  # fmt: skip
    assert flagged.plan.review == ("cure",) and flagged.plan.caption.startswith("A cure for Mondays.")


# ---- Property 16 ----


# Feature: ai-influencer-pipeline, Property 16: Planning never changes entries it was not asked to write
@given(st.data())
def test_planning_never_changes_entries_it_was_not_asked_to_write(data) -> None:
    """**Validates: Requirements 6.11, 6.16, 6.17**"""
    calendar, sheet = data.draw(prompt_sheets())
    ids = [post.id for post in calendar.posts]
    replan = data.draw(st.sets(st.sampled_from(ids)))
    planned = [data.draw(outcomes(post.id, post.slides, post.type == "reel")) for post in calendar.posts
               if post.id in replan]  # fmt: skip
    merged = PL.merge(sheet, [PL.entry_for(sheet.entry(outcome.post), outcome) for outcome in planned])
    assert [entry.post for entry in merged.entries[: len(sheet.entries)]] == [entry.post for entry in sheet.entries]
    for entry in sheet.entries:
        if entry.post not in replan:
            assert merged.entry(entry.post) == entry
    for outcome in planned:
        written, before = merged.entry(outcome.post), sheet.entry(outcome.post)
        assert written.approved is False
        if outcome.plan is None:
            assert written.needs_manual_prompt and written.raw_response == outcome.raw_response
            if before is not None:
                restored = dataclasses.replace(written, approved=before.approved,
                                               needs_manual_prompt=before.needs_manual_prompt,
                                               raw_response=before.raw_response)  # fmt: skip
                assert restored == before
        else:
            assert [(s.scene, s.alt) for s in written.slides] == list(outcome.plan.slides)
            assert written.needs_manual_prompt is False and written.raw_response is None
    assert len(merged.entries) == len(sheet.entries) + len({o.post for o in planned} - {e.post for e in sheet.entries})
    print_prompt_sheet(merged)  # still a valid Prompt_Sheet


# ---- the stage against the fake Ollama ----


def test_four_failed_attempts_mark_the_post_with_the_last_clean_answer_and_keep_its_values(world: World) -> None:
    before = old_entry("tue-street", 2)
    world.write(carousel(), sheet=PromptSheet(WEEK, (before,)))
    too_many_tags = response(2, hashtags=[f"#t{n}" for n in range(6)])
    three_slides = json.dumps(response(3))
    world.ollama.script(answer(too_many_tags), Answer(content=three_slides),
                        answer(response(2, caption=f"hi {TERM}")), Answer(content=f"not json {TERM}"))  # fmt: skip
    result = world.run(replan=["tue-street"])
    assert result.code == Exit.FAILED
    entry = world.sheet().entry("tue-street")
    assert entry == dataclasses.replace(before, approved=False, needs_manual_prompt=True, raw_response=three_slides)
    for attempt, rule in enumerate((PL.LIMITS, PL.SHAPE, PL.SAFETY, PL.PARSING), 1):
        assert f"attempt {attempt} of 4 for Post 'tue-street' failed: {rule}" in result.err
    assert TERM not in world.sheet_path.read_text() and TERM not in result.err + result.out
    assert len(world.chats()) == 4 and "needs a manual prompt: Post 'tue-street'" in result.out


def test_a_post_whose_every_answer_the_safety_guard_rejects_gets_no_raw_response(world: World) -> None:
    world.write(single())
    world.ollama.default_answer = answer(response(1, scene=TERM))
    result = world.run()
    assert result.code == Exit.FAILED
    entry = world.sheet().entry("mon-cafe")
    assert entry.needs_manual_prompt and entry.raw_response is None and entry.slides == () and not entry.approved
    assert entry.planned == Planned(QWEN, entry.planned.at, 4)
    assert TERM not in world.sheet_path.read_text()


def test_a_model_missing_from_api_tags_stops_before_any_chat_and_keeps_the_sheet(world: World) -> None:
    kept = old_entry("mon-cafe", 1)
    world.write(single(), single("thu-other", platform="other"), sheet=PromptSheet(WEEK, (kept,)))
    del world.ollama.models[DOLPHIN]
    text = world.sheet_path.read_bytes()
    result = world.run()
    assert result.code == Exit.ENVIRONMENT and DOLPHIN in result.err and "ollama pull dolphin3:8b" in result.err
    assert world.chats() == [] and world.sheet_path.read_bytes() == text


def test_an_ollama_on_a_closed_port_stops_naming_the_url(world: World, closed_port: int) -> None:
    world.write(single(), carousel(), sheet=PromptSheet(WEEK, (old_entry("mon-cafe", 1),)))
    text = world.sheet_path.read_bytes()
    url = f"http://127.0.0.1:{closed_port}"
    result = world.run(ollama=OllamaClient(url, timing=SHORT_TIMING))
    assert result.code == Exit.ENVIRONMENT and url in result.err
    assert world.sheet_path.read_bytes() == text


def test_an_answer_past_the_budget_fails_the_attempt_and_the_next_one_plans(world: World) -> None:
    world.write(single())
    world.ollama.script(Answer(content=json.dumps(response(1)), delay_s=SHORT_TIMING.plan_attempt_s + 0.5),
                        answer(response(1)))  # fmt: skip
    result = world.run()
    assert result.code == Exit.OK, result.err
    assert f"attempt 1 of 4 for Post 'mon-cafe' failed: {PL.BUDGET}" in result.err
    entry = world.sheet().entry("mon-cafe")
    assert entry.planned.attempts == 2 and not entry.needs_manual_prompt and not entry.approved


def test_a_stop_mid_run_keeps_every_entry_written_before_it(world: World) -> None:
    world.write(single(), single("thu-cafe"))

    def first(body: dict) -> Answer:
        with world.ollama._cond:
            del world.ollama.models[QWEN]  # the next chat for the model is Ollama's 404
        return answer(response(1))

    world.ollama.script(first)
    result = world.run()
    assert result.code == Exit.ENVIRONMENT and QWEN in result.err
    sheet = world.sheet()
    assert [entry.post for entry in sheet.entries] == ["mon-cafe"] and sheet.entry("mon-cafe").slides


def test_each_platform_goes_to_its_model_and_comfyui_gets_no_socket(world: World, comfy_fake, monkeypatch) -> None:
    ports: list[object] = []
    real = socket.socket.connect

    def recording(sock: socket.socket, address: object) -> None:
        ports.append(address[1] if isinstance(address, tuple) else address)
        return real(sock, address)

    monkeypatch.setattr(socket.socket, "connect", recording)
    world.write(carousel(slides=3), reel(platform="other"))
    world.ollama.script(answer(response(3)), answer(response(0, motion="a slow push-in")))
    result = world.run()
    assert result.code == Exit.OK, result.err
    first, second = world.chats()
    assert first["model"] == QWEN and first["think"] is False
    assert second["model"] == DOLPHIN and "think" not in second
    for body in (first, second):
        assert body["stream"] is False and body["keep_alive"] == "10m"
        assert body["options"]["temperature"] == 0.8 and isinstance(body["options"]["seed"], int)
        assert body["messages"][0]["role"] == "system" and PERSONA.name in body["messages"][0]["content"]
    assert first["format"]["properties"]["slides"]["minItems"] == 3 == first["format"]["properties"]["slides"]["maxItems"]
    assert "motion" in second["format"]["required"] and "motion" not in first["format"]["required"]
    assert "cobbled street" in first["messages"][0]["content"] and "8 seconds" in second["messages"][0]["content"]
    sheet = world.sheet()
    assert sheet.entry("wed-reel").motion == "a slow push-in" and sheet.entry("tue-street").caption.endswith(DISCLOSURE)
    assert comfy_fake.requests == [] and comfy_fake.port not in ports and ports


def test_an_instagram_post_with_a_look_outside_the_allowlist_gets_no_request_and_exits_3(world: World) -> None:
    bad = (LoraRef("looks/not-installed.safetensors", 0.5),)
    world.write(single(looks=bad), single("thu-cafe"), single("fri-other", platform="other", looks=bad))
    world.ollama.default_answer = answer(response(1))
    result = world.run()
    assert result.code == Exit.FAILED
    assert "refused: Post 'mon-cafe'" in result.out and "looks/not-installed.safetensors" in result.out
    assert [body["model"] for body in world.chats()] == [QWEN, DOLPHIN]
    assert {entry.post for entry in world.sheet().entries} == {"thu-cafe", "fri-other"}


def test_a_replan_of_one_post_leaves_every_other_entry_byte_identical(world: World) -> None:
    posts = (single(), carousel(), reel(), single("thu-other", platform="other"))
    entries = (
        old_entry("mon-cafe", 1), old_entry("tue-street", 2, needs_manual_prompt=True, raw_response="{}"),
        old_entry("wed-reel", 0, motion="old motion", approved=False), old_entry("gone", 1),
        old_entry("thu-other", 1, slides=(Slide("x", "y", None),)),
    )  # fmt: skip
    world.write(*posts, sheet=PromptSheet(WEEK, entries))
    world.ollama.script(answer(response(2)))
    result = world.run(replan=["tue-street"])
    assert result.code == Exit.OK, result.err
    after = world.sheet()
    for entry in entries:
        if entry.post != "tue-street":
            printed = print_prompt_sheet(PromptSheet(WEEK, (entry,)))
            assert print_prompt_sheet(PromptSheet(WEEK, (after.entry(entry.post),))) == printed
    assert [entry.post for entry in after.entries] == [entry.post for entry in entries]
    replanned = after.entry("tue-street")
    assert not replanned.approved and not replanned.needs_manual_prompt and replanned.raw_response is None
    assert "matches no Post of the calendar; it is kept" in result.out and len(world.chats()) == 1


def test_only_posts_without_an_entry_or_with_another_slide_count_are_selected(world: World) -> None:
    world.write(single(), carousel(slides=3), single("thu-cafe"),
                sheet=PromptSheet(WEEK, (old_entry("mon-cafe", 1), old_entry("tue-street", 2))))  # fmt: skip
    world.ollama.script(answer(response(3)), answer(response(1)))
    assert world.run().code == Exit.OK
    assert [body["messages"][1]["content"].split()[4] for body in world.chats()] == ["tue-street", "thu-cafe"]
    assert world.sheet().entry("mon-cafe") == old_entry("mon-cafe", 1)


def test_an_unknown_replan_id_and_a_held_lock_are_refused(world: World) -> None:
    world.write(single())
    assert world.run(replan=["nope"]).code == Exit.USAGE
    with ProcessLock(world.folder / ".lock"):
        held = world.run()
    assert held.code == Exit.LOCKED and f"PID {os.getpid()}" in held.err
    assert world.chats() == [] and not world.sheet_path.exists()


def test_the_planner_imports_no_comfyui_module() -> None:
    code = (
        "import sys; import influencer.planner, influencer.__main__; "
        "print(','.join(m for m in ('influencer.comfy', 'influencer.engine', 'influencer.jobs', "
        "'influencer.preflight', 'influencer.templates') if m in sys.modules))"
    )
    env = {**os.environ, "PYTHONPATH": str(SCRIPTS)}
    run = run_bounded([sys.executable, "-c", code], timeout=60, env=env)
    assert run.returncode == 0, run.stderr.decode()
    assert run.stdout.decode().strip() == ""


def test_the_plan_command_runs_the_stage(world: World, monkeypatch) -> None:
    from influencer import __main__ as cli

    world.write(single())
    (world.env.config_dir / "models.list").write_text(
        "lora|film-grain.safetensors|hf|owner/looks::film-grain.safetensors|loras/looks|wf:influencer\n"
    )
    world.ollama.script(answer(response(1)))
    monkeypatch.setattr(PL, "DEFAULT_TIMING", SHORT_TIMING)
    assert cli.main(["plan", "--week", WEEK]) == Exit.OK
    assert world.sheet().entry("mon-cafe").caption.endswith(DISCLOSURE)
