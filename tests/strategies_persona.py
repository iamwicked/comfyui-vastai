"""Hypothesis strategies for the Persona_File (task 8.5; design §M1, Properties 1 and 2).

- personas(): a random valid Persona. Every free-text field comes from texts(), so the round trip meets
  emoji, `#`, `: `, `no`, `2026` and multi-line text, and every list field spans its allowed range: 0-5
  references, 0-8 LoRAs in each look list, 0-6 locations (below, inside and above the recommended 3-4).
- faults("persona"): the Property 2 fault catalogue. Fault.apply() writes one fault into the plain data of
  a valid Persona_File (persona.persona_data) and returns the field the Reader must name.
- independent(): no two faults of a set touch the same field, or a field and one inside it.

A module of its own so tests/strategies.py stays untouched; texts() comes from there.
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from hypothesis import strategies as st

from helpers.harness import REPO
from strategies import WORDS, texts

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import persona as P  # noqa: E402
from influencer.safety import default_guard, words  # noqa: E402

GUARD = default_guard()  # the Persona_Reader's Safety_Guard (Req 4.4)
NAN = float("nan")


def is_safe(text: str) -> bool:
    return not GUARD.check_text(text)


# ---- personas() ----


def _text(*, blank: bool = True, single_line: bool = False, max_size: int = 40) -> st.SearchStrategy[str]:
    """Free text from texts() that the Safety_Guard passes; blank=False also makes it non-blank."""
    strategy = texts(single_line=single_line, max_size=max_size)
    if not blank:
        strategy = strategy.filter(lambda text: text.strip() != "")
    return strategy.filter(is_safe)


def _is_paragraph(text: str) -> bool:
    """Decision 16: non-blank, no leading or trailing whitespace, no blank line."""
    return text != "" and text == text.strip() and all(line.strip() for line in text.splitlines())


_LINE = _text(blank=False, single_line=True, max_size=20)  # language, the Ollama models
_IDENTITY = texts(max_size=60).map(str.strip).filter(_is_paragraph).filter(is_safe)
_DISCLOSURE = (
    texts(single_line=True, max_size=40).map(lambda text: text.replace("#", "")).filter(lambda t: t.strip() != "")
).filter(is_safe)
_SLUG = st.from_regex(r"[a-z0-9-]{1,32}", fullmatch=True).filter(is_safe)
_TRIGGER_WORD = st.from_regex(r"[a-z0-9_]{4,32}", fullmatch=True).filter(is_safe)

# Path segments: plain words, YAML-ambiguous names, spaces and non-ASCII; never "", "." or "..".
_SEGMENT = st.one_of(
    st.sampled_from(WORDS),
    st.sampled_from(("no", "2026", "#x", "a: b", "on", "null", "~", "café", "\U0001f600", "x y", "v2.1")),
    st.from_regex(r"[A-Za-z0-9_-]{1,10}", fullmatch=True),
)


def _paths(folders: tuple[str, ...], extensions: tuple[str, ...]) -> st.SearchStrategy[str]:
    """Relative paths without a `..` segment, sometimes below one of folders."""

    @st.composite
    def path(draw: st.DrawFn) -> str:
        segments = draw(st.lists(_SEGMENT, min_size=1, max_size=3))
        if draw(st.booleans()):
            segments.insert(0, draw(st.sampled_from(folders)))
        return "/".join(segments) + draw(st.sampled_from(extensions))

    return path().filter(is_safe)


_IMAGE = _paths(("refs",), (".png", ".jpg", ".webp", ""))
_LUT = _paths(("grade",), (".cube", ""))
_SHOT = _paths(("locations",), (".jpg", ".png"))
_LORA = _paths(("characters", "looks", "MINIMAX"), (".safetensors",))

_CHARACTER_LORA = st.builds(P.LoraRef, lora=_LORA, strength=st.floats(0.0, P.CHARACTER_STRENGTH_MAX, exclude_min=True))
_LOOK = st.builds(P.LoraRef, lora=_LORA, strength=st.floats(-P.LOOK_STRENGTH_LIMIT, P.LOOK_STRENGTH_LIMIT))
_LOOK_LIST = st.lists(_LOOK, max_size=P.MAX_LOOKS).map(tuple)
_LOOK_STACK = st.builds(P.LookStack, instagram=_LOOK_LIST, other=_LOOK_LIST)
_LOCATION = st.builds(
    P.Location,
    name=_text(blank=False, max_size=20),
    description=_text(blank=False),
    shots=st.lists(_SHOT, max_size=3).map(tuple),
)
_OUTFIT = st.builds(P.Outfit, name=_text(blank=False, max_size=20), description=_text(blank=False))
_TEXT_LIST = st.lists(_text(), max_size=4).map(tuple)

# Built once: a strategy made inside personas() would be built and validated again for every example.
_FIELDS: dict[str, st.SearchStrategy[Any]] = {
    "slug": _SLUG,
    "name": _text(blank=False),
    "age": st.integers(P.AGE_MIN, P.AGE_MAX),
    "voice": _text(blank=False, max_size=80),
    "hero_image": _IMAGE,
    "grade": st.builds(P.Grade, lut=_LUT, grain=st.none() | st.floats(0.0, 1.0)),
    "niche": st.none() | _text(),
    "language": st.just(P.DEFAULT_LANGUAGE) | _LINE,
    "themes": _TEXT_LIST,
    "reference_pack": st.lists(_IMAGE, max_size=P.MAX_REFERENCES).map(tuple),
    "sharp_decode": st.booleans(),
    "locations": st.lists(_LOCATION, max_size=6, unique_by=lambda item: item.name).map(tuple),
    "wardrobe": st.lists(_OUTFIT, max_size=5, unique_by=lambda item: item.name).map(tuple),
    "signature_items": _TEXT_LIST,
    "character_loras": st.builds(
        P.CharacterLoras, stills=st.none() | _CHARACTER_LORA, video=st.none() | _CHARACTER_LORA
    ),
    "looks": st.builds(P.Looks, stills=_LOOK_STACK, video=_LOOK_STACK),
    "captions": st.builds(
        P.Captions,
        rules=st.none() | _text(max_size=80),
        disclosure=st.none() | _DISCLOSURE,
        hashtag_limit=st.integers(0, 12),
        flagged_terms=st.lists(_text(), max_size=5).map(tuple),
    ),
    "ollama": st.builds(
        P.Ollama,
        instagram=st.just(P.DEFAULT_OLLAMA_INSTAGRAM) | _LINE,
        other=st.just(P.DEFAULT_OLLAMA_OTHER) | _LINE,
    ),
}


@st.composite
def personas(draw: st.DrawFn) -> P.Persona:
    """A valid Persona (design §M1) with every optional field sometimes left out, sometimes given."""
    trigger_word = draw(_TRIGGER_WORD)
    identity = draw(_IDENTITY)
    if identity in trigger_word:  # Decision 16; rare, as the identity would have to be a few of [a-z0-9_]
        identity = trigger_word.upper() + "!"
    fields = {name: draw(strategy) for name, strategy in _FIELDS.items()}
    return P.Persona(**fields, identity=identity, trigger_word=trigger_word)


# ---- faults("persona") ----

_TOKEN = re.compile(r"\[\+\]|[^.\[\]]+")
_APPEND = "[+]"


def _join(parent: str, key: str) -> str:
    return f"{parent}.{key}" if parent else key


def _fresh_name(items: list[Any], stem: str) -> str:
    """stem-<n> for the first n no item of items is named."""
    taken = {item.get("name") for item in items if isinstance(item, dict)}
    number = 1
    while f"{stem}-{number}" in taken:
        number += 1
    return f"{stem}-{number}"


def _template(generic: str, parent: Any, next_token: str | None) -> Any:
    """A valid value for a node a fault needs but the document lacks. generic is the node's field path with
    "[+]" for appended items; parent is the list an appended item goes into."""
    if generic in ("character_loras.stills", "character_loras.video"):
        return {"lora": "characters/extra.safetensors", "strength": 1.0}
    if generic == "locations[+]":
        return {"name": _fresh_name(parent, "extra"), "description": "a quiet room with pale walls"}
    if generic == "wardrobe[+]":
        return {"name": _fresh_name(parent, "extra"), "description": "a plain grey wool coat"}
    if generic.startswith("looks.") and generic.endswith(_APPEND):
        return {"lora": "looks/extra.safetensors", "strength": 0.5}
    return [] if next_token == _APPEND else {}


@dataclass(frozen=True, order=True)
class Fault:
    """One fault of the Property 2 catalogue.

    field is the field it breaks, as the Reader names fields, except that "[+]" stands for an item the
    fault appends to that list, so no fault changes an item the persona already has. value is the JSON of
    what the fault writes. kind "missing" deletes the field, "duplicate" appends two items sharing one
    fresh name, "identity-in-trigger" writes identity and trigger_word, and every other kind writes value.
    also lists the other fields a fault writes."""

    kind: str
    field: str
    value: str = "null"
    also: tuple[str, ...] = ()

    @property
    def targets(self) -> tuple[str, ...]:
        return (self.field, *self.also)

    def apply(self, document: dict[str, Any]) -> str:
        """Writes the fault into document and returns the field the Reader must name, item numbers
        filled in."""
        if self.kind == "identity-in-trigger":
            document["trigger_word"] = "maralind"
            document["identity"] = "mara"
            return "identity"
        tokens = _TOKEN.findall(self.field)
        if self.kind == "duplicate":  # "<list>[+].name"
            items = document.setdefault(tokens[0], [])
            name = _fresh_name(items, "twin")
            for _ in range(2):
                item = _template(f"{tokens[0]}[+]", items, None)
                item["name"] = name
                items.append(item)
            return f"{tokens[0]}[{len(items)}].name"
        node: Any = document
        where = generic = ""
        for index, token in enumerate(tokens[:-1]):
            if token == _APPEND:
                generic += _APPEND
                node.append(_template(generic, node, tokens[index + 1]))
                where += f"[{len(node)}]"
                node = node[-1]
                continue
            generic, where = _join(generic, token), _join(where, token)
            if token not in node:
                node[token] = _template(generic, None, tokens[index + 1])
            node = node[token]
        last = tokens[-1]
        if last == _APPEND:
            node.append(json.loads(self.value))
            return f"{where}[{len(node)}]"
        if self.kind == "missing":
            node.pop(last, None)
        else:
            node[last] = json.loads(self.value)
        return _join(where, last)


def _faults(kind: str, fields: Iterable[str], values: Iterable[object] = (None,)) -> list[Fault]:
    listed = list(values)
    return [Fault(kind, field, json.dumps(value)) for field in fields for value in listed]


def _each(kind: str, table: Mapping[str, Iterable[object]]) -> list[Fault]:
    return [fault for field, values in table.items() for fault in _faults(kind, (field,), values)]


REQUIRED_TEXT = ("slug", "name", "voice", "identity", "trigger_word", "hero_image", "grade.lut")
BLANKS = ("", " ", "\t", "\n", " \u3000\n")

# Values of another type than the format's, including null (no field takes null).
WRONG_TYPES: dict[str, tuple[object, ...]] = {
    "version": ("1", True, 1.0),
    "slug": (42, ["mara-lind"], {"a": "b"}, True, None),
    "name": (7, ["Mara"], True),
    "niche": (["travel"], 3, None),
    "language": (5, ["English"]),
    "voice": (["warm"], 3.5, None),
    "themes": ("morning rituals", 3, {"a": "b"}, None),
    "themes[+]": (5, ["nested"], {"a": "b"}, True, None),
    "identity": (["a"], 12),
    "trigger_word": (1234, ["maralind"]),
    "hero_image": (["refs/hero.png"], 5, {"path": "refs/hero.png"}),
    "reference_pack": ("refs/a.png", {"a": "b"}, None),
    "reference_pack[+]": (3, ["refs/a.png"]),
    "grade": ("grade/mara.cube", ["grade/mara.cube"], None),
    "grade.lut": (3, ["grade/mara.cube"]),
    "grade.grain": ("0.2", True, [0.2], None),
    "sharp_decode": ("yes", "false", 1, 0, "on", None),
    "locations": ("corner-cafe", {"name": "corner-cafe"}),
    "locations[+]": ("corner-cafe", 5, ["corner-cafe"]),
    "locations[+].name": (5, ["corner-cafe"]),
    "locations[+].description": (5, ["a café"]),
    "locations[+].shots": ("locations/a.jpg", 5),
    "wardrobe": ("linen-set",),
    "wardrobe[+]": ("linen-set", 5),
    "signature_items": ("film camera",),
    "signature_items[+]": (5, ["film camera"]),
    "character_loras": ("characters/x.safetensors", ["characters/x.safetensors"]),
    "character_loras.stills": ("characters/x.safetensors", ["characters/x.safetensors"], 1.0),
    "character_loras.stills.lora": (5, ["characters/x.safetensors"]),
    "character_loras.stills.strength": ("1.0", True, [1.0]),
    "character_loras.video.strength": ("1.0", False),
    "looks": ("looks/x.safetensors", ["looks/x.safetensors"]),
    "looks.stills": ("looks/x.safetensors", ["looks/x.safetensors"]),
    "looks.video.other": ("looks/x.safetensors", {"lora": "looks/x.safetensors"}),
    "looks.stills.instagram[+]": (5, ["looks/x.safetensors"], True),
    "looks.video.instagram[+].strength": ("0.5", True),
    "captions": ("First person.", ["First person."]),
    "captions.rules": (["First person."], 5),
    "captions.hashtag_limit": ("5", 2.5, True, 5.0),
    "captions.flagged_terms": ("cure",),
    "captions.flagged_terms[+]": (5, ["cure"]),
    "ollama": ("qwen3:8b",),
    "ollama.instagram": (8, ["qwen3:8b"]),
    "ollama.other": (3.5,),
}

# Fields the format does not define, at every level; looks.edit is one (there is no edit look stack).
UNKNOWN_FIELDS: dict[str, tuple[object, ...]] = {
    "mood": ("warm",),
    "grade.contrast": (0.5,),
    "looks.edit": ({"instagram": [], "other": []}, ["looks/x.safetensors"]),
    "looks.stills.stories": ([],),
    "captions.tone": ("dry",),
    "ollama.video": ("qwen3:8b",),
    "character_loras.audio": ({"lora": "characters/x.safetensors"},),
    "character_loras.stills.weight": (1.0,),
    "locations[+].colour": ("ochre",),
    "wardrobe[+].shots": (["wardrobe/linen-set.jpg"],),
    "looks.video.other[+].weight": (0.5,),
}

CATALOGUE: tuple[Fault, ...] = (
    # missing and blank required fields, at the top and inside list items and LoRA mappings (Req 4.12)
    *_faults("missing", (*REQUIRED_TEXT, "age", "locations[+].name", "locations[+].description", "wardrobe[+].name",
                         "wardrobe[+].description", "character_loras.stills.lora", "character_loras.video.lora",
                         "looks.stills.other[+].lora")),
    *_faults("blank", (*REQUIRED_TEXT, "language", "locations[+].name", "locations[+].description", "wardrobe[+].name",
                       "wardrobe[+].description", "reference_pack[+]", "locations[+].shots[+]", "captions.disclosure",
                       "ollama.instagram", "ollama.other", "character_loras.stills.lora",
                       "looks.video.instagram[+].lora", "looks.stills.instagram[+]"), BLANKS),
    # wrong types and unknown fields (Req 4.12)
    *_each("wrong-type", WRONG_TYPES),
    *_each("unknown", UNKNOWN_FIELDS),
    # the age out of range or not a YAML int (Req 4.3)
    *_faults("age", ("age",), (24, 0, -1, 100, 150, 10**30)),
    *_faults("age-type", ("age",), ("29", 29.5, 30.0, True, False, [29], {"years": 29}, None)),
    # a bad Trigger_Word or slug (Req 4.5, 4.6)
    *_faults("trigger-word", ("trigger_word",),
             ("abc", "a" * 33, "Mara", "mara-lind", "mara lind", "maré", "mara.lind")),
    *_faults("slug", ("slug",), ("a" * 33, "Mara", "mara_lind", "mara lind", "maré", "mara.lind", "mara/lind")),
    # more than 5 references (Req 4.8) and duplicate names (Req 4.13)
    *_faults("references", ("reference_pack",), [[f"refs/ref-{i}.png" for i in range(1, n + 1)] for n in (6, 7, 9)]),
    Fault("duplicate", "locations[+].name"),
    Fault("duplicate", "wardrobe[+].name"),
    # strengths and look lists out of bounds (Req 8.10)
    *_faults("strength", ("character_loras.stills.strength", "character_loras.video.strength"),
             (0, 0.0, -0.5, 2.0001, 3, 1e9, NAN)),
    *_faults("strength", ("looks.stills.instagram[+].strength", "looks.video.other[+].strength"),
             (-2.0001, 2.5, -10, 100, NAN)),
    *_faults("look-list",
             ("looks.stills.instagram", "looks.stills.other", "looks.video.instagram", "looks.video.other"),
             [[f"looks/look-{i}.safetensors" for i in range(1, n + 1)] for n in (9, 12)]),
    # the other §M1 rules: Decision 16, paths, ranges, single lines
    *_faults("identity", ("identity",), ("A woman\n\nwith freckles", "A woman\n \t\nwith freckles", " A woman",
                                          "A woman ", "A woman\n", "\u3000A woman")),
    Fault("identity-in-trigger", "identity", also=("trigger_word",)),
    *_faults("path", ("hero_image", "grade.lut", "reference_pack[+]", "locations[+].shots[+]",
                      "character_loras.stills.lora", "looks.video.instagram[+].lora", "looks.stills.other[+]"),
             ("/abs/file.png", "../file.png", "refs/../../file.png")),
    *_faults("range", ("grade.grain",), (-0.1, 1.5, 2, NAN)),
    *_faults("range", ("captions.hashtag_limit",), (-1, -5)),
    *_faults("range", ("version",), (0, 2)),
    *_faults("single-line", ("language", "ollama.instagram", "captions.disclosure"),
             ("two\nlines", "two\u2028lines", "a trailing break\n")),
    *_faults("disclosure", ("captions.disclosure",), ("AI-generated #ad", "#ai")),
)  # fmt: skip

# A Safety_Guard term (Req 4.4): each committed minor term and some numeric ages under 25, written into a
# string of each kind of field, or as a mapping key.
AGES = ("19 year old", "22yo", "12 yrs", "24 years old", "aged 19")
TERMS = tuple(term for term_list in GUARD.term_lists for term in term_list.terms) + AGES
SAFETY_FIELDS: dict[str, tuple[str, ...]] = {
    "text": ("name", "niche", "voice", "identity", "language", "themes[+]", "signature_items[+]", "locations[+].name",
             "locations[+].description", "wardrobe[+].description", "captions.rules", "captions.disclosure",
             "captions.flagged_terms[+]", "ollama.other"),
    "slug": ("slug",),
    "trigger": ("trigger_word",),
    "path": ("hero_image", "grade.lut", "reference_pack[+]", "locations[+].shots[+]"),
    "lora": ("character_loras.video.lora", "looks.stills.other[+].lora", "looks.video.instagram[+]"),
    "key": ("", "grade", "looks", "captions", "locations[+]"),  # the parent of a new key
}  # fmt: skip


def _carriers(term: str) -> dict[str, str]:
    """term written as a value of each kind of field, valid for that field in every other way."""
    parts = words(term)
    return {
        "text": f"with {term} here",
        "slug": "-".join(parts),
        "trigger": "_".join(parts) + "_look",
        "path": f"refs/{'-'.join(parts)}.png",
        "lora": f"looks/{'-'.join(parts)}.safetensors",
        "key": "_".join(parts),
    }


# Only the carriers the Safety_Guard really rejects: "aged-19", for one, is no age for it.
CARRIERS: dict[str, tuple[str, ...]] = {
    kind: tuple(dict.fromkeys(c for term in TERMS if GUARD.check_text(c := _carriers(term)[kind])))
    for kind in SAFETY_FIELDS
}
assert all(len(found) >= len(TERMS) // 2 for found in CARRIERS.values()), {k: len(v) for k, v in CARRIERS.items()}


def _safety_fault(field: str, kind: str, carrier: str) -> Fault:
    if kind == "key":
        return Fault("safety-key", _join(field, carrier), json.dumps("x"))
    return Fault("safety", field, json.dumps(carrier))


def _safety_faults(kinds: Iterable[str]) -> st.SearchStrategy[Fault]:
    pairs = [(field, kind) for kind in kinds for field in SAFETY_FIELDS[kind]]
    carriers = {kind: st.sampled_from(CARRIERS[kind]) for _, kind in pairs}

    @st.composite
    def fault(draw: st.DrawFn) -> Fault:
        field, kind = draw(st.sampled_from(pairs))
        return _safety_fault(field, kind, draw(carriers[kind]))

    return fault()


# One strategy per kind of fault, so a kind with few entries (the Reference_Pack limit, duplicates) is drawn
# as often as one with many (wrong types, blanks).
_KINDS: dict[str, st.SearchStrategy[Fault]] = {
    kind: st.sampled_from([fault for fault in CATALOGUE if fault.kind == kind])
    for kind in dict.fromkeys(fault.kind for fault in CATALOGUE)
}
_KINDS["safety"] = _safety_faults(kind for kind in SAFETY_FIELDS if kind != "key")
_KINDS["safety-key"] = _safety_faults(("key",))


def faults(kind: str) -> st.SearchStrategy[Fault]:
    """One fault of the named catalogue, its kind drawn first; only "persona" (Property 2) is defined here."""
    if kind != "persona":
        raise ValueError(f"faults({kind!r}): tests/strategies_persona.py defines only the 'persona' catalogue")
    return st.one_of(*_KINDS.values())


def _nested(a: str, b: str) -> bool:
    """a and b are the same field, or one holds the other."""
    return a == b or b.startswith((a + ".", a + "[")) or a.startswith((b + ".", b + "["))


def independent(fault_set: Iterable[Fault]) -> bool:
    """No two faults touch the same field, or a field and one inside it, so none can undo another."""
    listed = sorted(fault_set)
    return not any(
        _nested(a, b) for i, first in enumerate(listed) for second in listed[i + 1 :]
        for a in first.targets for b in second.targets
    )  # fmt: skip
