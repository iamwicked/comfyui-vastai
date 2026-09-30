"""The Content_Calendar: the Calendar model, the Calendar_Reader for YAML and CSV files, the two
Calendar_Printers and the skeleton of new-calendar (design §M2; Req 5, 8.10, 11.6, 16.10-16.12, 26.12).

    read_calendar(path, persona, root, *, check_files=True) -> CalendarResult
    print_calendar_yaml(calendar) -> str        YAML that read_calendar reads back equal (Req 5.13)
    print_calendar_csv(calendar) -> str         CSV that read_calendar reads back equal (Req 5.18)
    skeleton_calendar(week, persona) -> Calendar  the one-Post calendar of new-calendar (Req 5.11)
    find_calendar(root, week) -> Path           the week's one calendar file (Req 5.19)
    week_problem(value) -> str | None           why value is not a Week_Id, None when it is one (Req 5.3)

Pure apart from reading the calendar file and checking that voice clips and Location_Shots exist, which
check_files=False turns off. Nothing here writes a file: the new-calendar command does (__main__).

The Week_Id is YYYY-Www with ww from 01 to date(YYYY, 12, 28).isocalendar()[1], so 2026-W53 passes and
2025-W53 fails. A YAML file is {week, posts}; a CSV file has a `week` column that is equal in every row.

The rules of a Post, identical in both formats:
- id: 1-48 of [a-z0-9-], not `other`. type: single, carousel, story or reel. platform: instagram
  (the default) or other. date: YYYY-MM-DD, a YAML date or text; a date outside the week only warns.
- location and outfit: exact names of the Persona_File, required for single, carousel and story Posts.
- aspect: 3:4 (default) or 4:5 for single and carousel Posts; stories and reels are 9:16.
- slides: required for carousels, 2-20. candidates: 1-4 (default 1) for single, carousel and story Posts.
  An integer field takes an int, an integral float or a numeric text ("5", "5.0"), never a bool.
- mode: carousel regenerate; reel reference or external; single and story Posts take no mode.
- looks: at most 8 LoRAs, each a path below models/loras or {lora, strength}, strength -2.0 to 2.0
  (default 1.0); a LoRA name holds no `;` or `@`. A Post's own list replaces the Persona_File's stills and
  video stacks for that Post, so an empty list means no look LoRAs (Req 8.3); no list means the stacks.
- reel Posts: duration, required, 5-10 seconds, any finite number, exact through Fraction(str(v)); exactly
  one of source_post, mode reference and mode external. source_post names a single, carousel or story
  Post of the calendar, source_slide (default 1) a Slide of it; an Instagram reel may not source an
  `other` Post. mode reference needs a non-empty Reference_Pack, and every Location_Shots file of the
  Post's location must exist. dialogue (single-line, non-blank lines) and voice (a path inside the
  Influencer_Root that must exist) come both or neither, and an external reel takes neither.
- A field the Post's type does not take, or a YAML key that is no Post field, rejects the Post.
In YAML a null or empty-text value counts as absent, as an empty CSV cell does; an empty dialogue list is
no dialogue.

A rejected Post goes into CalendarResult.rejected under its id, or "#<position>" (counted from 1 among
the Post rows) when it has no valid id, with one Problem per faulted field, named "<label>.<field>". Every
other Post is kept with the values a fault-free read gives it. A reel whose source Post was rejected is
checked against what can be read of that Post: when its Slide count (its type, or a carousel's slides)
cannot be read, the Slide number is not range-checked, and when its platform cannot be read, the
Instagram rule is not applied. File-level problems (a missing or invalid week, an id given to two Posts,
`posts` not a non-empty list, a CSV header or row that breaks the format) leave calendar None; the Posts
are still checked when they can be read, so one run reports everything.

The CSV format (utf-8-sig, so with or without a BOM; RFC 4180 quoting through the csv module): one header
row naming each column once, required columns week, id, type, date, location and outfit, optional columns
platform, idea, aspect, slides, candidates, mode, looks, duration, source_post, source_slide, dialogue and
voice. One row per Post; rows whose cells are all empty are skipped. An empty cell is an absent field.
A looks cell holds `name[@strength]` items separated by `;` (a lone `;` is the empty list); a dialogue
cell holds one line per line break, split on \\n after \\r\\n is normalised, empty lines dropped. For a CSV
file a Problem's line is the spreadsheet row, counting the header as row 1.
"""
from __future__ import annotations

import csv
import dataclasses
import datetime
import io
import math
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from influencer.errors import Problem, ValidationError
from influencer.persona import (
    DEFAULT_STRENGTH,
    LOOK_STRENGTH_LIMIT,
    MAX_LOOKS,
    LoraRef,
    Persona,
    _describe,
    _escapes,
    _short,
    resolve_path,
)
from influencer.yamlio import dump_yaml, load_yaml

WEEK_RE = re.compile(r"([0-9]{4})-W([0-9]{2})")
POST_ID_RE = re.compile(r"[a-z0-9-]{1,48}")
RESERVED_ID = "other"  # the folder of platform `other` Posts (Req 5.4, 10.11)
POST_TYPES = ("single", "carousel", "story", "reel")
STILL_TYPES = ("single", "carousel", "story")
ASPECT_TYPES = ("single", "carousel")
PLATFORMS = ("instagram", "other")
DEFAULT_PLATFORM = "instagram"
ASPECTS = ("3:4", "4:5")
DEFAULT_ASPECT = "3:4"
VERTICAL_ASPECT = "9:16"  # stories and reels
MODES = {"single": (), "carousel": ("regenerate",), "story": (), "reel": ("reference", "external")}
SLIDES_MIN, SLIDES_MAX = 2, 20  # Req 5.6
CANDIDATES_MIN, CANDIDATES_MAX = 1, 4  # Req 11.6
DEFAULT_CANDIDATES = 1
DURATION_MIN, DURATION_MAX = Fraction(5), Fraction(10)  # seconds (Req 5.7)
DEFAULT_SOURCE_SLIDE = 1
SKELETON_DURATION = Fraction(8)  # the external reel of a skeleton for a persona without locations or outfits

CALENDAR_FIELDS = ("week", "posts")
POST_FIELDS = (
    "id", "type", "platform", "date", "location", "outfit", "idea", "aspect", "slides", "candidates", "mode",
    "looks", "duration", "source_post", "source_slide", "dialogue", "voice",
)  # fmt: skip
_COMMON = ("id", "type", "platform", "date", "location", "outfit", "idea", "looks")
TYPE_FIELDS = {  # the fields each type takes; mode is checked separately (Req 5.20)
    "single": frozenset((*_COMMON, "aspect", "candidates")),
    "carousel": frozenset((*_COMMON, "aspect", "slides", "candidates")),
    "story": frozenset((*_COMMON, "candidates")),
    "reel": frozenset((*_COMMON, "duration", "source_post", "source_slide", "dialogue", "voice")),
}
REQUIRED_COLUMNS = ("week", "id", "type", "date", "location", "outfit")
OPTIONAL_COLUMNS = (
    "platform", "idea", "aspect", "slides", "candidates", "mode", "looks", "duration", "source_post",
    "source_slide", "dialogue", "voice",
)  # fmt: skip
COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS
LOOK_SEPARATOR, STRENGTH_MARK = ";", "@"
EMPTY_LOOKS_CELL = LOOK_SEPARATOR  # the CSV form of an empty look list

WEEKS_FOLDER = "weeks"
CALENDAR_NAMES = ("calendar.yaml", "calendar.yml", "calendar.csv")
SKELETON_IDS = {"single": "mon-post", "reel": "mon-reel"}

# ---- Week_Ids (Req 5.3) ----


def iso_weeks(year: int) -> int:
    """The number of ISO weeks of year: 52 or 53."""
    return datetime.date(year, 12, 28).isocalendar()[1]


def week_problem(value: object) -> str | None:
    """Why value is not a valid Week_Id, or None when it is one."""
    if not isinstance(value, str):
        return f"must be a Week_Id such as 2026-W40, not {_describe(value)}"
    match = WEEK_RE.fullmatch(value)
    if match is None:
        return f"{_short(value)} is not a Week_Id: use YYYY-Www, such as 2026-W40"
    year, week = int(match.group(1)), int(match.group(2))
    if year == 0:
        return f"{value!r} names the year 0000, which has no ISO weeks"
    weeks = iso_weeks(year)
    if not 1 <= week <= weeks:
        return f"{value!r} names week {week:02d}, but {year} has the ISO weeks 01 to {weeks}"
    return None


def check_week(value: str, source: str = "command line", field: str = "--week") -> str:
    """value when it is a valid Week_Id; otherwise a ValidationError naming source, field and the value."""
    problem = week_problem(value)
    if problem is not None:
        raise ValidationError([Problem(source, field, None, problem)])
    return value


def week_monday(week: str) -> datetime.date:
    """The Monday of a valid Week_Id."""
    check_week(week, "week_monday", "week")
    return datetime.date.fromisocalendar(int(week[:4]), int(week[6:]), 1)


# ---- the model ----


@dataclass(frozen=True)
class Post:
    """An accepted Post, defaults filled in. aspect, slides and source_slide left None take their value
    from the type: aspect 3:4 for single and carousel Posts and 9:16 otherwise; slides 1 for single and
    story Posts and 0 for a reel (a Reel has no Slides), while a carousel gives its own; source_slide 1
    when source_post is set. looks None means the Persona_File's stacks, () no look LoRAs."""

    id: str
    type: str
    date: str  # YYYY-MM-DD
    platform: str = DEFAULT_PLATFORM
    location: str | None = None
    outfit: str | None = None
    idea: str | None = None
    aspect: str | None = None
    slides: int | None = None
    candidates: int = DEFAULT_CANDIDATES
    mode: str | None = None
    looks: tuple[LoraRef, ...] | None = None
    duration: Fraction | None = None  # seconds, reel Posts only
    source_post: str | None = None
    source_slide: int | None = None
    dialogue: tuple[str, ...] = ()
    voice: str | None = None  # relative to the Influencer_Root

    def __post_init__(self) -> None:
        def fill(name: str, value: object) -> None:
            object.__setattr__(self, name, value)

        if self.aspect is None:
            fill("aspect", DEFAULT_ASPECT if self.type in ASPECT_TYPES else VERTICAL_ASPECT)
        if self.slides is None:
            fill("slides", 0 if self.type == "reel" else 1)
        if self.source_post is not None and self.source_slide is None:
            fill("source_slide", DEFAULT_SOURCE_SLIDE)
        if self.looks is not None and not isinstance(self.looks, tuple):
            fill("looks", tuple(self.looks))
        if not isinstance(self.dialogue, tuple):
            fill("dialogue", tuple(self.dialogue))
        if self.duration is not None and not isinstance(self.duration, Fraction):
            fill("duration", Fraction(str(self.duration)))


@dataclass(frozen=True)
class Calendar:
    """An accepted Content_Calendar: its Week_Id and its accepted Posts in file order. `source`, the file
    it was read from, takes no part in equality."""

    week: str
    posts: tuple[Post, ...] = ()
    source: str = dataclasses.field(default="", compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.posts, tuple):
            object.__setattr__(self, "posts", tuple(self.posts))

    def post(self, post_id: str) -> Post | None:
        return next((post for post in self.posts if post.id == post_id), None)


@dataclass
class CalendarResult:
    """What read_calendar found. calendar is None when a file-level problem rejects the whole file."""

    calendar: Calendar | None
    rejected: dict[str, list[Problem]] = dataclasses.field(default_factory=dict)  # "<id>" or "#<position>"
    problems: list[Problem] = dataclasses.field(default_factory=list)  # file-level
    warnings: list[Problem] = dataclasses.field(default_factory=list)  # dates outside the week

    @property
    def accepted(self) -> bool:
        """The whole file is accepted: no file-level problem and no rejected Post."""
        return self.calendar is not None and not self.rejected

    def all_problems(self) -> list[Problem]:
        """The file-level problems, then each rejected Post's, in file order."""
        return self.problems + [problem for problems in self.rejected.values() for problem in problems]


# ---- values ----

_ABSENT = object()
_NUMBER_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_EXTREME = 1000  # a numeric text beyond 10**±1000 is outside every range; it is not expanded exactly


def _exact(value: object) -> Fraction | None:
    """The exact value of a number field's value: an int, a finite float (as Fraction(str(v))) or a plain
    decimal text; None for anything else, bools, NaN and infinities included."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        return Fraction(str(value)) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if not _NUMBER_RE.fullmatch(text):
            return None
        number = Decimal(text)
        if number and not -_EXTREME <= number.adjusted() <= _EXTREME:
            scale = Fraction(10) ** (_EXTREME + 1 if number.adjusted() > 0 else -(_EXTREME + 1))
            return scale if number > 0 else -scale
        return Fraction(number)
    return None


def _what(value: object) -> str:
    """value for a "... is not ..." reason: text quoted, a number as written, anything else described."""
    if isinstance(value, str) or (isinstance(value, (int, float)) and not isinstance(value, bool)):
        return _short(value)
    return _describe(value)


def _listing(items: Iterable[str], conjunction: str = "and") -> str:
    listed = list(items)
    if len(listed) <= 1:
        return "".join(listed)
    return ", ".join(listed[:-1]) + f" {conjunction} " + listed[-1]


def decimal_text(value: Fraction) -> str:
    """value written exactly in decimal ("7.5", "8"). Every value read from a file has such a form."""
    numerator, denominator = value.numerator, value.denominator
    twos = fives = 0
    rest = denominator
    while rest % 2 == 0:
        rest //= 2
        twos += 1
    while rest % 5 == 0:
        rest //= 5
        fives += 1
    if rest != 1:
        raise ValueError(f"{value} has no exact decimal form")
    places = max(twos, fives)
    if places == 0:
        return str(numerator)
    digits = str(abs(numerator) * 10**places // denominator).rjust(places + 1, "0")
    return ("-" if numerator < 0 else "") + digits[:-places] + "." + digits[-places:]


def _file_problem(root: Path, relative: str) -> str | None:
    """Why root/relative is not an existing regular file, or None when it is one."""
    resolved = resolve_path(root, relative)
    try:
        mode = os.stat(resolved).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return f"does not exist: there is no file at {resolved}"
    except (OSError, ValueError) as error:  # a NUL byte, a name too long, no permission
        detail = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
        return f"cannot be checked at {resolved}: {detail}"
    if stat.S_ISDIR(mode):
        return f"is a folder, not a file: {resolved}"
    if not stat.S_ISREG(mode):
        return f"is not a regular file: {resolved}"
    return None


# ---- the Reader ----


@dataclass
class _Raw:
    """One Post as the file gives it: YAML typed values or CSV cell texts, absent fields left out."""

    position: int  # counted from 1 among the Post rows
    line: int | None  # the spreadsheet row of a CSV Post
    data: Any  # a dict, or for a YAML item that is no mapping, the item


@dataclass
class _Draft:
    """One Post while it is checked: the values accepted so far and every Problem found."""

    position: int
    line: int | None
    label: str
    problems: list[Problem] = dataclasses.field(default_factory=list)
    values: dict[str, Any] = dataclasses.field(default_factory=dict)
    type: str | None = None  # None when the type cannot be read
    platform: str | None = None  # None when the platform cannot be read
    source_text: str | None = None  # a reel's source_post, when it is text

    @property
    def slide_count(self) -> int | None:
        """The Post's Slide count, None when it cannot be read."""
        if self.type == "carousel":
            return self.values.get("slides")
        if self.type in ("single", "story"):
            return 1
        return 0 if self.type == "reel" else None


class _Checker:
    """Checks the Posts of one calendar file against the rules of the module docstring."""

    def __init__(self, source: str, persona: Persona, root: Path | None, check_files: bool) -> None:
        self.source = source
        self.persona = persona
        self.root = root
        self.check_files = check_files
        self.locations = {location.name: location for location in persona.locations}  # in Persona_File order
        self.outfits = {outfit.name: outfit for outfit in persona.wardrobe}
        self.problems: list[Problem] = []  # file-level
        self.warnings: list[Problem] = []
        self.week: str | None = None
        self.raws: list[_Raw] | None = None  # None when the Posts cannot be read

    def file_fail(self, field: str | None, reason: str, line: int | None = None) -> None:
        self.problems.append(Problem(self.source, field, line, reason))

    def set_week(self, value: object, where: str = "") -> None:
        problem = week_problem(value)
        if problem is None:
            self.week = value  # type: ignore[assignment]
        else:
            self.file_fail("week", problem + where)

    # ---- one Post ----

    def draft(self, raw: _Raw) -> _Draft:
        data = raw.data
        if not isinstance(data, dict):
            draft = _Draft(raw.position, raw.line, f"#{raw.position}")
            reason = f"must be a mapping of the Post fields ({_listing(POST_FIELDS)}), not {_describe(data)}"
            draft.problems.append(Problem(self.source, draft.label, raw.line, reason))
            return draft
        ident, id_reason = data.get("id", _ABSENT), None
        if ident is _ABSENT:
            id_reason = "is missing: give the Post an id of 1 to 48 of the characters a-z, 0-9 and -"
        elif not isinstance(ident, str):
            id_reason = f"must be text, not {_describe(ident)}"
        elif not POST_ID_RE.fullmatch(ident):
            id_reason = f"{_short(ident)} is not a Post id: use 1 to 48 of the characters a-z, 0-9 and -"
        elif ident == RESERVED_ID:
            id_reason = "'other' is reserved: platform other Posts go into the folder other/, so no Post has that id"
        draft = _Draft(raw.position, raw.line, ident if id_reason is None else f"#{raw.position}")
        _PostFields(self, draft, data).check(id_reason)
        return draft

    def link(self, drafts: list[_Draft]) -> None:
        """The checks of a reel against its source Post (Req 5.8, 16.11)."""
        by_id: dict[str, _Draft] = {}
        for draft in drafts:
            if "id" in draft.values:  # a valid id: the Post is in the calendar, rejected or not
                by_id.setdefault(draft.values["id"], draft)
        for draft in drafts:
            if draft.type != "reel" or draft.source_text is None:
                continue
            fields = _PostFields(self, draft, {})
            name = draft.source_text
            source = by_id.get(name)
            if source is None:
                fields.fail("source_post", f"names {_short(name)}, which is not the id of a Post of this calendar")
                continue
            count = source.slide_count
            if source.type == "reel":
                fields.fail("source_post", f"names {name!r}, a reel Post; a reel takes its first frame from a "
                                           "Slide of a single, carousel or story Post")  # fmt: skip
            elif count is not None and "source_slide" in draft.values:
                slide = draft.values["source_slide"]
                if slide > count:
                    noun = "Slide" if count == 1 else "Slides"
                    fields.fail("source_slide", f"Slide {slide} is outside Post {name!r}, which has {count} {noun}")
            if draft.platform == "instagram" and source.platform == "other":
                fields.fail("source_post", f"the Instagram reel {draft.label!r} names Post {name!r}, which has "
                                           "platform other; an Instagram reel takes its first frame from an "
                                           "Instagram Post")  # fmt: skip

    # ---- the whole file ----

    def result(self) -> CalendarResult:
        if self.raws is None:
            return CalendarResult(None, {}, self.problems, self.warnings)
        drafts = [self.draft(raw) for raw in self.raws]
        self.link(drafts)
        seen: dict[str, list[_Draft]] = {}
        for raw, draft in zip(self.raws, drafts):
            ident = raw.data.get("id") if isinstance(raw.data, dict) else None
            if isinstance(ident, str):
                seen.setdefault(ident, []).append(draft)
        for ident, owners in seen.items():
            if len(owners) > 1:
                where = "rows" if owners[0].line is not None else "Posts"
                places = _listing(str(o.line if o.line is not None else o.position) for o in owners)
                self.file_fail("id", f"{ident!r} is the id of {len(owners)} Posts ({where} {places}); each Post "
                                     "needs its own id")  # fmt: skip
        rejected: dict[str, list[Problem]] = {}
        posts: list[Post] = []
        for draft in drafts:
            if draft.problems:
                rejected.setdefault(draft.label, []).extend(draft.problems)
            else:
                posts.append(_build(draft))
        calendar = None if self.problems or self.week is None else Calendar(self.week, tuple(posts), self.source)
        return CalendarResult(calendar, rejected, self.problems, self.warnings)


class _PostFields:
    """The field checks of one Post. Each check records a Problem named "<label>.<field>" or stores the
    value it accepted in draft.values."""

    def __init__(self, checker: _Checker, draft: _Draft, data: dict[Any, Any]) -> None:
        self.checker = checker
        self.persona = checker.persona
        self.draft = draft
        self.data = data

    def fail(self, field: str, reason: str) -> None:
        draft = self.draft
        draft.problems.append(Problem(self.checker.source, f"{draft.label}.{field}", draft.line, reason))

    def get(self, field: str) -> Any:
        return self.data.get(field, _ABSENT)

    def text(self, field: str, value: object) -> str | None:
        if not isinstance(value, str):
            self.fail(field, f"must be text, not {_describe(value)}")
            return None
        return value

    def whole(self, field: str, value: object, low: int, high: int | None, what: str) -> int | None:
        exact = _exact(value)
        rule = f"{what} is a whole number from {low} to {high}" if high is not None else f"{what} is a whole number from {low}"
        if exact is None or exact.denominator != 1:
            self.fail(field, f"{_what(value)} is not a whole number: {rule}")
            return None
        if exact < low or (high is not None and exact > high):
            self.fail(field, f"{_what(value)} is out of range: {rule}")
            return None
        return int(exact)

    def check(self, id_reason: str | None) -> None:
        draft, data, get = self.draft, self.data, self.get
        if id_reason is not None:
            self.fail("id", id_reason)
        else:
            draft.values["id"] = data["id"]
        for key in data:
            if key not in POST_FIELDS:
                self.fail(str(key), f"is not a Post field; a Post takes {_listing(POST_FIELDS)}")

        kind = get("type")
        if kind is _ABSENT:
            self.fail("type", f"is missing: use {_listing(POST_TYPES, 'or')}")
        elif kind not in POST_TYPES:
            self.fail("type", f"{_what(kind)} is not a Post type: use {_listing(POST_TYPES, 'or')}")
        else:
            draft.type = kind
        platform = get("platform")
        if platform is _ABSENT:
            draft.platform = DEFAULT_PLATFORM
        elif platform not in PLATFORMS:
            self.fail("platform", f"{_what(platform)} is not a platform: use instagram or other")
        else:
            draft.platform = platform
        if draft.platform is not None:
            draft.values["platform"] = draft.platform
        self.date()
        self.named("location", self.checker.locations, "locations")
        self.named("outfit", self.checker.outfits, "outfits")
        if get("idea") is not _ABSENT:
            idea = self.text("idea", data["idea"])
            if idea is not None:
                draft.values["idea"] = idea
        if get("looks") is not _ABSENT:
            looks = self.looks(data["looks"])
            if looks is not None:
                draft.values["looks"] = looks
        if draft.type is None:
            return
        self.mode()
        for key in data:
            if key in POST_FIELDS and key != "mode" and key not in TYPE_FIELDS[draft.type]:
                self.fail(key, self.not_taken(key))
        if draft.type in ASPECT_TYPES and get("aspect") is not _ABSENT:
            self.aspect(data["aspect"])
        if draft.type == "carousel":
            if get("slides") is _ABSENT:
                self.fail("slides", f"is missing: a carousel Post has {SLIDES_MIN} to {SLIDES_MAX} Slides")
            else:
                slides = self.whole("slides", data["slides"], SLIDES_MIN, SLIDES_MAX, "a carousel's Slide count")
                if slides is not None:
                    draft.values["slides"] = slides
        if draft.type in STILL_TYPES and get("candidates") is not _ABSENT:
            candidates = self.whole("candidates", data["candidates"], CANDIDATES_MIN, CANDIDATES_MAX,
                                    "the Candidate count")  # fmt: skip
            if candidates is not None:
                draft.values["candidates"] = candidates
        if draft.type == "reel":
            self.reel()

    def not_taken(self, key: str) -> str:
        kind = self.draft.type
        if key == "aspect":
            return f"is not taken by a {kind} Post: {kind} Posts are always {VERTICAL_ASPECT}"
        if key == "slides":
            return f"is only for carousel Posts: a {kind} Post has {'no Slides' if kind == 'reel' else '1 Slide'}"
        if key == "candidates":
            return "is not taken by a reel Post, which renders one Reel"
        if key in ("duration", "source_post", "source_slide", "dialogue", "voice"):
            return f"is only for reel Posts, and this is a {kind} Post"
        return f"is not a field of a {kind} Post"

    # ---- fields every type has ----

    def date(self) -> None:
        value = self.get("date")
        if value is _ABSENT:
            self.fail("date", "is missing: give the day the Post goes out as YYYY-MM-DD")
            return
        if isinstance(value, datetime.datetime):
            self.fail("date", f"{value.isoformat()} is a date and time; give the day alone as YYYY-MM-DD")
            return
        if isinstance(value, datetime.date):
            day = value
        elif isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            try:
                day = datetime.date(int(value[:4]), int(value[5:7]), int(value[8:]))
            except ValueError:
                self.fail("date", f"{value!r} is not a day of the calendar")
                return
        else:
            self.fail("date", f"{_what(value)} is not a date: use YYYY-MM-DD")
            return
        self.draft.values["date"] = day.isoformat()
        week = self.checker.week
        if week is not None and day.isocalendar()[:2] != (int(week[:4]), int(week[6:])):
            reason = f"{day.isoformat()} is outside {week}, the calendar's week"
            self.checker.warnings.append(Problem(self.checker.source, f"{self.draft.label}.date", self.draft.line, reason))

    def named(self, field: str, names: Iterable[str], plural: str) -> None:
        """An exact name of the Persona_File's locations or outfits (Req 5.5); required for still Posts."""
        value = self.get(field)
        if value is _ABSENT:
            if self.draft.type in STILL_TYPES:
                self.fail(field, f"is missing: a {self.draft.type} Post needs one of the Persona_File's {plural}"
                                 f" ({self.defined(names, plural)})")  # fmt: skip
            return
        text = self.text(field, value)
        if text is None:
            return
        if text not in names:
            self.fail(field, f"{text!r} is not one of the Persona_File's {plural}: {self.defined(names, plural)}")
            return
        self.draft.values[field] = text

    @staticmethod
    def defined(names: Iterable[str], plural: str) -> str:
        listed = [repr(name) for name in names]
        return _listing(listed) if listed else f"the Persona_File defines no {plural}"

    def looks(self, value: object) -> tuple[LoraRef, ...] | None:
        """A Post look list (Req 8.3, 8.10)."""
        if not isinstance(value, list):
            self.fail("looks", f"must be a list of look LoRAs, not {_describe(value)}")
            return None
        valid = True
        if len(value) > MAX_LOOKS:
            self.fail("looks", f"lists {len(value)} LoRAs; a look list holds at most {MAX_LOOKS}")
            valid = False
        refs = []
        for number, item in enumerate(value, 1):
            ref = self.look(item, f"looks[{number}]")
            valid = valid and ref is not None
            refs.append(ref)
        return tuple(refs) if valid else None

    def look(self, item: object, where: str) -> LoraRef | None:
        if isinstance(item, str):
            lora = self.lora(item, where)
            return None if lora is None else LoraRef(lora, DEFAULT_STRENGTH)
        if not isinstance(item, dict):
            self.fail(where, f"must be a LoRA path or a mapping of lora and strength, not {_describe(item)}")
            return None
        valid = True
        for key in item:
            if key not in ("lora", "strength"):
                self.fail(f"{where}.{key}", "is not a field of a look entry, which takes lora and strength")
                valid = False
        if item.get("lora") is None:
            self.fail(f"{where}.lora", "is missing")
            return None
        lora = self.lora(item["lora"], f"{where}.lora")
        strength = self.strength(item.get("strength", DEFAULT_STRENGTH), f"{where}.strength", lora)
        return LoraRef(lora, strength) if valid and lora is not None and strength is not None else None

    def lora(self, value: object, where: str) -> str | None:
        if not isinstance(value, str):
            self.fail(where, f"must be a LoRA path below models/loras, not {_describe(value)}")
            return None
        if not value.strip():
            self.fail(where, "is blank")
            return None
        reason = _escapes(value)
        if reason is not None:
            self.fail(where, f"{value!r} {reason}: give a path inside models/loras")
            return None
        if LOOK_SEPARATOR in value or STRENGTH_MARK in value:
            self.fail(where, f"{value!r} holds ';' or '@', which a calendar's LoRA names may not: the CSV format "
                             "separates look entries with ';' and strengths with '@'")  # fmt: skip
            return None
        return value

    def strength(self, value: object, where: str, lora: str | None) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.fail(where, f"must be a number, not {_describe(value)}")
            return None
        if not -LOOK_STRENGTH_LIMIT <= value <= LOOK_STRENGTH_LIMIT:  # NaN fails too
            of = f" of {lora!r}" if lora is not None else ""
            self.fail(where, f"the strength {_short(value)}{of} is out of range: use {-LOOK_STRENGTH_LIMIT} to "
                             f"{LOOK_STRENGTH_LIMIT}")  # fmt: skip
            return None
        return float(value)

    # ---- fields of some types ----

    def mode(self) -> None:
        """Only the modes the type accepts (Req 5.20)."""
        value = self.get("mode")
        if value is _ABSENT:
            return
        kind = self.draft.type
        accepted = MODES[kind]  # type: ignore[index]
        if value not in accepted:
            takes = f"take only {_listing(accepted)}" if accepted else "take no mode"
            self.fail("mode", f"{_what(value)} is not a mode of a {kind} Post: {kind} Posts {takes}")
            return
        self.draft.values["mode"] = value

    def aspect(self, value: object) -> None:
        if value in ASPECTS:
            self.draft.values["aspect"] = value
            return
        hint = ""
        if isinstance(value, int) and not isinstance(value, bool) and value in (184, 245):
            hint = "; YAML reads 3:4 and 4:5 without quotes as the numbers 184 and 245, so quote them"
        self.fail("aspect", f"{_what(value)} is not an aspect ratio: use \"3:4\" or \"4:5\"{hint}")

    def reel(self) -> None:
        """The reel rules (Req 5.7, 16.10, 16.12, 16.14); the source Post is checked by _Checker.link."""
        draft, get = self.draft, self.get
        if get("duration") is _ABSENT:
            self.fail("duration", "is missing: a reel lasts 5 to 10 seconds")
        else:
            value = self.data["duration"]
            exact = _exact(value)
            if exact is None:
                self.fail("duration", f"{_what(value)} is not a number of seconds: a reel lasts 5 to 10 seconds, "
                                      "fractions such as 7.5 included")  # fmt: skip
            elif not DURATION_MIN <= exact <= DURATION_MAX:
                self.fail("duration", f"{_what(value)} s is out of range: a reel lasts 5 to 10 seconds")
            else:
                draft.values["duration"] = exact
        has_source = get("source_post") is not _ABSENT
        if has_source:
            text = self.text("source_post", self.data["source_post"])
            if text is not None:
                draft.values["source_post"] = draft.source_text = text
            slide = get("source_slide")
            number = DEFAULT_SOURCE_SLIDE if slide is _ABSENT else self.whole(
                "source_slide", slide, 1, None, "a source Slide number")  # fmt: skip
            if number is not None:
                draft.values["source_slide"] = number
        elif get("source_slide") is not _ABSENT:
            self.fail("source_slide", "is set without source_post: name the Post whose Slide is the first frame")
        mode = get("mode")
        if mode is _ABSENT or mode in MODES["reel"]:  # an invalid mode is already reported
            chosen = [name for name, on in (("source_post", has_source), ("mode reference", mode == "reference"),
                                            ("mode external", mode == "external")) if on]  # fmt: skip
            if not chosen:
                self.fail("mode", "a reel Post sets one of source_post, mode reference and mode external, and "
                                  "this one sets none")  # fmt: skip
            elif len(chosen) > 1:
                self.fail("mode", f"sets {_listing(chosen)}; a reel Post sets exactly one of source_post, mode "
                                  "reference and mode external")  # fmt: skip
        if mode == "reference":
            self.reference()
        if mode != "external":
            self.dialogue()
            return
        for key in ("dialogue", "voice"):
            if get(key) is not _ABSENT:
                self.fail(key, "is not taken by an external reel: its video comes from outside the pipeline, "
                               "without dialogue or a voice clip")  # fmt: skip

    def reference(self) -> None:
        """mode reference needs the Reference_Pack and the location's Location_Shots (Req 16.10, 16.14)."""
        if not self.persona.reference_pack:
            self.fail("mode", "mode reference needs the Reference_Pack images, and the Persona_File lists none")
        location = self.checker.locations.get(self.draft.values.get("location"))  # type: ignore[arg-type]
        if location is None or not self.checker.check_files:
            return
        assert self.checker.root is not None  # read_calendar refuses check_files without a root
        for shot in location.shots:
            problem = _file_problem(self.checker.root, shot)
            if problem is not None:
                self.fail("location", f"the Location_Shots file {shot!r} of {location.name!r} {problem}")

    def dialogue(self) -> None:
        """dialogue lines and a voice clip, both or neither (Req 16.12)."""
        draft, get = self.draft, self.get
        lines: tuple[str, ...] | None = ()
        if get("dialogue") is not _ABSENT:
            lines = self.lines(self.data["dialogue"])
            if lines is not None and lines:
                draft.values["dialogue"] = lines
        voice = get("voice")
        if voice is not _ABSENT:
            path = self.voice(voice)
            if path is not None:
                draft.values["voice"] = path
            if lines == ():
                self.fail("dialogue", "is missing: a voice clip needs the dialogue lines it speaks")
        elif lines:
            self.fail("voice", "is missing: dialogue lines need a voice clip, a path inside the Influencer_Root")

    def lines(self, value: object) -> tuple[str, ...] | None:
        if not isinstance(value, list):
            self.fail("dialogue", f"must be a list of single-line texts, not {_describe(value)}")
            return None
        found, valid = [], True
        for number, line in enumerate(value, 1):
            where = f"dialogue[{number}]"
            if not isinstance(line, str):
                self.fail(where, f"must be text, not {_describe(line)}")
            elif not line.strip():
                self.fail(where, "is blank: each dialogue line is spoken text")
            elif line.splitlines() != [line]:
                self.fail(where, "holds a line break: each dialogue line is one line, spoken in its own <d>…</d>")
            else:
                found.append(line)
                continue
            valid = False
        return tuple(found) if valid else None

    def voice(self, value: object) -> str | None:
        if not isinstance(value, str):
            self.fail("voice", f"must be the path of a voice clip, not {_describe(value)}")
            return None
        if not value.strip():
            self.fail("voice", "is blank")
            return None
        reason = _escapes(value)
        if reason is not None:
            self.fail("voice", f"{value!r} {reason}: give a path inside the Influencer_Root")
            return None
        if self.checker.check_files:
            assert self.checker.root is not None
            problem = _file_problem(self.checker.root, value)
            if problem is not None:
                self.fail("voice", f"the voice clip {value!r} {problem}")
                return None
        return value


def _build(draft: _Draft) -> Post:
    values = draft.values
    return Post(
        id=values["id"], type=draft.type, date=values["date"], platform=values["platform"],  # type: ignore[arg-type]
        location=values.get("location"), outfit=values.get("outfit"), idea=values.get("idea"),
        aspect=values.get("aspect"), slides=values.get("slides"),
        candidates=values.get("candidates", DEFAULT_CANDIDATES), mode=values.get("mode"), looks=values.get("looks"),
        duration=values.get("duration"), source_post=values.get("source_post"),
        source_slide=values.get("source_slide"), dialogue=values.get("dialogue", ()), voice=values.get("voice"),
    )  # fmt: skip


def _yaml_post(item: object) -> object:
    """A YAML Post item with its null and empty-text values left out, as an empty CSV cell is."""
    if not isinstance(item, dict):
        return item
    return {key: value for key, value in item.items() if value is not None and not (isinstance(value, str) and value == "")}


def _read_yaml(checker: _Checker, path: str | os.PathLike[str]) -> None:
    try:
        data = load_yaml(path)
    except ValidationError as error:
        checker.problems.extend(error.problems)
        return
    if not isinstance(data, dict):
        found = "the file is empty" if data is None else f"the file holds {_describe(data)}"
        checker.file_fail(None, f"{found}; a Content_Calendar is a mapping of week and posts (design §M2)")
        return
    for key in data:
        if key not in CALENDAR_FIELDS:
            checker.file_fail(str(key), "is not a field of a Content_Calendar, which takes week and posts")
    week = data.get("week")
    if week is None or week == "":
        checker.file_fail("week", "is missing: give the calendar's Week_Id, such as 2026-W40")
    else:
        checker.set_week(week)
    posts = data.get("posts")
    if posts is None:
        checker.file_fail("posts", "is missing: list the week's Posts")
    elif not isinstance(posts, list):
        checker.file_fail("posts", f"must be a list of Posts, not {_describe(posts)}")
    elif not posts:
        checker.file_fail("posts", "is empty: a Content_Calendar holds at least one Post")
    else:
        checker.raws = [_Raw(number, None, _yaml_post(item)) for number, item in enumerate(posts, 1)]


def _csv_strength(text: str) -> object:
    """A look strength from a CSV cell: the number when the text is one, else the text, which the look
    check then rejects as it rejects a YAML strength that is text."""
    return float(text.strip()) if _NUMBER_RE.fullmatch(text.strip()) else text


def split_looks(cell: str) -> list[object]:
    """A looks cell as the YAML list it stands for: `name` and `name@strength` items separated by `;`, empty
    items skipped, so `;` alone is the empty list."""
    items: list[object] = []
    for part in cell.split(LOOK_SEPARATOR):
        if part == "":
            continue
        name, mark, strength = part.partition(STRENGTH_MARK)
        items.append({"lora": name, "strength": _csv_strength(strength)} if mark else name)
    return items


def split_dialogue(cell: str) -> list[str]:
    """A dialogue cell as its lines: split on \\n after \\r\\n is normalised, empty lines dropped."""
    return [line for line in cell.replace("\r\n", "\n").split("\n") if line != ""]


def _read_csv(checker: _Checker, path: str | os.PathLike[str]) -> None:
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        checker.file_fail(None, f"cannot read the file: {error.strerror or error}")
        return
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        checker.file_fail(None, f"not UTF-8 text: byte 0x{data[error.start]:02x} at byte offset {error.start}")
        return
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        records = list(reader)
    except csv.Error as error:
        checker.file_fail(None, f"not a CSV file the csv module reads: {error}", reader.line_num)
        return
    if not records:
        checker.file_fail(None, "the file is empty; a CSV Content_Calendar starts with a header row of column names")
        return
    header = records[0]
    columns_ok = True  # every required column once, and no other name: then the rows can be read as Posts
    for column in REQUIRED_COLUMNS:
        if column not in header:
            checker.file_fail(column, "is a required column, and the header row lacks it")
            columns_ok = False
    reported: set[str] = set()
    for number, column in enumerate(header, 1):
        if column not in COLUMNS and column not in reported:
            shown = column if column else f"column {number}"
            checker.file_fail(shown, f"is not a column of the CSV format, which takes {_listing(COLUMNS)}")
            reported.add(column or f"column {number}")
            columns_ok = False
    for column in dict.fromkeys(header):
        places = [str(number) for number, name in enumerate(header, 1) if name == column]
        if len(places) > 1 and column in COLUMNS:
            checker.file_fail(column, f"is named {len(places)} times in the header row (columns {_listing(places)})")
            columns_ok = False
    rows = [(number, record) for number, record in enumerate(records[1:], 2) if any(cell != "" for cell in record)]
    for number, record in rows:
        if any(cell != "" for cell in record[len(header):]):
            checker.file_fail(None, f"row {number} has a value in column {len(header) + 1} or later, which the "
                                    "header row does not name")  # fmt: skip
    if not rows:
        checker.file_fail(None, "has no Post row, so it names no Week_Id; a Content_Calendar holds at least one Post")
    if header.count("week") == 1 and rows:
        column = header.index("week")

        def week_of(record: list[str]) -> str:
            return record[column] if column < len(record) else ""

        first_row, first = rows[0][0], week_of(rows[0][1])
        for number, record in rows[1:]:
            if week_of(record) != first:
                found = f"the Week_Id {week_of(record)!r}" if week_of(record) else "no Week_Id"
                checker.file_fail("week", f"row {number} has {found}, but the first Post row (row {first_row}) has "
                                          f"{first!r}; a Content_Calendar holds one week")  # fmt: skip
        if first == "":
            checker.file_fail("week", f"is empty in the first Post row (row {first_row}): give the Week_Id, such "
                                      "as 2026-W40")  # fmt: skip
        else:
            checker.set_week(first, f" (row {first_row})")
    if not columns_ok:
        return
    raws = []
    for position, (number, record) in enumerate(rows, 1):
        fields: dict[str, object] = {}
        for column_name, cell in zip(header, record):
            if column_name == "week" or cell == "":
                continue
            if column_name == "looks":
                fields[column_name] = split_looks(cell)
            elif column_name == "dialogue":
                fields[column_name] = split_dialogue(cell)
            else:
                fields[column_name] = cell
        raws.append(_Raw(position, number, fields))
    checker.raws = raws or None


def read_calendar(path: str | os.PathLike[str], persona: Persona, root: str | os.PathLike[str] | None, *,
                  check_files: bool = True) -> CalendarResult:  # fmt: skip
    """The Content_Calendar at path (.yaml, .yml or .csv), checked against persona and the rules of the
    module docstring. Voice clips and Location_Shots are resolved against root, the Influencer_Root, and
    must exist unless check_files is False (root may then be None)."""
    if check_files and root is None:
        raise ValueError("read_calendar needs the Influencer_Root to check that voice clips and Location_Shots exist")
    checker = _Checker(os.fspath(path), persona, None if root is None else Path(root), check_files)
    suffix = Path(path).suffix.lower()
    if suffix == ".csv":
        _read_csv(checker, path)
    elif suffix in (".yaml", ".yml"):
        _read_yaml(checker, path)
    else:
        checker.file_fail(None, "is not a .yaml, .yml or .csv file, so it is no Content_Calendar")
    return checker.result()


# ---- the Printers ----


def _duration_value(value: Fraction) -> int | float | str:
    """A duration as YAML prints it: an int or a float when that reads back exactly, else exact decimal text."""
    if value.denominator == 1:
        return int(value)
    as_float = float(value)
    if Fraction(str(as_float)) == value:
        return as_float
    return decimal_text(value)


def post_data(post: Post) -> dict[str, object]:
    """The plain data print_calendar_yaml prints for post: the fields its type takes, in §M2 order, with the
    defaults written out, None values left out and the date as a date."""
    data: dict[str, object] = {
        "id": post.id,
        "type": post.type,
        "platform": post.platform,
        "date": datetime.date.fromisoformat(post.date),
    }
    for field in ("location", "outfit", "idea"):
        if getattr(post, field) is not None:
            data[field] = getattr(post, field)
    if post.type in ASPECT_TYPES:
        data["aspect"] = post.aspect
    if post.type == "carousel":
        data["slides"] = post.slides
    if post.type in STILL_TYPES:
        data["candidates"] = post.candidates
    if post.mode is not None:
        data["mode"] = post.mode
    if post.looks is not None:
        data["looks"] = [{"lora": ref.lora, "strength": ref.strength} for ref in post.looks]
    if post.duration is not None:
        data["duration"] = _duration_value(post.duration)
    if post.source_post is not None:
        data["source_post"] = post.source_post
        data["source_slide"] = post.source_slide
    if post.dialogue:
        data["dialogue"] = list(post.dialogue)
    if post.voice is not None:
        data["voice"] = post.voice
    return data


def calendar_data(calendar: Calendar) -> dict[str, object]:
    """The plain data print_calendar_yaml prints: {week, posts}."""
    return {"week": calendar.week, "posts": [post_data(post) for post in calendar.posts]}


def print_calendar_yaml(calendar: Calendar) -> str:
    """calendar as YAML through yamlio.dump_yaml, which read_calendar reads back equal (Req 5.13)."""
    return dump_yaml(calendar_data(calendar))


def _looks_cell(looks: tuple[LoraRef, ...] | None) -> str:
    if looks is None:
        return ""
    if not looks:
        return EMPTY_LOOKS_CELL
    return LOOK_SEPARATOR.join(
        ref.lora if ref.strength == DEFAULT_STRENGTH else f"{ref.lora}{STRENGTH_MARK}{ref.strength!r}" for ref in looks
    )


def _cells(calendar: Calendar, post: Post) -> list[str]:
    data = post_data(post)
    cells = []
    for column in COLUMNS:
        if column == "week":
            cells.append(calendar.week)
        elif column == "date":
            cells.append(post.date)
        elif column == "looks":
            cells.append(_looks_cell(post.looks))
        elif column == "duration":
            cells.append("" if post.duration is None else decimal_text(post.duration))
        elif column == "dialogue":
            cells.append("\n".join(post.dialogue))
        else:
            value = data.get(column)
            cells.append("" if value is None else str(value))
    return cells


def print_calendar_csv(calendar: Calendar) -> str:
    """calendar as a CSV file with every column, which read_calendar reads back equal (Req 5.18)."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(COLUMNS)
    for post in calendar.posts:
        writer.writerow(_cells(calendar, post))
    return buffer.getvalue()


# ---- the skeleton and the week folder ----


def skeleton_calendar(week: str, persona: Persona) -> Calendar:
    """The calendar new-calendar writes (Req 5.11): one single Post dated the Monday of week, at the
    Persona_File's first location in its first outfit. A Persona_File without a location or an outfit
    gets an external reel instead, the one Post type that needs neither."""
    monday = week_monday(week).isoformat()
    if persona.locations and persona.wardrobe:
        post = Post(SKELETON_IDS["single"], "single", monday, location=persona.locations[0].name,
                    outfit=persona.wardrobe[0].name)  # fmt: skip
    else:
        post = Post(SKELETON_IDS["reel"], "reel", monday, mode="external", duration=SKELETON_DURATION)
    return Calendar(week, (post,))


def week_folder(root: str | os.PathLike[str], week: str) -> Path:
    """Influencer_Root/weeks/<week>; an invalid Week_Id is a ValidationError naming --week."""
    return Path(root) / WEEKS_FOLDER / check_week(week)


def calendar_files(root: str | os.PathLike[str], week: str) -> list[Path]:
    """Every calendar file of the week that exists: calendar.yaml, calendar.yml and calendar.csv, in that
    order (a folder or a dangling link of that name counts)."""
    folder = week_folder(root, week)
    return [folder / name for name in CALENDAR_NAMES if os.path.lexists(folder / name)]


def find_calendar(root: str | os.PathLike[str], week: str) -> Path:
    """The week's one Content_Calendar file. A ValidationError names every calendar file when there is more
    than one (Req 5.19), and the week folder when there is none; nothing is written."""
    found = calendar_files(root, week)
    if len(found) > 1:
        names = _listing(path.name for path in found)
        raise ValidationError(
            Problem(str(path), None, None, f"is one of {len(found)} calendar files for {week} ({names}); keep one")
            for path in found
        )
    if not found:
        folder = week_folder(root, week)
        reason = f"holds no {_listing(CALENDAR_NAMES, 'or')}; write one with new-calendar --week {week}"
        raise ValidationError([Problem(str(folder), None, None, reason)])
    return found[0]
