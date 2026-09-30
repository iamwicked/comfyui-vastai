"""Hypothesis strategies for the plan stage (task 21.6; design §A14, §M3, Properties 15 and 16).

- prompt_sheets(): (calendar, sheet), a calendar from calendars(persona) and a valid Prompt_Sheet over it
  with a random mix of missing entries, entries whose Slide count differs from the calendar's, `approved`
  true and false, `needs_manual_prompt`, recorded Picks, `review` lists, `raw_response` text from texts(),
  and entries whose Post left the calendar
- plan_posts(): calendar Posts of every type and platform, for the validator
- plan_responses(post): JSON values answering for post: valid ones for its Slide count, and the near-valid
  catalogue of NEAR_VALID (N±1 slides, an empty scene, alt, caption or motion, a non-string hashtag, a
  missing key, a non-object top level, a Safety_Guard term injected into one string);
  labelled_responses(post) gives the same values with the fault's name, None for a valid one
- outcomes(post_id, slides, reel): a planned result for one Post, a success or a failure

Task 21.6 names tests/strategies.py; these live in a module of their own, as every earlier task's
strategies do, so tests/strategies.py stays untouched.
"""
from __future__ import annotations

import sys
from typing import Any

from hypothesis import strategies as st

from helpers.harness import REPO
from strategies import texts
from strategies_calendar import POST_IDS, calendars
from strategies_jobs import BASE_PERSONA
from strategies_persona import CARRIERS, GUARD, is_safe

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer.calendar_io import Calendar, Post  # noqa: E402
from influencer.planner import Outcome, Plan  # noqa: E402
from influencer.prompt_sheet import Entry, Planned, PromptSheet, Slide  # noqa: E402

TERM_CARRIERS = CARRIERS["text"]  # "with <term> here", each rejected by the Safety_Guard
BLANKS = ("", " ", "\n", "\t ")

# ---- prompt_sheets() ----

_PLANNED = st.builds(Planned, model=texts(min_size=1, max_size=20), at=texts(max_size=20), attempts=st.integers(1, 4))


@st.composite
def _entry(draw: st.DrawFn, post: str, slides: int, candidates: int) -> Entry:
    picks = st.none() | st.integers(1, candidates)
    return Entry(
        post,
        approved=draw(st.booleans()),
        needs_manual_prompt=draw(st.booleans()),
        review=draw(st.lists(texts(max_size=20), max_size=3)),
        planned=draw(st.none() | _PLANNED),
        slides=[Slide(draw(texts(max_size=30)), draw(texts(max_size=30)), draw(picks)) for _ in range(slides)],
        caption=draw(st.none() | texts(max_size=60)),
        hashtags=draw(st.lists(texts(max_size=15), max_size=4)),
        motion=draw(st.none() | texts(max_size=30)),
        raw_response=draw(st.none() | texts(max_size=60)),
    )


@st.composite
def prompt_sheets(draw: st.DrawFn, persona: Any = BASE_PERSONA, max_posts: int = 5) -> tuple[Calendar, PromptSheet]:
    """(calendar, sheet): a calendar of calendars(persona) and a Prompt_Sheet that the Printer writes, over
    it. Each Post has no entry, an entry of its Slide count, or one of another count (waiting for planning);
    0-2 entries name Posts that are not in the calendar; entries come in any order."""
    calendar = draw(calendars(persona, max_posts=max_posts))
    entries = []
    for post in calendar.posts:
        state = draw(st.sampled_from(("missing", "entry", "entry", "other-count")))
        if state == "missing":
            continue
        count = post.slides
        if state == "other-count":
            count = draw(st.integers(0, post.slides + 2).filter(lambda n, slides=post.slides: n != slides))
        entries.append(draw(_entry(post.id, count, post.candidates)))
    taken = {post.id for post in calendar.posts}
    for ident in draw(st.lists(POST_IDS.filter(lambda i: i not in taken), max_size=2, unique=True)):
        entries.append(draw(_entry(ident, draw(st.integers(0, 3)), 4)))
    return calendar, PromptSheet(calendar.week, draw(st.permutations(entries)))


# ---- plan_posts() ----


@st.composite
def plan_posts(draw: st.DrawFn) -> Post:
    """A Post of any type and platform; carousels of 2-6 Slides keep the answers small."""
    kind = draw(st.sampled_from(("single", "story", "carousel", "reel")))
    platform = draw(st.sampled_from(("instagram", "other")))
    fields: dict[str, Any] = {"location": "cafe", "outfit": "cream"}
    if kind == "carousel":
        fields["slides"] = draw(st.integers(2, 6))
    if kind == "reel":
        fields = {"mode": "external", "duration": 8}
    return Post(draw(st.sampled_from(("mon-cafe", "tue-street", "0", "a--b"))), kind, "2026-09-28", platform,
                **fields)  # fmt: skip


# ---- plan_responses(post) ----

_FILLED = texts(max_size=40).filter(lambda text: text.strip() != "").filter(is_safe)
_TAGS = st.lists(texts(max_size=15).filter(is_safe), max_size=5)
_NOT_TEXT = st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), st.just([]), st.just({}))


def _reel(post: Post) -> bool:
    return post.type == "reel"


@st.composite
def _slide(draw: st.DrawFn) -> dict[str, str]:
    return {"scene": draw(_FILLED), "alt": draw(_FILLED)}


@st.composite
def valid_responses(draw: st.DrawFn, post: Post) -> dict[str, Any]:
    """An answer the validator accepts for post: its Slide count of filled scene and alt texts, a filled
    caption, 0-5 hashtags and, for a reel, a filled motion; every text passes the Safety_Guard."""
    value: dict[str, Any] = {
        "slides": [draw(_slide()) for _ in range(post.slides)],
        "caption": draw(_FILLED),
        "hashtags": draw(_TAGS),
    }
    if _reel(post):
        value["motion"] = draw(_FILLED)
    elif draw(st.booleans()):
        value["motion"] = draw(st.none() | _FILLED)  # a still Post's motion is ignored
    return value


def _faults(post: Post) -> list[str]:
    """The near-valid faults that apply to post."""
    found = ["slides+1", "empty-caption", "non-string-hashtag", "missing-key", "not-object", "term"]
    if post.slides >= 1:
        found += ["slides-1", "empty-scene", "empty-alt"]
    if _reel(post):
        found.append("empty-motion")
    return found


NEAR_VALID = ("slides+1", "slides-1", "empty-scene", "empty-alt", "empty-caption", "empty-motion",
              "non-string-hashtag", "missing-key", "not-object", "term")  # fmt: skip


@st.composite
def _broken(draw: st.DrawFn, post: Post, fault: str) -> Any:
    value = draw(valid_responses(post))
    slides = value["slides"]
    if fault == "slides+1":
        slides.insert(draw(st.integers(0, len(slides))), draw(_slide()))
    elif fault == "slides-1":
        slides.pop(draw(st.integers(0, len(slides) - 1)))
    elif fault in ("empty-scene", "empty-alt"):
        slides[draw(st.integers(0, len(slides) - 1))][fault.split("-")[1]] = draw(st.sampled_from(BLANKS))
    elif fault in ("empty-caption", "empty-motion"):
        value[fault.split("-")[1]] = draw(st.sampled_from(BLANKS))
    elif fault == "non-string-hashtag":
        tags = value["hashtags"]
        tags.insert(draw(st.integers(0, len(tags))), draw(_NOT_TEXT))
    elif fault == "missing-key":
        keys = ["slides", "caption", "hashtags", *(["motion"] if _reel(post) else [])]
        places = [(None, key) for key in keys]
        places += [(index, key) for index in range(len(slides)) for key in ("scene", "alt")]
        index, key = draw(st.sampled_from(places))
        del (value if index is None else slides[index])[key]
    elif fault == "not-object":
        return draw(st.sampled_from(([value], value["caption"], len(slides), None, True)))
    elif fault == "term":
        carrier = draw(st.sampled_from(TERM_CARRIERS))
        places = ["caption", "hashtag", *(["motion"] if _reel(post) else []), *(["scene", "alt"] if slides else [])]
        place = draw(st.sampled_from(places))
        if place in ("scene", "alt"):
            slide = slides[draw(st.integers(0, len(slides) - 1))]
            slide[place] = f"{slide[place]} {carrier}"
        elif place == "hashtag":
            value["hashtags"].insert(draw(st.integers(0, len(value["hashtags"]))), carrier)
        else:
            value[place] = f"{value[place]} {carrier}"
    else:
        raise ValueError(f"unknown fault {fault!r}")
    return value


@st.composite
def labelled_responses(draw: st.DrawFn, post: Post) -> tuple[Any, str | None]:
    """(value, fault): a valid answer for post with fault None, or a near-valid one and its fault's name."""
    if draw(st.booleans()):
        return draw(valid_responses(post)), None
    fault = draw(st.sampled_from(_faults(post)))
    return draw(_broken(post, fault)), fault


def plan_responses(post: Post) -> st.SearchStrategy[Any]:
    """Valid answers for post and the near-valid catalogue, as JSON values."""
    return labelled_responses(post).map(lambda pair: pair[0])


# ---- outcomes() ----


@st.composite
def outcomes(draw: st.DrawFn, post: str, slides: int, reel: bool) -> Outcome:
    """A planned result for Post post: a success with slides (scene, alt) pairs, or a failure after the
    last attempt with or without a kept raw_response."""
    model, at = draw(st.sampled_from(("qwen3:8b", "dolphin3:8b"))), "2026-09-28T09:12:31Z"
    if draw(st.booleans()):
        plan = Plan(
            slides=tuple((draw(texts(max_size=20)), draw(texts(max_size=20))) for _ in range(slides)),
            caption=draw(texts(max_size=40)),
            hashtags=tuple(draw(st.lists(texts(max_size=10), max_size=3))),
            motion=draw(texts(max_size=20)) if reel else None,
            review=tuple(draw(st.lists(texts(max_size=10), max_size=2))),
        )
        return Outcome(post, model, at, draw(st.integers(1, 4)), plan=plan)
    return Outcome(post, model, at, 4, raw_response=draw(st.none() | texts(max_size=60)))


assert TERM_CARRIERS and all(GUARD.check_text(carrier) for carrier in TERM_CARRIERS)
