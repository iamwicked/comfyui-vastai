"""Unit tests of the thin clients against the fake servers, scripts/influencer/comfy.py and ollama.py (task
15.4, 15.5, 15.9; design §C6.1, §C6.2; Req 6.15, 9.1, 9.7, 10.6, 25.1).

- ComfyUI: the identity check, the Autogrow expansion of /object_info, model folders, the multipart upload,
  our prompt_id, a 400 with node_errors, history (success, error, VHS gifs), a forgotten prompt, held
  answers that hit the shortened timeouts, /interrupt and /free
- Ollama: a model missing from /api/tags, think only for thinking models, vision for captioning, the chat
  body, the shortened plan and caption budgets, and the /api/ps plus keep_alive 0 unload pair
- both clients refuse a non-loopback COMFYUI_URL or OLLAMA_URL before any lookup or socket
"""
from __future__ import annotations

import json
import socket
import sys
import time
import uuid

import pytest

from fakes._server import parse_multipart
from fakes.comfy import ERROR, FOREVER, FORGET, REJECT, Plan
from fakes.ollama import Answer
from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer.comfy import ComfyClient, IdentityError, Output, Queue, Rejected, Uploaded  # noqa: E402
from influencer.http import Refused, StatusError, TimedOut  # noqa: E402
from influencer.ollama import MissingCapability, MissingModel, OllamaClient  # noqa: E402

GRAPH = {
    "1": {"class_type": "EmptyLatentImage", "inputs": {"width": 8, "height": 8, "batch_size": 1}},
    "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "influencer/still"}},
}
MESSAGES = [{"role": "system", "content": "Plan the Post."}, {"role": "user", "content": "Week 2026-W40"}]
SCHEMA = {"type": "object", "properties": {"slides": {"type": "array"}}, "required": ["slides"]}


@pytest.fixture
def comfy(comfy_fake, timing) -> ComfyClient:
    return ComfyClient.from_env(timing=timing)  # COMFYUI_URL points at the fake


@pytest.fixture
def ollama(ollama_fake, timing) -> OllamaClient:
    return OllamaClient.from_env(timing=timing)  # OLLAMA_URL points at the fake


# ---- ComfyUI ----


def test_the_identity_check_needs_system_comfyui_version(comfy, comfy_fake) -> None:
    assert comfy.version() == "0.3.60"
    where = f"ComfyUI at {comfy_fake.url}/system_stats"
    hint = "so it is not the ComfyUI_API: check that COMFYUI_URL points at ComfyUI"
    comfy_fake.override("GET", "/system_stats", 200, json.dumps({"system": {"os": "posix"}, "devices": []}))
    with pytest.raises(IdentityError) as caught:
        comfy.system_stats()
    assert str(caught.value) == f"{where} answered without system.comfyui_version, {hint}"
    assert caught.value.url == f"{comfy_fake.url}/system_stats"
    comfy_fake.override("GET", "/system_stats", 200, "<!doctype html><title>Jupyter</title>", "text/html")
    with pytest.raises(IdentityError, match=f"^{where} did not answer JSON .*, {hint}$"):
        comfy.version()
    comfy_fake.override("GET", "/system_stats", 404, "404: Not Found", "text/plain")
    with pytest.raises(IdentityError, match=f"^{where} answered HTTP 404: 404: Not Found, {hint}$"):
        comfy.version()


def test_a_400_with_node_errors_is_a_rejection(comfy, comfy_fake) -> None:
    comfy_fake.script(Plan(REJECT, message="Value not in list: sampler_name: 'euler_x'", node="1"))
    with pytest.raises(Rejected) as caught:
        comfy.submit(GRAPH)
    assert list(caught.value.node_errors) == ["1"]
    assert str(caught.value) == (
        f"ComfyUI at {comfy_fake.url}/prompt rejected the prompt: Prompt outputs failed validation; "
        "node 1 (EmptyLatentImage): Value not in list: sampler_name: 'euler_x'"
    )
    with pytest.raises(Rejected, match="rejected the prompt: Invalid prompt"):
        comfy.submit({"1": {"inputs": {}}})  # no class_type: ComfyUI's invalid_prompt, node_errors {}
    assert comfy_fake.prompt_ids == []  # nothing was queued


def test_submit_sends_our_uuid4_prompt_id_and_client_id(comfy, comfy_fake) -> None:
    submitted = comfy.submit(GRAPH)
    [request] = comfy_fake.calls("POST", "/prompt")
    body = request.json()
    assert body == {"prompt": GRAPH, "prompt_id": submitted.prompt_id, "client_id": comfy.client_id}
    assert uuid.UUID(submitted.prompt_id).version == 4 and submitted.number == 1
    ours = "0f8fad5b-d9cb-469f-a165-70867728950e"
    assert comfy.submit(GRAPH, ours).prompt_id == ours
    with pytest.raises(ValueError, match="lowercase UUID"):
        comfy.submit(GRAPH, ours.upper())


def test_history_reads_the_status_the_message_timestamps_and_the_outputs(comfy, comfy_fake) -> None:
    graph = {**GRAPH, "10": {"class_type": "VHS_VideoCombine", "inputs": {"filename_prefix": "influencer/reel"}}}
    prompt_id = comfy.submit(graph).prompt_id
    history = comfy.history(prompt_id)
    assert (history.status, history.completed, history.outcome, history.error) == ("success", True, "success", None)
    assert list(history.events) == ["execution_start", "execution_success"]
    assert all(isinstance(stamp, int) and stamp > 1_700_000_000_000 for stamp in history.events.values())
    assert history.outputs == (
        Output("9", "images", "still_00001_.png", "influencer", "output"),
        Output("10", "gifs", "reel_00001.mp4", "influencer", "output"),
    )
    comfy_fake.script(Plan(ERROR, message="CUDA out of memory", node="1"))
    failed = comfy.history(comfy.submit(GRAPH).prompt_id)
    assert (failed.status, failed.outcome, failed.error, failed.error_node) == ("error", "error", "CUDA out of memory", "1")
    assert "execution_error" in failed.events and failed.outputs == ()


def test_a_forgotten_prompt_has_no_history_and_is_not_queued(comfy, comfy_fake) -> None:
    comfy_fake.script(Plan(FORGET))
    lost = comfy.submit(GRAPH).prompt_id
    assert comfy.history(lost) is None and lost not in comfy.queue()
    done = comfy.submit(GRAPH).prompt_id
    assert comfy.history(done).outcome == "success"
    comfy_fake.forget()  # a ComfyUI restart
    assert comfy.history(done) is None and comfy.queue() == Queue((), ())


def test_a_held_answer_hits_the_shortened_timeout(comfy, comfy_fake, timing) -> None:
    prompt_id = comfy.submit(GRAPH).prompt_id
    comfy_fake.pause("/history")
    started = time.monotonic()
    with pytest.raises(TimedOut) as caught:
        comfy.history(prompt_id)
    assert time.monotonic() - started < timing.request_s + 2
    assert str(caught.value) == f"ComfyUI at {comfy_fake.url}/history/{prompt_id}: no answer within {timing.request_s:g} s"
    comfy_fake.resume()
    assert comfy.history(prompt_id).outcome == "success"
    comfy_fake.pause("/prompt")
    with pytest.raises(TimedOut, match=f"/prompt: no answer within {timing.submit_s:g} s"):
        comfy.submit(GRAPH)


def test_interrupt_stops_the_running_prompt_and_history_shows_it(comfy, comfy_fake) -> None:
    comfy_fake.script(Plan(FOREVER))
    prompt_id = comfy.submit(GRAPH).prompt_id
    assert comfy.queue() == Queue((prompt_id,), ()) and comfy.history(prompt_id) is None
    comfy.interrupt(prompt_id)
    assert comfy_fake.interrupts == [prompt_id]
    history = comfy.history(prompt_id)
    assert (history.status, history.outcome) == ("error", "interrupted") and "execution_interrupted" in history.events
    assert prompt_id not in comfy.queue()


def test_free_unloads_the_models_and_frees_their_memory(comfy, comfy_fake) -> None:
    comfy.free()
    assert comfy_fake.frees == [{"unload_models": True, "free_memory": True}]


def test_upload_posts_multipart_into_the_influencer_input_folder(comfy, comfy_fake, temp_env) -> None:
    uploaded = comfy.upload("5f2b0c.png", b"\x89PNG first")
    assert uploaded == Uploaded("5f2b0c.png", "influencer", "input") and uploaded.reference == "influencer/5f2b0c.png"
    stored = temp_env.comfyui_dir / "input" / "influencer" / "5f2b0c.png"
    assert stored.read_bytes() == b"\x89PNG first"
    [request] = comfy_fake.calls("POST", "/upload/image")
    assert parse_multipart(request.headers["content-type"], request.body) == {
        "image": ("5f2b0c.png", b"\x89PNG first"),
        "subfolder": (None, b"influencer"),
        "type": (None, b"input"),
        "overwrite": (None, b"true"),
    }
    comfy.upload("5f2b0c.png", b"\x89PNG second")  # overwrite=true: the same name is replaced
    assert stored.read_bytes() == b"\x89PNG second" and len(list(stored.parent.iterdir())) == 1
    with pytest.raises(ValueError):
        comfy.upload("../escape.png", b"")


def test_object_info_expands_autogrow_inputs(comfy, comfy_fake) -> None:
    comfy_fake.object_info["BatchImagesNode"] = {"input": {"required": {"images": ["COMFY_AUTOGROW_V3", {
        "template": {"input": {"required": {"image": ["IMAGE", {}]}}, "prefix": "image", "min": 2, "max": 3},
    }]}}, "input_order": {"required": ["images"]}, "output": ["IMAGE"], "name": "BatchImagesNode"}  # fmt: skip
    comfy_fake.object_info["ReferenceNames"] = {"input": {"required": {"seed": ["INT", {}]}, "optional": {"refs": [
        "COMFY_AUTOGROW_V3", {"template": {"input": {"optional": {"ref": ["LATENT", {}]}}, "names": ["front", "side"], "min": 0}},
    ]}}}  # fmt: skip
    info = comfy.object_info()
    assert info["BatchImagesNode"]["input"] == {
        "required": {"images.image0": ["IMAGE", {}], "images.image1": ["IMAGE", {}]},
        "optional": {"images.image2": ["IMAGE", {}]},
    }
    assert info["ReferenceNames"]["input"] == {
        "required": {"seed": ["INT", {}]},
        "optional": {"refs.front": ["LATENT", {}], "refs.side": ["LATENT", {}]},
    }
    assert info["SaveImage"] == comfy_fake.object_info["SaveImage"]  # no Autogrow input: unchanged


def test_models_lists_a_folder_and_names_one_comfyui_lacks(comfy, comfy_fake) -> None:
    comfy_fake.models["loras"] = ["looks/b.safetensors", "a.safetensors"]
    assert comfy.models("loras") == ("looks/b.safetensors", "a.safetensors")
    with pytest.raises(StatusError, match=r"/models/vosr3: ComfyUI has no model folder 'vosr3' \(HTTP 404\)"):
        comfy.models("vosr3")


# ---- Ollama ----


def test_a_model_missing_from_tags_is_named_before_any_chat(ollama, ollama_fake) -> None:
    ollama_fake.add_model("qwen3:8b", ("completion", "thinking"))
    ollama_fake.add_model("llava", ("completion", "vision"))
    ollama.require(["qwen3:8b", "llava", "llava:latest"])  # a name without a tag is its :latest
    with pytest.raises(MissingModel) as caught:
        ollama.require(["qwen3:8b", "dolphin3:8b", "qwen2.5vl:7b", "dolphin3:8b"])
    assert caught.value.models == ("dolphin3:8b", "qwen2.5vl:7b")
    assert str(caught.value) == (
        f"Ollama at {ollama_fake.url} does not have the models dolphin3:8b, qwen2.5vl:7b: install with "
        "ollama pull dolphin3:8b; ollama pull qwen2.5vl:7b"
    )
    assert ollama_fake.calls("POST", "/api/chat") == [] and len(ollama_fake.calls("GET", "/api/tags")) == 2


def test_think_false_goes_only_to_a_model_that_lists_thinking(ollama, ollama_fake) -> None:
    ollama_fake.add_model("qwen3:8b", ("completion", "thinking"))
    ollama_fake.add_model("dolphin3:8b", ("completion",))
    ollama_fake.default_answer = Answer(content='{"slides": []}')
    # the fake answers 400 to think sent to a model without thinking, as Ollama does
    assert ollama.plan("dolphin3:8b", MESSAGES, schema=SCHEMA, seed=11) == '{"slides": []}'
    assert "think" not in ollama_fake.calls("POST", "/api/chat")[-1].json()
    for seed in (12, 13):
        ollama.plan("qwen3:8b", MESSAGES, schema=SCHEMA, seed=seed)
    assert ollama_fake.calls("POST", "/api/chat")[-1].json() == {
        "model": "qwen3:8b",
        "messages": MESSAGES,
        "stream": False,
        "format": SCHEMA,
        "options": {"temperature": 0.8, "seed": 13},
        "keep_alive": "10m",
        "think": False,
    }
    assert len(ollama_fake.calls("POST", "/api/show")) == 2  # capabilities asked once per model


def test_captioning_needs_a_model_that_lists_vision(ollama, ollama_fake) -> None:
    ollama_fake.add_model("qwen3:8b", ("completion", "thinking"))
    ollama_fake.add_model("qwen2.5vl:7b", ("completion", "vision"))
    with pytest.raises(MissingCapability) as caught:
        ollama.caption("qwen3:8b", MESSAGES, schema=SCHEMA, seed=1)
    assert str(caught.value) == (
        f"Ollama at {ollama_fake.url}: the model qwen3:8b lacks the vision capability, which captioning needs "
        "(its capabilities: completion, thinking); choose a model that lists vision"
    )
    assert ollama_fake.calls("POST", "/api/chat") == []
    ollama_fake.default_answer = Answer(content='{"caption": "mara_lind at a cafe"}')
    image = [{"role": "user", "content": "Describe the image.", "images": ["iVBORw0KGgo="]}]
    assert ollama.caption("qwen2.5vl:7b", image, schema=SCHEMA, seed=1) == '{"caption": "mara_lind at a cafe"}'
    assert ollama_fake.calls("POST", "/api/chat")[-1].json()["messages"] == image
    with pytest.raises(MissingModel, match="does not have the model llava:13b"):
        ollama.caption("llava:13b", image, schema=SCHEMA, seed=1)


def test_the_plan_and_caption_budgets_come_from_timing(ollama, ollama_fake, timing) -> None:
    ollama_fake.add_model("qwen2.5vl:7b", ("completion", "vision"))
    late = Answer(content="{}", delay_s=timing.caption_attempt_s + 0.3)  # past the caption budget
    ollama_fake.script(late, Answer(content='{"slides": []}', delay_s=timing.plan_attempt_s * 0.3))
    with pytest.raises(TimedOut, match=f"/api/chat: no answer within {timing.caption_attempt_s:g} s"):
        ollama.caption("qwen2.5vl:7b", MESSAGES, schema=SCHEMA, seed=1)
    assert ollama.plan("qwen2.5vl:7b", MESSAGES, schema=SCHEMA, seed=1) == '{"slides": []}'  # within its budget
    ollama_fake.pause("/api/tags")
    with pytest.raises(TimedOut, match=f"/api/tags: no answer within {timing.request_s:g} s"):
        ollama.tags()


def test_unload_all_reads_api_ps_and_sends_keep_alive_0(ollama, ollama_fake) -> None:
    for name in ("qwen3:8b", "dolphin3:8b"):
        ollama_fake.add_model(name)
    ollama_fake.default_answer = Answer(content="{}")
    for name in ("qwen3:8b", "dolphin3:8b"):
        ollama.plan(name, MESSAGES, schema=SCHEMA, seed=1)  # each chat loads its model
    assert set(ollama.loaded()) == {"qwen3:8b", "dolphin3:8b"}
    assert set(ollama.unload_all()) == {"qwen3:8b", "dolphin3:8b"}
    assert ollama_fake.loaded == {}
    unloads = [call.json() for call in ollama_fake.calls("POST", "/api/chat") if call.json()["messages"] == []]
    assert sorted(unloads, key=lambda body: body["model"]) == [
        {"model": "dolphin3:8b", "messages": [], "keep_alive": 0},
        {"model": "qwen3:8b", "messages": [], "keep_alive": 0},
    ]
    assert ollama.unload_all() == ()


# ---- both: the start check ----


@pytest.mark.parametrize(
    "url", ["http://192.0.2.10:8188", "http://[2001:db8::10]:11434", "http://0.0.0.0:18188", "https://127.0.0.1:18188"]
)
@pytest.mark.parametrize(("client", "setting"), [(ComfyClient, "COMFYUI_URL"), (OllamaClient, "OLLAMA_URL")])
def test_a_non_loopback_url_is_refused_before_any_socket(client, setting, url, monkeypatch, timing, socket_guard) -> None:
    monkeypatch.setenv(setting, url)

    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"a lookup or connection was attempted: {args}")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    refused_before = list(socket_guard.refused)
    with pytest.raises(Refused) as caught:
        client.from_env(timing=timing)
    assert url.rstrip("/") in str(caught.value) and setting in str(caught.value)
    assert socket_guard.refused == refused_before
