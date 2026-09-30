"""A fake ComfyUI_API (task 3.3, design §C6.1 and Testing Strategy, Req 27.2).

Endpoints: GET /system_stats, GET /object_info[/<class>], GET /models[/<folder>], POST /upload/image,
POST /prompt, GET /queue, GET /history[/<id>], POST /interrupt and POST /free, answering in ComfyUI's shapes.

Each POST /prompt takes the next Plan from the script (script(*plans)), or default_plan when the script is
empty. A graph that is not a map of nodes with a class_type, or a client prompt_id that is not a lowercase
UUID, is a 400 `invalid_prompt` that uses no Plan. A Plan's outcome is one of:

- SUCCESS: the prompt runs run_s seconds, writes one output file per saver node (SaveImage and
  VHS_VideoCombine, named from filename_prefix with ComfyUI's counter) into output_dir, and its history
  reads status_str "success" with execution_start and execution_success messages.
- REJECT: POST /prompt answers 400 with node_errors naming the Plan's node (default the first node);
  nothing is queued.
- ERROR: it runs run_s seconds, then its history reads "error" with execution_error.exception_message.
- FOREVER: it runs until POST /interrupt names it (or names nothing); its history then reads "error"
  with execution_interrupted.
- FORGET: it is accepted, runs run_s seconds, then vanishes from the queue and history with no trace.

One prompt runs at a time, the rest wait in queue_pending, as in ComfyUI. Time moves on whenever a request
arrives (or advance() is called), so a test sees the same states a polling client sees. pause() from
FakeServer holds answers; forget() drops ids at any time, as a ComfyUI restart does; enqueue() puts a prompt
in the queue without a POST, for resume tests.
"""
from __future__ import annotations

import os
import re
import struct
import time
import uuid
import zlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fakes._server import FakeServer, Request, Response, parse_multipart

SUCCESS, REJECT, ERROR, FOREVER, FORGET = "success", "reject", "error", "forever", "forget"
OUTCOMES = (SUCCESS, REJECT, ERROR, FOREVER, FORGET)

# Saver node classes: class_type -> (outputs key, file extension, ComfyUI's "_" after the counter).
SAVERS = {"SaveImage": ("images", "png", True), "VHS_VideoCombine": ("gifs", "mp4", False)}

MODEL_FOLDERS = (
    "checkpoints", "diffusion_models", "text_encoders", "vae", "loras", "upscale_models", "ultralytics_bbox",
    "vosr2", "audio_encoders", "latent_upscale_models", "model_patches",
)  # fmt: skip


@dataclass(frozen=True)
class Plan:
    """What happens to one submitted prompt (see the module docstring)."""

    outcome: str = SUCCESS
    run_s: float = 0.0  # seconds from the start of its run to its outcome
    message: str = ""  # the rejection or execution error text
    node: str | None = None  # the node a REJECT or ERROR names; default the first node id
    size: tuple[int, int] = (8, 8)  # width and height of the PNG each SaveImage node writes
    content: bytes | None = None  # write these bytes instead of the generated PNG or video stand-in

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {self.outcome!r}; one of {OUTCOMES}")


@dataclass
class Prompt:
    """The fake's record of one prompt."""

    prompt_id: str
    number: int
    graph: dict[str, Any]
    client_id: str | None
    plan: Plan
    submitted: float
    state: str = "pending"  # pending | running | finished | lost
    started: float | None = None
    history: dict[str, Any] | None = None
    files: list[Path] = field(default_factory=list)

    @property
    def output_nodes(self) -> list[str]:
        return [node_id for node_id, node in _nodes(self.graph) if node.get("class_type") in SAVERS]

    def queue_entry(self) -> list[Any]:
        return [self.number, self.prompt_id, self.graph, {"client_id": self.client_id}, self.output_nodes]


def _node_key(node_id: str) -> tuple[int, int | str]:
    return (0, int(node_id)) if node_id.isdigit() else (1, node_id)


def _nodes(graph: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    return sorted(graph.items(), key=lambda item: _node_key(item[0]))


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


def is_lowercase_uuid(value: object) -> bool:
    """True for a UUID in canonical lowercase form, the only client prompt_id ComfyUI accepts."""
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def png_bytes(width: int, height: int, rgb: tuple[int, int, int] = (128, 128, 128)) -> bytes:
    """A valid RGB PNG of one flat colour, built with the standard library only."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    row = b"\x00" + bytes(rgb) * width
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * height, 9))
        + chunk(b"IEND", b"")
    )


def default_object_info() -> dict[str, Any]:
    """A few core node classes in /object_info's shape; tests replace object_info as they need."""

    def spec(name: str, required: dict[str, Any], outputs: list[str], output_node: bool = False) -> dict[str, Any]:
        return {
            "input": {"required": required},
            "input_order": {"required": list(required)},
            "output": outputs,
            "output_is_list": [False] * len(outputs),
            "output_name": outputs,
            "name": name,
            "display_name": name,
            "description": "",
            "python_module": "nodes",
            "category": "fake",
            "output_node": output_node,
        }

    return {
        "LoadImage": spec("LoadImage", {"image": [["example.png"], {"image_upload": True}]}, ["IMAGE", "MASK"]),
        "SaveImage": spec(
            "SaveImage", {"images": ["IMAGE"], "filename_prefix": ["STRING", {"default": "ComfyUI"}]}, [], True
        ),
        "EmptyLatentImage": spec(
            "EmptyLatentImage",
            {"width": ["INT", {"default": 512}], "height": ["INT", {"default": 512}], "batch_size": ["INT", {}]},
            ["LATENT"],
        ),
    }


class ComfyFake(FakeServer):
    """The fake ComfyUI_API; see the module docstring for the endpoints and the Plan script."""

    name = "comfy-fake"

    def __init__(self, output_dir: Path, input_dir: Path, *, comfyui_version: str = "0.3.60") -> None:
        super().__init__()
        self.output_dir = Path(output_dir)
        self.input_dir = Path(input_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.system_stats: dict[str, Any] = {
            "system": {
                "os": "posix",
                "ram_total": 2_000_000_000_000,
                "ram_free": 1_900_000_000_000,
                "comfyui_version": comfyui_version,
                "required_frontend_version": "1.28.6",
                "python_version": "3.12.11",
                "pytorch_version": "2.8.0+cu128",
                "embedded_python": False,
                "argv": ["main.py", "--listen", "127.0.0.1", "--port", "18188"],
            },
            "devices": [
                {
                    "name": "cuda:0 NVIDIA H200 : cudaMallocAsync",
                    "type": "cuda",
                    "index": 0,
                    "vram_total": 150_000_000_000,
                    "vram_free": 149_000_000_000,
                    "torch_vram_total": 0,
                    "torch_vram_free": 0,
                }
            ],
        }
        self.object_info: dict[str, Any] = default_object_info()
        self.models: dict[str, list[str]] = {folder: [] for folder in MODEL_FOLDERS}
        self.default_plan = Plan()
        self.frees: list[Any] = []  # POST /free bodies
        self.interrupts: list[str | None] = []  # POST /interrupt prompt ids (None: no id sent)
        self._plans: deque[Plan] = deque()
        self._prompts: dict[str, Prompt] = {}
        self._number = 0
        self._free_at = 0.0  # when the last run ended

    # ---- script API ----

    def script(self, *plans: Plan) -> None:
        """Queue plans for the next POST /prompt calls, in order."""
        with self._cond:
            self._plans.extend(plans)

    def enqueue(
        self,
        graph: dict[str, Any],
        *,
        prompt_id: str | None = None,
        client_id: str | None = None,
        plan: Plan | None = None,
    ) -> str:
        """Put a prompt in the queue as if it had been submitted earlier; returns its id."""
        with self._cond:
            return self._enqueue(graph, prompt_id or str(uuid.uuid4()), client_id, plan or self.default_plan)

    def forget(self, *prompt_ids: str) -> None:
        """Drop these prompts (all when none is named) from the queue and history, as a restart does."""
        with self._cond:
            now = time.time()
            for prompt in list(self._prompts.values()):
                if prompt_ids and prompt.prompt_id not in prompt_ids:
                    continue
                if prompt.state == "running":
                    self._free_at = now
                prompt.state, prompt.history = "lost", None

    def advance(self) -> None:
        """Move time on to now, as any request does."""
        with self._cond:
            self._advance(time.time())

    def prompt(self, prompt_id: str) -> Prompt:
        """The record of one prompt, including the graph it was submitted with."""
        with self._cond:
            return self._prompts[prompt_id]

    @property
    def prompt_ids(self) -> list[str]:
        """Every prompt id the fake accepted or was given, in queue order."""
        with self._cond:
            return [p.prompt_id for p in sorted(self._prompts.values(), key=lambda p: p.number)]

    # ---- the queue ----

    def _enqueue(self, graph: dict[str, Any], prompt_id: str, client_id: str | None, plan: Plan) -> str:
        self._number += 1
        self._prompts[prompt_id] = Prompt(prompt_id, self._number, graph, client_id, plan, time.time())
        return prompt_id

    def _by_state(self, state: str) -> list[Prompt]:
        return sorted((p for p in self._prompts.values() if p.state == state), key=lambda p: p.number)

    def _advance(self, now: float) -> None:
        while True:
            running = self._by_state("running")
            if not running:
                pending = self._by_state("pending")
                if not pending:
                    return
                head = pending[0]
                head.state, head.started = "running", max(head.submitted, self._free_at)
                continue
            prompt = running[0]
            if prompt.plan.outcome == FOREVER:
                return
            end = (prompt.started or now) + prompt.plan.run_s
            if now < end:
                return
            self._finish(prompt, end)
            self._free_at = end

    def _messages(self, prompt: Prompt, last: list[Any]) -> list[Any]:
        start = _ms(prompt.started or prompt.submitted)
        return [
            ["execution_start", {"prompt_id": prompt.prompt_id, "timestamp": start}],
            ["execution_cached", {"nodes": [], "prompt_id": prompt.prompt_id, "timestamp": start}],
            last,
        ]

    def _history(self, prompt: Prompt, status: str, last: list[Any], outputs: dict[str, Any]) -> None:
        prompt.state = "finished"
        prompt.history = {
            "prompt": prompt.queue_entry(),
            "outputs": outputs,
            "status": {"status_str": status, "completed": status == "success", "messages": self._messages(prompt, last)},
            "meta": {node: {"node_id": node, "display_node": node} for node in outputs},
        }

    def _failure(self, prompt: Prompt, at: float, message: str) -> None:
        node_id, node = self._named_node(prompt.graph, prompt.plan.node)
        self._history(
            prompt,
            "error",
            [
                "execution_error",
                {
                    "prompt_id": prompt.prompt_id,
                    "node_id": node_id,
                    "node_type": node.get("class_type", ""),
                    "executed": [],
                    "exception_message": message,
                    "exception_type": "RuntimeError",
                    "traceback": [],
                    "current_inputs": {},
                    "current_outputs": {},
                    "timestamp": _ms(at),
                },
            ],
            {},
        )

    def _finish(self, prompt: Prompt, at: float) -> None:
        outcome = prompt.plan.outcome
        if outcome == FORGET:
            prompt.state = "lost"
            return
        if outcome == ERROR:
            self._failure(prompt, at, prompt.plan.message or "fake execution error")
            return
        try:
            outputs = self._write_outputs(prompt)
        except ValueError as error:  # ComfyUI refuses to save outside its output folder
            self._failure(prompt, at, str(error))
            return
        self._history(
            prompt, "success", ["execution_success", {"prompt_id": prompt.prompt_id, "timestamp": _ms(at)}], outputs
        )

    def _interrupt(self, prompt: Prompt, at: float) -> None:
        node_id, node = self._named_node(prompt.graph, None)
        self._history(
            prompt,
            "error",
            [
                "execution_interrupted",
                {
                    "prompt_id": prompt.prompt_id,
                    "node_id": node_id,
                    "node_type": node.get("class_type", ""),
                    "executed": [],
                    "timestamp": _ms(at),
                },
            ],
            {},
        )
        self._free_at = at

    @staticmethod
    def _named_node(graph: dict[str, Any], wanted: str | None) -> tuple[str, dict[str, Any]]:
        if wanted is not None and wanted in graph:
            return wanted, graph[wanted]
        nodes = _nodes(graph)
        return nodes[0] if nodes else ("", {})

    def _write_outputs(self, prompt: Prompt) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        root = self.output_dir.resolve()
        for node_id, node in _nodes(prompt.graph):
            saver = SAVERS.get(node.get("class_type", ""))
            if saver is None:
                continue
            key, extension, underscore = saver
            prefix = node.get("inputs", {}).get("filename_prefix", "ComfyUI")
            if not isinstance(prefix, str) or not prefix:
                prefix = "ComfyUI"
            subfolder, base = os.path.split(prefix)
            folder = (root / subfolder).resolve()
            if folder != root and root not in folder.parents:
                raise ValueError("Saving image outside the output folder is not allowed.")
            folder.mkdir(parents=True, exist_ok=True)
            counter = self._next_counter(folder, base, extension)
            name = f"{base}_{counter:05}_.{extension}" if underscore else f"{base}_{counter:05}.{extension}"
            path = folder / name
            if prompt.plan.content is not None:
                data = prompt.plan.content
            elif key == "images":
                data = png_bytes(*prompt.plan.size)
            else:
                data = b"fake video from the ComfyUI fake\n"
            path.write_bytes(data)
            prompt.files.append(path)
            entry = {"filename": name, "subfolder": subfolder, "type": "output"}
            if key == "gifs":
                entry.update({"format": "video/h264-mp4", "frame_rate": 24.0, "fullpath": str(path)})
            outputs[node_id] = {key: [entry]}
        return outputs

    @staticmethod
    def _next_counter(folder: Path, base: str, extension: str) -> int:
        pattern = re.compile(re.escape(base) + r"_(\d{5})_?\." + re.escape(extension) + r"\Z")
        counters = [int(m.group(1)) for m in (pattern.match(p.name) for p in folder.iterdir()) if m]
        return max(counters, default=0) + 1

    # ---- endpoints ----

    def handle(self, request: Request) -> Response:
        with self._cond:
            self._advance(time.time())
            method, path = request.method, request.path
            if method == "GET" and path == "/system_stats":
                return Response.json(self.system_stats)
            if method == "GET" and path == "/object_info":
                return Response.json(self.object_info)
            if method == "GET" and path.startswith("/object_info/"):
                name = path[len("/object_info/") :]
                return Response.json({name: self.object_info[name]} if name in self.object_info else {})
            if method == "GET" and path == "/models":
                return Response.json(sorted(self.models))
            if method == "GET" and path.startswith("/models/"):
                folder = path[len("/models/") :]
                if folder not in self.models:
                    return Response(404, b"", "text/plain")
                return Response.json(list(self.models[folder]))
            if method == "POST" and path == "/upload/image":
                return self._upload(request)
            if method == "POST" and path == "/prompt":
                return self._submit(request)
            if method == "GET" and path == "/queue":
                return Response.json(
                    {
                        "queue_running": [p.queue_entry() for p in self._by_state("running")],
                        "queue_pending": [p.queue_entry() for p in self._by_state("pending")],
                    }
                )
            if method == "GET" and path == "/history":
                return Response.json({p.prompt_id: p.history for p in self._by_state("finished")})
            if method == "GET" and path.startswith("/history/"):
                prompt = self._prompts.get(path[len("/history/") :])
                if prompt is None or prompt.history is None:
                    return Response.json({})
                return Response.json({prompt.prompt_id: prompt.history})
            if method == "POST" and path == "/interrupt":
                return self._interrupt_request(request)
            if method == "POST" and path == "/free":
                try:
                    self.frees.append(request.json())
                except ValueError:
                    self.frees.append(request.body)  # not JSON: kept raw for the test to see
                return Response(200, b"", "text/plain")
            return Response(404, b"404: Not Found", "text/plain")

    def _submit(self, request: Request) -> Response:
        try:
            data = request.json()
        except ValueError:
            data = None
        graph = data.get("prompt") if isinstance(data, dict) else None
        if not isinstance(graph, dict) or not graph or not all(
            isinstance(node, dict) and isinstance(node.get("class_type"), str) for node in graph.values()
        ):
            error = {"type": "invalid_prompt", "message": "Invalid prompt", "details": "", "extra_info": {}}
            return Response.json({"error": error, "node_errors": {}}, 400)
        if "prompt_id" in data and not is_lowercase_uuid(data["prompt_id"]):
            # ComfyUI takes a client prompt_id only in canonical lowercase UUID form (design, sources table)
            details = f"prompt_id must be a lowercase UUID, not {data['prompt_id']!r}"
            error = {"type": "invalid_prompt", "message": "Invalid prompt_id", "details": details, "extra_info": {}}
            return Response.json({"error": error, "node_errors": {}}, 400)
        plan = self._plans.popleft() if self._plans else self.default_plan
        if plan.outcome == REJECT:
            node_id, node = self._named_node(graph, plan.node)
            message = plan.message or "Value not in list"
            error = {
                "type": "prompt_outputs_failed_validation",
                "message": "Prompt outputs failed validation",
                "details": "",
                "extra_info": {},
            }
            node_error = {
                "errors": [{"type": "value_not_in_list", "message": message, "details": message, "extra_info": {}}],
                "dependent_outputs": [n for n, v in _nodes(graph) if v.get("class_type") in SAVERS],
                "class_type": node.get("class_type", ""),
            }
            return Response.json({"error": error, "node_errors": {node_id: node_error}}, 400)
        prompt_id = data.get("prompt_id") or str(uuid.uuid4())
        client_id = data.get("client_id")
        self._enqueue(graph, str(prompt_id), client_id if isinstance(client_id, str) else None, plan)
        self._advance(time.time())
        return Response.json({"prompt_id": prompt_id, "number": self._prompts[str(prompt_id)].number, "node_errors": {}})

    def _interrupt_request(self, request: Request) -> Response:
        try:
            data = request.json()
        except ValueError:
            data = None
        wanted = data.get("prompt_id") if isinstance(data, dict) else None
        self.interrupts.append(wanted)
        for prompt in self._by_state("running"):
            if wanted is None or wanted == prompt.prompt_id:
                self._interrupt(prompt, time.time())
        self._advance(time.time())
        return Response(200, b"", "text/plain")

    def _upload(self, request: Request) -> Response:
        try:
            fields = parse_multipart(request.headers.get("content-type", ""), request.body)
        except ValueError as error:
            return Response(400, str(error).encode(), "text/plain")
        if "image" not in fields or not fields["image"][0]:
            return Response(400, b"", "text/plain")
        filename, data = fields["image"]
        upload_type = fields.get("type", (None, b"input"))[1].decode() or "input"
        folders = {"input": self.input_dir, "output": self.output_dir, "temp": self.input_dir.parent / "temp"}
        if upload_type not in folders:
            return Response(400, b"", "text/plain")
        base = folders[upload_type].resolve()
        subfolder = fields.get("subfolder", (None, b""))[1].decode()
        folder = (base / os.path.normpath(subfolder)).resolve() if subfolder else base
        target = (folder / str(filename)).resolve()
        if base not in target.parents:
            return Response(400, b"", "text/plain")
        folder.mkdir(parents=True, exist_ok=True)
        overwrite = fields.get("overwrite", (None, b""))[1].decode() in ("true", "1")
        name = str(filename)
        if not overwrite:
            stem, suffix = os.path.splitext(name)
            count = 1
            while (folder / name).exists() and (folder / name).read_bytes() != data:
                name = f"{stem} ({count}){suffix}"
                count += 1
        (folder / name).write_bytes(data)
        return Response.json({"name": name, "subfolder": subfolder, "type": upload_type})
