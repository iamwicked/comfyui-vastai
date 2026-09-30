"""Unit tests of the pre-submission checks, scripts/influencer/preflight.py, against the fake servers
(task 19.6; design §A13, §C6.1, §C6.2; Req 9.1-9.5, 9.8, 6.15).

- identity: a closed port, a 500, an answer without system.comfyui_version and a held answer each give
  their own named failure with the URL, and no further ComfyUI request
- missing items: a class the fake omits, a renamed input, an Autogrow group, LoRAs, a VOSR2 extra file,
  the 2511 UNet, a core class, and a Character_LoRA no Catalog id installs, each named once
- Ollama: a missing model, a model without vision, an Ollama that never answers
- free space just below and just above 20 GB
- the deadline, an untouched Manifest, and a report that lists every failure together
"""
from __future__ import annotations

import copy
import dataclasses
import shutil
import sys
import time
from pathlib import Path

import pytest

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import preflight as PF  # noqa: E402
from influencer.comfy import ComfyClient  # noqa: E402
from influencer.errors import EnvironmentFailure  # noqa: E402
from influencer.ollama import OllamaClient  # noqa: E402
from influencer.templates import fill, load_template  # noqa: E402

CHARACTER_LORA = "characters/ava/ava-v3.safetensors"  # no models.list line installs it
LOOK_LORA = "looks/film-grain.safetensors"
CONFIG = PF.read_config(REPO / "config")
AUTOGROW_SPEC = [
    "COMFY_AUTOGROW_V3",
    {"template": {"input": {"required": {"image": ["IMAGE", {}]}}, "prefix": "ref_image_", "min": 1, "max": 10}},
]

STILL_VALUES = {
    "prompt": "a portrait", "seed": 7, "latent_width": 1024, "latent_height": 1344, "width": 1008,
    "height": 1344, "crop_x": 8, "crop_y": 0, "output_prefix": "influencer/2026-W40/p01-s01-c1",
    "loras": [{"name": CHARACTER_LORA, "strength": 1.0}, {"name": LOOK_LORA, "strength": 0.5}],
}  # fmt: skip
EDIT_VALUES = {
    "reference_image": "influencer/abc.png", "scene": "on the stairs", "seed": 8, "width": 896, "height": 1152,
    "output_prefix": "influencer/2026-W40/p02-s02-c1",
}  # fmt: skip
FINISH_VALUES = {
    "prompt": "a portrait", "seed": 9, "character_lora": CHARACTER_LORA, "character_strength": 1.0,
    "pick_image": "influencer/pick.png", "upscale": 2, "output_prefix": "influencer/2026-W40/finish/p01-s01",
}  # fmt: skip


def filled_prompts() -> list[PF.FilledPrompt]:
    return [
        PF.FilledPrompt(template, fill(template, values), name)
        for name, template, values in (
            ("still", load_template("still"), STILL_VALUES),
            ("edit", load_template("edit"), EDIT_VALUES),
            ("finish", load_template("finish"), FINISH_VALUES),
        )
    ]


def complete_object_info(prompts: list[PF.FilledPrompt]) -> dict:
    """An /object_info that reports every class and input the prompts use."""
    info: dict = {}
    for filled in prompts:
        for node in filled.prompt.values():
            entry = info.setdefault(node["class_type"], {"input": {"required": {}, "optional": {}}, "output": []})
            for name in node["inputs"]:
                entry["input"]["required"][name] = ["*", {}]
    return info


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    root = tmp_path / "output"
    root.mkdir()
    return root


@pytest.fixture
def plenty_of_space(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10**12, 0, 10**12))


@pytest.fixture
def ready(comfy_fake, plenty_of_space):
    """The fake ComfyUI reports every class, input and file the three filled prompts need."""
    prompts = filled_prompts()
    comfy_fake.object_info = complete_object_info(prompts)
    request = PF.PreflightInput(prompts, Path("."), config=CONFIG)
    for item, _ in PF.required_files(request):
        comfy_fake.models.setdefault(item.folder, []).append(item.name)
    return prompts


def check(prompts, output_root, timing, *, url=None, ollama=None, ollama_models=None, files=(), started=None):
    comfy = ComfyClient(url, timing=timing) if url else ComfyClient.from_env(timing=timing)
    request = PF.PreflightInput(prompts, output_root, files, ollama_models or {}, CONFIG)
    return PF.run(request, comfy=comfy, ollama=ollama, timing=timing, started=started)


def only(report: PF.Report, kind: str) -> list[PF.Failure]:
    return [failure for failure in report.failures if failure.kind == kind]


# ---- a clean pass ----


def test_everything_present_passes_and_requests_each_folder_once(ready, comfy_fake, output_root, timing) -> None:
    report = check(ready, output_root, timing)
    assert report.ok, report.text()
    assert report.text() == "preflight: every check passed (ComfyUI 0.3.60)"
    folders = [request.path for request in comfy_fake.calls("GET", "/models/*")]
    assert sorted(folders) == sorted(set(folders))
    assert set(folders) == {f"/models/{f}" for f in ("diffusion_models", "text_encoders", "vae", "loras",
                                                      "ultralytics_bbox", "vosr2")}  # fmt: skip
    assert "bbox/face_yolov8m.pt" in comfy_fake.models["ultralytics_bbox"]  # looked up under its value
    assert not comfy_fake.calls("POST")


# ---- identity (Req 9.1) ----


def test_a_refused_connection_is_its_own_failure(closed_port, output_root, timing, plenty_of_space) -> None:
    url = f"http://127.0.0.1:{closed_port}"
    report = check(filled_prompts(), output_root, timing, url=url)
    [failure] = report.failures
    assert (failure.check, failure.kind, failure.url) == (PF.COMFYUI, PF.REFUSED, f"{url}/system_stats")
    assert f"{url}/system_stats: connection refused" in failure.message


@pytest.mark.parametrize(
    ("status", "body", "kind", "words"),
    [
        (500, "Internal Server Error", PF.ERROR_ANSWER, "error answer"),
        (200, '{"system": {"os": "posix"}}', PF.NOT_SERVICE, "not a ComfyUI answer"),
    ],
)
def test_an_error_or_foreign_answer_is_named(ready, comfy_fake, output_root, timing, status, body, kind, words) -> None:
    comfy_fake.override("GET", "/system_stats", status, body)
    report = check(ready, output_root, timing)
    [failure] = report.failures
    assert (failure.kind, failure.url) == (kind, f"{comfy_fake.url}/system_stats")
    assert failure.message.startswith(f"ComfyUI at {comfy_fake.url}/system_stats: {words}: ")
    assert not comfy_fake.calls("GET", "/object_info") and not comfy_fake.calls("GET", "/models/*")


def test_a_held_answer_hits_the_shortened_timeout(ready, comfy_fake, output_root, timing) -> None:
    comfy_fake.pause("/system_stats")
    begun = time.monotonic()
    report = check(ready, output_root, timing)
    assert time.monotonic() - begun < timing.request_s + 1.0
    [failure] = report.failures
    assert (failure.kind, failure.url) == (PF.TIMEOUT, f"{comfy_fake.url}/system_stats")
    assert "no answer in time" in failure.message


# ---- missing items (Req 9.2, 9.3) ----


def test_each_missing_item_is_named_once_with_its_pack_ids_or_note(ready, comfy_fake, output_root, timing) -> None:
    info = comfy_fake.object_info
    del info["VAEDeGrid"]  # a pack class the fake omits, used by all three templates
    del info["TextEncodeQwenImageEditPlus"]  # a core class, used twice by edit
    method = info["FluxKontextMultiReferenceLatentMethod"]["input"]["required"]
    method["reference_latents"] = method.pop("reference_latents_method")  # a renamed core input
    comfy_fake.models["loras"].remove(CHARACTER_LORA)  # used by still (a filled loader) and finish
    comfy_fake.models["vosr2"].remove("VOSR2/args.json")
    comfy_fake.models["diffusion_models"].remove("qwen_image_edit_2511_bf16.safetensors")
    report = check(ready, output_root, timing)
    items = [failure.item for failure in report.failures]
    assert sorted(items) == sorted(
        [
            "VAEDeGrid", "TextEncodeQwenImageEditPlus", "FluxKontextMultiReferenceLatentMethod.reference_latents_method",
            f"loras/{CHARACTER_LORA}", "vosr2/VOSR2/args.json", "diffusion_models/qwen_image_edit_2511_bf16.safetensors",
        ]
    )  # fmt: skip
    by_item = {failure.item: failure.message for failure in report.failures}
    assert "node pack ComfyUI-DeGrid, installed by the Selection of Catalog id influencer" in by_item["VAEDeGrid"]
    assert "(used by still, edit, finish)" in by_item["VAEDeGrid"]
    assert by_item["TextEncodeQwenImageEditPlus"].endswith(
        "ComfyUI core; update ComfyUI with UPDATE_COMFYUI=1 stock-setup.sh init"
    )
    assert PF.CORE_NOTE in by_item["FluxKontextMultiReferenceLatentMethod.reference_latents_method"]
    lora = by_item[f"loras/{CHARACTER_LORA}"]
    assert "in folder loras (used by still, finish)" in lora and lora.endswith(PF.NO_CATALOG_ID)
    assert "in folder vosr2" in by_item["vosr2/VOSR2/args.json"]
    assert by_item["vosr2/VOSR2/args.json"].endswith("Catalog id influencer")
    assert by_item["diffusion_models/qwen_image_edit_2511_bf16.safetensors"].endswith("Catalog id influencer")
    text = report.text()
    assert text.startswith("preflight: 6 failed checks or missing items; no Job was submitted")
    for item_message in by_item.values():
        assert text.count(item_message) == 1


def test_the_filler_lora_loader_class_is_compared(ready, comfy_fake, output_root, timing) -> None:
    del comfy_fake.object_info["LoraLoaderModelOnly"]  # the class of the loaders fill inserts at 900+
    comfy_fake.models["loras"].remove(LOOK_LORA)
    report = check(ready, output_root, timing)
    assert [failure.item for failure in report.failures] == ["LoraLoaderModelOnly", f"loras/{LOOK_LORA}"]
    assert report.failures[1].message.endswith(PF.NO_CATALOG_ID)


def test_autogrow_inputs_count_as_present(ready, comfy_fake, output_root, timing) -> None:
    comfy_fake.object_info["RefStack"] = {"input": {"required": {"ref_images": AUTOGROW_SPEC}}}
    edit = ready[1]
    meta = {**edit.template.meta, "node_packs": {**edit.template.meta["node_packs"], "RefStack": "ComfyUI-KJNodes"}}
    template = dataclasses.replace(edit.template, meta=meta)
    prompt = copy.deepcopy(dict(edit.prompt))
    prompt["20"] = {"class_type": "RefStack", "inputs": {"ref_images.ref_image_0": ["6", 0],
                                                         "ref_images.ref_image_12": ["6", 0]}}  # fmt: skip
    report = check([PF.FilledPrompt(template, prompt, "edit")], output_root, timing)
    [failure] = report.failures  # ref_image_0 is present; the group grows only 10; ids in catalog.txt order
    assert (failure.kind, failure.item) == (PF.MISSING_INPUT, "RefStack.ref_images.ref_image_12")
    assert "node pack ComfyUI-KJNodes, installed by the Selection of Catalog ids minimax-h3, wan-animate, global" in (
        failure.message
    )


def test_optional_inputs_are_compared_by_name(ready, comfy_fake, output_root, timing) -> None:
    # TextEncodeQwenImageEditPlus reports vae and image1 as optional; a core that lacks image1 fails
    comfy_fake.object_info["TextEncodeQwenImageEditPlus"] = {
        "input": {"required": {"clip": ["CLIP"], "prompt": ["STRING", {}]}, "optional": {"vae": ["VAE"]}},
        "output": ["CONDITIONING"],
    }
    report = check(ready, output_root, timing)
    [failure] = report.failures  # nodes 8 and 9 both use it: named once
    assert (failure.kind, failure.item) == (PF.MISSING_INPUT, "TextEncodeQwenImageEditPlus.image1")
    assert failure.message.endswith(PF.CORE_NOTE)


def test_request_files_are_checked_too(ready, comfy_fake, output_root, timing) -> None:
    grid = PF.ModelFile("loras", "characters/ava/ava-v4.safetensors")
    report = check(ready, output_root, timing, files=[grid])
    [failure] = report.failures
    assert failure.item == "loras/characters/ava/ava-v4.safetensors"
    assert len(comfy_fake.calls("GET", "/models/loras")) == 1


# ---- Ollama (Req 6.15) ----


@pytest.fixture
def ollama(ollama_fake, timing) -> OllamaClient:
    ollama_fake.add_model("qwen3:8b", ("completion", "thinking"))
    ollama_fake.add_model("gemma3:12b", ("completion",))
    return OllamaClient.from_env(timing=timing)


def test_ollama_models_and_capabilities(ready, ollama, ollama_fake, output_root, timing) -> None:
    needs = {"qwen3:8b": (), "gemma3:12b": ("vision",), "dolphin3:8b": ()}
    report = check(ready, output_root, timing, ollama=ollama, ollama_models=needs)
    assert [(f.kind, f.item) for f in report.failures] == [
        (PF.MISSING_CAPABILITY, "gemma3:12b:vision"),
        (PF.MISSING_MODEL, "dolphin3:8b"),
    ]  # in the order the request names the models
    by_kind = {failure.kind: failure.message for failure in report.failures}
    assert "the model dolphin3:8b is not installed" in by_kind[PF.MISSING_MODEL]
    assert "gemma3:12b lacks the vision capability" in by_kind[PF.MISSING_CAPABILITY]
    assert len(ollama_fake.calls("GET", "/api/tags")) == 1
    assert ollama.capabilities("qwen3:8b") == ("completion", "thinking")  # cached for think: false
    assert len(ollama_fake.calls("POST", "/api/show")) == 2


def test_an_ollama_that_never_answers(ready, ollama, ollama_fake, output_root, timing) -> None:
    ollama_fake.pause()
    report = check(ready, output_root, timing, ollama=ollama, ollama_models={"qwen3:8b": ()})
    [failure] = report.failures
    assert (failure.check, failure.kind, failure.url) == (PF.OLLAMA, PF.TIMEOUT, f"{ollama_fake.url}/api/tags")


# ---- free space (Req 9.4) ----


@pytest.mark.parametrize(("free", "fails"), [(PF.MIN_FREE_BYTES - 1, True), (PF.MIN_FREE_BYTES + 1, False)])
def test_free_space_around_20_gb(ready, output_root, timing, monkeypatch, free, fails) -> None:
    seen = []

    def usage(path):
        seen.append(Path(path))
        return shutil._ntuple_diskusage(10**12, 10**12 - free, free)

    monkeypatch.setattr(shutil, "disk_usage", usage)
    report = check(ready, output_root, timing)
    assert seen == [output_root]
    if fails:
        [failure] = report.failures
        assert failure.kind == PF.LOW_SPACE and failure.item == str(output_root)
        assert failure.message == f"free space of {output_root}: 19.9 GB ({free} bytes), below the 20 GB minimum"
    else:
        assert report.ok


# ---- the deadline, the Manifest and the one report (Req 9.5, 9.8) ----


def test_the_report_arrives_inside_the_deadline(ready, comfy_fake, output_root, timing) -> None:
    comfy_fake.pause("/models/")  # six folders at 0.5 s each would overrun the 2 s deadline
    started = time.monotonic()
    report = check(ready, output_root, timing, started=started)
    assert time.monotonic() - started <= timing.preflight_deadline_s + 0.5
    kinds = [failure.kind for failure in report.failures]
    assert PF.TIMEOUT in kinds and PF.DEADLINE in kinds
    assert set(kinds) == {PF.TIMEOUT, PF.DEADLINE}


def test_no_time_left_makes_no_request(ready, comfy_fake, output_root, timing) -> None:
    report = check(ready, output_root, timing, started=time.monotonic() - timing.preflight_deadline_s)
    [failure] = report.failures
    assert failure.kind == PF.DEADLINE and "deadline of 2 s" in failure.message
    assert not comfy_fake.requests


def test_every_failure_is_reported_together_and_the_manifest_is_untouched(
    ready, comfy_fake, ollama, output_root, timing, monkeypatch
) -> None:
    week = output_root / "weeks" / "2026-W40"
    week.mkdir(parents=True)
    manifest = week / "manifest.json"
    manifest.write_bytes(b'{"format": "influencer-manifest/1", "jobs": {}}\n')
    before = (manifest.read_bytes(), manifest.stat().st_mtime_ns, sorted(output_root.rglob("*")))
    del comfy_fake.object_info["VAEDeGrid"]
    comfy_fake.models["vae"].clear()
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10**12, 10**12, 10**9))
    report = check(ready, output_root, timing, ollama=ollama, ollama_models={"missing:1b": ()})
    assert {failure.check for failure in report.failures} == {PF.NODES, PF.MODELS, PF.OLLAMA, PF.DISK}
    assert (manifest.read_bytes(), manifest.stat().st_mtime_ns, sorted(output_root.rglob("*"))) == before
    assert not comfy_fake.calls("POST")  # nothing uploaded or submitted, as in a dry run
    with pytest.raises(PF.PreflightFailed) as caught:
        report.raise_if_failed()
    assert isinstance(caught.value, EnvironmentFailure)  # exit status 2 at the command line
    assert str(caught.value) == report.text()
    assert all(f"- {failure.message}" in report.text() for failure in report.failures)
