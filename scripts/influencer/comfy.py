"""The ComfyUI_API client (design §C6.1; Req 9.1, 10.6, 25.1).

A thin layer over http.LoopbackClient, so every request stays on loopback: one method per call of the
§C6.1 table, each with its time limit from Timing, and each answer turned into plain data. What the
answers mean (preflight, the Job engine) is decided by the callers.

| method             | call                                                              | time limit           |
|--------------------|-------------------------------------------------------------------|----------------------|
| system_stats()     | GET /system_stats: JSON with system.comfyui_version (Req 9.1)     | request_s (10 s)     |
| object_info()      | GET /object_info, COMFY_AUTOGROW_V3 inputs expanded               | object_info_s (30 s) |
| models(folder)     | GET /models/<folder>                                              | request_s            |
| upload(name, data) | POST /upload/image: multipart image, subfolder=influencer,        | upload_s (60 s)      |
|                    | type=input, overwrite=true                                        |                      |
| submit(graph, id)  | POST /prompt {prompt, prompt_id, client_id}: our UUID4 (Decision  | submit_s (30 s)      |
|                    | 13); a 400 with node_errors raises Rejected (Req 10.6)            |                      |
| queue()            | GET /queue: queue_running[i][1] and queue_pending[i][1]           | request_s            |
| history(id)        | GET /history/<id>: status_str, message timestamps, outputs        | request_s            |
| interrupt(id)      | POST /interrupt {prompt_id} (Req 10.7)                            | request_s            |
| free()             | POST /free {unload_models: true, free_memory: true} (Req 19.6)    | request_s            |

Build the client when the command starts: the LoopbackClient checks COMFYUI_URL then, before any request.
Failures are http.HttpError (exit status 2), IdentityError among them; Rejected is a Job's failure, which
the stage records before it goes on.
"""
from __future__ import annotations

import urllib.parse
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from influencer import env
from influencer.http import BadAnswer, LoopbackClient, Response, StatusError
from influencer.timing import DEFAULT_TIMING, Timing

SERVICE, SETTING = "ComfyUI", "COMFYUI_URL"
UPLOAD_SUBFOLDER = "influencer"  # below ComfyUI's input folder
AUTOGROW = "COMFY_AUTOGROW_V3"
AUTOGROW_MIN, AUTOGROW_MAX = 1, 10  # ComfyUI's TemplatePrefix defaults, for a template that leaves them out
OUTPUT_KINDS = ("images", "gifs")  # outputs[node].images (SaveImage) and outputs[node].gifs (VHS)
EVENTS = ("execution_start", "execution_success", "execution_error", "execution_interrupted")
SUCCESS, ERROR, INTERRUPTED = "success", "error", "interrupted"

_CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac", ".mp4": "video/mp4",
}  # fmt: skip
_NAME_FORBIDDEN = frozenset('/\\"\r\n\0')


class IdentityError(BadAnswer):
    """The server at COMFYUI_URL did not answer /system_stats as ComfyUI does (Req 9.1)."""


class Rejected(Exception):
    """ComfyUI refused a prompt: POST /prompt answered 400 with node_errors (Req 10.6). The Job fails with
    this message; the stage goes on with the other Jobs."""

    def __init__(self, url: str, error: Any, node_errors: Mapping[str, Any]) -> None:
        self.url = url
        self.error = error
        self.node_errors = dict(node_errors)
        super().__init__(f"{SERVICE} at {url} rejected the prompt: {_rejection(error, self.node_errors)}")


def _node_key(node_id: str) -> tuple[int, int | str]:
    return (0, int(node_id)) if node_id.isdigit() else (1, node_id)


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value not in (None, "") else ""


def _rejection(error: Any, node_errors: Mapping[str, Any]) -> str:
    """ComfyUI's error and each node error, on one line: `message (details); node 3 (KSampler): ...`."""
    parts = []
    if isinstance(error, Mapping):
        head = _text(error.get("message")) or _text(error.get("type")) or "no message"
        details = _text(error.get("details"))
        parts.append(head + (f" ({details})" if details else ""))
    elif error is not None:
        parts.append(_text(error))
    for node_id in sorted(node_errors, key=lambda key: _node_key(str(key))):
        entry = node_errors[node_id]
        entry = entry if isinstance(entry, Mapping) else {}
        label = f"node {node_id}" + (f" ({entry['class_type']})" if entry.get("class_type") else "")
        items = entry.get("errors") if isinstance(entry.get("errors"), list) else []
        texts = []
        for item in items:
            item = item if isinstance(item, Mapping) else {"message": item}
            message, details = _text(item.get("message")), _text(item.get("details"))
            texts.append(message + (f": {details}" if details and details != message else ""))
        parts.append(f"{label}: {'; '.join(texts) or 'rejected'}")
    return "; ".join(parts) or "no reason given"


# ---- /object_info: COMFY_AUTOGROW_V3 ----


def _template_spec(template: Mapping[str, Any]) -> Any:
    """The spec of the one input an Autogrow template repeats (its "input" mapping holds it)."""
    inner = template.get("input")
    if isinstance(inner, Mapping):
        for section in ("required", "optional"):
            entries = inner.get(section)
            if isinstance(entries, Mapping) and entries:
                return next(iter(entries.values()))
    return None


def _count(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def autogrow_inputs(group: str, spec: Any) -> tuple[int, list[tuple[str, Any]]] | None:
    """For an input spec of type COMFY_AUTOGROW_V3: (min, [(name, spec), ...]) of the inputs it grows,
    named <group>.<prefix><i> for i from 0 below max (a prefix template) or <group>.<name> for each of its
    names, as ComfyUI's Autogrow names them, each with the template input's spec. None for any other spec."""
    if not (isinstance(spec, Sequence) and not isinstance(spec, str) and spec and spec[0] == AUTOGROW):
        return None
    options = spec[1] if len(spec) > 1 and isinstance(spec[1], Mapping) else {}
    template = options.get("template")
    if not isinstance(template, Mapping):
        return 0, []
    inner = _template_spec(template)
    names = template.get("names")
    if isinstance(names, list):
        grown = [str(name) for name in names]
    else:
        prefix = str(template.get("prefix", ""))
        grown = [f"{prefix}{index}" for index in range(_count(template.get("max"), AUTOGROW_MAX))]
    return _count(template.get("min"), AUTOGROW_MIN), [(f"{group}.{name}", inner) for name in grown]


def expand_autogrow(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """An /object_info `input` mapping with each COMFY_AUTOGROW_V3 input replaced by the inputs it grows
    (autogrow_inputs). The first `min` stay in the group's own section and the rest become optional,
    matching what ComfyUI requires of a prompt. Other inputs are kept as they are."""
    expanded: dict[str, Any] = {}
    for section, entries in inputs.items():
        if not isinstance(entries, Mapping):
            expanded[section] = entries
            continue
        target = expanded.setdefault(section, {})
        for name, spec in entries.items():
            grown = autogrow_inputs(str(name), spec)
            if grown is None:
                target[name] = spec
                continue
            minimum, names = grown
            for index, (grown_name, grown_spec) in enumerate(names):
                expanded.setdefault(section if index < minimum else "optional", {})[grown_name] = grown_spec
    return expanded


# ---- answers ----


@dataclass(frozen=True)
class Uploaded:
    """Where POST /upload/image stored a file."""

    name: str
    subfolder: str
    type: str

    @property
    def reference(self) -> str:
        """The value a LoadImage (or LoadAudio) input takes: <subfolder>/<name>."""
        return f"{self.subfolder}/{self.name}" if self.subfolder else self.name


@dataclass(frozen=True)
class Submitted:
    prompt_id: str
    number: int | None  # the queue number ComfyUI gave it


@dataclass(frozen=True)
class Queue:
    running: tuple[str, ...]  # queue_running[i][1]
    pending: tuple[str, ...]  # queue_pending[i][1]

    def __contains__(self, prompt_id: object) -> bool:
        return prompt_id in self.running or prompt_id in self.pending


@dataclass(frozen=True)
class Output:
    """One file in outputs[node].images or outputs[node].gifs."""

    node: str
    kind: str  # "images" or "gifs"
    filename: str
    subfolder: str
    type: str  # "output" or "temp"


@dataclass(frozen=True)
class History:
    """GET /history/<id> for one prompt."""

    prompt_id: str
    status: str | None  # status.status_str: "success" or "error"
    completed: bool
    events: Mapping[str, int | None]  # the EVENTS messages present -> their timestamp (ms since the epoch)
    error: str | None  # execution_error.exception_message
    error_node: str | None  # execution_error.node_id
    outputs: tuple[Output, ...]

    @property
    def outcome(self) -> str | None:
        """INTERRUPTED, ERROR or SUCCESS; None while the record shows neither."""
        if "execution_interrupted" in self.events:
            return INTERRUPTED
        if "execution_error" in self.events or self.status == ERROR:
            return ERROR
        if self.status == SUCCESS:
            return SUCCESS
        return None


def _timestamp(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_history(prompt_id: str, entry: Any, url: str) -> History:
    """One /history entry as a History; a BadAnswer naming url when it is not a mapping."""
    if not isinstance(entry, Mapping):
        raise BadAnswer(f"{SERVICE} at {url}: the history of {prompt_id} is not a JSON object", url)
    status = entry.get("status") if isinstance(entry.get("status"), Mapping) else {}
    events: dict[str, int | None] = {}
    error = error_node = None
    for message in status.get("messages") or ():
        if not (isinstance(message, Sequence) and not isinstance(message, str) and len(message) >= 2):
            continue
        kind, data = message[0], message[1] if isinstance(message[1], Mapping) else {}
        if kind in EVENTS:
            events[kind] = _timestamp(data.get("timestamp"))
        if kind == "execution_error":
            error = _text(data.get("exception_message")) or None
            error_node = str(data["node_id"]) if data.get("node_id") is not None else None
    outputs = []
    found = entry.get("outputs") if isinstance(entry.get("outputs"), Mapping) else {}
    for node in sorted(found, key=lambda key: _node_key(str(key))):
        produced = found[node] if isinstance(found[node], Mapping) else {}
        for kind in OUTPUT_KINDS:
            for item in produced.get(kind) or ():
                if isinstance(item, Mapping) and isinstance(item.get("filename"), str):
                    subfolder, where = item.get("subfolder"), item.get("type")
                    outputs.append(Output(str(node), kind, item["filename"], str(subfolder or ""), str(where or "")))
    status_str = status.get("status_str")
    return History(
        prompt_id=prompt_id,
        status=status_str if isinstance(status_str, str) else None,
        completed=status.get("completed") is True,
        events=events,
        error=error,
        error_node=error_node,
        outputs=tuple(outputs),
    )


def is_prompt_id(value: object) -> bool:
    """A UUID in canonical lowercase form, the only client prompt_id ComfyUI takes."""
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def multipart(fields: Sequence[tuple[str, str]], files: Sequence[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    """A multipart/form-data body, files (name, filename, data, content type) first, then the text fields;
    and its Content-Type header."""
    boundary = f"influencer-{uuid.uuid4().hex}"
    chunks = []
    for name, filename, data, content_type in files:
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        )
        chunks.append(head.encode("utf-8") + data + b"\r\n")
    for name, value in fields:
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
        chunks.append(head.encode("utf-8") + value.encode("utf-8") + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


class ComfyClient:
    """The §C6.1 calls (see the module docstring). url is COMFYUI_URL, checked here; loopback replaces the
    LoopbackClient (the tests' resolver and connector); client_id is sent with every prompt."""

    def __init__(self, url: str = env.DEFAULT_COMFYUI_URL, *, timing: Timing | None = None,
                 loopback: LoopbackClient | None = None, client_id: str | None = None) -> None:  # fmt: skip
        self.loopback = loopback or LoopbackClient(url, SERVICE, setting=SETTING)
        self.timing = timing or DEFAULT_TIMING
        self.client_id = client_id or str(uuid.uuid4())

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, timing: Timing | None = None) -> ComfyClient:
        """The client for COMFYUI_URL (default http://127.0.0.1:18188), checked before any request."""
        return cls(env.paths(environ).comfyui_url, timing=timing)

    def _get(self, path: str, timeout: float) -> Any:
        return self.loopback.get_json(path, timeout=timeout)

    def system_stats(self) -> dict[str, Any]:
        """The /system_stats answer, which must be a JSON object with system.comfyui_version (Req 9.1);
        anything else is an IdentityError naming the URL."""
        response = self.loopback.request("GET", "/system_stats", timeout=self.timing.request_s)
        hint = f"check that {SETTING} points at ComfyUI"
        try:
            data = response.expect().json()
        except BadAnswer as error:
            raise IdentityError(f"{error}, so it is not the ComfyUI_API: {hint}", response.url) from None
        system = data.get("system") if isinstance(data, dict) else None
        version = system.get("comfyui_version") if isinstance(system, dict) else None
        if not isinstance(version, str) or not version.strip():
            raise IdentityError(
                f"{response.where} answered without system.comfyui_version, so it is not the ComfyUI_API: {hint}",
                response.url,
            )
        return data

    def version(self) -> str:
        """system.comfyui_version, after the identity check."""
        return str(self.system_stats()["system"]["comfyui_version"])

    def object_info(self) -> dict[str, Any]:
        """Every node class's /object_info entry, with its `input` sections put through expand_autogrow
        (input_order is left as ComfyUI sent it)."""
        data = self._get("/object_info", self.timing.object_info_s)
        if not isinstance(data, dict):
            raise BadAnswer(f"{SERVICE} at {self.loopback.url}/object_info: not a JSON object", self.loopback.url)
        info = {}
        for name, entry in data.items():
            if isinstance(entry, dict) and isinstance(entry.get("input"), Mapping):
                entry = {**entry, "input": expand_autogrow(entry["input"])}
            info[name] = entry
        return info

    def models(self, folder: str) -> tuple[str, ...]:
        """The files ComfyUI lists for one model folder key (diffusion_models, loras, vosr2, ...). A folder
        key ComfyUI does not know is a StatusError saying so."""
        path = "/models/" + urllib.parse.quote(folder, safe="")
        response = self.loopback.request("GET", path, timeout=self.timing.request_s)
        if response.status == 404:
            message = f"{response.where}: ComfyUI has no model folder {folder!r} (HTTP 404)"
            raise StatusError(message, response.url, response.status, response.body)
        data = response.expect().json()
        if not isinstance(data, list) or not all(isinstance(name, str) for name in data):
            raise BadAnswer(f"{response.where}: not a JSON list of file names", response.url)
        return tuple(data)

    def upload(self, name: str, data: bytes, *, subfolder: str = UPLOAD_SUBFOLDER) -> Uploaded:
        """POST /upload/image: the file (an image or audio, content-addressed by its name) into
        input/<subfolder>/, replacing a file of that name (overwrite=true)."""
        if not name or any(char in _NAME_FORBIDDEN for char in name):
            raise ValueError(f"an upload name holds no /, \\, \", line break or NUL, and is not empty: {name!r}")
        suffix = name[name.rfind(".") :].lower() if "." in name else ""
        files = [("image", name, bytes(data), _CONTENT_TYPES.get(suffix, "application/octet-stream"))]
        fields = [("subfolder", subfolder), ("type", "input"), ("overwrite", "true")]
        body, content_type = multipart(fields, files)
        response = self.loopback.request(
            "POST", "/upload/image", body=body, content_type=content_type, timeout=self.timing.upload_s
        )
        answer = response.expect().json()
        if not isinstance(answer, dict) or not isinstance(answer.get("name"), str):
            raise BadAnswer(f"{response.where}: the answer names no stored file", response.url)
        return Uploaded(answer["name"], str(answer.get("subfolder") or ""), str(answer.get("type") or "input"))

    def submit(self, graph: Mapping[str, Any], prompt_id: str | None = None) -> Submitted:
        """POST /prompt with our prompt_id (a new UUID4 when None) and client_id. A 400 carrying node_errors
        or an error is Rejected (Req 10.6); an answer naming another prompt_id is a BadAnswer, because the
        Job would then be tracked under the wrong id."""
        prompt_id = str(uuid.uuid4()) if prompt_id is None else prompt_id
        if not is_prompt_id(prompt_id):
            raise ValueError(f"a prompt_id is a lowercase UUID, not {prompt_id!r}")
        body = {"prompt": dict(graph), "prompt_id": prompt_id, "client_id": self.client_id}
        response = self.loopback.post_json("/prompt", body, timeout=self.timing.submit_s)
        if response.status == 400:
            rejected = self._rejected(response)
            if rejected is not None:
                raise rejected
        answer = response.expect().json()
        if not isinstance(answer, dict):
            raise BadAnswer(f"{response.where}: the answer is not a JSON object", response.url)
        if answer.get("prompt_id") != prompt_id:
            raise BadAnswer(
                f"{response.where} answered prompt_id {answer.get('prompt_id')!r} for our {prompt_id}; this "
                "ComfyUI ignores client prompt ids, so update it",
                response.url,
            )
        number = answer.get("number")
        return Submitted(prompt_id, number if isinstance(number, int) and not isinstance(number, bool) else None)

    @staticmethod
    def _rejected(response: Response) -> Rejected | None:
        try:
            answer = response.json()
        except BadAnswer:
            return None
        if not isinstance(answer, dict) or not ("node_errors" in answer or "error" in answer):
            return None
        node_errors = answer.get("node_errors")
        return Rejected(response.url, answer.get("error"), node_errors if isinstance(node_errors, Mapping) else {})

    def queue(self) -> Queue:
        """The running and pending prompt ids."""
        data = self._get("/queue", self.timing.request_s)
        url = f"{self.loopback.url}/queue"

        def ids(key: str) -> tuple[str, ...]:
            entries = data.get(key) if isinstance(data, dict) else None
            if not isinstance(entries, list) or not all(isinstance(e, list) and len(e) > 1 for e in entries):
                raise BadAnswer(f"{SERVICE} at {url}: {key} is not a list of queue entries", url)
            return tuple(str(entry[1]) for entry in entries)

        return Queue(ids("queue_running"), ids("queue_pending"))

    def history(self, prompt_id: str) -> History | None:
        """The prompt's history, or None when ComfyUI has none: still queued, or forgotten (a restart)."""
        path = "/history/" + urllib.parse.quote(prompt_id, safe="")
        data = self._get(path, self.timing.request_s)
        url = self.loopback.url + path
        if not isinstance(data, dict):
            raise BadAnswer(f"{SERVICE} at {url}: not a JSON object", url)
        if prompt_id not in data:
            return None
        return parse_history(prompt_id, data[prompt_id], url)

    def interrupt(self, prompt_id: str | None) -> None:
        """POST /interrupt for one prompt (Req 10.7). Older servers ignore the body and interrupt whatever
        runs, which is safe because only our Job is running."""
        body = {} if prompt_id is None else {"prompt_id": prompt_id}
        self.loopback.post_json("/interrupt", body, timeout=self.timing.request_s).expect()

    def free(self) -> None:
        """POST /free: unload the models and free their memory (before training, Req 19.6)."""
        body = {"unload_models": True, "free_memory": True}
        self.loopback.post_json("/free", body, timeout=self.timing.request_s).expect()
