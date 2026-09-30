"""Tests of the four committed stills Render_Templates and the Config_Checker's Render_Template rules
(task 18; design §C7, §M6, §C10; Req 7.1, 8.1, 8.6, 8.7, 11.2, 11.3, 12.2, 12.4, 26.11).

- each of still, still-sharp, edit and finish loads, and every declared placeholder, pack, model file and
  extra file resolves against the committed config: the `influencer` Selection and extra_model_paths.yaml
- filling each with values_for(t) gives a prompt whose every link points at an existing node, with no cycle
- the graph facts: one VAEDeGrid per whole-image decode, exact size ratios, the edit sizes' area, the
  still-sharp halving, the edit canvas wiring and sampler, the Krea 2 Raw bf16 UNet, the finish factors
- scripts/influencer/checks/template_rules.py passes the committed templates and reports each rule on a
  broken copy, every failure of a run together

The tests read committed files only: no GPU, no ComfyUI, no network.
"""
from __future__ import annotations

import copy
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

import strategies_prompts as SP  # noqa: E402
import strategies_templates as ST  # noqa: E402
from influencer import lines as L  # noqa: E402
from influencer import templates as T  # noqa: E402
from influencer.checks import template_rules as R  # noqa: E402
from influencer.geometry import finish_factor  # noqa: E402

NAMES = ("still", "still-sharp", "edit", "finish")
STILLS = ("still", "still-sharp")
CONFIG = REPO / "config"
FOLDER = CONFIG / "influencer" / "templates"
KREA2_RAW = "krea2_raw_bf16.safetensors"
DEGRID_SETTINGS = {
    "enabled": True, "mode": "auto", "limit": 0.02, "skip_when_clean": True, "grid_gain": 10.0, "grid_view": "4x zoom",
}  # fmt: skip
DECODES = ("VAEDecode", "VAEUtils_VAEDecodeTiled")


def load(name: str) -> T.Template:
    return T.load_template(name)


def document(name: str) -> dict[str, Any]:
    return json.loads((FOLDER / f"{name}.json").read_text(encoding="utf-8"))


def config_texts() -> dict[str, str]:
    return {name: (CONFIG / name).read_text(encoding="utf-8") for name in (L.CATALOG, L.MODELS, L.NODES)}


def selection() -> L.Selection:
    return L.select(config_texts(), "influencer")


def path_of(name: str) -> str:
    return f"config/influencer/templates/{name}.json"


def is_link(value: object) -> bool:
    return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and type(value[1]) is int


def links(prompt: dict[str, Any]) -> list[tuple[str, str]]:
    """(consumer, producer) for every link of the prompt."""
    return [(node_id, value[0]) for node_id, node in prompt.items() for value in node["inputs"].values() if is_link(value)]


def of_class(prompt: dict[str, Any], class_type: str) -> list[str]:
    return [node_id for node_id, node in prompt.items() if node["class_type"] == class_type]


def readers(prompt: dict[str, Any], producer: str) -> list[str]:
    return [consumer for consumer, source in links(prompt) if source == producer]


# ---- loading and resolving (Req 7.1, 26.11) ----


@pytest.mark.parametrize("name", NAMES)
def test_each_template_loads_and_every_declared_placeholder_is_used(name: str) -> None:
    template = load(name)
    assert template.name == name and template.catalog_id == "influencer" and template.timeout_s == 900
    assert ST.names_used(template) == set(template.meta["placeholders"])  # declared = used, both ways
    assert template.meta["placeholders"]["seed"] == {"type": "int", "min": 0, "max": 4_294_967_295}
    assert template.prompt[template.output_node]["class_type"] == "SaveImage"
    assert all(int(node_id) < T.FIRST_LOADER_ID for node_id in template.prompt)


def test_kinds_output_nodes_and_identity_flags() -> None:
    expected = {"still": ("still", "15", True), "still-sharp": ("still", "15", True),
                "edit": ("edit", "16", False), "finish": ("finish", "14", True)}  # fmt: skip
    assert {name: (load(name).kind, load(name).output_node, load(name).identity_required) for name in NAMES} == expected


@pytest.mark.parametrize("name", NAMES)
def test_every_pack_is_core_or_a_nodes_txt_line_of_the_influencer_selection(name: str) -> None:
    installed = {R.repo_key(R.repo_name(line.url)) for line in selection().nodes}
    packs = set(load(name).meta["node_packs"].values()) - {"core"}
    assert packs and all(R.repo_key(pack) in installed for pack in packs)


def test_the_pack_maps_name_the_design_packs() -> None:
    outside = {name: set(load(name).meta["node_packs"].values()) - {"core"} for name in NAMES}
    assert outside == {
        "still": {"RES4LYF", "ComfyUI-DeGrid"},
        "still-sharp": {"RES4LYF", "ComfyUI-DeGrid", "ComfyUI-VAE-Utils"},
        "edit": {"ComfyUI-DeGrid"},
        "finish": {"ComfyUI-Impact-Pack", "ComfyUI-Impact-Subpack", "ComfyUI-VOSR2", "ComfyUI-DeGrid"},
    }


@pytest.mark.parametrize("name", NAMES)
def test_every_model_and_extra_file_matches_a_models_list_install_path(name: str) -> None:
    paths = {L.install_path(line) for line in selection().models}
    folders = set(yaml.safe_load((REPO / "extra_model_paths.yaml").read_text(encoding="utf-8"))["vastai"])
    meta = load(name).meta
    values = {value for node in load(name).prompt.values() for value in node["inputs"].values() if isinstance(value, str)}
    for model in meta["models"]:
        assert f"models/{L.kind_folder(model['kind'])}/{model['name']}" in paths
        assert model["folder"] in folders
        assert model.get("value", model["name"]) in values  # the graph loads the file under that spelling
    for entry in meta["extra_files"]:
        assert entry.get("derived", False) or f"models/{entry['folder']}/{entry['name']}" in paths


def test_finish_declares_the_detector_and_the_vosr2_files() -> None:
    meta = load("finish").meta
    assert {"kind": "detection", "folder": "ultralytics_bbox", "name": "face_yolov8m.pt",
            "value": "bbox/face_yolov8m.pt"} in meta["models"]  # fmt: skip
    assert [e["name"] for e in meta["extra_files"] if not e.get("derived")] == [
        "VOSR2/args.json", "VOSR2/checkpoints/ema_model.safetensors", "VOSR2/Qwen-Image-vae-2d/config.json",
        "VOSR2/Qwen-Image-vae-2d/diffusion_pytorch_model.safetensors", "VOSR2/dinov2_vitl14_pretrain.pth",
    ]  # fmt: skip
    assert [e for e in meta["extra_files"] if e.get("derived")] == [
        {"folder": "vosr2", "name": "VOSR2/dinov2_vitl14.safetensors", "derived": True}
    ]


# ---- filling (Req 7.2, 7.5) ----


def assert_links_resolve_and_acyclic(prompt: dict[str, Any]) -> None:
    edges = links(prompt)
    assert all(producer in prompt for _, producer in edges)
    pending = {node_id: {p for c, p in edges if c == node_id} for node_id in prompt}
    while pending:  # peel off nodes whose producers are all gone; a cycle leaves none to peel
        ready = [node_id for node_id, producers in pending.items() if not producers & set(pending)]
        assert ready, f"cycle among {sorted(pending)}"
        for node_id in ready:
            del pending[node_id]


@pytest.mark.parametrize("name", NAMES)
@settings(max_examples=25, deadline=None)
@given(data=st.data())
def test_filling_with_valid_values_gives_a_linked_acyclic_prompt(name: str, data: st.DataObject) -> None:
    template = load(name)
    prompt = T.fill(template, data.draw(ST.values_for(template)))
    assert_links_resolve_and_acyclic(prompt)
    assert prompt[template.output_node]["class_type"] == "SaveImage"


def test_the_edit_template_is_one_property_23_covers() -> None:
    assert "edit" in SP.edit_template_names()


# ---- the graph rules (Req 12.2, 8.6, 8.7, 11.2) ----


@pytest.mark.parametrize("name", ("still", "still-sharp", "edit"))
def test_each_whole_image_decode_feeds_exactly_one_degrid(name: str) -> None:
    prompt = load(name).prompt
    decodes = [node_id for node_id, node in prompt.items() if node["class_type"] in DECODES]
    assert len(decodes) == 1 == len(of_class(prompt, "VAEDeGrid"))
    [grid] = readers(prompt, decodes[0])
    assert prompt[grid]["class_type"] == "VAEDeGrid"
    # everything downstream reads DeGrid: its one reader is the next image node
    assert len(readers(prompt, grid)) == 1


def test_finish_has_no_whole_image_decode_and_degrids_before_the_upscaler() -> None:
    prompt = load("finish").prompt
    assert not [node for node in prompt.values() if node["class_type"] in DECODES]
    assert prompt["11"]["inputs"]["image"] == ["10", 0] and prompt["13"]["inputs"]["image"] == ["11", 0]


@pytest.mark.parametrize("name", NAMES)
def test_every_degrid_carries_the_fixed_settings(name: str) -> None:
    prompt = load(name).prompt
    for node_id in of_class(prompt, "VAEDeGrid"):
        inputs = dict(prompt[node_id]["inputs"])
        assert is_link(inputs.pop("image")) and inputs == DEGRID_SETTINGS


def test_the_size_rows_have_exact_ratios() -> None:
    for name in STILLS:
        sizes = load(name).meta["sizes"]
        assert {k: v["output"][0] / v["output"][1] for k, v in sizes.items()} == {"3:4": 0.75, "4:5": 0.8, "9:16": 0.5625}
        assert sizes["9:16"] == {"latent": [1088, 1920], "output": [1080, 1920], "crop": [4, 0]}
    edit = load("edit").meta["sizes"]
    assert edit == {"3:4": {"output": [864, 1152]}, "4:5": {"output": [896, 1120]}}
    below = {key: round(100 * (1 - w * h / 1024**2), 1) for key, (w, h) in ((k, v["output"]) for k, v in edit.items())}
    assert below == {"3:4": 5.1, "4:5": 4.3}
    assert all(side % 16 == 0 for size in edit.values() for side in size["output"])


def test_still_sharp_decodes_at_twice_the_size_and_halves_after_degrid() -> None:
    sharp, still = load("still-sharp").prompt, load("still").prompt
    assert sharp["12"]["class_type"] == "VAEUtils_VAEDecodeTiled" and sharp["12"]["inputs"]["upscale"] == -1
    assert sharp["16"]["class_type"] == "ImageScaleBy" and sharp["16"]["inputs"]["image"] == ["13", 0]
    assert sharp["14"]["inputs"]["image"] == ["16", 0]
    scale = sharp["16"]["inputs"]["scale_by"]
    for key, size in load("still-sharp").meta["sizes"].items():
        latent_w, latent_h = size["latent"]
        assert (latent_w * 2 * scale, latent_h * 2 * scale) == tuple(size["latent"])  # the crop input size is kept
        assert size == load("still").meta["sizes"][key]
    # every other node is the still graph's
    assert {k: v for k, v in sharp.items() if k not in ("3", "12", "14", "16")} == {
        k: v for k, v in still.items() if k not in ("3", "12", "14")
    }


def test_the_still_sampler_passes_copy_the_guide() -> None:
    prompt = load("still").prompt
    shared = {"eta": 0.5, "cfg": 1.0, "steps_to_run": -1, "sampler_mode": "standard", "bongmath": True,
              "seed": "{{seed}}", "model": "{{loras}}"}  # fmt: skip
    first = {"sampler_name": "exponential/res_2s", "scheduler": "beta", "steps": 6, "denoise": 1.0}
    second = {"sampler_name": "multistep/deis_3m", "scheduler": "bong_tangent", "steps": 2, "denoise": 0.2}
    for node_id, own in (("10", first), ("11", second)):
        assert prompt[node_id]["class_type"] == "ClownsharKSampler_Beta"
        assert {k: prompt[node_id]["inputs"][k] for k in {**shared, **own}} == {**shared, **own}
    spec = load("still").placeholders["loras"]
    assert (spec.source, spec.loader, spec.max_items, spec.strength_min, spec.strength_max) == (
        ("4", 0), "LoraLoaderModelOnly", 16, -2.0, 2.0,
    )
    assert prompt["4"]["inputs"]["strength_model"] == 0.6


def test_the_edit_graph() -> None:
    template = load("edit")
    prompt = template.prompt
    assert prompt["6"] == {"class_type": "LoadImage", "inputs": {"image": "{{reference_image}}"}}
    assert prompt["7"]["class_type"] == "ImageScale" and prompt["7"]["inputs"]["image"] == ["6", 0]
    assert prompt["12"]["class_type"] == "VAEEncode" and prompt["12"]["inputs"]["pixels"] == ["7", 0]
    assert [prompt[n]["inputs"]["image1"] for n in of_class(prompt, "TextEncodeQwenImageEditPlus")] == [["6", 0]] * 2
    classes = {node["class_type"] for node in prompt.values()}
    assert "FluxKontextImageScale" not in classes and not any("Lora" in c for c in classes)
    assert "lightning" not in json.dumps(document("edit")).lower() and not template.lora_lists
    assert prompt["8"]["inputs"]["prompt"] == SP.EDIT_PROMPT and "Picture 1" in SP.EDIT_INSTRUCTION
    sampler = prompt["13"]["inputs"]
    assert (sampler["steps"], sampler["cfg"], sampler["sampler_name"], sampler["scheduler"]) == (40, 4.0, "euler", "simple")
    assert [prompt[n]["inputs"]["reference_latents_method"] for n in ("10", "11")] == ["index_timestep_zero"] * 2
    assert prompt["3"]["inputs"] == {"model": ["2", 0], "strength": 1.0}  # no pre_cfg


@pytest.mark.parametrize("name", ("still", "still-sharp", "finish"))
def test_the_krea_templates_load_krea2_raw_bf16(name: str) -> None:
    [unet] = of_class(load(name).prompt, "UNETLoader")
    assert load(name).prompt[unet]["inputs"] == {"unet_name": KREA2_RAW, "weight_dtype": "default"}


def test_finish_factor_is_2_for_still_picks_and_3_for_edit_picks() -> None:
    upscale = load("finish").meta["placeholders"]["upscale"]
    assert upscale == {"type": "int", "min": 2, "max": 4}
    for name, expected in (("still", 2), ("edit", 3)):
        for key, size in load(name).meta["sizes"].items():
            (w, h), (export_w, export_h) = size["output"], R.EXPORT_SIZES[key]
            factor = finish_factor(w, h, export_w, export_h)
            assert factor == expected and upscale["min"] <= factor <= upscale["max"]
            assert factor * w >= 2 * export_w and factor * h >= 2 * export_h


# ---- the Config_Checker rules (Req 26.11 and the §C10 graph rules) ----


def run(changes: dict[str, Callable[[dict[str, Any]], object] | str] | None = None) -> list[tuple[str, str | None, str]]:
    """check_templates over the four committed templates, each change applied to a copy: a function edits
    the document in place (or returns replacement text), a string replaces the file text. Returns
    (template name, field, rule) of every Problem."""
    texts: dict[str, str] = {}
    for name in NAMES:
        change = (changes or {}).get(name)
        if isinstance(change, str):
            texts[path_of(name)] = change
            continue
        doc = copy.deepcopy(document(name))
        replaced = change(doc) if change else None
        texts[path_of(name)] = replaced if isinstance(replaced, str) else json.dumps(doc)
    return [
        (Path(p.path).stem, p.field, p.reason.split(":", 1)[0]) for p in R.check_templates(texts, config_texts())
    ]


def test_the_committed_templates_pass_every_rule() -> None:
    assert run() == []


def test_invalid_json_is_reported_with_its_line() -> None:
    text = (FOLDER / "still.json").read_text(encoding="utf-8").replace('"kind": "still",', '"kind": "still"', 1)
    problems = R.check_templates({path_of("still"): text}, config_texts())
    assert len(problems) == 1 and problems[0].line == 6 and problems[0].reason.startswith("templates: not valid JSON")


def test_a_key_twice_is_reported() -> None:
    text = (FOLDER / "edit.json").read_text(encoding="utf-8").replace('"kind": "edit",', '"kind": "edit", "kind": "edit",', 1)
    problems = R.check_templates({path_of("edit"): text}, config_texts())
    assert [p.reason for p in problems] == ["templates: the key 'kind' appears twice in one object"]


def test_an_undeclared_placeholder_is_reported() -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["prompt"]["5"]["inputs"]["text"] = "{{prompt}} {{mood}}"

    assert run({"still": change}) == [("still", "prompt.5.inputs.text", "templates")]


def test_a_class_without_a_pack_entry_is_reported() -> None:
    def change(doc: dict[str, Any]) -> None:
        del doc["meta"]["node_packs"]["ImageScaleBy"]

    assert run({"still-sharp": change}) == [("still-sharp", "meta.node_packs", "templates")]


def test_a_pack_outside_the_selection_is_reported() -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["meta"]["node_packs"]["ClownsharKSampler_Beta"] = "RES4LYF-fork"
        doc["meta"]["node_packs"]["VAEDeGrid"] = "comfyui-degrid.git"  # case and .git are ignored

    assert run({"still": change}) == [("still", "meta.node_packs.ClownsharKSampler_Beta", "templates")]


def test_an_unknown_catalog_id_is_reported() -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["meta"]["catalog_id"] = "no-such-id"

    assert run({"edit": change}) == [("edit", "meta.catalog_id", "templates")]


def test_a_model_no_line_installs_is_reported() -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["meta"]["models"][2]["name"] = "Wan2_1_VAE_bf16.safetensors"
        doc["meta"]["models"][3]["kind"] = "checkpoint"  # the turbo LoRA below models/checkpoints/

    assert run({"still": change}) == [("still", "meta.models[3]", "templates"), ("still", "meta.models[4]", "templates")]


def test_an_extra_file_no_line_installs_is_reported_unless_derived() -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["meta"]["extra_files"][0]["name"] = "VOSR2/missing.json"
        doc["meta"]["extra_files"][5]["name"] = "VOSR2/also-missing.safetensors"  # derived: not checked

    assert run({"finish": change}) == [("finish", "meta.extra_files[1]", "templates")]


def test_a_decode_that_feeds_more_than_the_degrid_is_reported() -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["prompt"]["14"]["inputs"]["image"] = ["12", 0]  # the crop reads the decode, not DeGrid

    assert run({"still": change}) == [("still", "prompt.12", "graph")]


def test_a_decode_with_no_degrid_is_reported() -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["prompt"]["15"]["class_type"] = "PreviewImage"
        doc["meta"]["node_packs"]["PreviewImage"] = "core"

    assert run({"edit": change}) == [("edit", "prompt.14", "graph")]


def test_an_inexact_still_ratio_is_reported_once() -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["meta"]["sizes"]["9:16"]["output"] = [1088, 1920]

    assert run({"still": change}) == [("still", "meta.sizes.9:16.output", "templates")]


@pytest.mark.parametrize(
    ("key", "output", "field"),
    [("3:4", [876, 1168], "meta.sizes.3:4.output"), ("3:4", [768, 1024], "meta.sizes.3:4.output"),
     ("1:1", [1024, 1024], "meta.sizes.1:1")],
    ids=["multiple-of-16", "area", "ratio"],
)  # fmt: skip
def test_an_edit_size_off_the_rules_is_reported(key: str, output: list[int], field: str) -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["meta"]["sizes"] = {key: {"output": output}}

    problems = run({"edit": change})
    assert ("edit", field, "graph") in problems
    assert all(rule == "graph" and name == "edit" for name, _, rule in problems)


def test_an_edit_canvas_not_scaled_from_the_reference_is_reported() -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["prompt"]["12"]["inputs"]["pixels"] = ["6", 0]

    assert run({"edit": change}) == [("edit", "prompt.12.inputs.pixels", "graph")]


def test_a_finish_factor_outside_the_upscale_bounds_is_reported() -> None:
    def change(doc: dict[str, Any]) -> None:
        doc["meta"]["placeholders"]["upscale"]["max"] = 2  # the Edit Picks need 3

    assert run({"finish": change}) == [("finish", "meta.placeholders.upscale", "graph")] * 2


def test_every_failure_of_a_run_is_reported_together() -> None:
    def still(doc: dict[str, Any]) -> None:
        doc["prompt"]["14"]["inputs"]["image"] = ["12", 0]
        doc["meta"]["node_packs"]["ClownsharKSampler_Beta"] = "RES4LYF-fork"

    def edit(doc: dict[str, Any]) -> None:
        doc["prompt"]["12"]["inputs"]["pixels"] = ["6", 0]

    problems = run({"still": still, "edit": edit, "finish": "{", "still-sharp": lambda doc: None})
    assert sorted(problems) == sorted([
        ("still", "meta.node_packs.ClownsharKSampler_Beta", "templates"), ("still", "prompt.12", "graph"),
        ("edit", "prompt.12.inputs.pixels", "graph"), ("finish", None, "templates"),
    ])  # fmt: skip
