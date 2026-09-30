"""Hypothesis strategies for the Content_Calendar (task 9.7; design §M2, Properties 3-8).

- weeks(): valid Week_Ids, years 1900-2200 and weeks 01 to that year's ISO week count (Property 3's range).
- calendars(persona): a random Calendar that read_calendar accepts for persona with no rejected Post, once
  write_needed_files() has created the voice clips and Location_Shots it names.
- faults("post"): the Property 7 catalogue. A PostFault writes one fault into the plain data of a valid
  calendar (calendar_io.calendar_data) and returns the field the Reader must name; applies() says whether
  it is a clean single fault for that Post, one that leaves every other Post as it was.
- csv_text(): calendar data, faults included, as a CSV file written without print_calendar_csv, with an
  optional BOM and blank rows, for Property 6.

A module of its own so tests/strategies.py stays untouched; texts() and WORDS come from there.
"""
from __future__ import annotations

import csv
import datetime
import io
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from hypothesis import strategies as st

from helpers.harness import REPO
from strategies import WORDS, texts

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import calendar_io as C  # noqa: E402
from influencer.persona import LoraRef, Persona  # noqa: E402

# ---- weeks() ----


@st.composite
def weeks(draw: st.DrawFn, min_year: int = 1900, max_year: int = 2200) -> str:
    """A valid Week_Id: YYYY-Www with ww from 01 to the ISO weeks of YYYY (52 or 53)."""
    year = draw(st.integers(min_year, max_year))
    return f"{year:04d}-W{draw(st.integers(1, C.iso_weeks(year))):02d}"


# ---- calendars() building blocks ----

# Post ids over the whole alphabet: hyphens anywhere, digits only, YAML-ambiguous, the longest.
POST_IDS = st.one_of(
    st.sampled_from(("a--b", "0", "-", "--", "0-0", "2026", "no", "on", "1e3", "mon-cafe", "other-1", "x" * 48)),
    st.from_regex(r"[a-z0-9-]{1,48}", fullmatch=True),
).filter(lambda ident: ident != C.RESERVED_ID)

IDEAS = st.none() | texts(max_size=60).filter(lambda text: text != "")  # "" is an absent idea
DIALOGUE_LINES = texts(single_line=True, max_size=30).filter(lambda text: text.strip() != "")
VOICES = st.builds(lambda word, number: f"voice/{word}-{number}.wav", st.sampled_from(WORDS), st.integers(1, 99))

# LoRA paths below models/loras: never blank, absolute or with a `..` segment, and without `;` or `@`, which
# the CSV looks cell uses; commas, quotes, `#`, `: ` and non-ASCII test the quoting of both formats.
_LORA_SEGMENT = st.one_of(
    st.sampled_from(WORDS),
    st.sampled_from(("no", "2026", "#x", "a: b", "a,b", 'say "hi"', "café", "\U0001f600", "x y", "v2.1", "~")),
    st.from_regex(r"[A-Za-z0-9_-]{1,10}", fullmatch=True),
)
LORAS = st.builds(
    lambda folder, segments, extension: "/".join([*folder, *segments]) + extension,
    st.lists(st.sampled_from(("looks", "MINIMAX")), max_size=1),
    st.lists(_LORA_SEGMENT, min_size=1, max_size=2),
    st.sampled_from((".safetensors", "")),
)
LOOK_LISTS = st.none() | st.lists(
    st.builds(LoraRef, lora=LORAS, strength=st.floats(-2.0, 2.0)), max_size=C.MAX_LOOKS
).map(tuple)

DURATIONS = st.one_of(
    st.integers(5, 10).map(Fraction),
    st.floats(5.0, 10.0).map(lambda seconds: Fraction(str(seconds))),
    st.decimals(min_value=5, max_value=10, allow_nan=False, allow_infinity=False).map(Fraction),
)


def _dates(monday: datetime.date) -> st.SearchStrategy[str]:
    """A day of the week, or any day at all (outside the week, which only warns)."""
    last = min(6, (datetime.date.max - monday).days)
    in_week = st.integers(0, last).map(lambda days: monday + datetime.timedelta(days=days))
    return st.one_of(in_week, st.dates()).map(datetime.date.isoformat)


@st.composite
def _still(draw: st.DrawFn, persona: Persona, ident: str, kind: str, platform: str, monday: datetime.date) -> C.Post:
    fields: dict[str, Any] = {
        "location": draw(st.sampled_from([location.name for location in persona.locations])),
        "outfit": draw(st.sampled_from([outfit.name for outfit in persona.wardrobe])),
        "idea": draw(IDEAS),
        "candidates": draw(st.integers(C.CANDIDATES_MIN, C.CANDIDATES_MAX)),
        "looks": draw(LOOK_LISTS),
    }
    if kind in C.ASPECT_TYPES:
        fields["aspect"] = draw(st.sampled_from(C.ASPECTS))
    if kind == "carousel":
        fields["slides"] = draw(st.integers(C.SLIDES_MIN, C.SLIDES_MAX))
        fields["mode"] = draw(st.sampled_from((None, "regenerate")))
    return C.Post(ident, kind, draw(_dates(monday)), platform, **fields)


@st.composite
def _reel(draw: st.DrawFn, persona: Persona, ident: str, kind: str, platform: str, monday: datetime.date,
          stills: Sequence[C.Post]) -> C.Post:  # fmt: skip
    fields: dict[str, Any] = {"idea": draw(IDEAS), "looks": draw(LOOK_LISTS), "duration": draw(DURATIONS)}
    if persona.locations:
        fields["location"] = draw(st.none() | st.sampled_from([location.name for location in persona.locations]))
    if persona.wardrobe:
        fields["outfit"] = draw(st.none() | st.sampled_from([outfit.name for outfit in persona.wardrobe]))
    if kind == "source":
        pool = [post for post in stills if platform == "other" or post.platform == "instagram"]
        if not pool:  # an Instagram reel may not source an `other` Post (Req 16.11)
            platform, pool = "other", list(stills)
        source = draw(st.sampled_from(pool))
        fields["source_post"] = source.id
        fields["source_slide"] = draw(st.integers(1, source.slides))
    else:
        fields["mode"] = kind
    if kind != "external" and draw(st.booleans()):
        fields["dialogue"] = tuple(draw(st.lists(DIALOGUE_LINES, min_size=1, max_size=3)))
        fields["voice"] = draw(VOICES)
    return C.Post(ident, "reel", draw(_dates(monday)), platform, **fields)


@st.composite
def calendars(draw: st.DrawFn, persona: Persona, *, week: str | None = None, max_posts: int = 6) -> C.Calendar:
    """A Calendar of 1 to max_posts Posts that read_calendar(path, persona, root) accepts with no rejected
    Post, both as YAML and as CSV, once write_needed_files(root, persona, calendar) has run.

    week: the Week_Id, drawn from weeks() when None. Posts of every type and platform, ids from the whole
    alphabet (a--b, 0, 48 characters), idea texts from texts() (multi-line, emoji, `#`, `: `, commas and
    quotes), single-line texts() for dialogue lines, 0-8 look LoRAs or none, and durations as ints, floats
    and long decimals. Still Posts need a location and an outfit, so a persona without either gets only
    reels; source reels name a still Post of the calendar and a Slide in its range (never an `other` Post
    from an Instagram reel), reference reels need the persona's Reference_Pack. Dates are in the week or
    anywhere else, which only warns.
    """
    week = draw(weeks()) if week is None else week
    monday = C.week_monday(week)
    count = draw(st.integers(1, max_posts))
    ids = draw(st.lists(POST_IDS, min_size=count, max_size=count, unique=True))
    available = ["external"]
    if persona.locations and persona.wardrobe:
        available += [*C.STILL_TYPES, "source"]
    if persona.reference_pack:
        available.append("reference")
    kinds = draw(st.lists(st.sampled_from(available), min_size=count, max_size=count))
    if not any(kind in C.STILL_TYPES for kind in kinds):
        kinds = ["external" if kind == "source" else kind for kind in kinds]
    platforms = draw(st.lists(st.sampled_from(C.PLATFORMS), min_size=count, max_size=count))
    posts: list[C.Post | None] = [None] * count
    for index, kind in enumerate(kinds):  # still Posts first: a source reel needs their Slide counts
        if kind in C.STILL_TYPES:
            posts[index] = draw(_still(persona, ids[index], kind, platforms[index], monday))
    stills = [post for post in posts if post is not None]
    for index, kind in enumerate(kinds):
        if kind not in C.STILL_TYPES:
            posts[index] = draw(_reel(persona, ids[index], kind, platforms[index], monday, stills))
    return C.Calendar(week, tuple(post for post in posts if post is not None))


def needed_files(persona: Persona, calendar: C.Calendar) -> list[str]:
    """The files the Reader's file checks look for: voice clips, and the Location_Shots of the location of
    each reference reel, relative to the Influencer_Root."""
    locations = {location.name: location for location in persona.locations}
    found: list[str] = []
    for post in calendar.posts:
        if post.voice is not None:
            found.append(post.voice)
        if post.mode == "reference" and post.location in locations:
            found.extend(locations[post.location].shots)
    return list(dict.fromkeys(found))


def write_needed_files(root: Path, persona: Persona, calendar: C.Calendar) -> None:
    for relative in needed_files(persona, calendar):
        target = Path(root) / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")


# ---- faults("post") ----

PostData = dict[str, Any]


def _platform(post: PostData) -> Any:
    return post.get("platform", C.DEFAULT_PLATFORM)


def _names(persona: Persona, field: str) -> list[str]:
    return [item.name for item in (persona.locations if field == "location" else persona.wardrobe)]


def _fresh(taken: Iterable[object], stem: str) -> str:
    listed = set(taken)
    number = 1
    while f"{stem}-{number}" in listed:
        number += 1
    return f"{stem}-{number}"


def _case_variant(names: Sequence[str]) -> str | None:
    """A name that differs from one of names only in letter case, and is none of them (Req 5.5)."""
    return next((name.swapcase() for name in names if name.swapcase() != name and name.swapcase() not in names), None)


def _other_source(posts: Sequence[PostData], index: int) -> tuple[str, str | None] | None:
    """How a change to the reel at index alone makes it an Instagram reel sourcing an `other` Post: retarget
    it at an `other` still Post, or make an `other` reel whose source is `other` an Instagram one."""
    reel = posts[index]
    if _platform(reel) == "instagram":
        other = next((p for p in posts if p.get("type") in C.STILL_TYPES and _platform(p) == "other"), None)
        return None if other is None else ("retarget", other["id"])
    source = next((p for p in posts if p.get("id") == reel.get("source_post")), None)
    return ("platform", None) if source is not None and _platform(source) == "other" else None


@dataclass(frozen=True)
class PostFault:
    """One fault of the Property 7 catalogue: kind, and the value it writes or the variant it takes."""

    kind: str
    value: Any = None

    def applies(self, posts: Sequence[PostData], index: int, persona: Persona) -> bool:
        """Whether the fault fits the Post at index as a clean single fault: every other Post keeps what a
        fault-free read gives it (an id fault would unlink a reel that sources the Post, so it skips those)."""
        post, kind, value = posts[index], self.kind, self.value
        post_type = post.get("type")
        reel = post_type == "reel"
        if kind in ("type", "platform", "looks", "looks-strength"):
            return True
        if kind == "id":
            return post.get("id") not in {p.get("source_post") for p in posts}
        if kind in ("location", "outfit"):
            if value == "missing":
                return post_type in C.STILL_TYPES
            return value == "unknown" or _case_variant(_names(persona, kind)) is not None
        if kind == "slides":
            return post_type == "carousel"
        if kind == "duration":
            return reel
        if kind == "candidates":
            return post_type in C.STILL_TYPES
        if kind == "source":
            return reel and "source_post" in post
        if kind == "mode":
            return post_type in C.MODES and value not in C.MODES[post_type]
        if kind == "combination":
            return reel and {
                "source-and-reference": "source_post" in post,
                "source-and-external": "source_post" in post,
                "mode-and-source": "mode" in post and any(p.get("type") in C.STILL_TYPES for p in posts),
                "none": True,
                "reference-without-pack": not persona.reference_pack,
            }[value]
        if kind == "other-source":
            return reel and "source_post" in post and _other_source(posts, index) is not None
        if kind == "dialogue":
            if not reel or post.get("mode") == "external":
                return False
            lines, voice = "dialogue" in post, "voice" in post
            return {"drop-voice": voice, "drop-dialogue": lines, "lines-only": not (lines or voice),
                    "voice-only": not (lines or voice), "missing-voice-file": True}[value]  # fmt: skip
        raise ValueError(f"unknown fault kind {kind!r}")

    def apply(self, posts: list[PostData], index: int, persona: Persona) -> str:
        """Writes the fault into the Post at index and returns the field the Reader must name."""
        post, kind, value = posts[index], self.kind, self.value
        if kind in ("type", "platform", "id", "slides", "duration", "candidates"):
            if value is None:
                post.pop(kind, None)
            else:
                post[kind] = value
            return kind
        if kind in ("location", "outfit"):
            names = _names(persona, kind)
            if value == "missing":
                post.pop(kind, None)
            else:
                post[kind] = _case_variant(names) if value == "case" else _fresh(names, "nowhere")
            return kind
        if kind == "source":
            if value == "unknown-post":
                post["source_post"] = _fresh((p.get("id") for p in posts), "no-such-post")
                return "source_post"
            if value == "a-reel":
                post["source_post"] = post["id"]  # itself: a reel has no Slides
                return "source_post"
            source = next(p for p in posts if p.get("id") == post["source_post"])
            count = source["slides"] if source["type"] == "carousel" else 1
            post["source_slide"] = {"slide-zero": 0, "slide-after": count + 1, "slide-fraction": 1.5}[value]
            return "source_slide"
        if kind == "mode":
            post["mode"] = value
            return "mode"
        if kind == "combination":
            if value in ("source-and-reference", "source-and-external"):
                post["mode"] = value.rsplit("-", 1)[1]
            elif value == "mode-and-source":
                still = next(p for p in posts if p.get("type") in C.STILL_TYPES)
                post["source_post"], post["source_slide"] = still["id"], 1
            else:  # none, or mode reference while the Reference_Pack is empty
                for key in ("mode", "source_post", "source_slide"):
                    post.pop(key, None)
                if value == "reference-without-pack":
                    post["mode"] = "reference"
            return "mode"
        if kind == "other-source":
            how, target = _other_source(posts, index)  # type: ignore[misc]
            if how == "retarget":
                post["source_post"], post["source_slide"] = target, 1
            else:
                post["platform"] = "instagram"
            return "source_post"
        if kind == "dialogue":
            if value == "drop-voice":
                del post["voice"]
                return "voice"
            if value == "drop-dialogue":
                del post["dialogue"]
                return "dialogue"
            if value == "lines-only":
                post["dialogue"] = ["Hello there."]
                return "voice"
            if value == "voice-only":
                post["voice"] = "voice/unpaired.wav"
                return "dialogue"
            post.setdefault("dialogue", ["Hello there."])  # missing-voice-file: never written by the tests
            post["voice"] = "voice/not-recorded.wav"
            return "voice"
        if kind == "looks":
            post["looks"] = [f"looks/look-{number}.safetensors" for number in range(1, value + 1)]
            return "looks"
        if kind == "looks-strength":
            post["looks"] = [{"lora": "looks/strong.safetensors", "strength": value}]
            return "looks"
        raise ValueError(f"unknown fault kind {kind!r}")


# Every value can be written both as YAML and as a CSV cell, so Property 6 can use the catalogue too.
CATALOGUE: Mapping[str, tuple[Any, ...]] = {
    # unknown or missing type or platform (Req 5.1, 5.2)
    "type": (None, "", "bogus", "Single", "reels", " single"),
    "platform": ("facebook", "Instagram", "tiktok", "other "),
    # the id rules (Req 5.4): missing, empty, too long, other characters, `other`
    "id": (None, "", "a" * 49, "Mon-Cafe", "mon cafe", "mon_cafe", "café", "mon.cafe", "mon\ncafe", C.RESERVED_ID),
    # an unknown location or outfit, one differing only in case, or none on a still Post (Req 5.5)
    "location": ("unknown", "case", "missing"),
    "outfit": ("unknown", "case", "missing"),
    # slide count, duration and Candidate count out of range (Req 5.6, 5.7, 11.6)
    "slides": (None, 0, 1, 21, 25, -3, 2.5, "3.5", "many", 10**6, "1e400"),
    "duration": (None, 4, 4.99, 10.01, 11, 0, -8, "7,5", "abc", float("inf"), "1e999999999"),
    "candidates": (0, 5, -1, 1.5, "two", 100),
    # an unresolved reel source (Req 5.8)
    "source": ("unknown-post", "a-reel", "slide-zero", "slide-after", "slide-fraction"),
    # a mode the type does not accept (Req 5.20), and the reel mode combinations (Req 16.10)
    "mode": ("regenerate", "reference", "external", "bogus", "Regenerate"),
    "combination": ("source-and-reference", "source-and-external", "mode-and-source", "none",
                    "reference-without-pack"),  # fmt: skip
    # an Instagram reel sourcing an `other` Post (Req 16.11)
    "other-source": (None,),
    # dialogue without voice and the reverse, and a voice clip that does not exist (Req 16.12)
    "dialogue": ("drop-voice", "lines-only", "drop-dialogue", "voice-only", "missing-voice-file"),
    # look lists out of bounds (Req 8.10)
    "looks": (9, 12),
    "looks-strength": (2.5, -2.01, 3, -10, float("nan")),
}

# One strategy per kind, so a kind with few variants is drawn as often as one with many.
_KINDS = {kind: st.sampled_from([PostFault(kind, value) for value in values]) for kind, values in CATALOGUE.items()}


def faults(kind: str) -> st.SearchStrategy[PostFault]:
    """One fault of the named catalogue, its kind drawn first; only "post" (Property 7) is defined here."""
    if kind != "post":
        raise ValueError(f"faults({kind!r}): tests/strategies_calendar.py defines only the 'post' catalogue")
    return st.one_of(*_KINDS.values())


# ---- csv_text() ----


def cell_text(value: Any, column: str) -> str:
    """One value of calendar data as the CSV cell that holds it."""
    if value is None:
        return ""
    if column == "looks":
        items = [item if isinstance(item, str) else f"{item['lora']}@{item['strength']!r}" for item in value]
        return ";".join(items) if items else ";"
    if column == "dialogue":
        return "\n".join(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


def csv_text(document: Mapping[str, Any], *, bom: bool = False, blank_rows: Iterable[int] = ()) -> str:
    """document ({week, posts} as calendar_data gives it, faults included) as a CSV Content_Calendar: every
    column in C.COLUMNS order, the week in each row, and before, between or after the Post rows a blank row
    for each of blank_rows (its position taken modulo the rows so far + 1; odd values give an empty line,
    even ones a row of empty cells). bom puts a UTF-8 byte-order mark first."""
    header = list(C.COLUMNS)
    rows: list[list[str]] = [
        [document["week"] if column == "week" else cell_text(post.get(column), column) for column in header]
        for post in document["posts"]
    ]
    for position in blank_rows:
        rows.insert(position % (len(rows) + 1), [] if position % 2 else [""] * len(header))
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    return ("\ufeff" if bom else "") + buffer.getvalue()
