"""The Ollama_API client (design §C6.2; Req 6.13, 6.15, 9.7, 18.9, 18.10, 19.6, 25.1).

A thin layer over http.LoopbackClient, so every request stays on loopback. The time limits come from the
Timing object, so the tests shorten them.

| method                   | call                                                          | time limit          |
|--------------------------|---------------------------------------------------------------|---------------------|
| tags(), require(models)  | GET /api/tags: the installed models, checked for every model  | request_s (10 s)    |
|                          | a plan needs before its first request (Req 6.15) and for the  |                     |
|                          | vision model (Req 18.9)                                       |                     |
| capabilities(model)      | POST /api/show {model}: `capabilities`, kept per model        | request_s           |
| plan(), caption(), chat()| POST /api/chat {model, messages, stream: false, format: <JSON | plan_attempt_s      |
|                          | schema>, think?, options: {temperature: 0.8, seed},           | (600 s, Req 6.13);  |
|                          | keep_alive: "10m"}. think: false is sent only when the model  | caption_attempt_s   |
|                          | lists `thinking`; caption() needs `vision`                    | (120 s, Req 18.10)  |
| unload_all()             | GET /api/ps, then POST /api/chat {model, messages: [],        | request_s each      |
|                          | keep_alive: 0} for each loaded model (Req 9.7, 19.6)          |                     |

Build the client when the command starts: the LoopbackClient checks OLLAMA_URL then, before any request.
Failures are http.HttpError or OllamaError, both EnvironmentFailure (exit status 2).
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from influencer import env
from influencer.errors import EnvironmentFailure
from influencer.http import BadAnswer, LoopbackClient, Response
from influencer.timing import DEFAULT_TIMING, Timing

SERVICE, SETTING = "Ollama", "OLLAMA_URL"
TEMPERATURE = 0.8
KEEP_ALIVE = "10m"
THINKING, VISION = "thinking", "vision"


class OllamaError(EnvironmentFailure):
    """Ollama cannot serve what the command needs."""


class MissingModel(OllamaError):
    """Models the command needs are not installed in Ollama (Req 6.15, 18.9)."""

    def __init__(self, url: str, models: Sequence[str]) -> None:
        self.models = tuple(models)
        pulls = "; ".join(f"ollama pull {model}" for model in self.models)
        noun = "model" if len(self.models) == 1 else "models"
        listed = ", ".join(self.models)
        super().__init__(f"{SERVICE} at {url} does not have the {noun} {listed}: install with {pulls}")


class MissingCapability(OllamaError):
    """A model lacks a capability the call needs: vision for captioning."""

    def __init__(self, url: str, model: str, capability: str, capabilities: Sequence[str], purpose: str) -> None:
        self.model, self.capability = model, capability
        listed = ", ".join(capabilities) or "none listed"
        super().__init__(
            f"{SERVICE} at {url}: the model {model} lacks the {capability} capability, which {purpose} needs "
            f"(its capabilities: {listed}); choose a model that lists {capability}"
        )


def model_key(name: str) -> str:
    """Ollama's full name for a model: a name without a tag means <name>:latest."""
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def _names(data: Any, url: str) -> tuple[str, ...]:
    """The model names of an /api/tags or /api/ps answer, {"models": [{"name": ...}, ...]}."""
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise BadAnswer(f"{SERVICE} at {url}: the answer has no models list", url)
    names = []
    for entry in models:
        name = entry.get("name") or entry.get("model") if isinstance(entry, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(names)


class OllamaClient:
    """The §C6.2 calls (see the module docstring). url is OLLAMA_URL, checked here; loopback replaces the
    LoopbackClient (the tests' resolver and connector)."""

    def __init__(self, url: str = env.DEFAULT_OLLAMA_URL, *, timing: Timing | None = None,
                 loopback: LoopbackClient | None = None) -> None:  # fmt: skip
        self.loopback = loopback or LoopbackClient(url, SERVICE, setting=SETTING)
        self.timing = timing or DEFAULT_TIMING
        self._capabilities: dict[str, tuple[str, ...]] = {}

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, timing: Timing | None = None) -> OllamaClient:
        """The client for OLLAMA_URL (default http://127.0.0.1:11434), checked before any request."""
        return cls(env.paths(environ).ollama_url, timing=timing)

    @property
    def url(self) -> str:
        return self.loopback.url

    def tags(self) -> tuple[str, ...]:
        """The installed models, as /api/tags names them."""
        data = self.loopback.get_json("/api/tags", timeout=self.timing.request_s)
        return _names(data, f"{self.url}/api/tags")

    def require(self, models: Iterable[str]) -> None:
        """MissingModel naming each of models that /api/tags does not list (a name without a tag is its
        :latest), before the command sends its first request that needs them (Req 6.15)."""
        installed = {model_key(name) for name in self.tags()}
        missing = [model for model in dict.fromkeys(models) if model_key(model) not in installed]
        if missing:
            raise MissingModel(self.url, missing)

    def capabilities(self, model: str) -> tuple[str, ...]:
        """The model's /api/show capabilities (kept for the next call); MissingModel when Ollama does not
        know the model."""
        if model not in self._capabilities:
            response = self.loopback.post_json("/api/show", {"model": model}, timeout=self.timing.request_s)
            self._missing(response, model)
            data = response.expect().json()
            found = data.get("capabilities") if isinstance(data, dict) else None
            if found is not None and not (isinstance(found, list) and all(isinstance(c, str) for c in found)):
                raise BadAnswer(f"{response.where}: capabilities is not a list of names", response.url)
            self._capabilities[model] = tuple(found or ())
        return self._capabilities[model]

    def _missing(self, response: Response, model: str) -> None:
        if response.status == 404:
            raise MissingModel(self.url, [model])

    def chat(self, model: str, messages: Sequence[Mapping[str, Any]], *, schema: Mapping[str, Any] | str, seed: int,
             timeout: float) -> str:  # fmt: skip
        """One /api/chat request that answers in one piece (stream: false) in the JSON format schema, with
        temperature 0.8, the seed and keep_alive 10m; think: false only for a model that lists thinking.
        Returns the message content, for the caller to parse and check."""
        body: dict[str, Any] = {
            "model": model,
            "messages": [dict(message) for message in messages],
            "stream": False,
            "format": schema,
            "options": {"temperature": TEMPERATURE, "seed": seed},
            "keep_alive": KEEP_ALIVE,
        }
        if THINKING in self.capabilities(model):
            body["think"] = False
        response = self.loopback.post_json("/api/chat", body, timeout=timeout)
        self._missing(response, model)
        answer = response.expect().json()
        message = answer.get("message") if isinstance(answer, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise BadAnswer(f"{response.where}: the answer has no message content", response.url)
        return content

    def plan(self, model: str, messages: Sequence[Mapping[str, Any]], *, schema: Mapping[str, Any] | str,
             seed: int) -> str:  # fmt: skip
        """One plan attempt: chat() within Timing.plan_attempt_s (600 s, Req 6.13)."""
        return self.chat(model, messages, schema=schema, seed=seed, timeout=self.timing.plan_attempt_s)

    def caption(self, model: str, messages: Sequence[Mapping[str, Any]], *, schema: Mapping[str, Any] | str,
                seed: int) -> str:  # fmt: skip
        """One caption attempt: chat() within Timing.caption_attempt_s (120 s, Req 18.10), for a model that
        lists vision; MissingCapability, before the chat, for one that does not."""
        capabilities = self.capabilities(model)
        if VISION not in capabilities:
            raise MissingCapability(self.url, model, VISION, capabilities, "captioning")
        return self.chat(model, messages, schema=schema, seed=seed, timeout=self.timing.caption_attempt_s)

    def loaded(self) -> tuple[str, ...]:
        """The models Ollama holds in memory, as /api/ps names them."""
        data = self.loopback.get_json("/api/ps", timeout=self.timing.request_s)
        return _names(data, f"{self.url}/api/ps")

    def unload(self, model: str) -> None:
        """POST /api/chat {model, messages: [], keep_alive: 0}: Ollama drops the model from memory."""
        body = {"model": model, "messages": [], "keep_alive": 0}
        self.loopback.post_json("/api/chat", body, timeout=self.timing.request_s).expect()

    def unload_all(self) -> tuple[str, ...]:
        """Unload every model /api/ps lists (Req 9.7, 19.6); returns their names."""
        names = self.loaded()
        for name in names:
            self.unload(name)
        return names
