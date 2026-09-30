"""Hypothesis strategies for Render_Templates and their values (task 12.5, design Properties 17-22).

- templates(): small valid Render_Templates: every placeholder type, whole-value and in-string placements,
  0-2 lora_list placeholders with their consumers, node ids below 900. Each is built as a JSON document and
  read by templates.parse_template, so a generator bug fails loudly as a ValidationError.
- values_for(t): a valid value for every placeholder t declares, with text seeded with {{seed}}-like
  strings; values_for(t, errors=kinds) draws (values, injected) instead, the Property 22 errors put in.
- lora_entries(): one valid lora_list entry {"name": ..., "strength": ...}.
- make_document(), document_of() and names_used(): a minimal valid document around a prompt, a
  Template's document, and the placeholder names a template needs values for.

The real Render_Templates arrive with the render stage (task 18), so the Filler's tests run on these. The
file sits beside strategies.py, whose texts() it builds on.
"""
from __future__ import annotations

import copy
import math
import re
import sys
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from hypothesis import strategies as st

from helpers.harness import REPO
from strategies import texts

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import templates as T  # noqa: E402

# The design's placeholder pattern (§A3), written out here so the tests do not lean on the module's own.
PLACEHOLDER = re.compile(r"\{\{([a-z0-9_]+)\}\}")
SEED_MIN, SEED_MAX = 0, 4_294_967_295
MAX_LORAS = 16

CLASSES = (
    "UNETLoader", "CLIPLoader", "CLIPTextEncode", "EmptyLatentImage", "KSampler", "VAEDecode", "ImageCrop",
    "ImageScale", "LoadImage", "SaveImage",
)  # fmt: skip
LOADERS = ("LoraLoaderModelOnly", "LoraLoaderModelOnly", "LoraLoaderModelOnlyV2")
PACKS = ("core", "core", "RES4LYF", "ComfyUI-DeGrid")
INPUT_NAMES = (
    "text", "seed", "width", "height", "model", "clip", "vae", "image", "samples", "steps", "cfg", "denoise",
    "filename_prefix", "x", "y", "switch",
)  # fmt: skip
PLACEHOLDER_WORDS = (
    "prompt", "scene", "width", "height", "latent_width", "latent_height", "crop_x", "crop_y", "output_prefix",
    "reference_image", "upscale", "use_ref_1", "loras", "looks",
)  # fmt: skip
SIZES = (
    {},
    {
        "3:4": {"latent": [1440, 1920], "output": [1440, 1920], "crop": [0, 0]},
        "4:5": {"latent": [1536, 1920], "output": [1536, 1920], "crop": [0, 0]},
        "9:16": {"latent": [1088, 1920], "output": [1080, 1920], "crop": [4, 0]},
    },
    {"3:4": {"output": [864, 1152]}, "4:5": {"output": [896, 1120]}},
)
MODELS = (
    [],
    [
        {"kind": "diffusion", "folder": "diffusion_models", "name": "krea2_raw_bf16.safetensors"},
        {"kind": "lora", "folder": "loras", "name": "krea2_turbo_lora_rank_64_bf16.safetensors"},
    ],
)

# Double braces around text that is not a placeholder name: plain text, which filling leaves alone.
NEAR_MISSES = (
    "{{Seed}}", "{{ seed }}", "{seed}", "{{}}", "{{a-b}}", "{{{", "}}}", "{{seed}", "{{seed", "seed}}", "{{é}}",
)  # fmt: skip
# Supplied text that JSON escapes, or that re.sub would read as a group reference in a replacement string.
TRICKY = ('"', "\\", "\n", "\r\n", "\t", "\U0001f600", "\\1", "\\g<0>", "$prev", "{{", "}}", "{{seed}}")


def _braced(name: str) -> str:
    return "{{" + name + "}}"


def _not_seed(name: str) -> bool:
    return name != "seed" and not name.endswith("_seed")  # a seed has fixed bounds (Req 7.1); added apart


placeholder_names = st.one_of(
    st.sampled_from(PLACEHOLDER_WORDS), st.from_regex(r"[a-z0-9_]{1,10}", fullmatch=True)
).filter(_not_seed)

# ---- template documents ----


def make_document(prompt: Mapping[str, Any], placeholders: Mapping[str, Any], **meta: Any) -> dict[str, Any]:
    """A minimal valid Render_Template document around prompt and placeholders: a `still` named `test` for
    `influencer`, its first node as output node, and the core pack for every node class and loader. meta
    replaces or adds meta keys. Everything is copied, so changing the document changes no argument."""
    prompt, placeholders, meta = copy.deepcopy((dict(prompt), dict(placeholders), meta))
    classes = {node["class_type"] for node in prompt.values() if isinstance(node, dict) and "class_type" in node}
    classes |= {
        spec["loader"] for spec in placeholders.values()
        if isinstance(spec, dict) and spec.get("type") == "lora_list" and isinstance(spec.get("loader"), str)
    }  # fmt: skip
    full = {
        "name": "test", "kind": "still", "catalog_id": "influencer", "identity_required": False,
        "output_node": next(iter(prompt), "1"), "placeholders": placeholders, "sizes": {},
        "node_packs": {class_type: "core" for class_type in sorted(classes)}, "models": [], "extra_files": [],
    }  # fmt: skip
    full.update(meta)
    return {"format": T.FORMAT, "meta": full, "prompt": prompt}


def document_of(template: T.Template) -> dict[str, Any]:
    """The JSON document template was read from."""
    return {"format": T.FORMAT, "meta": template.meta, "prompt": template.prompt}


def names_used(template: T.Template) -> set[str]:
    """The placeholders template needs values for, found as §A3 says: the placeholder pattern over every
    string input value, plus every lora_list."""
    names = {name for name, spec in template.meta["placeholders"].items() if spec["type"] == "lora_list"}
    for node in template.prompt.values():
        for value in node["inputs"].values():
            if isinstance(value, str):
                names.update(PLACEHOLDER.findall(value))
    return names


# ---- templates() ----

_FINITE = st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False)
_SPAN = st.floats(0, 1e6, allow_nan=False, allow_infinity=False)


@st.composite
def _int_spec(draw: st.DrawFn) -> dict[str, Any]:
    low = draw(st.one_of(st.sampled_from((0, 2, 256, -5)), st.integers(-(2**40), 2**40)))
    high = low + draw(st.one_of(st.sampled_from((0, 1, 64, 1792)), st.integers(0, 2**40)))
    return {"type": "int", "min": low, "max": high}


@st.composite
def _float_spec(draw: st.DrawFn) -> dict[str, Any]:
    low = draw(st.one_of(st.sampled_from((0.0, -2.0, 0, 1)), _FINITE))  # int bounds are valid too
    high = low + draw(st.one_of(st.sampled_from((0.0, 1.0, 4.0, 0)), _SPAN))
    return {"type": "float", "min": low, "max": high}


_SPECS_BY_TYPE = {
    "int": _int_spec(),
    "float": _float_spec(),
    **{kind: st.builds(dict, type=st.just(kind)) for kind in ("text", "file", "bool")},
}
_scalar_spec = st.sampled_from(tuple(_SPECS_BY_TYPE)).flatmap(_SPECS_BY_TYPE.__getitem__)  # each type as often


@st.composite
def _lora_spec(draw: st.DrawFn, source: list[Any]) -> dict[str, Any]:
    low, high = draw(st.one_of(
        st.sampled_from(((-2.0, 2.0), (0.0, 1.0), (-1, 1), (0.5, 0.5))), st.tuples(_FINITE, _FINITE).map(sorted),
    ))  # fmt: skip
    inputs = [("model", "$prev"), ("lora_name", "$name"), ("strength_model", "$strength")]
    if draw(st.booleans()):
        inputs.append(("strength_clip", "$strength"))
    return {
        "type": "lora_list", "max_items": draw(st.sampled_from((MAX_LORAS, MAX_LORAS, MAX_LORAS, 1, 4))),
        "strength_min": low, "strength_max": high, "source": source, "loader": draw(st.sampled_from(LOADERS)),
        "loader_inputs": dict(draw(st.permutations(inputs))),
    }  # fmt: skip


# Text around a placeholder. Without "{{" it can form no placeholder of its own, next to one or not.
_PIECE = texts(max_size=10).filter(lambda text: "{{" not in text)
_NON_EMPTY_PIECE = texts(min_size=1, max_size=10).filter(lambda text: "{{" not in text)
_LITERAL_TEXT = st.one_of(
    texts(max_size=16),
    st.sampled_from(NEAR_MISSES),
    st.lists(st.one_of(texts(max_size=6), st.sampled_from(NEAR_MISSES)), min_size=2, max_size=4).map("".join),
).filter(lambda text: not PLACEHOLDER.search(text))
_LITERAL = st.one_of(
    st.integers(-(10**6), 10**6),
    st.floats(allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.none(),
    _LITERAL_TEXT,
    st.lists(st.integers(0, 9), min_size=3, max_size=4),  # a list that is no link
    st.fixed_dictionaries({"mode": _LITERAL_TEXT}),
)


@st.composite
def _longer_string(draw: st.DrawFn, names: Sequence[str]) -> str:
    """The placeholders of names, in that order, with text around them; never one placeholder alone."""
    text = draw(_PIECE)
    for name in names:
        text += _braced(name) + draw(_PIECE)
    if PLACEHOLDER.fullmatch(text):  # that would be a whole value
        text += draw(_NON_EMPTY_PIECE)
    return text


@st.composite
def _distinct_ids(draw: st.DrawFn, size: int) -> list[int]:
    """size distinct node numbers below FIRST_LOADER_ID, drawn without rejection: the k-th is the n-th
    number not yet taken, n from 0 to the count still free. A unique list of integers() gave up on the
    small values Hypothesis favours whenever two collided."""
    taken: list[int] = []
    for k in range(size):
        number = draw(st.integers(0, T.FIRST_LOADER_ID - 1 - k))
        for used in sorted(taken):
            if used <= number:
                number += 1
        taken.append(number)
    return taken


@st.composite
def _distinct_names(draw: st.DrawFn, count: int) -> list[str]:
    """count distinct placeholder_names, drawn without rejection: a name already taken gets `_<position>`
    appended until it is new (still [a-z0-9_]+, and ending in a digit it is never a seed). A unique list
    gave up whenever the 14 PLACEHOLDER_WORDS collided a few times in a row."""
    names: list[str] = []
    for position in range(count):
        name = draw(placeholder_names)
        while name in names:
            name += f"_{position}"
        names.append(name)
    return names


@st.composite
def templates(draw: st.DrawFn, *, min_loras: int = 0, max_loras: int = 2) -> T.Template:
    """A small valid Render_Template (see the module docstring), of any kind, often with a seed.

    Each placeholder is placed 1-2 times: a text, file or int one inside a longer string (with up to two
    others) or as a whole value, at random; any other one as a whole value; a lora_list as the whole value
    of a consumer. Now and then one placeholder stays declared but unused. Node j of the draw order links
    only to nodes drawn before it and consumes only lora_lists whose source is one of those, so the graph
    is acyclic; the prompt then lists the nodes in a random order."""
    lora_count = draw(st.integers(min_loras, max_loras))
    size = draw(st.integers(2 if lora_count else 1, 6))
    ids = [str(n) for n in draw(_distinct_ids(size))]
    count = draw(st.integers(0, 6))
    total = count + lora_count
    names = draw(_distinct_names(total))
    specs: dict[str, dict[str, Any]] = {name: draw(_scalar_spec) for name in names[:count]}
    if draw(st.booleans()):
        specs["seed"] = {"type": "int", "min": SEED_MIN, "max": SEED_MAX}
    sources: dict[str, int] = {}  # lora_list name -> the draw position of its source node
    lora_names = names[count:]
    for name in lora_names:
        sources[name] = draw(st.integers(0, size - 2))
        specs[name] = draw(_lora_spec([ids[sources[name]], draw(st.integers(0, 2))]))

    scalars = sorted(name for name, spec in specs.items() if spec["type"] != "lora_list")
    in_string = [name for name in scalars if specs[name]["type"] in ("text", "file", "int")]
    placed: list[list[Any]] = [[] for _ in range(size)]  # the input values of each node
    unused = draw(st.sampled_from(scalars)) if scalars and draw(st.sampled_from((False,) * 9 + (True,))) else None
    for name in scalars:
        if name == unused:  # declared, never used: its value is still checked, and otherwise ignored
            continue
        for _ in range(draw(st.integers(1, 2))):
            if name in in_string and draw(st.booleans()):
                inside = draw(st.permutations([name, *draw(st.lists(st.sampled_from(in_string), max_size=2))]))
                value = draw(_longer_string(inside))
            else:
                value = _braced(name)
            placed[draw(st.integers(0, size - 1))].append(value)
    for name in lora_names:
        for _ in range(draw(st.integers(1, 2))):
            placed[draw(st.integers(sources[name] + 1, size - 1))].append(_braced(name))
    inputs_of: list[dict[str, Any]] = []
    for position in range(size):
        extras = [_LITERAL]  # placeholder-free values, and links to nodes drawn earlier
        if position:
            extras.append(st.tuples(st.sampled_from(ids[:position]), st.integers(0, 3)).map(list))
        values = draw(st.permutations(placed[position] + draw(st.lists(st.one_of(extras), max_size=3))))
        input_names = draw(st.permutations(INPUT_NAMES))
        inputs_of.append({
            input_names[number] if number < len(input_names) else f"input_{number}": value
            for number, value in enumerate(values)
        })  # fmt: skip

    classes = [draw(st.sampled_from(CLASSES)) for _ in range(size)]
    prompt: dict[str, Any] = {}
    for position in draw(st.permutations(range(size))):
        node: dict[str, Any] = {"inputs": inputs_of[position], "class_type": classes[position]}
        if draw(st.booleans()):
            node["_meta"] = {"title": draw(_LITERAL_TEXT)}
        prompt[ids[position]] = node

    kind = draw(st.sampled_from(T.KINDS))
    loaders = {spec["loader"] for spec in specs.values() if spec["type"] == "lora_list"}
    meta: dict[str, Any] = {
        "name": draw(st.from_regex(r"[a-z0-9][a-z0-9_-]{0,15}", fullmatch=True)),
        "kind": kind,
        "catalog_id": draw(st.sampled_from(("influencer", "influencer-video"))),
        "identity_required": draw(st.booleans()),
        "output_node": draw(st.sampled_from(ids)),
        "placeholders": {name: specs[name] for name in draw(st.permutations(sorted(specs)))},
        "sizes": draw(st.sampled_from(SIZES)),
        "node_packs": {class_type: draw(st.sampled_from(PACKS)) for class_type in sorted(set(classes) | loaders)},
        "models": draw(st.sampled_from(MODELS)),
        "extra_files": [],
    }
    timeout = draw(st.one_of(st.none(), st.integers(1, 7200)))
    if timeout is not None:
        meta["timeout_s"] = timeout
    if kind in T.REEL_KINDS:
        meta["max_frames"], meta["fps"] = draw(st.integers(1, 241)), 24
    if kind == "ref_reel":
        meta["ref_slots"] = 9
    return T.parse_template({"format": T.FORMAT, "meta": meta, "prompt": prompt}, "<generated>")


# ---- values ----


def supplied_texts(names: Sequence[str] = (), *, non_empty: bool = False) -> st.SearchStrategy[str]:
    """Text values: texts(), often holding {{seed}}-like strings (the template's own names among them) and
    the characters JSON escapes or re.sub treats specially. Non-empty by construction when asked, since a
    filter would throw away most draws of a long LoRA list."""
    looks_like = st.sampled_from(
        tuple(_braced(name) for name in names) + ("{{seed}}", "{{prompt}}", "{{x}}", "{{loras}}")
    )
    piece = st.one_of(texts(min_size=1, max_size=6), looks_like, st.sampled_from(TRICKY))
    return st.one_of(
        texts(min_size=1 if non_empty else 0), looks_like, st.lists(piece, min_size=1, max_size=5).map("".join)
    )


def _numbers(low: int | float, high: int | float) -> st.SearchStrategy[int | float]:
    """Ints and finite floats from low to high, the bounds themselves often."""
    options = [st.sampled_from((low, high)), st.floats(low, high, allow_nan=False, allow_infinity=False)]
    if math.ceil(low) <= math.floor(high):
        options.append(st.integers(math.ceil(low), math.floor(high)))
    return st.one_of(options)


def lora_entries(
    strength_min: int | float = -2.0, strength_max: int | float = 2.0
) -> st.SearchStrategy[dict[str, Any]]:
    """One valid lora_list entry: a non-empty name and an int or finite float strength within the bounds
    (by default §M6's -2.0 to 2.0)."""
    names = st.one_of(
        st.from_regex(r"(looks/)?[a-z0-9_-]{1,16}\.safetensors", fullmatch=True), supplied_texts(non_empty=True)
    )
    return st.fixed_dictionaries({"name": names, "strength": _numbers(strength_min, strength_max)})


def _value(spec: Mapping[str, Any], names: Sequence[str]) -> st.SearchStrategy[Any]:
    kind = spec["type"]
    if kind == "int":
        return st.one_of(st.sampled_from((spec["min"], spec["max"])), st.integers(spec["min"], spec["max"]))
    if kind == "float":
        return _numbers(spec["min"], spec["max"])
    if kind == "text":
        return supplied_texts(names)
    if kind == "file":
        return supplied_texts(names, non_empty=True)
    if kind == "bool":
        return st.booleans()
    return st.lists(lora_entries(spec["strength_min"], spec["strength_max"]), max_size=spec["max_items"])


# The Property 22 error kinds. Each injected error hits a placeholder of its own and is one Problem.
ERROR_KINDS = (
    "missing", "unknown", "bool_for_int", "float_for_int", "wrong_type", "empty_file", "out_of_bounds",
    "strength_out_of_bounds", "empty_lora_name", "too_many_loras",
)  # fmt: skip
WRONG_TYPES = {
    "int": ("1", None, [1]),
    "float": ("0.5", True, None, math.nan, math.inf),
    "text": (5, None, 1.5, ["a"]),
    "file": (5, None, ["a.png"]),
    "bool": (1, 0, "true", None),
    "lora_list": ({"name": "a.safetensors", "strength": 1.0}, "a.safetensors", None, 5),
}
UNKNOWN_NAMES = st.one_of(
    st.from_regex(r"[a-z0-9_]{1,12}", fullmatch=True), st.sampled_from(("Seed", "lora-list", "x y", "SEED_2"))
)


@dataclass(frozen=True)
class Injected:
    """One injected error: its kind, the Problem field that must report it, the declared placeholder (None
    for an unknown name) and the value put in (None for a missing value, the entry count for too many)."""

    kind: str
    field: str
    placeholder: str | None
    value: object


def values_for(template: T.Template, *, errors: Collection[str] = ()) -> st.SearchStrategy[Any]:
    """A valid value for every placeholder template declares, keyed in declaration order. With errors, a set
    of ERROR_KINDS, draws (values, injected) instead: each kind that fits the template put in at a
    placeholder of its own, and an unknown name when none fits."""
    specs = template.meta["placeholders"]
    names = sorted(specs)
    valid = st.fixed_dictionaries({name: _value(spec, names) for name, spec in specs.items()})
    if not errors:
        return valid
    unknown = sorted(set(errors) - set(ERROR_KINDS))
    if unknown:
        raise ValueError(f"not error kinds: {unknown}")
    return valid.flatmap(lambda values: _with_errors(template, values, sorted(errors)))


def _outside(low: int | float, high: int | float, below: bool) -> int | float:
    """A number just outside [low, high]: an int for int bounds, else a float."""
    if type(low) is int and type(high) is int:
        return low - 1 if below else high + 1
    return low - max(1.0, abs(low)) if below else high + max(1.0, abs(high))


@st.composite
def _with_errors(
    draw: st.DrawFn, template: T.Template, valid: Mapping[str, Any], kinds: Sequence[str]
) -> tuple[dict[str, Any], tuple[Injected, ...]]:
    specs = template.meta["placeholders"]
    values = dict(valid)
    used = sorted(names_used(template))
    taken: set[str] = set()
    injected: list[Injected] = []

    def target(types: Collection[str] | None = None, among: Sequence[str] | None = None) -> str | None:
        pool = [name for name in (sorted(specs) if among is None else among)
                if name not in taken and (types is None or specs[name]["type"] in types)]  # fmt: skip
        if not pool:
            return None
        name = draw(st.sampled_from(pool))
        taken.add(name)
        return name

    def put(kind: str, name: str, value: object) -> None:
        values[name] = value
        injected.append(Injected(kind, name, name, value))

    def add_unknown() -> None:
        name = draw(UNKNOWN_NAMES.filter(lambda name: name not in specs))
        value = draw(st.one_of(st.integers(), texts(max_size=8), st.booleans()))
        values[name] = value
        injected.append(Injected("unknown", name, None, value))

    for kind in kinds:
        if kind == "missing":
            if (name := target(among=used)) is not None:
                del values[name]
                injected.append(Injected(kind, name, name, None))
        elif kind == "unknown":
            add_unknown()
        elif kind in ("bool_for_int", "float_for_int"):
            if (name := target({"int"})) is not None:
                put(kind, name, draw(st.booleans()) if kind == "bool_for_int" else float(values[name]))
        elif kind == "wrong_type":
            if (name := target()) is not None:
                put(kind, name, draw(st.sampled_from(WRONG_TYPES[specs[name]["type"]])))
        elif kind == "empty_file":
            if (name := target({"file"})) is not None:
                put(kind, name, "")
        elif kind == "out_of_bounds":
            if (name := target({"int", "float"})) is not None:
                put(kind, name, _outside(specs[name]["min"], specs[name]["max"], draw(st.booleans())))
        elif kind in ("strength_out_of_bounds", "empty_lora_name"):
            if (name := target({"lora_list"})) is not None:
                spec = specs[name]
                entries = [dict(entry) for entry in values[name]]
                if not entries:
                    entries.append(draw(lora_entries(spec["strength_min"], spec["strength_max"])))
                number = draw(st.integers(1, len(entries)))
                if kind == "empty_lora_name":
                    bad, key = "", "name"
                else:
                    bad, key = _outside(spec["strength_min"], spec["strength_max"], draw(st.booleans())), "strength"
                entries[number - 1][key] = bad
                values[name] = entries
                injected.append(Injected(kind, f"{name}[{number}].{key}", name, bad))
        elif kind == "too_many_loras":
            if (name := target({"lora_list"})) is not None:
                spec = specs[name]
                entries = lora_entries(spec["strength_min"], spec["strength_max"])
                values[name] = draw(st.lists(entries, min_size=MAX_LORAS + 1, max_size=MAX_LORAS + 1))
                injected.append(Injected(kind, name, name, MAX_LORAS + 1))
    if not injected:  # no drawn kind fits this template; an unknown name always does
        add_unknown()
    return values, tuple(injected)
