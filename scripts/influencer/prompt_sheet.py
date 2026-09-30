"""The Prompt_Sheet, weeks/<W>/prompts.yaml: its model, Reader and Printer (design §M3; Req 6.12, 11.7,
11.10).

The plan stage writes one entry per Post and the user edits it: scene and alt texts, caption, hashtags,
Picks and the Approval. read_prompt_sheet reads the file with yamlio.load_yaml and checks its shape,
reporting every Problem at once; print_prompt_sheet writes through yamlio.dump_yaml, so every text value
reads back character for character (multi-line text, `#`, `: `, `no`, `2026`, `~`, emoji). The Printer
refuses a sheet the Reader would reject.

Keys other than `post` may be left out of an entry, and read as their defaults: false, an empty list or
null (`pick` included). An unknown key is a Problem, so a misspelt `approved` never passes silently.

What the file alone cannot tell is checked against the Content_Calendar by PromptSheet.check:
- a calendar Post with no entry, or whose entry's `slides` length differs from the Post's Slide count,
  waits for planning, with a message telling the user to plan or re-plan it;
- an entry whose Post left the calendar is kept (the Printer writes it back) and reported;
- `pick` null means Candidate 1 for a Post with a Candidate count of 1 (Req 11.7) and waits for a Pick
  otherwise (Req 11.8); any value but a whole number from 1 to the Candidate count is a Problem naming the
  Post, the Slide, the recorded value and the valid numbers (Req 11.10). The Reader keeps such a value as
  recorded, so the stage holds only the Jobs that use that Slide's Pick and runs the others.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass

from influencer.errors import Problem, ValidationError, format_problems
from influencer.yamlio import dump_yaml, load_yaml

FORMAT = "influencer-prompt-sheet/1"
FILE_NAME = "prompts.yaml"

SHEET_KEYS = ("format", "week", "entries")
ENTRY_KEYS = (
    "post", "approved", "needs_manual_prompt", "review", "planned", "slides", "caption", "hashtags",
    "motion", "raw_response",
)
PLANNED_KEYS = ("model", "at", "attempts")
SLIDE_KEYS = ("scene", "alt", "pick")

_SHOWN_MAX = 60
_REQUIRED = object()  # the default of a key that must be present


# ---- the model ----


def _lists_as_tuples(obj: object, *names: str) -> None:
    for name in names:
        value = getattr(obj, name)
        if isinstance(value, list):
            object.__setattr__(obj, name, tuple(value))


@dataclass(frozen=True)
class Planned:
    """The plan stage's last run for an entry."""

    model: str  # the Ollama model asked, for example qwen3:8b
    at: str  # when, as the plan stage wrote it, for example 2026-09-28T09:12:31Z
    attempts: int  # requests that run made, from 1


@dataclass(frozen=True)
class Slide:
    """One Slide's texts and its Pick."""

    scene: str
    alt: str
    pick: object = None  # as recorded: a Candidate number, None, or a value PromptSheet.check reports


@dataclass(frozen=True)
class Entry:
    """One Post's entry; lists passed as lists are stored as tuples."""

    post: str
    approved: bool = False  # only the user sets true (Req 6.16, 6.17)
    needs_manual_prompt: bool = False  # set after the 4th failed attempt (Req 6.14)
    review: tuple[str, ...] = ()  # flagged terms found in the caption or hashtags (Req 6.10)
    planned: Planned | None = None
    slides: tuple[Slide, ...] = ()
    caption: str | None = None
    hashtags: tuple[str, ...] = ()
    motion: str | None = None  # reel Posts only
    raw_response: str | None = None  # the last raw response the Safety_Guard passed, with needs_manual_prompt

    def __post_init__(self) -> None:
        _lists_as_tuples(self, "review", "slides", "hashtags")


@dataclass(frozen=True)
class CalendarPost:
    """What an entry is checked against: a Content_Calendar Post's id, Slide count and Candidate count."""

    id: str
    slides: int
    candidates: int


@dataclass(frozen=True)
class CalendarCheck:
    """PromptSheet.check's findings, in calendar order where there is one."""

    planning: dict[str, str]  # calendar Post id -> why it waits for planning
    left_calendar: tuple[str, ...]  # Post ids of entries kept although their Post left the calendar
    picks: dict[tuple[str, int], int | None]  # (Post id, Slide number) -> its Pick; None: waits for a Pick
    problems: tuple[Problem, ...]  # recorded Picks that are not Candidate numbers; absent from picks


@dataclass(frozen=True)
class PromptSheet:
    """A week's Prompt_Sheet; entries passed as a list are stored as a tuple."""

    week: str
    entries: tuple[Entry, ...] = ()

    def __post_init__(self) -> None:
        _lists_as_tuples(self, "entries")

    def entry(self, post: str) -> Entry | None:
        """The entry of Post id post, or None."""
        return next((entry for entry in self.entries if entry.post == post), None)

    def check(self, posts: Iterable[CalendarPost], *, path: str) -> CalendarCheck:
        """The sheet read against the calendar's Posts (see the module docstring); path, the sheet's file,
        goes into the Problems."""
        calendar = {post.id: post for post in posts}
        numbers = {entry.post: number for number, entry in enumerate(self.entries, 1)}
        planning: dict[str, str] = {}
        picks: dict[tuple[str, int], int | None] = {}
        problems: list[Problem] = []
        for post in calendar.values():
            number = numbers.get(post.id)
            if number is None:
                planning[post.id] = f"has no Prompt_Sheet entry yet; plan it (plan --week {self.week})"
                continue
            entry = self.entries[number - 1]
            if len(entry.slides) != post.slides:
                planning[post.id] = (
                    f"its Prompt_Sheet entry has {_count(len(entry.slides), 'Slide')} and the calendar gives it "
                    f"{_count(post.slides, 'Slide')}; re-plan it (plan --week {self.week} --replan {post.id})"
                )
                continue
            for index, slide in enumerate(entry.slides, 1):
                try:
                    picks[(post.id, index)] = pick_candidate(slide.pick, post.candidates)
                except ValueError as error:
                    field = f"entries[{number}].slides[{index}].pick"
                    problems.append(Problem(path, field, None, f"Post {post.id!r}, Slide {index}: {error}"))
        left = tuple(entry.post for entry in self.entries if entry.post not in calendar)
        return CalendarCheck(planning, left, picks, tuple(problems))


def pick_candidate(pick: object, candidates: int) -> int | None:
    """The Candidate number a Slide uses as its Pick, for a Post with `candidates` Candidates.

    With no Pick recorded (None) that is Candidate 1 when the Post has one Candidate (Req 11.7), and None,
    waiting for a Pick, when it has more (Req 11.8). A recorded whole number from 1 to candidates is used
    as it is (Req 11.9); any other value raises a ValueError naming it and the valid numbers (Req 11.10).
    """
    if pick is None:
        return 1 if candidates == 1 else None
    if _whole(pick) and 1 <= pick <= candidates:
        return pick
    valid = ", ".join(str(number) for number in range(1, candidates + 1))
    raise ValueError(f"pick {_shown(pick)} is not a Candidate number; the valid Candidate numbers are {valid}")


# ---- Reader and Printer ----


def read_prompt_sheet(path: str | os.PathLike[str]) -> PromptSheet:
    """The Prompt_Sheet at path. A file that does not load or is not a valid Prompt_Sheet raises one
    ValidationError holding every Problem, each naming the path and the field."""
    name = os.fspath(path)
    found = _Problems(name)
    sheet = _sheet(load_yaml(path), found)
    if found.problems or sheet is None:
        raise ValidationError(found.problems)
    return sheet


def print_prompt_sheet(sheet: PromptSheet) -> str:
    """sheet as YAML text that read_prompt_sheet reads back equal, every key written; raises ValueError for
    a sheet the Reader would reject."""
    data = _sheet_data(sheet)
    found = _Problems("the Prompt_Sheet to print")
    _sheet(data, found)
    if found.problems:
        raise ValueError("an invalid Prompt_Sheet cannot be printed:\n" + format_problems(found.problems))
    return dump_yaml(data)


def _as_list(value: object) -> object:
    return list(value) if isinstance(value, (list, tuple)) else value


def _sheet_data(sheet: PromptSheet) -> dict[str, object]:
    return {
        "format": FORMAT,
        "week": sheet.week,
        "entries": [_entry_data(entry) for entry in _as_list(sheet.entries)],  # type: ignore[union-attr]
    }


def _entry_data(entry: Entry) -> dict[str, object]:
    planned = entry.planned
    return {
        "post": entry.post,
        "approved": entry.approved,
        "needs_manual_prompt": entry.needs_manual_prompt,
        "review": _as_list(entry.review),
        "planned": None if planned is None else {
            "model": planned.model, "at": planned.at, "attempts": planned.attempts,
        },
        "slides": [{"scene": s.scene, "alt": s.alt, "pick": s.pick} for s in entry.slides],
        "caption": entry.caption,
        "hashtags": _as_list(entry.hashtags),
        "motion": entry.motion,
        "raw_response": entry.raw_response,
    }


# ---- checking the loaded YAML ----


class _Problems:
    def __init__(self, path: str) -> None:
        self.path = path
        self.problems: list[Problem] = []

    def add(self, field: str | None, reason: str) -> None:
        self.problems.append(Problem(self.path, field, None, reason))


class _Fields:
    """The keys of one mapping, each bad one reported under where, its reason starting with label."""

    def __init__(self, data: dict, where: str | None, label: str, found: _Problems) -> None:
        self.data, self.where, self.label, self.found = data, where, label, found

    def field(self, key: object) -> str:
        return f"{self.where}.{key}" if self.where else str(key)

    def bad(self, key: object, reason: str) -> None:
        self.found.add(self.field(key), self.label + reason)

    def unknown(self, known: tuple[str, ...], what: str) -> None:
        for key in self.data:
            if key not in known:
                self.bad(key, f"unknown key; {what} has the keys {', '.join(known)}")

    def get(self, key: str, check, default: object = _REQUIRED) -> object:  # noqa: ANN001
        if key not in self.data:
            if default is _REQUIRED:
                self.bad(key, "missing")
                return None
            return default
        value = self.data[key]
        reason = check(value)
        if reason is not None:
            self.bad(key, reason)
        return value

    def texts(self, key: str) -> tuple[str, ...]:
        value = self.data.get(key, [])
        if not isinstance(value, list):
            self.bad(key, f"must be a list of text, not {_shown(value)}")
            return ()
        for number, item in enumerate(value, 1):
            if not isinstance(item, str):
                self.bad(f"{key}[{number}]", f"must be text, not {_shown(item)}")
        return tuple(value)


def _sheet(data: object, found: _Problems) -> PromptSheet | None:
    if not isinstance(data, dict):
        found.add(None, f"holds {_what(data)}; a Prompt_Sheet is a mapping of {', '.join(SHEET_KEYS)}")
        return None
    fields = _Fields(data, None, "", found)
    fields.unknown(SHEET_KEYS, "a Prompt_Sheet")
    fields.get("format", _format)
    week = fields.get("week", _week)
    entries = _entries(data.get("entries", []), found)
    if found.problems:
        return None
    return PromptSheet(week, entries)  # type: ignore[arg-type]


def _entries(value: object, found: _Problems) -> tuple[Entry, ...]:
    if not isinstance(value, list):
        found.add("entries", f"must be a list of entries, not {_shown(value)}")
        return ()
    first: dict[str, int] = {}
    entries = []
    for number, item in enumerate(value, 1):
        post = item.get("post") if isinstance(item, dict) else None
        if _name(post):
            if post in first:
                reason = f"Post {post!r} has a second entry; entries[{first[post]}] is its first; keep one"
                found.add(f"entries[{number}].post", reason)
            else:
                first[post] = number  # type: ignore[index]
        entry = _entry(item, number, found)
        if entry is not None:
            entries.append(entry)
    return tuple(entries)


def _entry(item: object, number: int, found: _Problems) -> Entry | None:
    where = f"entries[{number}]"
    if not isinstance(item, dict):
        found.add(where, f"holds {_what(item)}; an entry is a mapping that starts with post")
        return None
    before = len(found.problems)
    post = item.get("post")
    fields = _Fields(item, where, f"Post {post!r}: " if _name(post) else "", found)
    fields.unknown(ENTRY_KEYS, "an entry")
    fields.get("post", _post)
    approved = fields.get("approved", _flag, False)
    needs_manual_prompt = fields.get("needs_manual_prompt", _flag, False)
    review = fields.texts("review")
    planned = _planned(item.get("planned"), f"{where}.planned", fields.label, found)
    slides = _slides(item.get("slides", []), f"{where}.slides", fields.label, found)
    caption = fields.get("caption", _optional_text, None)
    hashtags = fields.texts("hashtags")
    motion = fields.get("motion", _optional_text, None)
    raw_response = fields.get("raw_response", _optional_text, None)
    if len(found.problems) > before:
        return None
    return Entry(
        post, approved, needs_manual_prompt, review, planned, slides, caption, hashtags,  # type: ignore[arg-type]
        motion, raw_response,  # type: ignore[arg-type]
    )


def _planned(value: object, where: str, label: str, found: _Problems) -> Planned | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        found.add(where, f"{label}must be a mapping of {', '.join(PLANNED_KEYS)}, or null; not {_shown(value)}")
        return None
    fields = _Fields(value, where, label, found)
    fields.unknown(PLANNED_KEYS, "planned")
    model = fields.get("model", _text)
    at = fields.get("at", _text)
    attempts = fields.get("attempts", _attempts)
    return Planned(model, at, attempts)  # type: ignore[arg-type]


def _slides(value: object, where: str, label: str, found: _Problems) -> tuple[Slide, ...]:
    if not isinstance(value, list):
        found.add(where, f"{label}must be a list of Slides, not {_shown(value)}")
        return ()
    slides = []
    for number, item in enumerate(value, 1):
        at = f"{where}[{number}]"
        if not isinstance(item, dict):
            found.add(at, f"{label}holds {_what(item)}; a Slide is a mapping of {', '.join(SLIDE_KEYS)}")
            continue
        fields = _Fields(item, at, label, found)
        fields.unknown(SLIDE_KEYS, "a Slide")
        scene, alt = fields.get("scene", _text), fields.get("alt", _text)
        slides.append(Slide(scene, alt, item.get("pick")))  # type: ignore[arg-type]
    return tuple(slides)


# ---- small checks and messages ----


def _whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _name(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _text(value: object) -> str | None:
    return None if isinstance(value, str) else f"must be text, not {_shown(value)}"


def _optional_text(value: object) -> str | None:
    return None if value is None or isinstance(value, str) else f"must be text or null, not {_shown(value)}"


def _flag(value: object) -> str | None:
    return None if isinstance(value, bool) else f"must be true or false, not {_shown(value)}"


def _format(value: object) -> str | None:
    return None if value == FORMAT else f"is {_shown(value)}; this version reads {FORMAT}"


def _week(value: object) -> str | None:
    return None if _name(value) else f"must be the Week_Id as text, not {_shown(value)}"


def _post(value: object) -> str | None:
    return None if _name(value) else f"must be a Post id, not {_shown(value)}"


def _attempts(value: object) -> str | None:
    if _whole(value) and value >= 1:  # type: ignore[operator]
        return None
    return f"must be a whole number from 1, not {_shown(value)}"


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" + ("" if number == 1 else "s")


def _what(value: object) -> str:
    if value is None:
        return "nothing"
    if isinstance(value, dict):
        return "a mapping"
    if isinstance(value, list):
        return "a list"
    return _shown(value)


def _shown(value: object) -> str:
    """value as YAML flow text for a message: "2" for text, 0, true, null, [1, 2]; cut to 60 characters."""
    try:
        shown = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        shown = repr(value)
    return shown if len(shown) <= _SHOWN_MAX else shown[: _SHOWN_MAX - 3] + "..."
