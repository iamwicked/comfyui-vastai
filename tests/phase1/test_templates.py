"""Tests of the Render_Template loader and the Template_Filler, scripts/influencer/templates.py (task 12;
design §C7, §M6, §A3; Req 7, 10.7).

- Properties 17-22 on generated templates (tests/strategies_templates.py); the real Render_Templates
  arrive with the render stage (task 18)
- load_template on a temp folder and its default folder, the §M6 metadata rules and every graph rejection
- the Filler's type rules one example each, and the §M6 Still_Template shape filled as §A3 describes
"""
from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.harness import REPO, run_bounded
from strategies import texts

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

import strategies_templates as ST  # noqa: E402
from influencer import templates as T  # noqa: E402
from influencer.errors import ValidationError  # noqa: E402

PLACEHOLDER = ST.PLACEHOLDER  # the design's pattern, not the module's

# ---- helpers ----


def as_json(value: object) -> str:
    """value as JSON text: equal texts mean equal values of equal JSON types (1, 1.0 and true differ)."""
    return json.dumps(value, ensure_ascii=False)


def canonical(prompt: object) -> bytes:
    """Canonical JSON: sorted keys, no spaces, UTF-8."""
    return json.dumps(prompt, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def as_text(value: object) -> str:
    """How §A3 writes a value into a longer string: a string as it is, an int in decimal."""
    return value if isinstance(value, str) else format(value, "d")


def rendered(template_value: str, values: dict[str, Any]) -> str:
    """The in-string rule, by re.split: each placeholder becomes its value, every other character stays."""
    parts = PLACEHOLDER.split(template_value)
    return "".join(part if index % 2 == 0 else as_text(values[part]) for index, part in enumerate(parts))


def is_link(value: object) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int


def links(prompt: dict[str, Any]) -> Iterator[tuple[str, list[Any]]]:
    """(node id, link) of every link in prompt."""
    for node_id, node in prompt.items():
        for value in node["inputs"].values():
            if is_link(value):
                yield node_id, value


def is_acyclic(prompt: dict[str, Any]) -> bool:
    """Kahn's algorithm over the links of prompt."""
    needs = {node_id: set() for node_id in prompt}
    for node_id, link in links(prompt):
        needs[node_id].add(link[0])
    ready = [node_id for node_id, sources in needs.items() if not sources]
    done = set()
    while ready:
        node_id = ready.pop()
        done.add(node_id)
        for other, sources in needs.items():
            if node_id in sources:
                sources.discard(node_id)
                if not sources and other not in done:
                    ready.append(other)
    return len(done) == len(prompt)


def strings_in(value: object) -> Iterator[str]:
    """Every string in value, mapping keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)


def lora_names(template: T.Template) -> list[str]:
    return sorted(name for name, spec in template.meta["placeholders"].items() if spec["type"] == "lora_list")


def neutral(values: dict[str, Any], template: T.Template) -> dict[str, Any]:
    """values with every text, file and LoRA name replaced by brace-free text."""
    specs = template.meta["placeholders"]
    plain = {}
    for name, value in values.items():
        kind = specs[name]["type"]
        if kind in ("text", "file"):
            value = "v"
        elif kind == "lora_list":
            value = [{"name": "v.safetensors", "strength": entry["strength"]} for entry in value]
        plain[name] = value
    return plain


@st.composite
def fill_cases(draw: st.DrawFn, **options: Any) -> tuple[T.Template, dict[str, Any]]:
    template = draw(ST.templates(**options))
    return template, draw(ST.values_for(template))


# ---- Property 17 ----


# Feature: ai-influencer-pipeline, Property 17: Filling preserves the template's structure
@given(case=fill_cases())
def test_filling_preserves_the_templates_structure(case: tuple[T.Template, dict[str, Any]]) -> None:
    """Every node id, class, input name, other node field and placeholder-free value comes through
    unchanged and in order; every placeholder is replaced, and supplied {{…}} text stays as supplied; the
    only additions are the LoRA loaders and the links to them. The template itself is not changed.

    **Validates: Requirements 7.2**
    """
    template, values = case
    specs = template.meta["placeholders"]
    before = as_json(ST.document_of(template))
    filled = T.fill(template, values)
    assert as_json(ST.document_of(template)) == before

    assert [node_id for node_id in filled if node_id in template.prompt] == list(template.prompt)
    added = [node_id for node_id in filled if node_id not in template.prompt]
    assert len(added) == sum(len(values[name]) for name in lora_names(template))
    assert all(int(node_id) >= T.FIRST_LOADER_ID for node_id in added)
    for node_id, node in template.prompt.items():
        out = filled[node_id]
        assert list(out) == list(node)
        assert all(as_json(out[key]) == as_json(node[key]) for key in node if key != "inputs")
        assert list(out["inputs"]) == list(node["inputs"])
        for input_name, value in node["inputs"].items():
            got = out["inputs"][input_name]
            names = PLACEHOLDER.findall(value) if isinstance(value, str) else []
            if not names:
                assert as_json(got) == as_json(value)
            elif PLACEHOLDER.fullmatch(value) and specs[names[0]]["type"] == "lora_list":
                source = specs[names[0]]["source"]
                assert got == source or (is_link(got) and got[0] in added and got[1] == 0)
            elif PLACEHOLDER.fullmatch(value):
                assert as_json(got) == as_json(values[names[0]])  # supplied {{…}} included
            else:
                assert got == rendered(value, values)

    # With brace-free values, no placeholder is left anywhere in the filled prompt.
    assert not any(PLACEHOLDER.search(text) for text in strings_in(T.fill(template, neutral(values, template))))


# ---- Property 18 ----

KIND_TYPES = {"int": (int,), "float": (int, float), "text": (str,), "file": (str,), "bool": (bool,)}


# Feature: ai-influencer-pipeline, Property 18: Whole-value placeholders take the supplied value unchanged
@given(case=fill_cases())
def test_whole_value_placeholders_take_the_supplied_value_unchanged(case: tuple[T.Template, dict[str, Any]]) -> None:
    """After a JSON round trip, each input whose template value is exactly {{x}} equals x's value with the
    same JSON type: ints and floats as numbers, text and file names (quotes, backslashes, line breaks and
    emoji among them) as strings, booleans as booleans.

    **Validates: Requirements 7.4**
    """
    template, values = case
    specs = template.meta["placeholders"]
    sent = json.loads(json.dumps(T.fill(template, values)))
    for node_id, node in template.prompt.items():
        for input_name, value in node["inputs"].items():
            whole = PLACEHOLDER.fullmatch(value) if isinstance(value, str) else None
            if whole is None or specs[whole[1]]["type"] == "lora_list":
                continue
            supplied, got = values[whole[1]], sent[node_id]["inputs"][input_name]
            assert type(got) is type(supplied) and type(got) in KIND_TYPES[specs[whole[1]]["type"]]
            assert got == supplied and as_json(got) == as_json(supplied)


# ---- Property 19 ----

_AFFIX = texts(max_size=12).filter(lambda text: "{{" not in text)
_NON_EMPTY_AFFIX = texts(min_size=1, max_size=12).filter(lambda text: "{{" not in text)


@st.composite
def in_string_cases(draw: st.DrawFn) -> tuple[str, str, str, dict[str, Any], object]:
    """(prefix, suffix, name, declaration, value): prefix and suffix are texts() without "{{", not both
    empty, and the value is a valid int, text or file value."""
    prefix = draw(_AFFIX)
    suffix = draw(_AFFIX if prefix else _NON_EMPTY_AFFIX)
    name = draw(ST.placeholder_names)
    kind = draw(st.sampled_from(("text", "file", "int")))
    if kind == "int":
        low = draw(st.integers(-(2**40), 2**40))
        high = low + draw(st.integers(0, 2**40))
        return prefix, suffix, name, {"type": "int", "min": low, "max": high}, draw(st.integers(low, high))
    # seeded with the placeholder's own syntax, which must come through as supplied
    return prefix, suffix, name, {"type": kind}, draw(ST.supplied_texts([name], non_empty=kind == "file"))


# Feature: ai-influencer-pipeline, Property 19: Placeholders inside longer strings
@given(case=in_string_cases())
def test_placeholders_inside_longer_strings(case: tuple[str, str, str, dict[str, Any], object]) -> None:
    """prefix + "{{x}}" + suffix fills to prefix + the value + suffix, an int written in decimal, every
    other character unchanged.

    **Validates: Requirements 7.8**
    """
    prefix, suffix, name, spec, value = case
    prompt = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": prefix + "{{" + name + "}}" + suffix}}}
    template = T.parse_template(ST.make_document(prompt, {name: spec}))
    filled = T.fill(template, {name: value})
    assert filled["1"]["inputs"]["text"] == prefix + as_text(value) + suffix


# ---- Property 20 ----


@st.composite
def lora_cases(draw: st.DrawFn) -> tuple[T.Template, dict[str, Any]]:
    """A template with 1-2 lora_lists, each given 0 to max_items (mostly 16) valid entries."""
    template = draw(ST.templates(min_loras=1))
    values = draw(ST.values_for(template))
    for name in lora_names(template):
        spec = template.meta["placeholders"][name]
        entries, size = ST.lora_entries(spec["strength_min"], spec["strength_max"]), spec["max_items"]
        full = st.lists(entries, min_size=size, max_size=size)  # a full list, often
        values[name] = draw(st.one_of(st.lists(entries, max_size=size), full))
    return template, values


# Feature: ai-influencer-pipeline, Property 20: LoRA chains are well-formed
@given(case=lora_cases())
def test_lora_chains_are_well_formed(case: tuple[T.Template, dict[str, Any]]) -> None:
    """One loader per entry in list order with its name and strength; the first takes the declared source
    and each further one the previous loader; every consumer takes the last loader, or the source for an
    empty list; every link targets an existing node, and the prompt is acyclic.

    **Validates: Requirements 7.5**
    """
    template, values = case
    filled = T.fill(template, values)
    loader_ids = set()
    for index, name in enumerate(lora_names(template)):
        spec = template.meta["placeholders"][name]
        previous = spec["source"]
        for number, entry in enumerate(values[name], 1):
            node_id = str(900 + 20 * index + number)
            loader_ids.add(node_id)
            tokens = {"$prev": previous, "$name": entry["name"], "$strength": entry["strength"]}
            expected = {input_name: tokens[token] for input_name, token in spec["loader_inputs"].items()}
            assert filled[node_id]["class_type"] == spec["loader"]
            assert as_json(filled[node_id]["inputs"]) == as_json(expected)
            previous = [node_id, 0]
        consumers = [
            (node_id, input_name) for node_id, node in template.prompt.items()
            for input_name, value in node["inputs"].items() if value == "{{" + name + "}}"
        ]  # fmt: skip
        assert consumers
        assert all(filled[node_id]["inputs"][input_name] == previous for node_id, input_name in consumers)
    assert set(filled) - set(template.prompt) == loader_ids
    assert all(link[0] in filled for _, link in links(filled))
    assert is_acyclic(filled)


# ---- Property 21 ----

# Fills the template and values it reads from stdin, and writes both serializations of the filled prompt.
CHILD = """\
import json, sys
from influencer import templates
request = json.loads(sys.stdin.buffer.read())
filled = templates.fill(templates.parse_template(request["template"]), request["values"])
out = json.dumps(filled, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
sys.stdout.buffer.write(out + b"\\n" + json.dumps(filled, ensure_ascii=False).encode())
"""
_CHILD_RUNS: list[bytes] = []  # set once an example of this run has matched a separate process


def serialized(prompt: object) -> bytes:
    """The canonical JSON, a line break, and the JSON in the prompt's own key order."""
    return canonical(prompt) + b"\n" + as_json(prompt).encode()


# Feature: ai-influencer-pipeline, Property 21: Filling is repeatable
@settings(deadline=None)  # one example starts a Python process
@given(case=fill_cases(), data=st.data())
def test_filling_is_repeatable(case: tuple[T.Template, dict[str, Any]], data: st.DataObject) -> None:
    """Filling twice, with the values in any key order, gives byte-identical JSON, canonical and in the
    prompt's own order, loader ids included. The first example of a run that inserts loaders also fills
    in a separate Python process, with another hash seed, and must match byte for byte.

    **Validates: Requirements 7.6**
    """
    template, values = case
    order = data.draw(st.permutations(list(values)), label="key order")
    reordered = {key: values[key] for key in order}
    first = serialized(T.fill(template, values))
    assert serialized(T.fill(template, reordered)) == first
    if _CHILD_RUNS or not any(values[name] for name in lora_names(template)):
        return
    request = json.dumps({"template": ST.document_of(template), "values": reordered}).encode("ascii")
    environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SCRIPTS), "PYTHONDONTWRITEBYTECODE": "1",
                   "PYTHONHASHSEED": "random"}  # fmt: skip
    child = run_bounded([sys.executable, "-c", CHILD], input=request, env=environment, timeout=60)
    assert child.returncode == 0, child.stderr.decode(errors="replace")
    assert child.stdout == first
    _CHILD_RUNS.append(child.stdout)


# ---- Property 22 ----


@st.composite
def error_cases(draw: st.DrawFn) -> tuple[T.Template, dict[str, Any], tuple[ST.Injected, ...]]:
    template = draw(ST.templates())
    kinds = draw(st.sets(st.sampled_from(ST.ERROR_KINDS), min_size=1))
    values, injected = draw(ST.values_for(template, errors=kinds))
    return template, values, injected


def expected_fragments(template: T.Template, injected: ST.Injected) -> list[str]:
    """What the Problem of an injected error must say: the declared type or bounds, and the value."""
    if injected.kind == "missing":
        return ["no value"]
    if injected.kind == "unknown":
        return ["no placeholder", repr(injected.value)]
    spec = template.meta["placeholders"][injected.placeholder]
    if injected.kind == "too_many_loras":
        return [f"at most {spec['max_items']} LoRA entries", f"{injected.value} entries"]
    if injected.kind == "strength_out_of_bounds":
        return [f"from {spec['strength_min']!r} to {spec['strength_max']!r}", repr(injected.value)]
    if injected.kind == "empty_lora_name":
        return ["a non-empty str", "''"]
    rules = {
        "int": [f"an int from {spec.get('min')!r} to {spec.get('max')!r}"],
        "float": [f"finite float from {spec.get('min')!r} to {spec.get('max')!r}"],
        "text": ["a str"],
        "file": ["a non-empty str"],
        "bool": ["a bool"],
        "lora_list": [f"a list of at most {spec.get('max_items')} LoRA entries"],
    }
    return rules[spec["type"]] + [repr(injected.value)]


# Feature: ai-influencer-pipeline, Property 22: Filling errors are complete
@given(case=error_cases())
def test_filling_errors_are_complete(case: tuple[T.Template, dict[str, Any], tuple[ST.Injected, ...]]) -> None:
    """Missing and unknown names, wrong types (bool or 1.0 for an int among them), empty file names,
    out-of-bound numbers and strengths, and more than 16 LoRAs: fill returns no prompt, and one FillError
    names the template and each injected error, once, with its placeholder, rule and value.

    **Validates: Requirements 7.3, 7.7**
    """
    template, values, injected = case
    with pytest.raises(T.FillError) as caught:
        T.fill(template, values)
    error = caught.value
    assert error.template == template.name and template.name in str(error)
    assert all(problem.path == template.name for problem in error.problems)
    assert sorted(problem.field for problem in error.problems) == sorted(item.field for item in injected)
    for item in injected:
        [problem] = [problem for problem in error.problems if problem.field == item.field]
        for fragment in expected_fragments(template, item):
            assert fragment in problem.reason, (item, problem)


# ---- a Still_Template in the shape of §M6 ----


def still_document() -> dict[str, Any]:
    """A small Still_Template in the shape of design §M6: the turbo LoRA at node 4 is the source of the
    loras chain, which the sampler at node 7 consumes; node 15 saves."""
    loras = {
        "type": "lora_list", "max_items": 16, "strength_min": -2.0, "strength_max": 2.0, "source": ["4", 0],
        "loader": "LoraLoaderModelOnly",
        "loader_inputs": {"model": "$prev", "lora_name": "$name", "strength_model": "$strength"},
    }  # fmt: skip
    placeholders = {
        "prompt": {"type": "text"},
        "seed": {"type": "int", "min": 0, "max": 4294967295},
        "width": {"type": "int", "min": 256, "max": 2048},
        "height": {"type": "int", "min": 256, "max": 2048},
        "crop_x": {"type": "int", "min": 0, "max": 64},
        "output_prefix": {"type": "text"},
        "loras": loras,
    }
    prompt = {
        "1": {"inputs": {"unet_name": "krea2_raw_bf16.safetensors", "weight_dtype": "default"}, "class_type": "UNETLoader"},
        "2": {"inputs": {"clip_name": "qwen3vl_4b_bf16.safetensors", "type": "qwen_image"}, "class_type": "CLIPLoader"},
        "3": {"inputs": {"vae_name": "Wan2_1_VAE_fp32.safetensors"}, "class_type": "VAELoader"},
        "4": {"inputs": {"model": ["1", 0], "lora_name": "krea2_turbo_lora_rank_64_bf16.safetensors",
                         "strength_model": 1.0}, "class_type": "LoraLoaderModelOnly"},
        "5": {"inputs": {"text": "{{prompt}}", "clip": ["2", 0]}, "class_type": "CLIPTextEncode",
              "_meta": {"title": "Positive"}},
        "6": {"inputs": {"width": "{{width}}", "height": "{{height}}", "batch_size": 1}, "class_type": "EmptyLatentImage"},
        "7": {"inputs": {"model": "{{loras}}", "positive": ["5", 0], "latent_image": ["6", 0], "seed": "{{seed}}",
                         "steps": 8, "cfg": 1.0}, "class_type": "KSampler"},
        "8": {"inputs": {"samples": ["7", 0], "vae": ["3", 0]}, "class_type": "VAEDecode"},
        "10": {"inputs": {"image": ["8", 0], "x": "{{crop_x}}", "y": 0, "width": "{{width}}", "height": "{{height}}"},
               "class_type": "ImageCrop"},
        "15": {"inputs": {"images": ["10", 0], "filename_prefix": "influencer/{{output_prefix}}_s{{seed}}"},
               "class_type": "SaveImage"},
    }  # fmt: skip
    return ST.make_document(
        prompt, placeholders, name="still", identity_required=True, output_node="15", sizes=ST.SIZES[1],
        models=ST.MODELS[1],
    )  # fmt: skip


STILL_VALUES = {
    "prompt": "a woman on a tram, {{seed}}", "seed": 4294967295, "width": 1440, "height": 1920, "crop_x": 0,
    "output_prefix": "2026-W40/p01",
    "loras": [{"name": "looks/a.safetensors", "strength": 0.6}, {"name": "looks/b.safetensors", "strength": -1}],
}  # fmt: skip


def write_template(folder: Path, name: str, document: object) -> None:
    (folder / f"{name}.json").write_text(json.dumps(document, indent=2), encoding="utf-8")


def test_load_template_reads_the_named_file(tmp_path: Path) -> None:
    write_template(tmp_path, "still", still_document())
    template = T.load_template("still", tmp_path)
    assert (template.name, template.kind, template.path) == ("still", "still", str(tmp_path / "still.json"))
    assert (template.catalog_id, template.identity_required, template.output_node) == ("influencer", True, "15")
    assert template.timeout_s == 900  # meta sets none: the stills default (Req 10.7)
    assert template.used == {"prompt", "seed", "width", "height", "crop_x", "output_prefix", "loras"}
    assert template.lora_lists == ("loras",)
    assert template.placeholders["seed"] == T.Placeholder("seed", "int", min=0, max=4294967295)
    assert template.placeholders["loras"] == T.Placeholder(
        "loras", "lora_list", max_items=16, strength_min=-2.0, strength_max=2.0, source=("4", 0),
        loader="LoraLoaderModelOnly",
        loader_inputs=(("model", "$prev"), ("lora_name", "$name"), ("strength_model", "$strength")),
    )  # fmt: skip
    assert template.prompt == still_document()["prompt"]

    write_template(tmp_path, "still-sharp", still_document())  # meta.name still says "still"
    with pytest.raises(ValidationError) as caught:
        T.load_template("still-sharp", tmp_path)
    assert [(p.path, p.field) for p in caught.value.problems] == [(str(tmp_path / "still-sharp.json"), "meta.name")]
    assert "'still-sharp'" in caught.value.problems[0].reason


def test_load_template_reads_config_influencer_templates_by_default() -> None:
    assert T.TEMPLATES_DIR == REPO / "config" / "influencer" / "templates"
    with pytest.raises(ValidationError) as caught:
        T.load_template("no-such-template")
    [problem] = caught.value.problems
    assert problem.path == str(REPO / "config" / "influencer" / "templates" / "no-such-template.json")
    assert problem.reason == "the Render_Template is missing"


@pytest.mark.parametrize("kind", T.KINDS)
def test_the_timeout_defaults_by_kind(kind: str) -> None:
    extras = {"max_frames": 241, "fps": 24} if kind in T.REEL_KINDS else {}
    if kind == "ref_reel":
        extras["ref_slots"] = 9
    document = ST.make_document({"1": {"class_type": "SaveImage", "inputs": {}}}, {}, kind=kind, **extras)
    assert T.parse_template(document).timeout_s == (3600 if kind in ("reel", "ref_reel") else 900)
    document["meta"]["timeout_s"] = 1234
    assert T.parse_template(document).timeout_s == 1234


def test_the_still_example_fills_as_the_design_describes() -> None:
    template = T.parse_template(still_document())
    filled = T.fill(template, STILL_VALUES)
    assert list(filled) == [*still_document()["prompt"], "901", "902"]
    assert filled["901"] == {"class_type": "LoraLoaderModelOnly", "inputs": {
        "model": ["4", 0], "lora_name": "looks/a.safetensors", "strength_model": 0.6}}  # fmt: skip
    assert filled["902"] == {"class_type": "LoraLoaderModelOnly", "inputs": {
        "model": ["901", 0], "lora_name": "looks/b.safetensors", "strength_model": -1}}  # fmt: skip
    assert filled["7"]["inputs"] == {"model": ["902", 0], "positive": ["5", 0], "latent_image": ["6", 0],
                                     "seed": 4294967295, "steps": 8, "cfg": 1.0}  # fmt: skip
    assert filled["5"]["inputs"]["text"] == "a woman on a tram, {{seed}}"  # never expanded again
    assert filled["15"]["inputs"]["filename_prefix"] == "influencer/2026-W40/p01_s4294967295"
    assert filled["5"]["_meta"] == {"title": "Positive"}

    without = T.fill(template, {**STILL_VALUES, "loras": []})
    assert list(without) == list(still_document()["prompt"])
    assert without["7"]["inputs"]["model"] == ["4", 0]  # the source, for an empty list


def test_loader_ids_leave_room_for_17_lists_of_16() -> None:
    names = [f"l{index:02d}" for index in range(17)]
    loras = {"type": "lora_list", "max_items": 16, "strength_min": 0, "strength_max": 1, "source": ["1", 0],
             "loader": "LoraLoaderModelOnly",
             "loader_inputs": {"model": "$prev", "lora_name": "$name", "strength_model": "$strength"}}  # fmt: skip
    prompt = {"1": {"class_type": "UNETLoader", "inputs": {}},
              "2": {"class_type": "KSampler", "inputs": {f"m{name}": "{{" + name + "}}" for name in names}}}  # fmt: skip
    template = T.parse_template(ST.make_document(prompt, {name: dict(loras) for name in names}))
    entries = [{"name": f"{k}.safetensors", "strength": 1} for k in range(16)]
    filled = T.fill(template, {name: entries for name in names})
    added = sorted((node_id for node_id in filled if node_id not in prompt), key=int)
    assert added == [str(900 + 20 * i + k) for i in range(17) for k in range(1, 17)]
    assert int(added[-1]) == 1236 <= 1239
    assert filled["2"]["inputs"] == {f"m{name}": [str(900 + 20 * i + 16), 0] for i, name in enumerate(names)}

    document = ST.make_document(prompt, {name: dict(loras) for name in [*names, "l17"]})
    with pytest.raises(ValidationError) as caught:
        T.parse_template(document)
    assert [(p.field, p.reason) for p in caught.value.problems] == [
        ("meta.placeholders", "declares 18 lora_list placeholders; at most 17 fit the loader ids 900-1239")
    ]


def test_supplied_placeholder_syntax_is_never_expanded() -> None:
    prompt = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}} / {{seed}} / {{prompt}}",
                                                               "whole": "{{prompt}}"}}}  # fmt: skip
    specs = {"prompt": {"type": "text"}, "seed": {"type": "int", "min": 0, "max": 4294967295}}
    template = T.parse_template(ST.make_document(prompt, specs))
    supplied = "{{seed}} \\1 \\g<0> $prev {{prompt}}"
    inputs = T.fill(template, {"prompt": supplied, "seed": 7})["1"]["inputs"]
    assert inputs == {"text": f"{supplied} / 7 / {supplied}", "whole": supplied}


# ---- rejected templates ----

DELETE = object()


def _put(document: dict[str, Any], dotted: str, value: object) -> None:
    *parents, last = dotted.split(".")
    target = document
    for key in parents:
        target = target[key]
    if value is DELETE:
        del target[last]
    else:
        target[last] = value


def _rename_node(document: dict[str, Any], old: str, new: str) -> None:
    prompt = document["prompt"]
    document["prompt"] = {new if node_id == old else node_id: node for node_id, node in prompt.items()}
    if document["meta"]["output_node"] == old:
        document["meta"]["output_node"] = new


REJECTED = [
    ("undeclared", {"prompt.5.inputs.text": "{{scene}}"}, [("prompt.5.inputs.text", "does not declare")]),
    ("id-900", ("15", "900"), [("prompt.900", "below 900")]),
    ("id-1239", ("15", "1239"), [("prompt.1239", "below 900")]),
    ("id-not-a-number", ("15", "a15"), [("prompt.a15", "decimal number")]),
    ("id-leading-zero", ("15", "015"), [("prompt.015", "decimal number")]),
    ("lora-in-a-string", {"prompt.5.inputs.text": "a {{loras}} look"}, [("prompt.5.inputs.text", "whole input value")]),
    ("lora-in-a-list", {"prompt.7.inputs.model": ["{{loras}}", 0]}, [("prompt.7.inputs.model", "inside a list")]),
    ("float-in-a-string", {"meta.placeholders.cfg": {"type": "float", "min": 0, "max": 10}, "prompt.7.inputs.cfg": "c{{cfg}}"},
     [("prompt.7.inputs.cfg", "float placeholder {{cfg}} inside a longer string")]),
    ("cycle", {"prompt.1.inputs.model": ["15", 0]}, [("prompt", "cycle: 1 -> 15 -> 10 -> 8 -> 7 -> 4 -> 1")]),
    ("cycle-through-a-lora-list", {"prompt.4.inputs.model": ["7", 0]}, [("prompt", "cycle: 4 -> 7 -> 4")]),
    ("dangling-link", {"prompt.5.inputs.clip": ["99", 0]}, [("prompt.5.inputs.clip", "does not hold")]),
    ("lora-source", {"meta.placeholders.loras.source": ["99", 0]}, [("meta.placeholders.loras.source", "does not hold")]),
    ("seed-bounds", {"meta.placeholders.seed.max": 2147483647}, [("meta.placeholders.seed", "from 0 to 4294967295")]),
    ("max-items", {"meta.placeholders.loras.max_items": 17}, [("meta.placeholders.loras.max_items", "from 0 to 16")]),
    ("loader-inputs", {"meta.placeholders.loras.loader_inputs": {"model": "$name", "strength_model": "$strength"}},
     [("meta.placeholders.loras.loader_inputs", "$prev to exactly one input")]),
    ("placeholder-type", {"meta.placeholders.width.type": "number"}, [("meta.placeholders.width.type", "one of")]),
    ("text-with-bounds", {"meta.placeholders.prompt.min": 0}, [("meta.placeholders.prompt.min", "not a key")]),
    ("float-bound-on-an-int", {"meta.placeholders.crop_x.max": 64.0}, [("meta.placeholders.crop_x.max", "an int")]),
    ("format", {"format": "influencer-render-template/2"}, [("format", "'influencer-render-template/1'")]),
    ("missing-meta-key", {"meta.models": DELETE}, [("meta.models", "is missing")]),
    ("kind", {"meta.kind": "video"}, [("meta.kind", "one of still, edit, finish, refine, reel, ref_reel")]),
    ("reel-extras", {"meta.kind": "reel"}, [("meta.max_frames", "is missing"), ("meta.fps", "is missing")]),
    ("ref-slots", {"meta.kind": "ref_reel", "meta.max_frames": 241, "meta.fps": 24}, [("meta.ref_slots", "is missing")]),
    ("output-node", {"meta.output_node": "99"}, [("meta.output_node", "node of the prompt")]),
    ("node-pack", {"meta.node_packs.KSampler": DELETE}, [("meta.node_packs", "'KSampler'")]),
    ("placeholder-in-a-title", {"prompt.5._meta.title": "{{prompt}}"}, [("prompt.5._meta", "only stand in input values")]),
    ("ratio", {"meta.sizes.3:4.output": [1440, 1921]}, [("meta.sizes.3:4.output", "not exactly 3:4")]),
    ("timeout", {"meta.timeout_s": 0}, [("meta.timeout_s", "above 0")]),
    ("identity-flag", {"meta.identity_required": "yes"}, [("meta.identity_required", "true or false")]),
]  # fmt: skip


@pytest.mark.parametrize(("change", "expected"), [case[1:] for case in REJECTED], ids=[case[0] for case in REJECTED])
def test_a_bad_template_is_rejected(
    change: dict[str, object] | tuple[str, str], expected: list[tuple[str, str]]
) -> None:
    document = still_document()
    if isinstance(change, tuple):
        _rename_node(document, *change)
    else:
        for dotted, value in change.items():
            _put(document, dotted, value)
    with pytest.raises(ValidationError) as caught:
        T.parse_template(document, "still.json")
    problems = caught.value.problems
    assert [(problem.path, problem.field) for problem in problems] == [("still.json", field) for field, _ in expected]
    for problem, (_, fragment) in zip(problems, expected):
        assert fragment in problem.reason, problem


def test_every_problem_of_a_template_is_reported_at_once() -> None:
    document = still_document()
    for dotted, value in {"format": "x", "meta.placeholders.seed.max": 9, "prompt.5.inputs.clip": ["99", 0]}.items():
        _put(document, dotted, value)
    with pytest.raises(ValidationError) as caught:
        T.parse_template(document, "still.json")
    fields = [problem.field for problem in caught.value.problems]
    assert fields == ["format", "meta.placeholders.seed", "prompt.5.inputs.clip"]


@pytest.mark.parametrize(
    ("content", "line", "reason"),
    [
        (None, None, "the Render_Template is missing"),
        (b'{"format": ', 1, "not valid JSON: Expecting value"),
        (b'{"format": "a",\n "format": "b"}', None, "the key 'format' appears twice in one object"),
        (b'{"format": NaN}', None, "not valid JSON: NaN is not a JSON number"),
        (b'{"format": "\xff"}', 1, "the Render_Template is not UTF-8 text"),
        (b"[]", None, "a Render_Template is one JSON object of format, meta and prompt; got a list"),
    ],
    ids=["missing", "not-json", "repeated-key", "nan", "not-utf8", "not-an-object"],
)
def test_a_file_that_cannot_be_read_is_rejected(
    tmp_path: Path, content: bytes | None, line: int | None, reason: str
) -> None:
    if content is not None:
        (tmp_path / "still.json").write_bytes(content)
    with pytest.raises(ValidationError) as caught:
        T.load_template("still", tmp_path)
    assert [(p.path, p.field, p.line, p.reason) for p in caught.value.problems] == [
        (str(tmp_path / "still.json"), None, line, reason)
    ]


@pytest.mark.parametrize("name", ["../still", "Still", "still.json", "", "a/b", "-still"])
def test_a_bad_template_name_is_rejected(tmp_path: Path, name: str) -> None:
    write_template(tmp_path, "still", still_document())
    with pytest.raises(ValidationError) as caught:
        T.load_template(name, tmp_path)
    [problem] = caught.value.problems
    assert "is not a Render_Template name" in problem.reason


# ---- the type rules (§A3) ----

INT = {"type": "int", "min": 0, "max": 10}
FLOAT = {"type": "float", "min": -1, "max": 1.5}
LORAS = {"type": "lora_list", "max_items": 16, "strength_min": -2.0, "strength_max": 2.0, "source": ["1", 0],
         "loader": "LoraLoaderModelOnly",
         "loader_inputs": {"model": "$prev", "lora_name": "$name", "strength_model": "$strength"}}  # fmt: skip
ENTRY = {"name": "looks/a.safetensors", "strength": 0.5}
TYPE_RULES = [
    (INT, 0, None), (INT, 10, None),
    (INT, True, ("x", "an int from 0 to 10", "True")),
    (INT, 1.0, ("x", "an int from 0 to 10", "1.0")),
    (INT, "5", ("x", "an int from 0 to 10", "'5'")),
    (INT, -1, ("x", "an int from 0 to 10", "-1")),
    (INT, 11, ("x", "an int from 0 to 10", "11")),
    (FLOAT, 1, None), (FLOAT, 1.5, None), (FLOAT, -0.0, None),
    (FLOAT, True, ("x", "an int or a finite float from -1 to 1.5", "True")),
    (FLOAT, float("nan"), ("x", "an int or a finite float from -1 to 1.5", "nan")),
    (FLOAT, float("inf"), ("x", "an int or a finite float from -1 to 1.5", "inf")),
    (FLOAT, "0.5", ("x", "an int or a finite float from -1 to 1.5", "'0.5'")),
    (FLOAT, 1.6, ("x", "an int or a finite float from -1 to 1.5", "1.6")),
    ({"type": "text"}, "", None), ({"type": "text"}, "{{x}}", None),
    ({"type": "text"}, 5, ("x", "a str", "5")),
    ({"type": "text"}, None, ("x", "a str", "None")),
    ({"type": "file"}, "a.png", None),
    ({"type": "file"}, "", ("x", "a non-empty str", "''")),
    ({"type": "file"}, 5, ("x", "a non-empty str", "5")),
    ({"type": "bool"}, False, None),
    ({"type": "bool"}, 1, ("x", "a bool", "1")),
    ({"type": "bool"}, "true", ("x", "a bool", "'true'")),
    (LORAS, [], None), (LORAS, [ENTRY] * 16, None), (LORAS, (ENTRY,), None),
    (LORAS, [ENTRY] * 17, ("x", "a list of at most 16 LoRA entries", "17 entries")),
    (LORAS, [{"name": "", "strength": 0}], ("x[1].name", "a non-empty str", "''")),
    (LORAS, [ENTRY, {"name": "b", "strength": 2.5}], ("x[2].strength", "an int or a finite float from -2.0 to 2.0", "2.5")),
    (LORAS, [{"name": "b", "strength": True}], ("x[1].strength", "an int or a finite float from -2.0 to 2.0", "True")),
    (LORAS, [{"name": "b", "strength": 0, "clip": 1}],
     ("x[1]", "an object with exactly a name and a strength", "{'name': 'b', 'strength': 0, 'clip': 1}")),
    (LORAS, [{"name": "b"}], ("x[1]", "an object with exactly a name and a strength", "{'name': 'b'}")),
    (LORAS, "a.safetensors", ("x", "a list of at most 16 LoRA entries", "'a.safetensors'")),
]  # fmt: skip


@pytest.mark.parametrize(
    ("spec", "value", "problem"),
    TYPE_RULES,
    ids=[f"{spec['type']}-{'rejects' if problem else 'takes'}-{n}" for n, (spec, _, problem) in enumerate(TYPE_RULES)],
)
def test_the_type_rules(spec: dict[str, Any], value: object, problem: tuple[str, str, str] | None) -> None:
    prompt = {"1": {"class_type": "UNETLoader", "inputs": {}},
              "2": {"class_type": "KSampler", "inputs": {"v": "{{x}}"}}}  # fmt: skip
    template = T.parse_template(ST.make_document(prompt, {"x": spec}))
    if problem is None:
        got = T.fill(template, {"x": value})["2"]["inputs"]["v"]
        if spec["type"] != "lora_list":
            assert as_json(got) == as_json(value)
        return
    with pytest.raises(T.FillError) as caught:
        T.fill(template, {"x": value})
    [found] = caught.value.problems
    field, rule, got = problem
    assert (found.path, found.field) == ("test", field)
    assert f"must be {rule}" in found.reason and f"got {got}" in found.reason


def test_missing_and_unknown_names_are_reported_together() -> None:
    template = T.parse_template(still_document())
    values = {key: value for key, value in STILL_VALUES.items() if key not in ("seed", "loras")}
    values.update({"sead": 7, "Prompt": "x"})
    with pytest.raises(T.FillError) as caught:
        T.fill(template, values)
    error = caught.value
    assert error.template == "still"
    assert [(p.field, p.reason) for p in error.problems] == [
        ("loras", "no value was supplied"),
        ("seed", "no value was supplied"),
        ("Prompt", "matches no placeholder of the Render_Template; got 'x'"),
        ("sead", "matches no placeholder of the Render_Template; got 7"),
    ]
    lines = [f"still: {problem.field}: {problem.reason}" for problem in error.problems]
    assert str(error).splitlines() == ["cannot fill the Render_Template still:", *lines]
