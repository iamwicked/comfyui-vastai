"""Hypothesis strategies and fixtures for the Prompt_Assembler and the caption rules (task 13; design §A4,
§A10, Properties 12, 13, 14 and 23).

- identity_cases(): a valid Persona, location and outfit descriptions and a scene text, with the
  Identity_Paragraph spliced into the scene and into each description 0-3 times at drawn insertion points,
  and now and then split across the join of the two descriptions (Property 12)
- disclosure_cases(): a caption and a disclosure line, the caption pre-seeded with the line on 0-3 lines
  and ending in blank lines now and then (Property 13)
- instagram_cases(): a caption, a disclosure line or None, 0-8 hashtags and a hashtag limit 0-12, the full
  caption text aimed at 2,190-2,210 UTF-16 code units with BMP characters and astral emoji (Property 14)
- EDIT_INSTRUCTION, edit_fixture() and edit_template_names(): the design's fixed Edit instruction (§C7), the
  minimal edit-kind Render_Template in tests/fixtures/edit-fixture.json holding it, and the edit-kind
  templates committed in config/influencer/templates/ (Property 23)

A module of its own so tests/strategies.py stays untouched; texts() comes from there and personas() from
strategies_persona.py.
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

from hypothesis import strategies as st

from helpers.harness import REPO
from strategies import texts
from strategies_persona import is_safe, personas

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import captions as C  # noqa: E402
from influencer import templates as T  # noqa: E402
from influencer.persona import Persona  # noqa: E402

# ---- identity_cases() (Property 12) ----


def is_identity_paragraph(text: str) -> bool:
    """Decision 16 as the Persona_Reader applies it: non-empty, no leading or trailing whitespace, no blank
    line."""
    return text != "" and text == text.strip() and all(line.strip() for line in text.splitlines())


# Identity_Paragraphs mixed in with personas()' own: realistic prose, one that overlaps itself ("aaa" holds
# "aa" twice), ones with a space that can span the join of the location and outfit descriptions, and one
# with a line break. Each is valid with any Trigger_Word it is not a substring of.
SAMPLE_IDENTITIES = (
    "A woman in her early thirties with freckles, auburn hair and green eyes",
    "aa",
    "ab ab",
    "x: y\nz \U0001f600",
    "Mara Lind",
)
assert all(is_identity_paragraph(text) and is_safe(text) for text in SAMPLE_IDENTITIES)


@st.composite
def spliced(draw: st.DrawFn, text: str, piece: str) -> str:
    """text with piece inserted 0-3 times, each at an insertion point drawn from the text so far, so a later
    copy can land inside an earlier one."""
    for _ in range(draw(st.integers(0, 3))):
        at = draw(st.sampled_from(range(len(text) + 1)))
        text = text[:at] + piece + text[at:]
    return text


def _description(persona: Persona, kind: str) -> st.SearchStrategy[str | None]:
    """None (the Post names none), any text, or one of the persona's own descriptions."""
    own = [item.description for item in (persona.locations if kind == "location" else persona.wardrobe)]
    options = [st.none(), texts()] + ([st.sampled_from(own)] if own else [])
    return st.one_of(options)


@st.composite
def identity_cases(draw: st.DrawFn) -> tuple[Persona, str | None, str | None, str]:
    """(persona, location description, outfit description, scene text); see the module docstring."""
    persona = draw(personas())
    paragraph = draw(st.one_of(st.just(persona.identity), st.sampled_from(SAMPLE_IDENTITIES)))
    if paragraph in persona.trigger_word:  # Decision 16 forbids it; keep personas()' own
        paragraph = persona.identity
    persona = dataclasses.replace(persona, identity=paragraph)
    location = draw(_description(persona, "location"))
    outfit = draw(_description(persona, "outfit"))
    location = None if location is None else draw(spliced(location, paragraph))
    outfit = None if outfit is None else draw(spliced(outfit, paragraph))
    spaces = [index for index, char in enumerate(paragraph) if char == " "]
    if spaces and draw(st.booleans()):  # P split at a space: "… P[:k]" + " " + "P[k+1:] …" joins to P
        k = draw(st.sampled_from(spaces))
        location = (location or "") + paragraph[:k]
        outfit = paragraph[k + 1 :] + (outfit or "")
    scene = draw(spliced(draw(texts()), paragraph))
    return persona, location, outfit, scene


# ---- disclosure_cases() (Property 13) ----

DISCLOSURES = texts(single_line=True, max_size=40).map(lambda text: text.replace("#", ""))
BLANK_LINES = ("", " ", "\t", "\r", "\u3000")


@st.composite
def disclosure_cases(draw: st.DrawFn) -> tuple[str, str]:
    """(caption, disclosure line): a texts() caption with the line, or the line with other whitespace
    around it, put on 0-3 lines of its own, and 0-3 trailing blank lines."""
    disclosure = draw(DISCLOSURES)
    lines = draw(texts()).split("\n")
    seeds = (disclosure, disclosure.strip(), f" {disclosure}\t", disclosure + "\r")
    for _ in range(draw(st.integers(0, 3))):
        lines.insert(draw(st.integers(0, len(lines))), draw(st.sampled_from(seeds)))
    lines += draw(st.lists(st.sampled_from(BLANK_LINES), max_size=3))
    return "\n".join(lines), disclosure


# ---- instagram_cases() (Property 14) ----

BOUNDARY = C.INSTAGRAM_MAX_UTF16
_BMP = ("a", "Z", "é", "ß", "中", "—", "\u0301", "#")  # one UTF-16 code unit each, none of them whitespace
_ASTRAL = ("\U0001f600", "\U0001f525", "\U0001d49c", "\U0001f469\U0001f3fd\u200d\U0001f4bb")  # 2, 2, 2 and 7
_PATTERN = st.lists(st.sampled_from(_BMP + _ASTRAL), min_size=1, max_size=6).map("".join)
HASHTAGS = st.one_of(
    st.from_regex(r"#[a-z0-9_]{1,12}", fullmatch=True),
    texts(single_line=True, max_size=8).map(lambda text: "#" + text),  # spaces, "#", emoji, or "#" alone
    texts(single_line=True, max_size=8),  # no "#" at the front, so it counts as no hashtag
)


def _units(text: str) -> int:
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def _filler(pattern: str, units: int) -> str:
    """pattern repeated, then "a"s, to exactly units UTF-16 code units."""
    text = pattern * (units // _units(pattern))
    return text + "a" * (units - _units(text))


@st.composite
def instagram_cases(draw: st.DrawFn) -> tuple[str, str | None, list[str], int]:
    """(caption, disclosure line or None, hashtags, hashtag limit), the full caption text 2,190 to 2,210
    UTF-16 code units long, often exactly 2,200 or 2,201.

    The caption is a filler line, then a texts() caption. The filler is non-blank and never the disclosure
    line, so it adds exactly its own length plus one "\\n" to the full text of the rest; that is how its
    length is aimed."""
    disclosure = draw(st.none() | DISCLOSURES)
    hashtags = draw(st.lists(HASHTAGS, max_size=8))
    limit = draw(st.integers(0, 12))
    rest = draw(texts(max_size=40))
    target = draw(st.one_of(st.sampled_from((BOUNDARY, BOUNDARY + 1)), st.integers(BOUNDARY - 10, BOUNDARY + 10)))
    need = target - _units(C.full_text(rest, hashtags, disclosure)) - 1
    caption = _filler(draw(_PATTERN), max(need, 1)) + "\n" + rest
    return caption, disclosure, hashtags, limit


# ---- the Edit_Template (Property 23) ----

# The fixed instruction of design §C7, the `edit` template's <EDIT_INSTRUCTION>. "Picture 1" is the label the
# encoder gives the reference image (image1).
EDIT_INSTRUCTION = (
    "Keep the person, the outfit and the location of Picture 1 exactly as they are: the same face, hair, body, "
    "skin, clothing, jewellery, background and light. Show another moment of the same scene, taken with the "
    "same phone camera:"
)
EDIT_PROMPT = EDIT_INSTRUCTION + " {{scene}}"  # the positive encoder's prompt (node 8 of §C7)
FIXTURES = REPO / "tests" / "fixtures"
EDIT_FIXTURE = "edit-fixture"  # tests/fixtures/edit-fixture.json


def edit_fixture() -> T.Template:
    """tests/fixtures/edit-fixture.json, read by the template loader: a minimal edit-kind Render_Template in
    the shape of §C7's Qwen-Image-Edit-2511 graph, with its node ids. It holds the text encoder and VAE
    loaders, the reference image, and a positive and an empty negative TextEncodeQwenImageEditPlus, the
    positive one's prompt EDIT_PROMPT. It is only ever filled, never run, so the positive encoder stands in
    as the output node."""
    return T.load_template(EDIT_FIXTURE, FIXTURES)


def edit_template_names(folder: Path = T.TEMPLATES_DIR) -> list[str]:
    """The names of the Render_Templates in folder whose meta.kind is edit, in name order. A file that is
    not JSON is left to the template loader and its tests."""
    names = []
    for path in sorted(Path(folder).glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        meta = document.get("meta") if isinstance(document, dict) else None
        if isinstance(meta, dict) and meta.get("kind") == "edit":
            names.append(path.stem)
    return names
