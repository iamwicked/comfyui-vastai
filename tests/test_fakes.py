"""Tests of the fake ComfyUI and Ollama servers (task 3.3): tests/fakes/ and the comfy_fake and ollama_fake
fixtures. Every request goes to 127.0.0.1 through http.client, under the socket guard."""
from __future__ import annotations

import json
import os
import struct
import threading
import time

import pytest

from fakes import Answer, ComfyFake, OllamaFake, Plan, encode_multipart, http_request
from fakes.comfy import ERROR, FOREVER, FORGET, REJECT

SAVE_PREFIX = "influencer/test-persona/2026-W40/p1/render/s01_c1"


def _graph(prefix: str = SAVE_PREFIX) -> dict:
    return {
        "3": {"class_type": "EmptyLatentImage", "inputs": {"width": 8, "height": 8, "batch_size": 1}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": prefix}},
    }


def _json(url: str, method: str = "GET", body: object = None, timeout: float = 5.0) -> tuple[int, object]:
    status, _, data = http_request(url, method, json_body=body, timeout=timeout)
    return status, json.loads(data) if data else None


def _submit(fake: ComfyFake, graph: dict | None = None, prompt_id: str | None = None) -> tuple[int, dict]:
    body = {"prompt": graph or _graph(), "client_id": "test-client"}
    if prompt_id:
        body["prompt_id"] = prompt_id
    return _json(fake.url + "/prompt", "POST", body)  # type: ignore[return-value]


def _history(fake: ComfyFake, prompt_id: str) -> dict:
    return _json(f"{fake.url}/history/{prompt_id}")[1]  # type: ignore[return-value]


def _queue(fake: ComfyFake) -> tuple[list[str], list[str]]:
    queue = _json(fake.url + "/queue")[1]
    return [e[1] for e in queue["queue_running"]], [e[1] for e in queue["queue_pending"]]  # type: ignore[index]


def _png_size(data: bytes) -> tuple[int, int]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


# ---- ComfyUI ----


def test_the_comfy_fixture_points_comfyui_url_at_the_fake(comfy_fake):
    assert os.environ["COMFYUI_URL"] == comfy_fake.url
    assert comfy_fake.url.startswith("http://127.0.0.1:")


def test_system_stats_object_info_and_models(comfy_fake):
    status, stats = _json(comfy_fake.url + "/system_stats")
    assert status == 200 and stats["system"]["comfyui_version"]
    comfy_fake.object_info = {"VAEDeGrid": {"input": {"required": {"image": ["IMAGE"]}}}}
    assert _json(comfy_fake.url + "/object_info")[1] == comfy_fake.object_info
    assert _json(comfy_fake.url + "/object_info/VAEDeGrid")[1] == {"VAEDeGrid": comfy_fake.object_info["VAEDeGrid"]}
    comfy_fake.models["loras"] = ["looks/a.safetensors", "characters/x/v1.safetensors"]
    assert _json(comfy_fake.url + "/models/loras")[1] == ["looks/a.safetensors", "characters/x/v1.safetensors"]
    assert "vosr2" in _json(comfy_fake.url + "/models")[1]
    assert http_request(comfy_fake.url + "/models/no_such_folder")[0] == 404


def test_upload_writes_into_the_input_folder(comfy_fake, temp_env):
    def upload(data: bytes, overwrite: str) -> dict:
        body, content_type = encode_multipart(
            {"subfolder": "influencer", "type": "input", "overwrite": overwrite},
            {"image": ("ref.png", data, "image/png")},
        )
        status, _, answer = http_request(
            comfy_fake.url + "/upload/image", "POST", body=body, headers={"Content-Type": content_type}
        )
        assert status == 200
        return json.loads(answer)

    data = b"\x89PNG\r\n\x1a\nbinary\r\n\x00bytes"
    assert upload(data, "true") == {"name": "ref.png", "subfolder": "influencer", "type": "input"}
    assert (temp_env.comfyui_dir / "input" / "influencer" / "ref.png").read_bytes() == data
    assert upload(data, "false")["name"] == "ref.png"  # same bytes: ComfyUI keeps the name
    assert upload(b"other", "false")["name"] == "ref (1).png"


def test_an_accepted_prompt_runs_and_writes_its_output(comfy_fake, temp_env):
    comfy_fake.script(Plan(size=(16, 24)))
    status, answer = _submit(comfy_fake, prompt_id="11111111-1111-4111-8111-111111111111")
    assert status == 200 and answer["prompt_id"] == "11111111-1111-4111-8111-111111111111"
    assert answer["node_errors"] == {}
    entry = _history(comfy_fake, answer["prompt_id"])[answer["prompt_id"]]
    assert entry["status"]["status_str"] == "success" and entry["status"]["completed"] is True
    kinds = [message[0] for message in entry["status"]["messages"]]
    assert kinds[0] == "execution_start" and kinds[-1] == "execution_success"
    assert all(isinstance(message[1]["timestamp"], int) for message in entry["status"]["messages"])
    [image] = entry["outputs"]["9"]["images"]
    assert image == {"filename": "s01_c1_00001_.png", "subfolder": "influencer/test-persona/2026-W40/p1/render", "type": "output"}
    saved = temp_env.output_root / "2026-W40" / "p1" / "render" / "s01_c1_00001_.png"
    assert _png_size(saved.read_bytes()) == (16, 24)
    assert _queue(comfy_fake) == ([], [])
    # the next save of the same prefix takes ComfyUI's next counter
    _, second = _submit(comfy_fake)
    image = _history(comfy_fake, second["prompt_id"])[second["prompt_id"]]["outputs"]["9"]["images"][0]
    assert image["filename"] == "s01_c1_00002_.png"
    assert comfy_fake.prompt(second["prompt_id"]).graph == _graph()


def test_one_prompt_runs_at_a_time(comfy_fake):
    comfy_fake.script(Plan(FOREVER), Plan(run_s=0.2))
    _, first = _submit(comfy_fake)
    _, second = _submit(comfy_fake)
    assert _queue(comfy_fake) == ([first["prompt_id"]], [second["prompt_id"]])
    assert _history(comfy_fake, first["prompt_id"]) == {}
    # an interrupt naming another prompt leaves the running one alone
    assert http_request(comfy_fake.url + "/interrupt", "POST", json_body={"prompt_id": "someone-else"})[0] == 200
    assert _queue(comfy_fake)[0] == [first["prompt_id"]]
    http_request(comfy_fake.url + "/interrupt", "POST", json_body={"prompt_id": first["prompt_id"]})
    entry = _history(comfy_fake, first["prompt_id"])[first["prompt_id"]]
    assert entry["status"]["status_str"] == "error"
    assert entry["status"]["messages"][-1][0] == "execution_interrupted"
    assert _queue(comfy_fake) == ([second["prompt_id"]], [])  # the next one runs for its 0.2 s
    time.sleep(0.3)
    assert _history(comfy_fake, second["prompt_id"])[second["prompt_id"]]["status"]["status_str"] == "success"
    assert comfy_fake.interrupts == ["someone-else", first["prompt_id"]]


def test_a_rejected_prompt_gets_400_with_node_errors(comfy_fake):
    comfy_fake.script(Plan(REJECT, message="Value not in list: sampler_name", node="3"))
    status, answer = _submit(comfy_fake)
    assert status == 400
    assert answer["error"]["type"] == "prompt_outputs_failed_validation"
    node = answer["node_errors"]["3"]
    assert node["class_type"] == "EmptyLatentImage"
    assert node["errors"][0]["message"] == "Value not in list: sampler_name"
    assert comfy_fake.prompt_ids == [] and _queue(comfy_fake) == ([], [])


def test_an_execution_error_shows_in_history(comfy_fake, temp_env):
    comfy_fake.script(Plan(ERROR, message="CUDA out of memory"))
    _, answer = _submit(comfy_fake)
    entry = _history(comfy_fake, answer["prompt_id"])[answer["prompt_id"]]
    assert entry["status"]["status_str"] == "error"
    kind, details = entry["status"]["messages"][-1]
    assert (kind, details["exception_message"]) == ("execution_error", "CUDA out of memory")
    assert not temp_env.output_root.exists()


def test_a_forgotten_prompt_vanishes(comfy_fake):
    comfy_fake.script(Plan(FORGET))
    _, lost = _submit(comfy_fake)
    assert _queue(comfy_fake) == ([], []) and _history(comfy_fake, lost["prompt_id"]) == {}
    _, done = _submit(comfy_fake)
    assert _history(comfy_fake, done["prompt_id"]) != {}
    comfy_fake.forget()  # a restart: queue and history are gone
    assert _history(comfy_fake, done["prompt_id"]) == {}


def test_enqueue_attaches_without_a_post(comfy_fake):
    comfy_fake.enqueue(_graph(), prompt_id="from-an-earlier-run", plan=Plan(FOREVER))
    assert _queue(comfy_fake) == (["from-an-earlier-run"], [])
    assert comfy_fake.calls("POST", "/prompt") == []


def test_a_save_outside_the_output_folder_fails(comfy_fake):
    _, answer = _submit(comfy_fake, _graph("../../escape"))
    entry = _history(comfy_fake, answer["prompt_id"])[answer["prompt_id"]]
    assert "outside the output folder" in entry["status"]["messages"][-1][1]["exception_message"]


def test_bad_prompts_and_free(comfy_fake):
    assert _json(comfy_fake.url + "/prompt", "POST", {"prompt": {"1": {"inputs": {}}}})[0] == 400
    assert http_request(comfy_fake.url + "/free", "POST", json_body={"unload_models": True, "free_memory": True})[0] == 200
    assert comfy_fake.frees == [{"unload_models": True, "free_memory": True}]
    assert http_request(comfy_fake.url + "/nothing-here")[0] == 404


def test_pause_holds_answers_until_resume(comfy_fake):
    comfy_fake.pause("/queue")
    assert http_request(comfy_fake.url + "/system_stats")[0] == 200  # not held
    with pytest.raises(TimeoutError):
        http_request(comfy_fake.url + "/queue", timeout=0.3)
    comfy_fake.resume()
    assert http_request(comfy_fake.url + "/queue")[0] == 200
    assert len(comfy_fake.calls("GET", "/queue")) == 2  # the held request was recorded too


def test_override_replaces_one_answer(comfy_fake):
    comfy_fake.override("GET", "/system_stats", 200, "<html>not ComfyUI</html>", "text/html")
    status, headers, body = http_request(comfy_fake.url + "/system_stats")
    assert (status, headers["content-type"], body) == (200, "text/html", b"<html>not ComfyUI</html>")
    comfy_fake.clear_overrides()
    assert b"comfyui_version" in http_request(comfy_fake.url + "/system_stats")[2]


def test_stop_releases_a_held_request(temp_env):
    fake = ComfyFake(temp_env.comfyui_dir / "output", temp_env.comfyui_dir / "input").start()
    fake.pause()
    outcome: list[object] = []

    def client() -> None:
        try:
            outcome.append(http_request(fake.url + "/queue", timeout=10)[0])
        except Exception as error:  # the connection closes without an answer
            outcome.append(type(error).__name__)

    thread = threading.Thread(target=client)
    thread.start()
    deadline = time.monotonic() + 5
    while not fake.calls("GET", "/queue") and time.monotonic() < deadline:
        time.sleep(0.01)
    started = time.monotonic()
    fake.stop()
    thread.join(5)
    assert time.monotonic() - started < 3
    assert outcome and outcome[0] != 200


# ---- Ollama ----


def test_the_ollama_fixture_points_ollama_url_at_the_fake(ollama_fake):
    assert os.environ["OLLAMA_URL"] == ollama_fake.url


def test_tags_and_show(ollama_fake):
    ollama_fake.add_model("qwen3:8b", ("completion", "thinking"))
    ollama_fake.add_model("qwen2.5vl:7b", ("completion", "vision"))
    assert [m["name"] for m in _json(ollama_fake.url + "/api/tags")[1]["models"]] == ["qwen3:8b", "qwen2.5vl:7b"]
    status, shown = _json(ollama_fake.url + "/api/show", "POST", {"model": "qwen2.5vl:7b"})
    assert status == 200 and shown["capabilities"] == ["completion", "vision"]
    assert _json(ollama_fake.url + "/api/show", "POST", {"model": "missing:1b"})[0] == 404


def test_chat_answers_from_the_script_and_loads_the_model(ollama_fake):
    ollama_fake.add_model("dolphin3:8b")
    ollama_fake.script(Answer(content='{"slides": []}'), lambda body: Answer(content=body["messages"][-1]["content"].upper()))
    request = {
        "model": "dolphin3:8b",
        "messages": [{"role": "user", "content": "plan"}],
        "stream": False,
        "format": {"type": "object"},
        "options": {"temperature": 0.8, "seed": 7},
        "keep_alive": "10m",
    }
    status, answer = _json(ollama_fake.url + "/api/chat", "POST", request)
    assert status == 200 and answer["done"] is True
    assert answer["message"] == {"role": "assistant", "content": '{"slides": []}'}
    assert _json(ollama_fake.url + "/api/chat", "POST", request)[1]["message"]["content"] == "PLAN"
    assert ollama_fake.calls("POST", "/api/chat")[0].json()["format"] == {"type": "object"}
    assert [m["name"] for m in _json(ollama_fake.url + "/api/ps")[1]["models"]] == ["dolphin3:8b"]
    status, answer = _json(ollama_fake.url + "/api/chat", "POST", request)
    assert status == 500 and "no scripted answer" in answer["error"]


def test_chat_needs_stream_false_but_unload_does_not(ollama_fake):
    ollama_fake.add_model("qwen3:8b", ("completion", "thinking"))
    ollama_fake.default_answer = Answer(content="ok", thinking="hmm")
    messages = [{"role": "user", "content": "x"}]
    status, answer = _json(ollama_fake.url + "/api/chat", "POST", {"model": "qwen3:8b", "messages": messages})
    assert status == 400 and "stream" in answer["error"]
    status, answer = _json(
        ollama_fake.url + "/api/chat", "POST", {"model": "qwen3:8b", "messages": messages, "stream": False, "think": False}
    )
    assert status == 200 and answer["message"]["thinking"] == "hmm"
    status, answer = _json(ollama_fake.url + "/api/chat", "POST", {"model": "qwen3:8b", "messages": [], "keep_alive": 0})
    assert status == 200 and answer["done_reason"] == "unload"
    assert _json(ollama_fake.url + "/api/ps")[1]["models"] == []


def test_think_goes_only_to_thinking_models(ollama_fake):
    ollama_fake.add_model("dolphin3:8b")
    ollama_fake.default_answer = Answer(content="ok")
    body = {"model": "dolphin3:8b", "messages": [{"role": "user", "content": "x"}], "stream": False, "think": False}
    status, answer = _json(ollama_fake.url + "/api/chat", "POST", body)
    assert status == 400 and "does not support thinking" in answer["error"]
    status, answer = _json(ollama_fake.url + "/api/chat", "POST", {"model": "nope:1b", "messages": [], "keep_alive": 0})
    assert status == 404


def test_delayed_and_failing_answers(ollama_fake):
    ollama_fake.add_model("dolphin3:8b")
    ollama_fake.script(Answer(content="late", delay_s=0.6), Answer(status=500, error="model crashed"))
    body = {"model": "dolphin3:8b", "messages": [{"role": "user", "content": "x"}], "stream": False}
    with pytest.raises(TimeoutError):
        http_request(ollama_fake.url + "/api/chat", "POST", json_body=body, timeout=0.2)
    status, answer = _json(ollama_fake.url + "/api/chat", "POST", body)
    assert (status, answer) == (500, {"error": "model crashed"})


def test_a_name_without_a_tag_matches_latest():
    with OllamaFake({"llava:latest": ("completion", "vision")}) as fake:
        assert _json(fake.url + "/api/show", "POST", {"name": "llava"})[1]["capabilities"] == ["completion", "vision"]
    with pytest.raises(RuntimeError):
        fake.url  # noqa: B018 - a stopped fake has no URL
