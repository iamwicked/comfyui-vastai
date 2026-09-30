"""The plan stage: the week's Prompt_Sheet entries from the local Ollama_API (design §A14, §M3; Req 6.1-6.5,
6.11, 6.13-6.17, 21.3, 21.4, 21.6).

    run_plan(week, *, persona=None, home=None, replan=(), ...) -> exit status   the `plan` command
    select_posts(calendar, sheet, replan) -> [Post]       the Posts this run plans (Req 6.1, 6.11)
    model_for(persona, post) -> str                       the Ollama model of the Post's platform (Req 6.2, 21.4)
    system_prompt(persona, post), messages(persona, post), response_schema(post)   the chat request
    check_response(value, slides, reel, guard) -> rule | None   the shape and Safety_Guard rules (Property 15)
    validate_attempt(content, post, persona, ...) -> Attempt    every rule of one attempt, in order
    entry_for(existing, outcome) -> Entry, merge(sheet, entries) -> PromptSheet   the write (Property 16)

run_plan:
1. takes the week lock (weeks/<W>/.lock; exit 4 with the holder's PID);
2. reads the Persona_File and applies the Instagram_Allowlist check (exit 1 before any request), then the
   Content_Calendar with its multi-file check, then the Prompt_Sheet (none yet: an empty one);
3. selects the Posts with no entry, those whose entry's Slide count differs from the calendar's, and the ids
   named in --replan (an id the calendar lacks is exit 1);
4. refuses each Instagram Post whose own look list leaves the allowlist, or whose reel source is a Slide of
   an `other` Post: no request, reported with the LoRA or Slide, and exit 3 when the stage ends;
5. checks that Ollama answers /api/tags within Timing.request_s (10 s) and lists every model the remaining
   Posts need; otherwise exit 2 naming the URL or the missing model, before any chat;
6. plans each Post with up to MAX_ATTEMPTS chats; each failed attempt is reported with the Post id, the
   attempt number and the rule it broke (never with text of the response);
7. writes the Post's entry and rewrites the Prompt_Sheet through write_atomic after every Post, so a stop
   or a kill keeps exactly what was written. An Ollama that stops answering, or loses a model, mid-run is
   exit 2 with every entry written before kept.

An attempt is validated in this order: the Timing.plan_attempt_s budget (600 s) counted from its request,
JSON parsing, the shape rules (exactly N slide items, each with a non-empty `scene` and `alt`; a non-empty
`caption`; `hashtags` a list of strings; a non-empty `motion` for a reel), the Safety_Guard over every
string (mapping keys included), then the Instagram caption limits. Non-empty means holding a character other
than whitespace. On success the disclosure line is added, flagged terms go to `review` (the caption is left
as it is and the attempt still passes), and the entry is written with `approved: false`. After the 4th
failure the entry is marked `needs_manual_prompt: true` with `raw_response` the most recent response the
Safety_Guard did not reject (None when there is none) and every other value the entry had kept. A response
the Safety_Guard rejects is written nowhere, not even as `raw_response`.

Exit statuses (errors.Exit): 0 every selected Post planned; 3 a Post was refused or needs a manual prompt;
1 invalid input; 2 Ollama unreachable, a missing model, or another environment failure; 4 the lock is held;
130 Ctrl-C (every entry written before it is kept).

No ComfyUI module is imported and no request goes to COMFYUI_URL (Req 6.3): planning runs on a Mac with no
ComfyUI and no GPU. The Ollama client, the clocks and the Safety_Guard are passed in for the tests.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from influencer import env
from influencer.atomic import write_atomic
from influencer.errors import EnvironmentFailure, Exit, Problem, ValidationError, format_problems
from influencer.http import HttpError, TimedOut, Unreachable
from influencer.locks import LockHeld, ProcessLock
from influencer.ollama import OllamaClient
from influencer.timing import DEFAULT_TIMING, Timing

MAX_ATTEMPTS = 4  # per Post in one run (Req 6.4)
LOCK_NAME = ".lock"  # weeks/<W>/.lock, the lock every week stage takes
INTERRUPTED_EXIT = 130
SEED_PREFIX = "influencer-plan/1"
OTHER = "other"
REEL = "reel"

# The rules an attempt can break, in the order they are checked; each report starts with one.
BUDGET, PARSING, SHAPE, SAFETY, LIMITS, ANSWER = (
    "the answer budget", "JSON parsing", "the shape rules", "the Safety_Guard", "the Instagram caption limits",
    "the Ollama answer",
)  # fmt: skip


# ---- what a run plans ----


def select_posts(calendar: Any, sheet: Any, replan: Collection[str] = ()) -> list[Any]:
    """The calendar's Posts this run plans, in calendar order: those with no Prompt_Sheet entry, those whose
    entry's `slides` length differs from the Post's Slide count (waiting for planning, §M3), and the ids of
    replan (Req 6.1, 6.11)."""
    wanted = set(replan)
    chosen = []
    for post in calendar.posts:
        entry = sheet.entry(post.id) if sheet is not None else None
        if post.id in wanted or entry is None or len(entry.slides) != post.slides:
            chosen.append(post)
    return chosen


def model_for(persona: Any, post: Any) -> str:
    """The Persona_File's `other` model only for a Post whose platform is `other`, its `instagram` model for
    every other Post (Req 6.2, 21.4)."""
    return persona.ollama.other if post.platform == OTHER else persona.ollama.instagram


def refusals(posts: Iterable[Any], calendar: Any, allowlist: Any, path: str) -> dict[str, list[Problem]]:
    """Each Instagram Post of posts that Req 21.3 refuses, with its Problems: a look LoRA of its own list
    outside the allowlist, or a reel source Slide of an `other` Post. A Post without its own look list uses
    the Persona_File's stacks, which check_persona has already checked."""
    from influencer.allowlist import ReelSource, post_problems

    refused: dict[str, list[Problem]] = {}
    for post in posts:
        source = None
        if post.source_post is not None:
            origin = calendar.post(post.source_post)
            platform = origin.platform if origin is not None else None
            source = ReelSource(post.source_post, post.source_slide or 1, platform)
        problems = post_problems(post.id, post.looks or (), allowlist, platform=post.platform, reel_source=source,
                                 path=path)  # fmt: skip
        if problems:
            refused[post.id] = problems
    return refused


# ---- the request (task 21.2) ----


def plan_seed(week: str, post: str, attempt: int) -> int:
    """The chat seed of one attempt, 0 to 2**32 - 1: the same Post and attempt always ask the same way."""
    text = "|".join((SEED_PREFIX, week, post, str(attempt)))
    return int.from_bytes(hashlib.sha256(text.encode("utf-8", "surrogatepass")).digest()[:4], "big")


def _described(items: Iterable[Any], name: str | None) -> str | None:
    return next((item.description for item in items if item.name == name), None) if name is not None else None


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def system_prompt(persona: Any, post: Any) -> str:
    """The system prompt: the Persona_File's name, age, niche, voice, themes, caption rules and language, the
    Post's platform, and the Post's type, location and outfit descriptions, idea, Slide count and duration."""
    from influencer.calendar_io import decimal_text
    from influencer.persona import HASHTAG_CAP

    reel = post.type == REEL
    lines = [
        f"You plan social media posts for {persona.name}, an AI-generated character, {persona.age} years old.",
    ]
    if persona.niche:
        lines.append(f"Niche: {persona.niche}")
    lines.append(f"Voice: {persona.voice}")
    if persona.themes:
        lines.append("Themes: " + "; ".join(persona.themes))
    if persona.captions.rules:
        lines.append(f"Caption rules: {persona.captions.rules}")
    lines.append(f"Write every text in {persona.language}.")
    lines.append(f"Platform: {post.platform}.")
    shape = f"a {post.type} Post" + ("" if reel else f" of {_count(post.slides, 'Slide')}")
    if reel and post.duration is not None:
        shape += f", {decimal_text(post.duration)} seconds long"
    lines.append(f"This is {shape}.")
    location = _described(persona.locations, post.location)
    if location:
        lines.append(f"Location: {location}")
    outfit = _described(persona.wardrobe, post.outfit)
    if outfit:
        lines.append(f"Outfit: {outfit}")
    if post.idea:
        lines.append(f"Idea: {post.idea}")
    limit = min(persona.captions.hashtag_limit, HASHTAG_CAP)
    answer = (
        f"Answer with one JSON object: \"slides\", a list of exactly {post.slides} objects, one per Slide in order, "
        "each with \"scene\" (what the image shows, one or two sentences) and \"alt\" (the alt text); "
        f"\"caption\", the caption in the voice above; \"hashtags\", a list of at most {limit} hashtags, each "
        "starting with #"
    )
    if reel:
        answer += "; and \"motion\", one description of the camera and subject motion"
    lines.append(answer + ".")
    return "\n".join(lines)


def messages(persona: Any, post: Any) -> list[dict[str, str]]:
    """The chat messages of one Post: the system prompt, then the request itself."""
    return [
        {"role": "system", "content": system_prompt(persona, post)},
        {"role": "user", "content": f"Plan the {post.type} Post {post.id} for {post.date}. Answer with the JSON only."},
    ]


def response_schema(post: Any) -> dict[str, Any]:
    """The JSON schema sent as `format`: `slides` with exactly the Post's Slide count of {scene, alt},
    `caption`, `hashtags` and, for a reel, `motion`."""
    text = {"type": "string", "minLength": 1}
    slide = {"type": "object", "properties": {"scene": text, "alt": text}, "required": ["scene", "alt"]}
    properties: dict[str, Any] = {
        "slides": {"type": "array", "items": slide, "minItems": post.slides, "maxItems": post.slides},
        "caption": text,
        "hashtags": {"type": "array", "items": {"type": "string"}},
    }
    required = ["slides", "caption", "hashtags"]
    if post.type == REEL:
        properties["motion"] = text
        required.append("motion")
    return {"type": "object", "properties": properties, "required": required}


# ---- validation (task 21.3) ----


def _filled(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def shape_problem(value: object, slides: int, reel: bool) -> str | None:
    """Why value breaks the shape rules of Req 6.13, or None. The reason never quotes the response."""
    if not isinstance(value, dict):
        return "the answer is not a JSON object"
    items = value.get("slides")
    if not isinstance(items, list):
        return "`slides` is missing or not a list"
    if len(items) != slides:
        return f"`slides` has {_count(len(items), 'item')}; the Post has {_count(slides, 'Slide')}"
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict):
            return f"slides[{number}] is not an object"
        for key in ("scene", "alt"):
            if not _filled(item.get(key)):
                return f"slides[{number}].{key} is missing, empty or not text"
    if not _filled(value.get("caption")):
        return "`caption` is missing, empty or not text"
    hashtags = value.get("hashtags")
    if not isinstance(hashtags, list):
        return "`hashtags` is missing or not a list"
    if not all(isinstance(tag, str) for tag in hashtags):
        return "`hashtags` holds an item that is not text"
    if reel and not _filled(value.get("motion")):
        return "`motion` is missing, empty or not text; a reel Post needs one"
    return None


def _guard(guard: Any) -> Any:
    if guard is not None:
        return guard
    from influencer.safety import default_guard

    return default_guard()


def safety_problem(value: object, guard: Any = None) -> str | None:
    """Why the Safety_Guard rejects value (any of its strings, mapping keys included), or None. The reason
    counts the strings and never names what matched, so no part of the response is written anywhere."""
    hits = _guard(guard).check_tree(value)
    if not hits:
        return None
    rejected = len({where for where, _ in hits})
    return f"the Safety_Guard rejected {_count(rejected, 'string')} of the answer"


def check_response(value: object, slides: int, reel: bool, guard: Any = None) -> str | None:
    """The rule a JSON value breaks, or None when it is accepted (Property 15): the shape rules for a Post of
    `slides` Slides (a reel when reel is True), then the Safety_Guard over every string."""
    problem = shape_problem(value, slides, reel)
    if problem is not None:
        return f"{SHAPE}: {problem}"
    problem = safety_problem(value, guard)
    return None if problem is None else f"{SAFETY}: {problem}"


def _surrogates(value: object) -> bool:
    """Whether a parsed JSON value holds a lone surrogate (a broken \\ud800 escape), which no file can hold."""
    from influencer.safety import strings

    for _, text in strings(value):
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            return True
    return False


@dataclass(frozen=True)
class Plan:
    """An accepted answer, as the entry stores it."""

    slides: tuple[tuple[str, str], ...]  # (scene, alt) per Slide
    caption: str  # ending in the disclosure line when the Persona_File defines one
    hashtags: tuple[str, ...]
    motion: str | None
    review: tuple[str, ...] = ()  # flagged terms found (Req 6.10)


@dataclass(frozen=True)
class Attempt:
    """One attempt's verdict: plan when it passed, else rule, the rule it broke. clean: the Safety_Guard did
    not reject the response, so it may be kept as raw_response."""

    plan: Plan | None
    rule: str | None
    clean: bool


def _clean(content: str, value: object, guard: Any) -> bool:
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if _guard(guard).check_text(content):
        return False
    return value is None or not _guard(guard).check_tree(value)


def validate_attempt(content: str, post: Any, persona: Any, *, elapsed: float, budget: float,
                     guard: Any = None) -> Attempt:  # fmt: skip
    """Every rule of one attempt, in the order of the module docstring. content is the chat's message
    content, elapsed the seconds from the request to the full answer."""
    from influencer.captions import check_caption

    value: object = None
    try:
        value = json.loads(content)
        parsed = True
    except (ValueError, RecursionError):
        parsed = False
    clean = _clean(content, value if parsed else None, guard)

    def failed(rule: str) -> Attempt:
        return Attempt(None, rule, clean)

    if elapsed > budget:
        return failed(f"{BUDGET}: the answer took {elapsed:.1f} s; an attempt has {budget:g} s")
    if not parsed:
        return failed(f"{PARSING}: the answer is not JSON")
    if _surrogates(value):
        return failed(f"{PARSING}: the answer holds a broken \\u escape (a lone surrogate)")
    rule = check_response(value, post.slides, post.type == REEL, guard)
    if rule is not None:
        return failed(rule)
    assert isinstance(value, dict)
    hashtags = tuple(value["hashtags"])
    checked = check_caption(value["caption"], hashtags, persona.captions, instagram=post.platform != OTHER)
    if checked.problems:
        return failed(f"{LIMITS}: " + "; ".join(checked.problems))
    slides = tuple((item["scene"], item["alt"]) for item in value["slides"])
    motion = value["motion"] if post.type == REEL else None
    return Attempt(Plan(slides, checked.caption, hashtags, motion, checked.review), None, clean)


# ---- the entry and the merge (task 21.4) ----


@dataclass(frozen=True)
class Outcome:
    """How one Post's planning ended: plan when an attempt passed, else raw_response, the most recent response
    the Safety_Guard did not reject (None when there is none)."""

    post: str
    model: str
    at: str  # when, UTC, 2026-09-28T09:12:31Z
    attempts: int
    plan: Plan | None = None
    raw_response: str | None = None


def entry_for(existing: Any, outcome: Outcome) -> Any:
    """The entry the plan stage writes for outcome.post; existing is its entry before the run, or None.

    A passed attempt gives a new entry with `approved: false`, its Picks unset. After the last failed
    attempt the entry keeps every value it had, only `needs_manual_prompt` set, `approved` false and
    `raw_response` replaced (Req 6.14, 6.16); a Post with no entry before gets its `planned` record."""
    from influencer.prompt_sheet import Entry, Planned, Slide

    planned = Planned(outcome.model, outcome.at, outcome.attempts)
    plan = outcome.plan
    if plan is not None:
        return Entry(
            outcome.post, approved=False, needs_manual_prompt=False, review=plan.review, planned=planned,
            slides=tuple(Slide(scene, alt) for scene, alt in plan.slides), caption=plan.caption,
            hashtags=plan.hashtags, motion=plan.motion, raw_response=None,
        )  # fmt: skip
    base = existing if existing is not None else Entry(outcome.post, planned=planned)
    return dataclasses.replace(base, approved=False, needs_manual_prompt=True, raw_response=outcome.raw_response)


def merge(sheet: Any, entries: Iterable[Any]) -> Any:
    """sheet with entries written: each replaces its Post's entry in place, or is appended in the given
    order; every other entry, those of Posts that left the calendar included, stays as it was (Property 16)."""
    from influencer.prompt_sheet import PromptSheet

    new = {entry.post: entry for entry in entries}
    kept = []
    for entry in sheet.entries:
        kept.append(new.pop(entry.post, entry))
    return PromptSheet(sheet.week, (*kept, *new.values()))


# ---- the run (task 21.1) ----


def utc_stamp(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


@dataclass
class _Run:
    week: str
    persona: Any
    calendar: Any
    sheet: Any
    sheet_path: Path
    client: Callable[[], OllamaClient]
    timing: Timing
    clock: Callable[[], float]
    wall: Callable[[], float]
    guard: Any
    out: TextIO
    err: TextIO
    manual: list[str] = dataclasses.field(default_factory=list)
    planned: list[str] = dataclasses.field(default_factory=list)

    def plan(self, posts: Sequence[Any]) -> None:
        if not posts:
            return
        client = self.client()
        from influencer.prompt_sheet import print_prompt_sheet

        client.require(model_for(self.persona, post) for post in posts)  # Req 6.15, before the first chat
        for post in posts:
            outcome = self.plan_post(client, post)
            self.sheet = merge(self.sheet, [entry_for(self.sheet.entry(post.id), outcome)])
            write_atomic(self.sheet_path, print_prompt_sheet(self.sheet))
            if outcome.plan is not None:
                self.planned.append(post.id)
                print(f"planned: Post {post.id!r} with {outcome.model} (attempt {outcome.attempts})", file=self.out)
                if outcome.plan.review:
                    terms = ", ".join(outcome.plan.review)
                    print(f"flagged for review: Post {post.id!r}: {terms}", file=self.out)
            else:
                self.manual.append(post.id)
                kept = "the last clean answer is kept as raw_response" if outcome.raw_response is not None else \
                    "no answer passed the Safety_Guard, so none is kept"  # fmt: skip
                print(f"needs a manual prompt: Post {post.id!r} after {MAX_ATTEMPTS} failed attempts; {kept}",
                      file=self.out)  # fmt: skip

    def plan_post(self, client: OllamaClient, post: Any) -> Outcome:
        model = model_for(self.persona, post)
        chat = messages(self.persona, post)
        schema = response_schema(post)
        budget = self.timing.plan_attempt_s
        last_clean: str | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            started = self.clock()
            try:
                content = client.plan(model, chat, schema=schema, seed=plan_seed(self.week, post.id, attempt))
            except TimedOut:
                self.report(post, attempt, f"{BUDGET}: no full answer within {budget:g} s")
                continue
            except Unreachable:
                raise
            except HttpError as error:  # an error status or an unusable answer: a failed attempt
                self.report(post, attempt, f"{ANSWER}: {error}")
                continue
            verdict = validate_attempt(content, post, self.persona, elapsed=self.clock() - started, budget=budget,
                                       guard=self.guard)  # fmt: skip
            if verdict.clean:
                last_clean = content
            if verdict.plan is not None:
                return Outcome(post.id, model, utc_stamp(self.wall()), attempt, plan=verdict.plan)
            self.report(post, attempt, verdict.rule or "")
        return Outcome(post.id, model, utc_stamp(self.wall()), MAX_ATTEMPTS, raw_response=last_clean)

    def report(self, post: Any, attempt: int, rule: str) -> None:
        print(f"attempt {attempt} of {MAX_ATTEMPTS} for Post {post.id!r} failed: {rule}", file=self.err)


def _plan_week(week: str, paths: env.Paths, root: Path, folder: Path, *, replan: Sequence[str],
               client: Callable[[], OllamaClient], timing: Timing, clock: Callable[[], float],
               wall: Callable[[], float], allowlist: Any, guard: Any, out: TextIO, err: TextIO) -> int:  # fmt: skip
    from influencer import allowlist as A
    from influencer.calendar_io import find_calendar, read_calendar
    from influencer.persona import read_persona
    from influencer.prompt_sheet import FILE_NAME, PromptSheet, read_prompt_sheet

    persona = read_persona(root / env.PERSONA_FILE, root)
    allowed = allowlist if allowlist is not None else A.read_allowlist(paths.config_dir, persona)
    A.check_persona(persona, allowed)  # Req 21.6: exit 1 before any request
    calendar_path = find_calendar(root, week)
    result = read_calendar(calendar_path, persona, root)
    if result.calendar is None:
        raise ValidationError(result.all_problems())
    calendar = result.calendar
    for problem in result.all_problems() + result.warnings:
        print(f"warning: {problem}", file=err)
    sheet_path = folder / FILE_NAME
    sheet = read_prompt_sheet(sheet_path) if sheet_path.exists() else PromptSheet(week)
    unknown = [ident for ident in dict.fromkeys(replan) if calendar.post(ident) is None]
    if unknown:
        raise ValidationError(
            Problem("command line", "--replan", None, f"{ident!r} is not a Post of {calendar_path}") for ident in unknown
        )  # fmt: skip
    selected = select_posts(calendar, sheet, replan)
    refused = refusals(selected, calendar, allowed, str(calendar_path))
    run = _Run(week, persona, calendar, sheet, sheet_path, client, timing, clock, wall, guard, out, err)
    try:
        run.plan([post for post in selected if post.id not in refused])
    finally:
        _summary(run, selected, refused)
    return Exit.FAILED if refused or run.manual else Exit.OK


def _summary(run: _Run, selected: Sequence[Any], refused: Mapping[str, Sequence[Problem]]) -> None:
    for post, problems in refused.items():
        for problem in problems:
            print(f"refused: Post {post!r}: {problem}", file=run.out)
    ids = {post.id for post in run.calendar.posts}
    for entry in run.sheet.entries:
        if entry.post not in ids:
            print(f"note: the Prompt_Sheet entry of Post {entry.post!r} matches no Post of the calendar; it is kept",
                  file=run.out)  # fmt: skip
    print(f"plan {run.week}: {len(run.planned)} planned, {len(run.manual)} need a manual prompt, "
          f"{len(refused)} refused, of {len(selected)} selected", file=run.out)  # fmt: skip


def run_plan(week: str, *, persona: str | None = None, home: str | os.PathLike[str] | None = None,
             environ: Mapping[str, str] | None = None, replan: Sequence[str] = (), ollama: OllamaClient | None = None,
             timing: Timing | None = None, clock: Callable[[], float] = time.monotonic,
             wall: Callable[[], float] = time.time, allowlist: Any = None, guard: Any = None,
             out: TextIO | None = None, err: TextIO | None = None) -> int:  # fmt: skip
    """The plan stage for one week (see the module docstring); returns the exit status. persona is the slug
    (--persona), home the Influencer home (--home), replan the --replan ids; environ defaults to os.environ.
    ollama defaults to the client for OLLAMA_URL, built only when a Post needs a request; allowlist to the one
    read from CONFIG_DIR/models.list; guard to the committed Safety_Guard."""
    from influencer.calendar_io import check_week, find_calendar, week_folder

    out = out or sys.stdout
    err = err or sys.stderr
    timing = timing or DEFAULT_TIMING

    def client() -> OllamaClient:
        return ollama if ollama is not None else OllamaClient.from_env(environ, timing=timing)

    try:
        week = check_week(week)
        paths = env.paths(environ, home=home)
        root = paths.influencer_root(persona)
        folder = week_folder(root, week)
        find_calendar(root, week)  # a missing week folder or calendar is reported before the lock is taken
        with ProcessLock(folder / LOCK_NAME):
            return _plan_week(week, paths, root, folder, replan=replan, client=client, timing=timing, clock=clock,
                              wall=wall, allowlist=allowlist, guard=guard, out=out, err=err)  # fmt: skip
    except KeyboardInterrupt:
        print("plan: interrupted; every Prompt_Sheet entry written before is kept", file=err)
        return INTERRUPTED_EXIT
    except LockHeld as held:
        print(f"plan: {held}; the Prompt_Sheet was not written", file=err)
        return Exit.LOCKED
    except ValidationError as error:
        print(format_problems(error.problems), file=err)
        return Exit.USAGE
    except EnvironmentFailure as error:
        print(f"plan: {error}; every Prompt_Sheet entry written before the stop is kept", file=err)
        return Exit.ENVIRONMENT
