"""Preflight: the checks a stage runs before it submits any Job, and their one report (design §A13 steps
1-4, §C6.1, §C6.2; Req 9.1-9.5, 9.8, 6.15).

The input is a PreflightInput, which the stage builds from the Jobs it would submit or hold as waiting
(task 20 builds it from its Jobs; nothing here knows a Job class):

- prompts: one FilledPrompt per Job, the filled prompt dict (templates.fill) with its Template
- files: model files beyond what the prompts and templates name, per folder key (ModelFile), such as a
  Character_LoRA that a template takes as a plain file value
- ollama_models: each Ollama model the run needs, with the capabilities it must list ("vision" for
  captioning, none for planning)
- output_root: the Output_Root whose file system must have 20 GB free
- config: the texts of catalog.txt, models.list, nodes.txt and ollama-models.txt (read_config), which
  name the Catalog ids whose Selection installs a missing item

run(request, comfy=..., ollama=..., timing=..., started=...) runs every check and returns a Report; it
never raises for a failed check. The checks, in order:

1. ComfyUI identity: GET /system_stats (Req 9.1). Refused, an error answer, an answer that is not a
   ComfyUI answer, and no answer in time are four named failures, each with the URL tried. When it fails,
   the class and model checks are skipped, since the server's lists would mean nothing.
2. Classes and inputs: GET /object_info, then every node class and input name of every filled prompt
   (the filler's LoRA loaders included) against the reported definitions, COMFY_AUTOGROW_V3 groups
   expanded by comfy.py (Req 9.2).
3. Model files: GET /models/<folder> once per folder: the templates' models (under their `value` when they
   have one) and extra_files, the LoRA names in the filled prompts, and request.files (Req 9.2).
4. Ollama: GET /api/tags, then POST /api/show per installed model for its capabilities (Req 6.15). The
   capabilities stay in the OllamaClient's cache, so its chats send think: false only to a model that
   lists thinking.
5. Free space: shutil.disk_usage(Output_Root) at least 20 GB (Req 9.4).

Every missing item is named once (Req 9.3): a node class or input name with its node pack and the Catalog
ids whose Selection installs the pack, a core class with "ComfyUI core" and the update command, and a model
file with its folder key and the Catalog ids whose Selection installs it; an item no Catalog id installs
says so.

Time (Req 9.8): one deadline, Timing.preflight_deadline_s (55 s) from `started`, the stage start on the
time.monotonic() clock. Each call's time limit is the smaller of its own and the time left; a call with
no time left is not made and is reported as a failure of its own, so the report always arrives in time.

Nothing here writes a file: the Manifest is neither created nor changed. The caller prints
Report.text() and exits 2 (Exit.ENVIRONMENT) when Report.ok is False, or raises Report.error() (a
PreflightFailed, an EnvironmentFailure, which __main__ turns into exit status 2). A dry run calls run() the
same way and prints the report before it exits (Req 9.5, 9.9).
"""
from __future__ import annotations

import copy
import dataclasses
import shutil
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from influencer import lines as L
from influencer.checks.template_rules import repo_key, repo_name
from influencer.comfy import ComfyClient, IdentityError
from influencer.errors import EnvironmentFailure, ValidationError
from influencer.http import HttpError, StatusError, TimedOut, Unreachable
from influencer.ollama import OllamaClient, model_key
from influencer.templates import Template
from influencer.timing import DEFAULT_TIMING, Timing

MIN_FREE_BYTES = 20 * 1000**3  # 20 GB (Req 9.4), in decimal gigabytes as df -H and the Runbook show them
GB = 1000**3
CORE = "core"  # the node_packs value of a ComfyUI core class
CORE_NOTE = "ComfyUI core; update ComfyUI with UPDATE_COMFYUI=1 stock-setup.sh init"
NO_CATALOG_ID = "no Catalog id's Selection installs it"
INPUT_SECTIONS = ("required", "optional")  # the /object_info sections a prompt's inputs may name

# A loader input whose value is a file of one model folder, for the LoRAs a filled prompt names: the
# filler's LoRA loaders and a template's own loader node alike (finish's Character_LoRA).
FILE_INPUTS: Mapping[tuple[str, str], str] = {
    ("LoraLoaderModelOnly", "lora_name"): "loras",
    ("LoraLoader", "lora_name"): "loras",
}

# Check names, the `check` of a Failure.
COMFYUI, NODES, MODELS, OLLAMA, DISK = "comfyui", "nodes", "models", "ollama", "disk"

# Failure kinds.
REFUSED = "refused"  # the connection was refused (or the host did not resolve)
ERROR_ANSWER = "error"  # an HTTP error status
NOT_SERVICE = "not-service"  # an answer that is not the service's (no comfyui_version, not JSON, a 404)
TIMEOUT = "timeout"  # no answer in time
DEADLINE = "deadline"  # the preflight deadline left no time for the call
MISSING_CLASS = "missing-class"
MISSING_INPUT = "missing-input"
MISSING_FILE = "missing-file"
MISSING_MODEL = "missing-model"
MISSING_CAPABILITY = "missing-capability"
LOW_SPACE = "low-space"

_KIND_WORDS = {
    REFUSED: "connection refused",
    ERROR_ANSWER: "error answer",
    NOT_SERVICE: "not a {service} answer",
    TIMEOUT: "no answer in time",
}


# ---- the input ----


@dataclass(frozen=True)
class FilledPrompt:
    """One Job's filled prompt: what templates.fill made of template, and a label (a Job id) for messages."""

    template: Template
    prompt: Mapping[str, Any]
    label: str = ""


@dataclass(frozen=True)
class ModelFile:
    """A file ComfyUI must list in /models/<folder> under name. install_path is where a models.list line
    installs it (lines.install_path), for the Catalog ids; default models/<folder>/<name>."""

    folder: str
    name: str
    install_path: str | None = None
    derived: bool = False  # made from a downloaded file after the download (an extra_files entry)

    @property
    def path(self) -> str:
        return self.install_path or f"models/{self.folder}/{self.name}"


@dataclass(frozen=True)
class PreflightInput:
    """What one stage's preflight checks (see the module docstring)."""

    prompts: Sequence[FilledPrompt]
    output_root: Path
    files: Sequence[ModelFile] = ()
    ollama_models: Mapping[str, Collection[str]] = field(default_factory=dict)
    config: Mapping[str, str | None] = field(default_factory=dict)


def read_config(config_dir: Path) -> dict[str, str | None]:
    """The texts of catalog.txt and the Config_Lists in config_dir, None for a missing file."""
    texts: dict[str, str | None] = {}
    for name in L.CONFIG_FILES:
        try:
            texts[name] = (Path(config_dir) / name).read_bytes().decode("utf-8", "surrogateescape")
        except FileNotFoundError:
            texts[name] = None
    return texts


def required_files(request: PreflightInput) -> list[tuple[ModelFile, tuple[str, ...]]]:
    """Every model file the request names, each (folder, name) once, in first-named order, with the
    templates that name it: each template's models (looked up under `value` when set) and extra_files, the
    FILE_INPUTS values of each filled prompt, then request.files."""
    found: dict[tuple[str, str], tuple[ModelFile, list[str]]] = {}

    def add(item: ModelFile, user: str | None) -> None:
        key = (item.folder, item.name)
        entry = found.setdefault(key, (item, []))
        if user and user not in entry[1]:
            entry[1].append(user)

    for filled in request.prompts:
        template = filled.template
        for model in template.meta.get("models") or ():
            path = f"models/{L.kind_folder(model['kind'])}/{model['name']}"
            add(ModelFile(model["folder"], model.get("value") or model["name"], path), template.name)
        for extra in template.meta.get("extra_files") or ():
            add(ModelFile(extra["folder"], extra["name"], derived=extra.get("derived") is True), template.name)
        for node in filled.prompt.values():
            inputs = node.get("inputs") if isinstance(node, Mapping) else None
            for name, value in (inputs or {}).items():
                folder = FILE_INPUTS.get((node.get("class_type"), name))
                if folder is not None and isinstance(value, str) and value:
                    add(ModelFile(folder, value), template.name)
    for item in request.files:
        add(item, None)
    return [(item, tuple(users)) for item, users in found.values()]


# ---- the report ----


@dataclass(frozen=True)
class Failure:
    """One failed check or missing item. message is the report line; url the URL tried, when there is one."""

    check: str  # COMFYUI, NODES, MODELS, OLLAMA or DISK
    kind: str  # one of the failure kinds above
    item: str  # what failed: a URL path, a class, "Class.input", "folder/name", a model, a path
    message: str
    url: str | None = None


@dataclass(frozen=True)
class Report:
    """Every failure of one preflight run, in check order; ok when there is none."""

    failures: tuple[Failure, ...]
    comfyui_version: str | None = None
    elapsed_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.failures

    def text(self) -> str:
        """The report: a summary line, then one line per failure."""
        if self.ok:
            version = f" (ComfyUI {self.comfyui_version})" if self.comfyui_version else ""
            return f"preflight: every check passed{version}"
        count = len(self.failures)
        head = f"preflight: {count} failed check{'s' if count != 1 else ''} or missing item{'s' if count != 1 else ''}"
        head += "; no Job was submitted"
        return "\n".join([head, *(f"- {failure.message}" for failure in self.failures)])

    def error(self) -> PreflightFailed:
        return PreflightFailed(self)

    def raise_if_failed(self) -> None:
        if not self.ok:
            raise PreflightFailed(self)


class PreflightFailed(EnvironmentFailure):
    """A preflight with failures (exit status 2); its message is the whole report."""

    def __init__(self, report: Report) -> None:
        super().__init__(report.text())
        self.report = report


# ---- Catalog ids ----


class _Catalog:
    """Which Catalog ids' Selections install a node pack or a model file."""

    def __init__(self, config: Mapping[str, str | None]) -> None:
        self.note: str | None = None
        self.selections: list[L.Selection] = []
        if config.get(L.CATALOG) is None:
            self.note = f"{L.CATALOG} was not found, so no Catalog id is known"
            return
        for line in L.parse_catalog(config[L.CATALOG] or ""):
            try:
                self.selections.append(L.select(config, line.id))
            except ValidationError:  # an id provision.sh would refuse installs nothing
                continue

    def pack_ids(self, pack: str) -> tuple[str, ...]:
        key = repo_key(pack)
        return tuple(
            selection.run
            for selection in self.selections
            if any(repo_key(repo_name(line.url)) == key for line in selection.nodes)
        )

    def file_ids(self, path: str) -> tuple[str, ...]:
        return tuple(
            selection.run
            for selection in self.selections
            if any(L.install_path(line) == path for line in selection.models)
        )

    def installed_by(self, ids: Sequence[str]) -> str:
        if ids:
            noun = "id" if len(ids) == 1 else "ids"
            return f"installed by the Selection of Catalog {noun} {', '.join(ids)}"
        return NO_CATALOG_ID + (f" ({self.note})" if self.note else "")


# ---- the deadline ----


class _Deadline:
    def __init__(self, started: float, total: float, clock: Callable[[], float]) -> None:
        self.end = started + total
        self.total = total
        self.clock = clock

    def left(self) -> float:
        return self.end - self.clock()

    def limit(self, own: float) -> float | None:
        """min(own, the time left), or None when no time is left."""
        left = self.left()
        return None if left <= 0 else min(own, left)


def _with_limits(client: Any, timing: Timing, **limits: float) -> Any:
    """A shallow copy of client whose Timing has these limits; it shares the connection settings and
    the capability cache."""
    view = copy.copy(client)
    view.timing = dataclasses.replace(timing, **limits)
    return view


def _kind(error: HttpError) -> str:
    """The Req 9.1 failure kind of a failed request."""
    if isinstance(error, TimedOut):
        return TIMEOUT
    if isinstance(error, Unreachable):
        return REFUSED
    if isinstance(error, IdentityError):
        cause = error.__context__  # comfy.py raises IdentityError from the StatusError it caught
        if isinstance(cause, StatusError) and cause.status != 404:
            return ERROR_ANSWER
        return NOT_SERVICE
    if isinstance(error, StatusError):
        return NOT_SERVICE if error.status == 404 else ERROR_ANSWER
    return NOT_SERVICE  # BadAnswer, Refused (a redirect) and the like


def _call_failure(check: str, service: str, url: str, error: HttpError) -> Failure:
    kind = _kind(error)
    words = _KIND_WORDS[kind].format(service=service)
    return Failure(check, kind, url, f"{service} at {url}: {words}: {error}", url)


def _deadline_failure(check: str, service: str, url: str, total: float) -> Failure:
    message = f"{service} at {url}: not requested, the preflight deadline of {total:g} s from the stage start had passed"
    return Failure(check, DEADLINE, url, message, url)


# ---- the checks ----


class _Run:
    def __init__(self, request: PreflightInput, timing: Timing, deadline: _Deadline) -> None:
        self.request = request
        self.timing = timing
        self.deadline = deadline
        self.failures: list[Failure] = []
        self.version: str | None = None
        self._catalog: _Catalog | None = None

    @property
    def catalog(self) -> _Catalog:
        if self._catalog is None:
            self._catalog = _Catalog(self.request.config)
        return self._catalog

    def call(self, check: str, service: str, url: str, own: float, attribute: str, client: Any,
             action: Callable[[Any], Any]) -> tuple[bool, Any]:  # fmt: skip
        """(True, the answer) or (False, None) with the failure recorded."""
        limit = self.deadline.limit(own)
        if limit is None:
            self.failures.append(_deadline_failure(check, service, url, self.deadline.total))
            return False, None
        try:
            return True, action(_with_limits(client, self.timing, **{attribute: limit}))
        except HttpError as error:
            self.failures.append(_call_failure(check, service, url, error))
            return False, None

    # 1. identity (Req 9.1)
    def identity(self, comfy: ComfyClient) -> bool:
        url = f"{comfy.loopback.url}/system_stats"
        ok, stats = self.call(COMFYUI, "ComfyUI", url, self.timing.request_s, "request_s", comfy,
                              lambda client: client.system_stats())  # fmt: skip
        if ok:
            self.version = str(stats["system"]["comfyui_version"])
        return ok

    # 2. classes and inputs (Req 9.2, 9.3)
    def nodes(self, comfy: ComfyClient) -> None:
        if not self.request.prompts:
            return
        url = f"{comfy.loopback.url}/object_info"
        ok, info = self.call(NODES, "ComfyUI", url, self.timing.object_info_s, "object_info_s", comfy,
                             lambda client: client.object_info())  # fmt: skip
        if not ok:
            return
        packs = self._packs()
        missing: dict[tuple[str, ...], list[str]] = {}  # (class,) or (class, input) -> templates
        for filled in self.request.prompts:
            for node in filled.prompt.values():
                if not isinstance(node, Mapping) or not isinstance(node.get("class_type"), str):
                    continue
                class_type = node["class_type"]
                entry = info.get(class_type)
                if not isinstance(entry, Mapping):
                    _note(missing, (class_type,), filled.template.name)
                    continue
                known = _input_names(entry)
                for name in node.get("inputs") or {}:
                    if name not in known:
                        _note(missing, (class_type, name), filled.template.name)
        for key, users in missing.items():
            self.failures.append(self._missing_node(key, users, packs, url))

    def _packs(self) -> dict[str, str]:
        """Node class -> node pack, from the node_packs of the request's templates (first one wins)."""
        packs: dict[str, str] = {}
        for filled in self.request.prompts:
            for class_type, pack in (filled.template.meta.get("node_packs") or {}).items():
                packs.setdefault(class_type, pack)
        return packs

    def _missing_node(self, key: tuple[str, ...], users: list[str], packs: Mapping[str, str], url: str) -> Failure:
        class_type = key[0]
        pack = packs.get(class_type)
        if pack == CORE:
            source = CORE_NOTE
        elif pack is None:
            source = f"node pack unknown; {NO_CATALOG_ID}"
        else:
            source = f"node pack {pack}, {self.catalog.installed_by(self.catalog.pack_ids(pack))}"
        used = f" (used by {', '.join(users)})" if users else ""
        if len(key) == 1:
            what, kind, item = f"node class {class_type}", MISSING_CLASS, class_type
        else:
            what, kind, item = f"input {key[1]} of node class {class_type}", MISSING_INPUT, f"{class_type}.{key[1]}"
        return Failure(NODES, kind, item, f"{what}{used}: not reported by {url}; {source}", url)

    # 3. model files (Req 9.2, 9.3)
    def models(self, comfy: ComfyClient) -> None:
        files = required_files(self.request)
        folders = list(dict.fromkeys(item.folder for item, _ in files))
        listed: dict[str, set[str] | None] = {}
        for folder in folders:
            url = f"{comfy.loopback.url}/models/{folder}"
            limit = self.deadline.limit(self.timing.request_s)
            if limit is None:
                self.failures.append(_deadline_failure(MODELS, "ComfyUI", url, self.deadline.total))
                continue
            try:
                names = _with_limits(comfy, self.timing, request_s=limit).models(folder)
            except StatusError as error:
                if error.status == 404:  # ComfyUI knows no such folder key: its files are all missing
                    listed[folder] = set()
                    continue
                self.failures.append(_call_failure(MODELS, "ComfyUI", url, error))
                continue
            except HttpError as error:
                self.failures.append(_call_failure(MODELS, "ComfyUI", url, error))
                continue
            listed[folder] = {name.replace("\\", "/") for name in names}
        for item, users in files:
            names = listed.get(item.folder)
            if names is None or item.name in names:
                continue  # the folder's request failed and is reported, or the file is there
            url = f"{comfy.loopback.url}/models/{item.folder}"
            if item.derived:
                source = "made from the downloaded files when the template's Catalog id is installed"
                ids = self.catalog.file_ids(item.path)
                source += f" ({self.catalog.installed_by(ids)})" if ids else ""
            else:
                source = self.catalog.installed_by(self.catalog.file_ids(item.path))
            used = f" (used by {', '.join(users)})" if users else ""
            message = f"model file {item.name} in folder {item.folder}{used}: not listed by {url}; {source}"
            self.failures.append(Failure(MODELS, MISSING_FILE, f"{item.folder}/{item.name}", message, url))

    # 4. Ollama (Req 6.15)
    def ollama(self, ollama: OllamaClient) -> None:
        wanted = self.request.ollama_models
        if not wanted:
            return
        url = f"{ollama.url}/api/tags"
        ok, installed = self.call(OLLAMA, "Ollama", url, self.timing.request_s, "request_s", ollama,
                                  lambda client: client.tags())  # fmt: skip
        if not ok:
            return
        keys = {model_key(name) for name in installed}
        for model, capabilities in wanted.items():
            if model_key(model) not in keys:
                message = f"Ollama at {ollama.url}: the model {model} is not installed; install it with ollama pull {model}"
                self.failures.append(Failure(OLLAMA, MISSING_MODEL, model, message, ollama.url))
                continue
            needed = [capability for capability in dict.fromkeys(capabilities)]
            show = f"{ollama.url}/api/show"
            ok, found = self.call(OLLAMA, "Ollama", show, self.timing.request_s, "request_s", ollama,
                                  lambda client, model=model: client.capabilities(model))  # fmt: skip
            if not ok:
                continue
            for capability in needed:
                if capability not in found:
                    listed = ", ".join(found) or "none listed"
                    message = (f"Ollama at {ollama.url}: the model {model} lacks the {capability} capability "
                               f"(its capabilities: {listed}); choose a model that lists {capability}")  # fmt: skip
                    self.failures.append(Failure(OLLAMA, MISSING_CAPABILITY, f"{model}:{capability}", message, show))

    # 5. free space (Req 9.4)
    def disk(self) -> None:
        path = _existing(Path(self.request.output_root))
        try:
            free = shutil.disk_usage(path).free
        except OSError as error:
            message = f"free space of {path}: cannot be read ({error.strerror or error})"
            self.failures.append(Failure(DISK, LOW_SPACE, str(path), message))
            return
        if free < MIN_FREE_BYTES:
            shown = (free * 10 // GB) / 10  # rounded down, so a value just below 20 GB never reads 20.0
            message = (f"free space of {path}: {shown:.1f} GB ({free} bytes), below the "
                       f"{MIN_FREE_BYTES // GB} GB minimum")  # fmt: skip
            self.failures.append(Failure(DISK, LOW_SPACE, str(path), message))


def _note(missing: dict[tuple[str, ...], list[str]], key: tuple[str, ...], user: str) -> None:
    users = missing.setdefault(key, [])
    if user not in users:
        users.append(user)


def _input_names(entry: Mapping[str, Any]) -> set[str]:
    """The input names an /object_info entry (Autogrow groups expanded) takes in a prompt."""
    names: set[str] = set()
    inputs = entry.get("input") if isinstance(entry.get("input"), Mapping) else {}
    for section in INPUT_SECTIONS:
        if isinstance(inputs.get(section), Mapping):
            names.update(str(name) for name in inputs[section])
    return names


def _existing(path: Path) -> Path:
    """path, or its nearest existing parent: the file system that will hold it."""
    path = path.absolute()
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return path


def run(request: PreflightInput, *, comfy: ComfyClient | None, ollama: OllamaClient | None = None,
        timing: Timing | None = None, started: float | None = None,
        clock: Callable[[], float] = time.monotonic) -> Report:  # fmt: skip
    """Every preflight check of the module docstring, within one deadline from started (default: now).
    comfy is None only for a stage that submits nothing to ComfyUI (the plan stage's Ollama checks);
    ollama is needed only when request.ollama_models names a model."""
    timing = timing or DEFAULT_TIMING
    started = clock() if started is None else started
    deadline = _Deadline(started, timing.preflight_deadline_s, clock)
    checks = _Run(request, timing, deadline)
    if comfy is not None and checks.identity(comfy):
        checks.nodes(comfy)
        checks.models(comfy)
    if request.ollama_models:
        if ollama is None:
            raise ValueError("request.ollama_models names models, so run() needs an OllamaClient")
        checks.ollama(ollama)
    checks.disk()
    return Report(tuple(checks.failures), checks.version, clock() - started)
