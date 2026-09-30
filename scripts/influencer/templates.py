"""Render_Templates and the Template_Filler (design §C7, §M6, §A3; Req 7, 10.7).

A Render_Template is one JSON object in config/influencer/templates/<name>.json:
{"format": "influencer-render-template/1", "meta": {…}, "prompt": {…}}. `prompt` is the API-format graph
that ComfyUI takes, with node ids that are decimal numbers below 900. A placeholder is the text {{name}},
name made of a-z, 0-9 and _, standing for a whole string input value or inside a longer one; `meta`
declares the type and bounds of every placeholder (§M6).

- load_template(name) reads and checks one file, parse_template one parsed document. Either raises one
  ValidationError holding every Problem found: the §M6 metadata rules, an undeclared placeholder, a node id
  at or above 900, a lora_list placeholder outside a whole value, a dangling link, or a cycle.
- fill(template, values) first checks every value (check_values) and raises one FillError naming every
  missing, unknown and invalid value (Req 7.3, 7.7). Otherwise it returns a new prompt (Req 7.2): an input
  whose value is exactly "{{x}}" takes the typed value (Req 7.4); placeholders inside a longer string are
  replaced in one regex pass over the template's own string, so placeholder syntax inside a supplied value
  is never expanded again (Req 7.8); and each lora_list becomes a chain of loader nodes (Req 7.5) whose ids
  depend only on the sorted placeholder names and the entry numbers (Req 7.6).

Standard library only. Only load_template reads a file; nothing here writes one or opens a connection.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from influencer.errors import Problem, ValidationError, format_problems

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "config" / "influencer" / "templates"
FORMAT = "influencer-render-template/1"

# A placeholder (§A3). Other text between double braces, such as "{{ seed }}" or "{{Seed}}", is plain text.
PLACEHOLDER = re.compile(r"\{\{([a-z0-9_]+)\}\}")
PLACEHOLDER_NAME = re.compile(r"[a-z0-9_]+")
TEMPLATE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")  # a file stem: still, still-sharp, ref-reel-voice
_CATALOG_ID = re.compile(r"[a-z0-9][a-z0-9-]*")
_NODE_ID = re.compile(r"0|[1-9][0-9]*")
_RATIO = re.compile(r"([1-9][0-9]*):([1-9][0-9]*)")

# Kinds (§M6). A variant such as still-sharp or reel-voice is a file of its own with its kind's rules
# (design Key decision 5), so timeouts and the Reel extras go by kind, never by file name.
STILLS_KINDS = ("still", "edit", "finish", "refine")
REEL_KINDS = ("reel", "ref_reel")
KINDS = STILLS_KINDS + REEL_KINDS
DEFAULT_TIMEOUT_S = {**dict.fromkeys(STILLS_KINDS, 900), **dict.fromkeys(REEL_KINDS, 3600)}  # Req 10.7

TYPES = ("int", "float", "text", "file", "bool", "lora_list")
IN_STRING_TYPES = ("text", "file", "int")  # the types that may sit inside a longer string (Req 7.8)
SEED_BOUNDS = (0, 4_294_967_295)  # a seed placeholder's min and max (Req 7.1)

MAX_LORAS = 16  # entries of one lora_list (Req 7.5)
FIRST_LOADER_ID = 900  # template node ids stay below it
LOADER_ID_STRIDE = 20  # entry k (1-16) of the i-th lora_list in name order gets id 900 + 20 * i + k
MAX_LORA_LISTS = 17  # so every loader id stays within 900-1239
LOADER_TOKENS = ("$prev", "$name", "$strength")  # what a loader input takes: the chain link, entry fields

_META_KEYS = (
    "name", "kind", "catalog_id", "identity_required", "output_node", "placeholders", "sizes",
    "node_packs", "models", "extra_files",
)  # fmt: skip
_SPEC_KEYS = {
    "int": ("min", "max"),
    "float": ("min", "max"),
    "text": (),
    "file": (),
    "bool": (),
    "lora_list": ("max_items", "strength_min", "strength_max", "source", "loader", "loader_inputs"),
}
_SIZE_KEYS = ("latent", "output", "crop")
_MODEL_KEYS = ("kind", "folder", "name")
_LORA_ENTRY_KEYS = frozenset({"name", "strength"})


@dataclass(frozen=True)
class Placeholder:
    """One declared placeholder (§M6). min and max bound an int or float; the lora_list fields stay None
    (loader_inputs empty) for every other type."""

    name: str
    type: str
    min: int | float | None = None
    max: int | float | None = None
    max_items: int | None = None
    strength_min: int | float | None = None
    strength_max: int | float | None = None
    source: tuple[str, int] | None = None  # the link the first loader takes
    loader: str | None = None  # the loader's node class
    loader_inputs: tuple[tuple[str, str], ...] = ()  # (input name, "$prev" | "$name" | "$strength")


@dataclass(frozen=True)
class Template:
    """A checked Render_Template; parse_template and load_template make them.

    meta and prompt are the document's own, as JSON data, and fill never changes them. timeout_s is
    meta.timeout_s or the kind's default. used names every placeholder the prompt contains, and every
    lora_list: these need a value (§A3)."""

    name: str
    kind: str
    timeout_s: int
    placeholders: Mapping[str, Placeholder]
    meta: Mapping[str, Any]
    prompt: Mapping[str, Any]
    used: frozenset[str]
    path: str

    @property
    def lora_lists(self) -> tuple[str, ...]:
        """The lora_list placeholders in sorted name order, the order their loader ids follow."""
        return tuple(sorted(name for name, spec in self.placeholders.items() if spec.type == "lora_list"))

    @property
    def catalog_id(self) -> str:
        return self.meta["catalog_id"]

    @property
    def identity_required(self) -> bool:
        return self.meta["identity_required"]

    @property
    def output_node(self) -> str:
        return self.meta["output_node"]


class FillError(ValidationError):
    """The values cannot fill the Render_Template, so fill returns no prompt (Req 7.3, 7.7). template is
    its name; each Problem names the template, the placeholder (with the entry number for a lora_list
    entry), the declared type or bounds, and the supplied value."""

    template: str

    def __init__(self, template: str, problems: Iterable[Problem]) -> None:
        super().__init__(problems)
        self.template = template
        self.args = (template, self.problems)

    def __str__(self) -> str:
        return f"cannot fill the Render_Template {self.template}:\n{format_problems(self.problems)}"


# ---- loading and checking (§C7, §M6) ----


def load_template(name: str, folder: str | os.PathLike[str] = TEMPLATES_DIR) -> Template:
    """The Render_Template <folder>/<name>.json, by default config/influencer/templates/<name>.json, checked
    by parse_template; its meta.name must be name. A bad name, or a file that is missing, unreadable, not
    UTF-8, not JSON or holds a key twice in one object, raises a ValidationError naming the file."""
    if not isinstance(name, str) or not TEMPLATE_NAME.fullmatch(name):
        reason = f"{name!r} is not a Render_Template name: lowercase letters, digits, - and _"
        raise ValidationError([Problem(os.fspath(folder), None, None, reason)])
    path = Path(folder) / f"{name}.json"
    shown = os.fspath(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise ValidationError([Problem(shown, None, None, "the Render_Template is missing")]) from None
    except OSError as error:
        reason = f"cannot read the Render_Template: {error.strerror or error}"
        raise ValidationError([Problem(shown, None, None, reason)]) from None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        line = data.count(b"\n", 0, error.start) + 1
        raise ValidationError([Problem(shown, None, line, "the Render_Template is not UTF-8 text")]) from None
    repeated: list[str] = []
    try:
        document = json.loads(text, object_pairs_hook=_object_hook(repeated), parse_constant=_no_constant)
    except json.JSONDecodeError as error:
        raise ValidationError([Problem(shown, None, error.lineno, f"not valid JSON: {error.msg}")]) from None
    except ValueError as error:  # NaN or Infinity
        raise ValidationError([Problem(shown, None, None, f"not valid JSON: {error}")]) from None
    if repeated:
        raise ValidationError(
            [Problem(shown, None, None, f"the key {key!r} appears twice in one object") for key in repeated]
        )
    return parse_template(document, shown, name=name)


def _object_hook(repeated: list[str]) -> Callable[[list[tuple[str, Any]]], dict[str, Any]]:
    """A json object_pairs_hook that notes each key given twice in one object (json keeps the last)."""

    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result and key not in repeated:
                repeated.append(key)
            result[key] = value
        return result

    return hook


def _no_constant(name: str) -> None:
    raise ValueError(f"{name} is not a JSON number")


class _Report:
    """The Problems found in one document, all at the same path."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.problems: list[Problem] = []

    def add(self, field: str | None, reason: str) -> None:
        self.problems.append(Problem(self.path, field, None, reason))


def parse_template(document: object, path: str = "<template>", *, name: str | None = None) -> Template:
    """The Render_Template that document (parsed JSON) holds, checked against §M6 and §C7; path names it
    in every Problem, and name, when given, is the file name meta.name must equal. Raises one
    ValidationError holding every Problem found."""
    report = _Report(path)
    document = _json_copy(document, None, report)
    if report.problems:
        raise ValidationError(report.problems)
    if not isinstance(document, dict):
        reason = f"a Render_Template is one JSON object of format, meta and prompt; got {_describe(document)}"
        raise ValidationError([Problem(path, None, None, reason)])
    for key in document:
        if key not in ("format", "meta", "prompt"):
            report.add(key, "is not a Render_Template key; the keys are format, meta and prompt")
    if "format" not in document:
        report.add("format", "is missing")
    elif document["format"] != FORMAT:
        report.add("format", f"must be {FORMAT!r}; got {document['format']!r}")
    prompt = _section(document, "prompt", report)
    meta = _section(document, "meta", report)
    if prompt is not None:
        _check_nodes(prompt, report)
    specs, declared = _check_meta(meta, prompt, name, report) if meta is not None else ({}, None)
    used = _check_graph(prompt, specs, declared, report) if prompt is not None else set()
    if report.problems:
        raise ValidationError(report.problems)
    assert meta is not None and prompt is not None  # a missing section is a Problem
    kind = meta["kind"]
    return Template(
        name=meta["name"],
        kind=kind,
        timeout_s=meta.get("timeout_s", DEFAULT_TIMEOUT_S[kind]),
        placeholders=specs,
        meta=meta,
        prompt=prompt,
        used=frozenset(used) | {n for n, spec in specs.items() if spec.type == "lora_list"},
        path=path,
    )


def _json_copy(value: object, where: str | None, report: _Report) -> object:
    """A copy of value made of JSON data only (objects with string keys, lists, strings, ints, finite
    floats, booleans, null), so a Template never shares data with its caller and holds no tuple that would
    serialize like a link. Anything else is a Problem."""
    if value is None or isinstance(value, (str, bool)) or type(value) is int:
        return value
    if type(value) is float:
        if math.isfinite(value):
            return value
        report.add(where, f"{value!r} is not a JSON number")
        return None
    if isinstance(value, dict):
        copy: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                report.add(where, f"the key {key!r} is not a string")
                continue
            copy[key] = _json_copy(item, key if where is None else f"{where}.{key}", report)
        return copy
    if isinstance(value, list):
        return [_json_copy(item, f"{where or ''}[{number}]", report) for number, item in enumerate(value, 1)]
    report.add(where, f"a {type(value).__name__} is not JSON data")
    return None


def _section(document: dict[str, Any], key: str, report: _Report) -> dict[str, Any] | None:
    if key not in document:
        report.add(key, "is missing")
        return None
    if not isinstance(document[key], dict):
        report.add(key, f"must be an object; got {_describe(document[key])}")
        return None
    return document[key]


def _check_nodes(prompt: dict[str, Any], report: _Report) -> None:
    """Node ids, node shapes, and placeholders outside input values."""
    for node_id, node in prompt.items():
        where = f"prompt.{node_id}"
        if not _NODE_ID.fullmatch(node_id):
            report.add(where, "a node id must be a decimal number without leading zeros")
        elif int(node_id) >= FIRST_LOADER_ID:
            report.add(where, f"a node id must be below {FIRST_LOADER_ID}; the Template_Filler numbers "
                              f"LoRA loaders from {FIRST_LOADER_ID}")  # fmt: skip
        if not isinstance(node, dict):
            report.add(where, f"must be a node object; got {_describe(node)}")
            continue
        class_type = node.get("class_type")
        if not (isinstance(class_type, str) and class_type.strip()):
            report.add(f"{where}.class_type", f"must be a node class name; got {class_type!r}")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            report.add(f"{where}.inputs", f"must be an object of inputs; got {_describe(inputs)}")
        else:
            for input_name in inputs:
                if PLACEHOLDER.search(input_name) or not input_name:
                    reason = "an input name must be non-empty and hold no placeholder"
                    report.add(f"{where}.inputs.{input_name}", reason)
        for key, value in node.items():
            if key != "inputs" and _has_placeholder([key, value]):
                report.add(f"{where}.{key}", "holds a placeholder; placeholders may only stand in input values")


def _check_meta(
    meta: dict[str, Any], prompt: dict[str, Any] | None, file_name: str | None, report: _Report
) -> tuple[dict[str, Placeholder], set[str] | None]:
    """The §M6 rules. Returns the well-formed placeholders and the declared names (None when
    meta.placeholders is not an object, so no placeholder can be called undeclared)."""
    for key in _META_KEYS:
        if key not in meta:
            report.add(f"meta.{key}", "is missing")
    if "name" in meta:
        name = meta["name"]
        if not (isinstance(name, str) and TEMPLATE_NAME.fullmatch(name)):
            report.add("meta.name", f"must be a template name of lowercase letters, digits, - and _; got {name!r}")
        elif file_name is not None and name != file_name:
            report.add("meta.name", f"must be the file's name {file_name!r}; got {name!r}")
    kind = meta.get("kind")
    if "kind" in meta and kind not in KINDS:
        report.add("meta.kind", f"must be one of {', '.join(KINDS)}; got {kind!r}")
    catalog_id = meta.get("catalog_id")
    if "catalog_id" in meta and not (isinstance(catalog_id, str) and _CATALOG_ID.fullmatch(catalog_id)):
        report.add("meta.catalog_id", f"must be a Catalog id; got {catalog_id!r}")
    if "identity_required" in meta and type(meta["identity_required"]) is not bool:
        report.add("meta.identity_required", f"must be true or false; got {meta['identity_required']!r}")
    if "output_node" in meta:
        output = meta["output_node"]
        if not isinstance(output, str) or (prompt is not None and output not in prompt):
            report.add("meta.output_node", f"must be the id of a node of the prompt; got {output!r}")
    if "timeout_s" in meta and not _is_count(meta["timeout_s"]):
        report.add("meta.timeout_s", f"must be a whole number of seconds above 0; got {meta['timeout_s']!r}")
    if kind in REEL_KINDS:
        for key in ("max_frames", "fps") + (("ref_slots",) if kind == "ref_reel" else ()):
            if key not in meta:
                report.add(f"meta.{key}", f"is missing; a {kind} template needs it")
            elif not _is_count(meta[key]):
                report.add(f"meta.{key}", f"must be a whole number above 0; got {meta[key]!r}")

    specs: dict[str, Placeholder] = {}
    declared: set[str] | None = None
    if "placeholders" in meta:
        placeholders = meta["placeholders"]
        if not isinstance(placeholders, dict):
            report.add("meta.placeholders", f"must be an object; got {_describe(placeholders)}")
        else:
            declared = set(placeholders)
            for placeholder_name, data in placeholders.items():
                spec = _check_placeholder(placeholder_name, data, prompt, report)
                if spec is not None:
                    specs[placeholder_name] = spec
            lora_lists = [n for n, data in placeholders.items() if _declares(data, "lora_list")]
            if len(lora_lists) > MAX_LORA_LISTS:
                report.add("meta.placeholders", f"declares {len(lora_lists)} lora_list placeholders; at most "
                                                f"{MAX_LORA_LISTS} fit the loader ids 900-1239")  # fmt: skip
    _check_sizes(meta, report)
    _check_node_packs(meta, prompt, specs, report)
    if "models" in meta:
        models = meta["models"]
        if not isinstance(models, list):
            report.add("meta.models", f"must be a list; got {_describe(models)}")
        else:
            for number, model in enumerate(models, 1):
                if not isinstance(model, dict):
                    report.add(f"meta.models[{number}]", "must be an object with a kind, a folder and a name")
                    continue
                for key in _MODEL_KEYS:
                    value = model.get(key)
                    if not (isinstance(value, str) and value.strip()):
                        report.add(f"meta.models[{number}].{key}", f"must be a non-empty string; got {value!r}")
    if "extra_files" in meta and not isinstance(meta["extra_files"], list):
        report.add("meta.extra_files", f"must be a list; got {_describe(meta['extra_files'])}")
    return specs, declared


def _check_placeholder(name: str, data: object, prompt: dict[str, Any] | None, report: _Report) -> Placeholder | None:
    """One placeholder declaration, or None when it breaks a rule (each broken rule is reported)."""
    where = f"meta.placeholders.{name}"
    before = len(report.problems)
    if not PLACEHOLDER_NAME.fullmatch(name):
        report.add(where, "a placeholder name may only hold a-z, 0-9 and _")
    if not isinstance(data, dict):
        report.add(where, f"must be an object with a type; got {_describe(data)}")
        return None
    kind = data.get("type")
    if kind not in TYPES:
        report.add(f"{where}.type", f"must be one of {', '.join(TYPES)}; got {kind!r}")
        return None
    for key in data:
        if key != "type" and key not in _SPEC_KEYS[kind]:
            report.add(f"{where}.{key}", f"is not a key of a {kind} placeholder")
    for key in _SPEC_KEYS[kind]:
        if key not in data:
            report.add(f"{where}.{key}", "is missing")
    if name == "seed" or name.endswith("_seed"):
        low, high = data.get("min"), data.get("max")
        if kind != "int" or type(low) is not int or type(high) is not int or (low, high) != SEED_BOUNDS:
            report.add(where, f"a seed placeholder must be an int from {SEED_BOUNDS[0]} to {SEED_BOUNDS[1]}; got "
                              f"type {kind}, min {low!r} and max {high!r}")  # fmt: skip
    if kind in ("int", "float"):
        _check_bounds(data, "min", "max", where, int if kind == "int" else None, report)
    elif kind == "lora_list":
        max_items = data.get("max_items")
        if "max_items" in data and not (type(max_items) is int and 0 <= max_items <= MAX_LORAS):
            report.add(f"{where}.max_items", f"must be an int from 0 to {MAX_LORAS}; got {max_items!r}")
        _check_bounds(data, "strength_min", "strength_max", where, None, report)
        source = data.get("source")
        if "source" in data:
            if not (_is_link(source) and source[1] >= 0):
                report.add(f"{where}.source", f"must be a link [node id, output index]; got {source!r}")
            elif prompt is not None and source[0] not in prompt:
                report.add(f"{where}.source", f"links to node {source[0]!r}, which the prompt does not hold")
        loader = data.get("loader")
        if "loader" in data and not (isinstance(loader, str) and loader.strip()):
            report.add(f"{where}.loader", f"must be a node class name; got {loader!r}")
        if "loader_inputs" in data:
            _check_loader_inputs(data["loader_inputs"], f"{where}.loader_inputs", report)
    if len(report.problems) > before:
        return None
    if kind == "lora_list":
        return Placeholder(
            name, kind,
            max_items=data["max_items"], strength_min=data["strength_min"], strength_max=data["strength_max"],
            source=(data["source"][0], data["source"][1]), loader=data["loader"],
            loader_inputs=tuple(data["loader_inputs"].items()),
        )  # fmt: skip
    return Placeholder(name, kind, min=data.get("min"), max=data.get("max"))


def _check_bounds(
    data: dict[str, Any], low_key: str, high_key: str, where: str, only: type | None, report: _Report
) -> None:
    """Both bounds are ints (only=int) or ints and finite floats, and the low one is not above the high."""
    rule = "an int" if only is int else "an int or a finite float"
    good = True
    for key in (low_key, high_key):
        if key in data and not (type(data[key]) is int if only is int else _is_number(data[key])):
            report.add(f"{where}.{key}", f"must be {rule}; got {data[key]!r}")
            good = False
    if good and low_key in data and high_key in data and data[low_key] > data[high_key]:
        report.add(where, f"{low_key} {data[low_key]!r} is above {high_key} {data[high_key]!r}")


def _check_loader_inputs(loader_inputs: object, where: str, report: _Report) -> None:
    if not (
        isinstance(loader_inputs, dict)
        and all(isinstance(token, str) and token in LOADER_TOKENS for token in loader_inputs.values())
    ):
        report.add(where, f"must map the loader's input names to $prev, $name or $strength; got {loader_inputs!r}")
        return
    tokens = list(loader_inputs.values())
    if "" in loader_inputs:
        report.add(where, "an input name must be non-empty")
    if tokens.count("$prev") != 1 or "$name" not in tokens or "$strength" not in tokens:
        report.add(where, f"must give $prev to exactly one input and $name and $strength to at least one each; "
                          f"got {loader_inputs!r}")  # fmt: skip


def _check_sizes(meta: dict[str, Any], report: _Report) -> None:
    """Each entry "W:H" holds an output [width, height] of exactly that ratio (Req 8.6), and optionally a
    latent [width, height] and a crop [x, y]."""
    if "sizes" not in meta:
        return
    sizes = meta["sizes"]
    if not isinstance(sizes, dict):
        report.add("meta.sizes", f"must be an object; got {_describe(sizes)}")
        return
    for ratio, size in sizes.items():
        where = f"meta.sizes.{ratio}"
        match = _RATIO.fullmatch(ratio)
        if not match:
            report.add(where, "an aspect ratio must read W:H, such as 3:4")
        if not isinstance(size, dict):
            report.add(where, f"must be an object of output, latent and crop; got {_describe(size)}")
            continue
        for key in size:
            if key not in _SIZE_KEYS:
                report.add(f"{where}.{key}", "is not a size key; the keys are latent, output and crop")
        if "output" not in size:
            report.add(f"{where}.output", "is missing")
        for key in _SIZE_KEYS:
            if key in size and not _is_pair(size[key], 0 if key == "crop" else 1):
                shape = "[x, y] of ints from 0" if key == "crop" else "[width, height] of ints above 0"
                report.add(f"{where}.{key}", f"must be {shape}; got {size[key]!r}")
        output = size.get("output")
        if match and _is_pair(output, 1) and output[0] * int(match[2]) != output[1] * int(match[1]):
            report.add(f"{where}.output", f"{output[0]}x{output[1]} is not exactly {ratio}")


def _check_node_packs(
    meta: dict[str, Any], prompt: dict[str, Any] | None, specs: Mapping[str, Placeholder], report: _Report
) -> None:
    """node_packs maps every node class of the prompt, and every lora_list loader, to its node pack."""
    if "node_packs" not in meta:
        return
    packs = meta["node_packs"]
    if not (isinstance(packs, dict) and all(isinstance(pack, str) and pack.strip() for pack in packs.values())):
        report.add("meta.node_packs", "must map each node class to its node pack, core for ComfyUI core")
        return
    if prompt is None:
        return
    classes = [node.get("class_type") for node in prompt.values() if isinstance(node, dict)]
    classes += [spec.loader for spec in specs.values() if spec.type == "lora_list"]
    for class_type in dict.fromkeys(c for c in classes if isinstance(c, str) and c.strip()):
        if class_type not in packs:
            report.add("meta.node_packs", f"names no node pack for the class {class_type!r}")


def _check_graph(
    prompt: dict[str, Any], specs: Mapping[str, Placeholder], declared: set[str] | None, report: _Report
) -> set[str]:
    """Placeholder placements, links and cycles. Returns the placeholder names the prompt contains.

    A consumer of a lora_list counts as linked to the list's source, since the filled chain runs from the
    source to it; so an acyclic template also fills to an acyclic prompt (Property 20)."""
    used: set[str] = set()
    edges: dict[str, list[str]] = {node_id: [] for node_id in prompt}
    for node_id, node in prompt.items():
        if not (isinstance(node, dict) and isinstance(node.get("inputs"), dict)):
            continue
        for input_name, value in node["inputs"].items():
            where = f"prompt.{node_id}.inputs.{input_name}"
            if isinstance(value, str):
                whole = PLACEHOLDER.fullmatch(value) is not None
                for match in PLACEHOLDER.finditer(value):
                    name = match.group(1)
                    used.add(name)
                    if declared is None:
                        continue  # meta.placeholders is broken, and reported
                    if name not in declared:
                        report.add(where, f"holds {_braced(name)}, which meta.placeholders does not declare")
                        continue
                    spec = specs.get(name)
                    if spec is None:
                        continue  # its declaration is broken, and reported
                    if spec.type == "lora_list":
                        if not whole:
                            reason = (f"holds the lora_list placeholder {_braced(name)} inside a longer string; "
                                      "a lora_list placeholder must be a whole input value")  # fmt: skip
                            report.add(where, reason)
                        elif spec.source is not None:
                            edges[node_id].append(spec.source[0])  # the filled chain runs from the source to here
                    elif not whole and spec.type not in IN_STRING_TYPES:
                        reason = (f"holds the {spec.type} placeholder {_braced(name)} inside a longer string; "
                                  "only text, file and int placeholders may sit inside one")  # fmt: skip
                        report.add(where, reason)
            elif _has_placeholder(value):
                report.add(where, "holds a placeholder inside a list or object; a placeholder may only stand in "
                                  "a string input value")  # fmt: skip
            elif _is_link(value):
                if value[0] in prompt:
                    edges[node_id].append(value[0])
                else:
                    report.add(where, f"links to node {value[0]!r}, which the prompt does not hold")
                if value[1] < 0:
                    report.add(where, f"links to output {value[1]}; an output index is 0 or more")
    cycle = _find_cycle(edges)
    if cycle is not None:
        report.add("prompt", "has a cycle: " + " -> ".join(cycle))
    return used


def _find_cycle(edges: Mapping[str, list[str]]) -> list[str] | None:
    """One cycle as [a, b, …, a], or None when the graph is acyclic. An iterative depth-first search in
    the order of edges, so a graph always reports the same cycle."""
    on_path, done = 1, 2
    state: dict[str, int] = {}
    for start in edges:
        if start in state:
            continue
        state[start] = on_path
        path, pending = [start], [iter(edges[start])]
        while pending:
            for child in pending[-1]:
                seen = state.get(child)
                if seen == on_path:
                    return path[path.index(child) :] + [child]
                if seen is None:
                    state[child] = on_path
                    path.append(child)
                    pending.append(iter(edges.get(child, ())))
                    break
            else:
                state[path.pop()] = done
                pending.pop()
    return None


# ---- the Template_Filler (§A3) ----


def check_values(template: Template, values: Mapping[str, object]) -> list[Problem]:
    """The Filler's validation pass: every placeholder in template.used without a value, then every
    supplied name that matches no declared placeholder, then every type or bounds error in placeholder
    name order (Req 7.3, 7.7). Empty when fill can fill."""
    declared = template.placeholders
    problems = [
        Problem(template.name, name, None, "no value was supplied")
        for name in sorted(template.used.difference(values.keys()))
    ]
    unknown = [key for key in values if not (isinstance(key, str) and key in declared)]
    for key in sorted(unknown, key=_key_order):
        field = key if isinstance(key, str) else repr(key)
        reason = f"matches no placeholder of the Render_Template; got {values[key]!r}"
        problems.append(Problem(template.name, field, None, reason))
    for name in sorted(key for key in values if isinstance(key, str) and key in declared):
        problems += _value_problems(template.name, declared[name], values[name])
    return problems


def _key_order(key: object) -> tuple[int, str]:
    return (0, key) if isinstance(key, str) else (1, repr(key))


def _value_problems(template_name: str, spec: Placeholder, value: object) -> list[Problem]:
    """The type rules of §A3, each Problem giving the declared type and bounds and the value."""

    def problem(field: str, rule: str, got: str) -> Problem:
        return Problem(template_name, field, None, f"must be {rule}; got {got}")

    kind = spec.type
    if kind == "int":
        if type(value) is int and spec.min <= value <= spec.max:
            return []
        return [problem(spec.name, f"an int from {spec.min!r} to {spec.max!r}", repr(value))]
    if kind == "float":
        if _is_number(value) and spec.min <= value <= spec.max:
            return []
        return [problem(spec.name, f"an int or a finite float from {spec.min!r} to {spec.max!r}", repr(value))]
    if kind == "text":
        return [] if isinstance(value, str) else [problem(spec.name, "a str", repr(value))]
    if kind == "file":
        return [] if isinstance(value, str) and value else [problem(spec.name, "a non-empty str", repr(value))]
    if kind == "bool":
        return [] if type(value) is bool else [problem(spec.name, "a bool", repr(value))]
    rule = f"a list of at most {spec.max_items} LoRA entries"
    if not isinstance(value, (list, tuple)):
        return [problem(spec.name, rule, repr(value))]
    problems = []
    if len(value) > spec.max_items:
        problems.append(problem(spec.name, rule, f"{len(value)} entries"))
    strength_rule = f"an int or a finite float from {spec.strength_min!r} to {spec.strength_max!r}"
    for number, entry in enumerate(value, 1):
        where = f"{spec.name}[{number}]"
        if not isinstance(entry, Mapping):
            problems.append(problem(where, "an object with exactly a name and a strength", repr(entry)))
            continue
        if set(entry) != _LORA_ENTRY_KEYS:
            problems.append(problem(where, "an object with exactly a name and a strength", repr(entry)))
        if "name" in entry and not (isinstance(entry["name"], str) and entry["name"]):
            problems.append(problem(f"{where}.name", "a non-empty str", repr(entry["name"])))
        if "strength" in entry:
            strength = entry["strength"]
            if not (_is_number(strength) and spec.strength_min <= strength <= spec.strength_max):
                problems.append(problem(f"{where}.strength", strength_rule, repr(strength)))
    return problems


def fill(template: Template, values: Mapping[str, object]) -> dict[str, Any]:
    """The prompt template.prompt becomes with values, a new dict; or a FillError listing every Problem of
    check_values, and no prompt (Req 7.3, 7.7).

    Node ids, classes, input names, every other node field and every placeholder-free value are copied
    unchanged; so is the node order, with the LoRA loaders after the template's nodes (Req 7.2):
    - an input whose value is exactly "{{x}}" takes the value as supplied: int and float as JSON numbers,
      text and file as strings, bool as a boolean (Req 7.4); for a lora_list, the chain's end
    - placeholders inside a longer string are replaced in one pass over the template's own string, with the
      supplied string or the int in decimal, so {{…}} inside a supplied value stays as supplied (Req 7.8)
    - the k-th entry (from 1) of the i-th lora_list in name order (from 0) becomes loader node
      str(900 + 20 * i + k) of the declared class and loader_inputs: $prev is the declared source for the
      first loader and the previous loader's output [id, 0] for each further one, $name and $strength the
      entry's. Every "{{x}}" of the list takes the last loader's output, or the source when the list is
      empty (Req 7.5). The ids depend only on the names and entry numbers (Req 7.6).
    """
    problems = check_values(template, values)
    if problems:
        raise FillError(template.name, problems)
    ends, loaders = _lora_chains(template, values)
    filled: dict[str, Any] = {}
    for node_id, node in template.prompt.items():
        filled[node_id] = {
            key: {name: _fill_input(value, template, values, ends) for name, value in item.items()}
            if key == "inputs"
            else _clone(item)
            for key, item in node.items()
        }
    filled.update(loaders)
    return filled


def _lora_chains(
    template: Template, values: Mapping[str, object]
) -> tuple[dict[str, list[Any]], dict[str, dict[str, Any]]]:
    """The link each lora_list's placeholder becomes, and the loader nodes, in id order."""
    ends: dict[str, list[Any]] = {}
    loaders: dict[str, dict[str, Any]] = {}
    for index, name in enumerate(template.lora_lists):
        spec = template.placeholders[name]
        previous: list[Any] = list(spec.source)
        for number, entry in enumerate(values[name], 1):
            node_id = str(FIRST_LOADER_ID + LOADER_ID_STRIDE * index + number)
            inputs: dict[str, Any] = {}
            for input_name, token in spec.loader_inputs:
                if token == "$prev":
                    inputs[input_name] = list(previous)
                elif token == "$name":
                    inputs[input_name] = entry["name"]
                else:
                    inputs[input_name] = entry["strength"]
            loaders[node_id] = {"class_type": spec.loader, "inputs": inputs}
            previous = [node_id, 0]
        ends[name] = previous
    return ends, loaders


def _fill_input(
    value: object, template: Template, values: Mapping[str, object], ends: Mapping[str, list[Any]]
) -> object:
    if not isinstance(value, str):
        return _clone(value)
    whole = PLACEHOLDER.fullmatch(value)
    if whole is not None:
        name = whole.group(1)
        return list(ends[name]) if name in ends else values[name]
    if "{{" not in value:
        return value
    # A function as the replacement, so a supplied "\1" or "\g<0>" is inserted as it is.
    return PLACEHOLDER.sub(lambda match: _as_text(values[match.group(1)]), value)


def _as_text(value: object) -> str:
    """A text or file value as it is, an int in decimal."""
    return value if isinstance(value, str) else str(value)


def _clone(value: object) -> object:
    """A copy of JSON data."""
    if isinstance(value, dict):
        return {key: _clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone(item) for item in value]
    return value


# ---- small predicates ----


def _is_number(value: object) -> bool:
    """An int or a finite float, never a bool."""
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _is_count(value: object) -> bool:
    return type(value) is int and value > 0


def _is_link(value: object) -> bool:
    """ComfyUI's link shape, [node id, output index]."""
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int


def _is_pair(value: object, minimum: int) -> bool:
    return isinstance(value, list) and len(value) == 2 and all(type(v) is int and v >= minimum for v in value)


def _strings(value: object) -> Iterator[str]:
    """Every string in value, mapping keys included."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def _has_placeholder(value: object) -> bool:
    return any(PLACEHOLDER.search(text) for text in _strings(value))


def _declares(data: object, kind: str) -> bool:
    return isinstance(data, dict) and data.get("type") == kind


def _braced(name: str) -> str:
    return "{{" + name + "}}"


def _describe(value: object) -> str:
    names = {dict: "an object", list: "a list", str: "a string", bool: "a boolean", int: "a number",
             float: "a number", type(None): "null"}  # fmt: skip
    return names.get(type(value), f"a {type(value).__name__}")
