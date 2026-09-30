"""A fake Ollama_API (task 3.3, design §C6.2 and Testing Strategy, Req 27.2).

Endpoints: GET /api/tags, POST /api/show, GET /api/ps and POST /api/chat, answering in Ollama's shapes.

- Models are added with add_model(name, capabilities); /api/tags lists them and /api/show reports their
  capabilities. An unknown model is a 404, as in Ollama; a name without a tag also matches `<name>:latest`.
- A chat with messages must send `stream: false` (the fake does not stream) and takes the next scripted
  Answer (script(*answers)), or default_answer; with neither it fails with a 500 naming the gap. An Answer
  may be a callable that gets the request body and returns an Answer.
- `think` sent to a model without the `thinking` capability is a 400, as in Ollama, so a client that sends
  it to every model is caught.
- The unload call, `{model, messages: [], keep_alive: 0}`, needs no `stream` and removes the model from
  /api/ps. A chat with messages loads its model into /api/ps.

pause() and override() come from FakeServer; Answer.delay_s delays one answer (math.inf: a chat that runs
forever), and stop() cuts it short. A chat answered with status 400 is a rejection, 500 a failure.
"""
from __future__ import annotations

import hashlib
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Union

from fakes._server import FakeServer, Request, Response


@dataclass(frozen=True)
class Answer:
    """One scripted /api/chat answer."""

    content: str = ""
    thinking: str | None = None
    delay_s: float = 0.0  # seconds before the answer is sent; math.inf: never answers (runs until stop())
    status: int = 200
    error: str | None = None  # with status >= 400 (or on its own): answered as {"error": error}


Scripted = Union[Answer, Callable[[dict[str, Any]], Answer]]


def _stamp(at: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() if at is None else at)) + ".000000Z"


class OllamaFake(FakeServer):
    """The fake Ollama_API; see the module docstring."""

    name = "ollama-fake"

    def __init__(self, models: Mapping[str, Iterable[str]] | None = None) -> None:
        super().__init__()
        self.models: dict[str, tuple[str, ...]] = {}  # name -> capabilities
        self.loaded: dict[str, float] = {}  # name -> when it was loaded
        self.default_answer: Scripted | None = None
        self._answers: deque[Scripted] = deque()
        for name, capabilities in (models or {}).items():
            self.add_model(name, capabilities)

    # ---- script API ----

    def add_model(self, name: str, capabilities: Iterable[str] = ("completion",)) -> None:
        with self._cond:
            self.models[name] = tuple(capabilities)

    def script(self, *answers: Scripted) -> None:
        """Queue answers for the next chats with messages, in order."""
        with self._cond:
            self._answers.extend(answers)

    # ---- helpers ----

    def _resolve(self, name: object) -> str | None:
        if not isinstance(name, str) or not name:
            return None
        if name in self.models:
            return name
        if ":" not in name and f"{name}:latest" in self.models:
            return f"{name}:latest"
        return None

    @staticmethod
    def _details(name: str) -> dict[str, Any]:
        family = name.split(":", 1)[0]
        return {
            "parent_model": "",
            "format": "gguf",
            "family": family,
            "families": [family],
            "parameter_size": "8.2B",
            "quantization_level": "Q4_K_M",
        }

    def _entry(self, name: str) -> dict[str, Any]:
        return {
            "name": name,
            "model": name,
            "modified_at": _stamp(0),
            "size": 5_000_000_000,
            "digest": hashlib.sha256(name.encode()).hexdigest(),
            "details": self._details(name),
        }

    # ---- endpoints ----

    def handle(self, request: Request) -> Response:
        method, path = request.method, request.path
        if method == "GET" and path == "/api/tags":
            with self._cond:
                return Response.json({"models": [self._entry(name) for name in self.models]})
        if method == "GET" and path == "/api/ps":
            with self._cond:
                models = []
                for name, at in self.loaded.items():
                    entry = self._entry(name)
                    entry.update({"expires_at": _stamp(at + 600), "size_vram": entry["size"]})
                    models.append(entry)
                return Response.json({"models": models})
        if method == "POST" and path == "/api/show":
            return self._show(request)
        if method == "POST" and path == "/api/chat":
            return self._chat(request)
        return Response(404, b"404 page not found", "text/plain")

    def _body(self, request: Request) -> dict[str, Any] | None:
        try:
            data = request.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _show(self, request: Request) -> Response:
        data = self._body(request)
        if data is None:
            return Response.json({"error": "invalid JSON body"}, 400)
        wanted = data.get("model", data.get("name"))
        with self._cond:
            name = self._resolve(wanted)
            if name is None:
                return Response.json({"error": f"model '{wanted}' not found"}, 404)
            return Response.json(
                {
                    "modelfile": f"FROM {name}\n",
                    "parameters": "",
                    "template": "{{ .Prompt }}",
                    "details": self._details(name),
                    "model_info": {"general.architecture": "fake"},
                    "capabilities": list(self.models[name]),
                    "modified_at": _stamp(0),
                }
            )

    def _chat(self, request: Request) -> Response:
        data = self._body(request)
        if data is None:
            return Response.json({"error": "invalid JSON body"}, 400)
        wanted = data.get("model")
        if not wanted:
            return Response.json({"error": "model is required"}, 400)
        messages = data.get("messages") or []
        with self._cond:
            name = self._resolve(wanted)
            if name is None:
                return Response.json({"error": f'model "{wanted}" not found, try pulling it first'}, 404)
            if "think" in data and "thinking" not in self.models[name]:
                return Response.json({"error": f'"{wanted}" does not support thinking'}, 400)
            if not messages:  # load or unload; no stream flag needed
                unload = data.get("keep_alive") in (0, "0", "0s", "0m")
                if unload:
                    self.loaded.pop(name, None)
                else:
                    self.loaded[name] = time.time()
                return Response.json(
                    {
                        "model": name,
                        "created_at": _stamp(),
                        "message": {"role": "assistant", "content": ""},
                        "done_reason": "unload" if unload else "load",
                        "done": True,
                    }
                )
            if data.get("stream") is not False:
                return Response.json({"error": "the Ollama fake answers only stream:false chats"}, 400)
            scripted = self._answers.popleft() if self._answers else self.default_answer
        if scripted is None:
            return Response.json({"error": "the Ollama fake has no scripted answer left"}, 500)
        answer = scripted if isinstance(scripted, Answer) else scripted(data)
        if answer.delay_s > 0 and not self.wait(answer.delay_s):
            return Response(503, b"", "text/plain")  # stopped while waiting; the connection closes
        if answer.error is not None or answer.status >= 400:
            return Response.json({"error": answer.error or "fake error"}, answer.status if answer.status >= 400 else 500)
        with self._cond:
            self.loaded[name] = time.time()
        message: dict[str, Any] = {"role": "assistant", "content": answer.content}
        if answer.thinking is not None:
            message["thinking"] = answer.thinking
        return Response.json(
            {
                "model": name,
                "created_at": _stamp(),
                "message": message,
                "done": True,
                "done_reason": "stop",
                "total_duration": 1_000_000,
                "load_duration": 100_000,
                "prompt_eval_count": 10,
                "prompt_eval_duration": 100_000,
                "eval_count": max(1, len(answer.content) // 4),
                "eval_duration": 500_000,
            },
            answer.status,
        )
