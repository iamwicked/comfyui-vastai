"""The Config_Checker's Render_Template rules (design §C10 rows "Templates" and "Extra (graph)"; Req 26.11,
12.2, 8.6, 11.2, 12.4).

check_templates(templates, config) runs every rule over the given Render_Template texts and returns every
Problem found, in file order; an empty list means the templates pass. Each Problem names the file, the
item (a meta field or a prompt node) and the rule: its reason starts with "templates:" for the Req 26.11
group and "graph:" for the §C10 extra graph rules. The check-config command (task 25) reads the files and
joins these Problems to the rest of its report.

The Req 26.11 group ("templates"):
- the file is UTF-8 JSON with no key twice in one object
- the document passes templates.parse_template, which holds the §M6 metadata rules, among them "every
  placeholder in the graph is declared" and "every node class has a node_packs entry"
- every pack in node_packs is core or the repo name of a nodes.txt line in the Selection of the
  template's catalog_id
- every models entry is installed by a models.list line of that Selection at models/<kind's default
  folder>/<name>, and every extra_files entry not marked derived at models/<folder>/<name>

The §C10 graph rules ("graph"), for the stills kinds:
- every whole-image decode (VAEDecode, VAEUtils_VAEDecodeTiled) feeds exactly one VAEDeGrid and nothing else
- a still size row's output has exactly its W:H ratio
- an edit size row is 3:4 or 4:5 with exactly that ratio, sides in multiples of 16 and an area within 10 %
  of 1024×1024; the edit graph's VAEEncode takes its pixels from an ImageScale fed by the LoadImage of
  {{reference_image}}
- finish_factor, for every still and edit size at its Post types' export size, stays inside the finish
  template's upscale bounds

Pure: the caller passes every text, so nothing here reads a file or starts a process.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from pathlib import PurePath
from typing import Any

from influencer import lines as L
from influencer import templates as T
from influencer.errors import Problem, ValidationError
from influencer.geometry import finish_factor

RULE_TEMPLATES = "templates"  # the Req 26.11 group
RULE_GRAPH = "graph"  # the §C10 extra graph rules

CORE = "core"
WHOLE_IMAGE_DECODES = ("VAEDecode", "VAEUtils_VAEDecodeTiled")
DEGRID = "VAEDeGrid"

EDIT_RATIOS = ("3:4", "4:5")
EDIT_MULTIPLE = 16
EDIT_AREA = 1024 * 1024  # the reference pipeline's working size (Req 11.2)
EDIT_AREA_TOLERANCE_PERCENT = 10
REFERENCE_IMAGE = "{{reference_image}}"

# The export size of the Post types that use each aspect (requirements glossary): Feed_Size for single and
# carousel Posts (3:4 by default, 4:5 when set), Vertical_Size for stories.
EXPORT_SIZES = {"3:4": (1080, 1440), "4:5": (1080, 1350), "9:16": (1080, 1920)}
FINISH_SOURCE_KINDS = ("still", "edit")  # the kinds whose Picks the finish template upscales
_RATIO = re.compile(r"([1-9][0-9]*):([1-9][0-9]*)")  # a sizes key, as templates.py reads it


def check_templates(templates: Mapping[str, str | bytes], config: Mapping[str, str | None]) -> list[Problem]:
    """Every Problem of the Render_Templates, path -> file text, against config, which maps catalog.txt,
    models.list and nodes.txt (lines.CATALOG, lines.MODELS, lines.NODES) to their text as lines.select
    takes them. The finish rule reads every template given, so pass them all at once."""
    problems: list[Problem] = []
    selections: dict[str, L.Selection | str] = {}  # catalog id -> its Selection, or why there is none
    metas: list[tuple[str, dict[str, Any]]] = []
    for path, text in templates.items():
        document = _read_json(path, text, problems)
        if document is None:
            continue
        schema = _schema_problems(path, document)
        problems += schema
        if not isinstance(document, dict):
            continue
        meta = document.get("meta") if isinstance(document.get("meta"), dict) else {}
        prompt = document.get("prompt") if isinstance(document.get("prompt"), dict) else {}
        reported = {problem.field for problem in schema}
        problems += _selection_problems(path, meta, config, selections)
        problems += _graph_problems(path, meta, prompt, reported)
        metas.append((path, meta))
    problems += _finish_factor_problems(metas)
    return problems


# ---- the Req 26.11 group ----


def _problem(path: str, field: str | None, rule: str, reason: str, line: int | None = None) -> Problem:
    return Problem(path, field, line, f"{rule}: {reason}")


def _read_json(path: str, text: str | bytes, problems: list[Problem]) -> object | None:
    """The parsed document, or None when the text is not UTF-8 JSON. A key given twice is reported, and
    the document (holding the key's last value, as json does) is still checked."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            line = text.count(b"\n", 0, error.start) + 1
            problems.append(_problem(path, None, RULE_TEMPLATES, "the file is not UTF-8 text", line))
            return None
    elif text.startswith("\ufeff"):
        text = text[1:]
    repeated: list[str] = []
    try:
        document = json.loads(text, object_pairs_hook=_pairs_hook(repeated), parse_constant=_no_constant)
    except json.JSONDecodeError as error:
        problems.append(_problem(path, None, RULE_TEMPLATES, f"not valid JSON: {error.msg}", error.lineno))
        return None
    except ValueError as error:  # NaN or Infinity
        problems.append(_problem(path, None, RULE_TEMPLATES, f"not valid JSON: {error}"))
        return None
    problems += [_problem(path, None, RULE_TEMPLATES, f"the key {key!r} appears twice in one object") for key in repeated]
    return document


def _pairs_hook(repeated: list[str]) -> Callable[[list[tuple[str, Any]]], dict[str, Any]]:
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


def _schema_problems(path: str, document: object) -> list[Problem]:
    """templates.parse_template's Problems, with the file's stem as the name meta.name must equal."""
    stem = PurePath(path).stem
    name = stem if T.TEMPLATE_NAME.fullmatch(stem) else None
    try:
        T.parse_template(document, path, name=name)
    except ValidationError as error:
        return [_problem(p.path, p.field, RULE_TEMPLATES, p.reason, p.line) for p in error.problems]
    return []


def repo_key(name: str) -> str:
    """A pack or repo name as Req 26.5 compares them: lowercased, without a trailing .git."""
    key = name.strip().lower()
    return key[:-4] if key.endswith(".git") else key


def repo_name(url: str) -> str:
    """The repo name of a nodes.txt URL: its last path segment."""
    return url.strip().rstrip("/").rsplit("/", 1)[-1]


def _selection(catalog_id: str, config: Mapping[str, str | None], cache: dict[str, L.Selection | str]) -> L.Selection | str:
    if catalog_id not in cache:
        try:
            cache[catalog_id] = L.select(config, catalog_id)
        except ValidationError as error:
            cache[catalog_id] = "; ".join(problem.reason for problem in error.problems)
    return cache[catalog_id]


def _selection_problems(
    path: str, meta: Mapping[str, Any], config: Mapping[str, str | None], cache: dict[str, L.Selection | str]
) -> list[Problem]:
    """Packs, models and extra files against the Selection of meta.catalog_id."""
    catalog_id = meta.get("catalog_id")
    if not isinstance(catalog_id, str):
        return []  # parse_template reports it
    selection = _selection(catalog_id, config, cache)
    if isinstance(selection, str):
        return [_problem(path, "meta.catalog_id", RULE_TEMPLATES, f"no Selection for {catalog_id!r}: {selection}")]
    problems: list[Problem] = []

    packs = meta.get("node_packs")
    if isinstance(packs, dict):
        installed = {repo_key(repo_name(line.url)) for line in selection.nodes}
        for class_type, pack in packs.items():
            if isinstance(pack, str) and pack != CORE and repo_key(pack) not in installed:
                problems.append(_problem(
                    path, f"meta.node_packs.{class_type}", RULE_TEMPLATES,
                    f"the pack {pack!r} is neither {CORE} nor installed by a nodes.txt line of the {catalog_id} Selection",
                ))  # fmt: skip

    install_paths = {L.install_path(line) for line in selection.models if line.kind != L.WORKFLOW_KIND}
    models = meta.get("models")
    if isinstance(models, list):
        for number, model in enumerate(models, 1):
            if not isinstance(model, dict):
                continue  # parse_template reports it
            kind, name = model.get("kind"), model.get("name")
            if not (isinstance(kind, str) and isinstance(name, str) and kind.strip() and name.strip()):
                continue  # parse_template reports it
            target = f"models/{L.kind_folder(kind)}/{name}"
            if target not in install_paths:
                problems.append(_problem(
                    path, f"meta.models[{number}]", RULE_TEMPLATES,
                    f"no models.list line of the {catalog_id} Selection installs {name!r} at {target}",
                ))  # fmt: skip

    extra_files = meta.get("extra_files")
    if isinstance(extra_files, list):
        for number, entry in enumerate(extra_files, 1):
            where = f"meta.extra_files[{number}]"
            if not (
                isinstance(entry, dict)
                and set(entry) <= {"folder", "name", "derived"}
                and all(isinstance(entry.get(key), str) and entry[key].strip() for key in ("folder", "name"))
                and type(entry.get("derived", False)) is bool
            ):
                problems.append(_problem(path, where, RULE_TEMPLATES,
                                         f"must be an object of folder, name and optionally derived: true; got {entry!r}"))  # fmt: skip
                continue
            if entry.get("derived", False):
                continue  # made on the Instance from another file, so no line installs it
            target = f"models/{entry['folder']}/{entry['name']}"
            if target not in install_paths:
                problems.append(_problem(path, where, RULE_TEMPLATES,
                                         f"no models.list line of the {catalog_id} Selection installs {target}"))  # fmt: skip
    return problems


# ---- the §C10 graph rules ----


def _is_link(value: object) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int


def _class_of(prompt: Mapping[str, Any], node_id: object) -> str | None:
    node = prompt.get(node_id) if isinstance(node_id, str) else None
    return node.get("class_type") if isinstance(node, dict) else None


def _inputs(node: object) -> Mapping[str, Any]:
    inputs = node.get("inputs") if isinstance(node, dict) else None
    return inputs if isinstance(inputs, dict) else {}


def _pair(value: object) -> tuple[int, int] | None:
    """[a, b] of ints from 1 as a tuple, else None."""
    if isinstance(value, list) and len(value) == 2 and all(type(v) is int and v >= 1 for v in value):
        return value[0], value[1]
    return None


def _ratio(key: object) -> tuple[int, int] | None:
    match = _RATIO.fullmatch(key) if isinstance(key, str) else None
    return (int(match[1]), int(match[2])) if match else None


def _graph_problems(path: str, meta: Mapping[str, Any], prompt: Mapping[str, Any], reported: set[str | None]) -> list[Problem]:
    kind = meta.get("kind")
    if kind not in T.STILLS_KINDS:
        return []
    problems = _decode_problems(path, prompt)
    sizes = meta.get("sizes") if isinstance(meta.get("sizes"), dict) else {}
    if kind == "still":
        for key, size in sizes.items():
            problems += _exact_ratio_problems(path, key, size, reported)
    elif kind == "edit":
        for key, size in sizes.items():
            problems += _edit_size_problems(path, key, size, reported)
        problems += _edit_canvas_problems(path, prompt)
    return problems


def _decode_problems(path: str, prompt: Mapping[str, Any]) -> list[Problem]:
    """Every whole-image decode feeds exactly one VAEDeGrid and nothing else (Req 12.2)."""
    consumers: dict[str, list[str]] = {}
    for node_id, node in prompt.items():
        for value in _inputs(node).values():
            if _is_link(value):
                consumers.setdefault(value[0], []).append(node_id)
    problems = []
    for node_id, node in prompt.items():
        class_type = _class_of(prompt, node_id)
        if class_type not in WHOLE_IMAGE_DECODES:
            continue
        readers = consumers.get(node_id, [])
        if len(readers) != 1 or _class_of(prompt, readers[0]) != DEGRID:
            shown = ", ".join(f"{reader} ({_class_of(prompt, reader)})" for reader in readers) or "no node"
            problems.append(_problem(
                path, f"prompt.{node_id}", RULE_GRAPH,
                f"the whole-image decode {class_type} feeds {shown}; it must feed exactly one {DEGRID} and nothing else",
            ))  # fmt: skip
    return problems


def _exact_ratio_problems(path: str, key: object, size: object, reported: set[str | None]) -> list[Problem]:
    ratio, output = _ratio(key), _pair(size.get("output")) if isinstance(size, dict) else None
    field = f"meta.sizes.{key}.output"
    if ratio is None or output is None or field in reported:
        return []  # parse_template reports a bad key, a bad pair and an inexact ratio
    if output[0] * ratio[1] != output[1] * ratio[0]:
        return [_problem(path, field, RULE_GRAPH, f"{output[0]}x{output[1]} is not exactly {key}")]
    return []


def _edit_size_problems(path: str, key: object, size: object, reported: set[str | None]) -> list[Problem]:
    """3:4 or 4:5 exactly, sides in multiples of 16, area within 10 % of 1024×1024 (Req 11.2)."""
    field = f"meta.sizes.{key}.output"
    if key not in EDIT_RATIOS:
        return [_problem(path, f"meta.sizes.{key}", RULE_GRAPH, f"an edit size must be one of {', '.join(EDIT_RATIOS)}")]
    problems = _exact_ratio_problems(path, key, size, reported)
    output = _pair(size.get("output")) if isinstance(size, dict) else None
    if output is None:
        return problems  # parse_template reports it
    width, height = output
    if width % EDIT_MULTIPLE or height % EDIT_MULTIPLE:
        problems.append(_problem(path, field, RULE_GRAPH,
                                 f"{width}x{height} has a side that is not a multiple of {EDIT_MULTIPLE}"))  # fmt: skip
    if 100 * abs(width * height - EDIT_AREA) > EDIT_AREA_TOLERANCE_PERCENT * EDIT_AREA:
        problems.append(_problem(path, field, RULE_GRAPH,
                                 f"{width}x{height} is not within {EDIT_AREA_TOLERANCE_PERCENT} % of 1024x1024 in area"))  # fmt: skip
    return problems


def _edit_canvas_problems(path: str, prompt: Mapping[str, Any]) -> list[Problem]:
    """The edit graph's VAEEncode takes its pixels from an ImageScale fed by the reference LoadImage."""
    encoders = [node_id for node_id in prompt if _class_of(prompt, node_id) == "VAEEncode"]
    if not encoders:
        return [_problem(path, "prompt", RULE_GRAPH, "the edit graph has no VAEEncode for the canvas")]
    problems = []
    for node_id in encoders:
        pixels = _inputs(prompt[node_id]).get("pixels")
        scale = pixels[0] if _is_link(pixels) else None
        image = _inputs(prompt.get(scale)).get("image") if _class_of(prompt, scale) == "ImageScale" else None
        loader = image[0] if _is_link(image) else None
        if not (_class_of(prompt, loader) == "LoadImage" and _inputs(prompt[loader]).get("image") == REFERENCE_IMAGE):
            problems.append(_problem(
                path, f"prompt.{node_id}.inputs.pixels", RULE_GRAPH,
                f"the VAEEncode must take its pixels from an ImageScale fed by the LoadImage of {REFERENCE_IMAGE}",
            ))  # fmt: skip
    return problems


def _finish_factor_problems(metas: list[tuple[str, dict[str, Any]]]) -> list[Problem]:
    """finish_factor for every still and edit size, at its Post types' export size, lies within each
    finish template's upscale bounds (Req 12.4, Decision 27)."""
    finishes = [(path, meta) for path, meta in metas if meta.get("kind") == "finish"]
    if not finishes:
        return []
    picks: list[tuple[str, str, tuple[int, int], tuple[int, int]]] = []  # path, aspect, Pick size, export size
    problems: list[Problem] = []
    for path, meta in metas:
        if meta.get("kind") not in FINISH_SOURCE_KINDS or not isinstance(meta.get("sizes"), dict):
            continue
        for key, size in meta["sizes"].items():
            output = _pair(size.get("output")) if isinstance(size, dict) else None
            if output is None:
                continue  # parse_template reports it
            if key not in EXPORT_SIZES:
                problems.append(_problem(path, f"meta.sizes.{key}", RULE_GRAPH,
                                         f"no Post type exports at {key}, so the finish factor is unknown"))  # fmt: skip
                continue
            picks.append((path, key, output, EXPORT_SIZES[key]))
    for path, meta in finishes:
        placeholders = meta.get("placeholders")
        spec = placeholders.get("upscale") if isinstance(placeholders, dict) else None
        low, high = (spec.get("min"), spec.get("max")) if isinstance(spec, dict) else (None, None)
        if not (isinstance(spec, dict) and spec.get("type") == "int" and type(low) is int and type(high) is int):
            problems.append(_problem(path, "meta.placeholders.upscale", RULE_GRAPH,
                                     "the finish template needs an int upscale placeholder with min and max"))  # fmt: skip
            continue
        for source, key, (width, height), (export_w, export_h) in picks:
            factor = finish_factor(width, height, export_w, export_h)
            if not low <= factor <= high:
                problems.append(_problem(
                    path, "meta.placeholders.upscale", RULE_GRAPH,
                    f"finish_factor is {factor} for the {width}x{height} {key} Pick of {source} at the "
                    f"{export_w}x{export_h} export size, outside the upscale bounds {low}-{high}",
                ))  # fmt: skip
    return problems
