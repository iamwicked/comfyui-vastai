"""The Disk_Estimator for Catalog ids: `influencer.sh disk --id X [--id Y ...] [--brief]` (design §C10, §C5;
Req 23.1, 23.2, 23.3, 23.5, 23.6, 23.8).

- Items are the distinct models.list install paths and Ollama models of the combined Selection of the ids
  (lines.select, the Python mirror of provision.sh), so an item several ids or lines install counts once.
- Present means the Provisioner would skip the item: a model file that exists and is non-empty, a workflow
  whose `.provisioned-<name>` marker exists, an Ollama model that `GET /api/tags` on the loopback Ollama
  lists. With no Ollama answer every Ollama item counts as still to download. Without the workspace folder
  (the Mac) every item counts as still to download and the free space reads "not measured".
- Sizes of the other items come from their sources, through one ExternalClient (so the tests hand in a
  recorded transport): HF paths-info batched per repository, Civitai's model-version API, a HEAD for a URL,
  the local file for `bundled`, and the Ollama registry manifest. At most WORKERS requests run at a time,
  each with the ExternalClient's 10 s limit. A refused, unanswered or size-less item is unknown: listed,
  counted, and left out of every figure (Req 23.2).
- Figures are bytes / 10^9 rounded to one decimal; the total is summed before rounding (Req 23.1).
- An id the Catalog does not list exits 1 before any request, naming it and listing the valid ids and base
  models (Req 23.8). The base-model estimate (`--base`) arrives with the Training_Kit in Phase 2.

Tokens go only as headers to their own service (the ExternalClient's rule) and every URL this module
prints is shown_url()'s, without its query string.

Standard library only, so it runs in the ComfyUI_Env before any node requirement is installed (§C5).
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from influencer import env
from influencer.errors import EnvironmentFailure, Exit, Problem, ValidationError
from influencer.http import ExternalClient, HttpError, LoopbackClient, Transport, shown_url
from influencer.lines import (
    CATALOG,
    CONFIG_FILES,
    WORKFLOW_KIND,
    ModelLine,
    install_path,
    kind_folder,
    parse_catalog,
    select,
)
from influencer.timing import DEFAULT_TIMING, Timing

BASE_MODELS = ("krea/Krea-2-Raw", "MiniMaxAI/MiniMax-H3")  # Req 23.7; estimated from Phase 2 on
GB = 10**9
WORKERS = 8
NOT_MEASURED = "not measured"

HF_PATHS_INFO = "https://huggingface.co/api/models/{repo}/paths-info/main"
CIVITAI_VERSION = "https://civitai.com/api/v1/model-versions/{vid}"
OLLAMA_MANIFEST = "https://registry.ollama.ai/v2/{name}/manifests/{tag}"
MANIFEST_ACCEPT = "application/vnd.docker.distribution.manifest.v2+json"
CIVITAI_MAX_BYTES = 8 * 1024 * 1024  # a model version's JSON lists its images too

MODEL, WORKFLOW, OLLAMA = "model", "workflow", "ollama"
PRESENT, DOWNLOAD, UNKNOWN = "present", "download", "unknown"


# ---- items (26.1) ----


@dataclass(frozen=True)
class Item:
    """One distinct thing the combined Selection installs."""

    key: str  # identity: the install path, or "ollama:<name:tag>"
    label: str  # how it is listed: the install path, or "ollama <model>"
    kind: str  # MODEL, WORKFLOW or OLLAMA
    source: str  # hf, civitai, url, bundled (the line's own) or ollama
    source_id: str
    filename: str  # the models.list filename, or the Ollama model as written
    line: ModelLine | None = None


def ollama_key(model: str) -> str:
    """An Ollama model as Ollama names it: a name without a tag is <name>:latest."""
    tail = model.rsplit("/", 1)[-1]
    return model if ":" in tail else f"{model}:latest"


def items(files: Mapping[str, str | None], ids: Sequence[str]) -> list[Item]:
    """The distinct install paths and Ollama models of the ids' combined Selection: models.list items first,
    each where it first appears (ids in the given order, lines in file order), then the Ollama models."""
    models: dict[str, Item] = {}
    ollama: dict[str, Item] = {}
    for ident in dict.fromkeys(ids):
        selection = select(files, ident)
        for line in selection.models:
            path = install_path(line)
            if path not in models:
                kind = WORKFLOW if line.kind == WORKFLOW_KIND else MODEL
                models[path] = Item(path, path, kind, line.source, line.source_id, line.filename, line)
        for record in selection.ollama:
            key = f"ollama:{ollama_key(record.model)}"
            if key not in ollama:
                ollama[key] = Item(key, f"ollama {record.model}", OLLAMA, OLLAMA, record.model, record.model)
    return [*models.values(), *ollama.values()]


@dataclass(frozen=True)
class Layout:
    """Where the Provisioner installs on this Instance (provision.sh's MODELS_DIR and WORKFLOWS_DIR)."""

    models_dir: Path
    workflows_dir: Path


def layout_of(paths: env.Paths, environ: Mapping[str, str]) -> Layout | None:
    """The Instance layout, or None when the workspace folder does not exist (Req 23.5)."""
    if not paths.on_instance:
        return None
    user_dir = environ.get("USER_DIR") or str(paths.workspace / "comfyui-user")
    return Layout(paths.models_dir, Path(user_dir).expanduser().absolute() / "default" / "workflows")


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def present_size(item: Item, layout: Layout | None, installed: Mapping[str, int] | None) -> int | None:
    """The installed size of an item the Provisioner would skip, else None. A present workflow counts the
    size of its file (0 for an archive, which the Provisioner extracts and removes)."""
    if layout is None:
        return None
    if item.kind == OLLAMA:
        if installed is None:
            return None
        return installed.get(ollama_key(item.filename))
    line = item.line
    assert line is not None
    if item.kind == WORKFLOW:
        name = line.filename[:-4] if line.filename.endswith(".zip") else line.filename
        if not (layout.workflows_dir / f".provisioned-{name}").is_file():
            return None
        return _file_size(layout.workflows_dir / line.filename) or 0
    size = _file_size(layout.models_dir / (line.subdir or kind_folder(line.kind)) / line.filename)
    return size if size else None  # provision.sh's `[ -s "$dest" ]`


def ollama_installed(client: LoopbackClient, timing: Timing) -> dict[str, int] | None:
    """<name:tag> -> size of each model `GET /api/tags` lists; None when Ollama gives no usable answer."""
    try:
        data = client.get_json("/api/tags", timeout=timing.request_s)
    except HttpError:
        return None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None
    found: dict[str, int] = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        size = model.get("size")
        size = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else 0
        for name in {model.get("name"), model.get("model")}:
            if isinstance(name, str) and name:
                found[ollama_key(name)] = size
    return found


# ---- sizes (26.2) ----

Sized = tuple[int | None, str]  # (bytes, "") or (None, why the size is unknown)


def _size(value: Any) -> int | None:
    """A byte count from an answer: a non-negative int (a float such as sizeKB * 1024 rounded)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value < 0:
        return None
    return round(value)


def _failure(error: Exception) -> str:
    return str(error) if isinstance(error, HttpError) else f"{type(error).__name__}: {error}"


def hf_repo_path(item: Item) -> tuple[str, str]:
    """(repository, path in it) of an hf line: `repo::path`, or the filename when the path is left out."""
    repo, separator, path = item.source_id.partition("::")
    return repo, (path if separator else item.filename)


def _hf(client: ExternalClient, repo: str, group: Sequence[Item]) -> dict[str, Sized]:
    """One paths-info request for every path of one repository."""
    wanted = {item.key: hf_repo_path(item)[1] for item in group}
    url = HF_PATHS_INFO.format(repo=urllib.parse.quote(repo, safe="/"))
    body = urllib.parse.urlencode([("paths", path) for path in dict.fromkeys(wanted.values())]).encode("ascii")
    try:
        data = client.post(url, body, "application/x-www-form-urlencoded").expect().json()
    except (HttpError, ValueError) as error:
        return {key: (None, f"hf {repo}: {_failure(error)}") for key in wanted}
    sizes: dict[str, int] = {}
    for entry in data if isinstance(data, list) else ():
        if isinstance(entry, dict) and isinstance(entry.get("path"), str) and entry.get("type", "file") == "file":
            lfs = entry.get("lfs")
            size = _size(lfs.get("size")) if isinstance(lfs, dict) else None
            size = _size(entry.get("size")) if size is None else size
            if size is not None:
                sizes[entry["path"]] = size
    return {
        key: (sizes[path], "") if path in sizes else (None, f"hf {repo}: no size for {path} in the file listing")
        for key, path in wanted.items()
    }


def _civitai(client: ExternalClient, vid: str, group: Sequence[Item]) -> dict[str, Sized]:
    """One model-version request: the primary file's sizeKB x 1024, or the archive's for a ::member line."""
    where = f"civitai version {vid}"
    if not vid.isdigit():
        return {item.key: (None, f"{where}: not a version id") for item in group}
    try:
        data = client.get_json(CIVITAI_VERSION.format(vid=vid), max_bytes=CIVITAI_MAX_BYTES)
    except (HttpError, ValueError) as error:
        return {item.key: (None, f"{where}: {_failure(error)}") for item in group}
    files = [f for f in (data.get("files") if isinstance(data, dict) else None) or () if isinstance(f, dict)]
    primary = next((f for f in files if f.get("primary") is True), files[0] if files else None)
    archive = next((f for f in files if f.get("type") == "Archive" or str(f.get("name", "")).lower().endswith(".zip")),
                   primary)  # fmt: skip
    result: dict[str, Sized] = {}
    for item in group:
        chosen = archive if "::" in item.source_id else primary
        kb = chosen.get("sizeKB") if chosen is not None else None
        size = _size(kb * 1024) if _size(kb) is not None else None
        result[item.key] = (size, "") if size is not None else (None, f"{where}: no file size in the answer")
    return result


def _url(client: ExternalClient, item: Item) -> dict[str, Sized]:
    """HEAD, following redirects, and the final answer's Content-Length."""
    where = f"url {shown_url(item.source_id)}"
    try:
        response = client.head(item.source_id).expect()
    except (HttpError, ValueError) as error:
        return {item.key: (None, f"{where}: {_failure(error)}")}
    length = response.headers.get("content-length", "").strip()
    if not length.isdigit():
        return {item.key: (None, f"{where}: no Content-Length in the answer")}
    return {item.key: (int(length), "")}


def ollama_manifest_url(model: str) -> str | None:
    """The registry manifest of a library model (`name[:tag]`) or a namespaced one (`user/name[:tag]`);
    None for a model from another registry (a first segment with a dot, such as hf.co/...)."""
    name, tag = ollama_key(model).rsplit(":", 1)
    segments = name.split("/")
    if not name or len(segments) > 2 or "" in segments or (len(segments) == 2 and "." in segments[0]):
        return None
    path = name if len(segments) == 2 else f"library/{name}"
    return OLLAMA_MANIFEST.format(name=urllib.parse.quote(path, safe="/"), tag=urllib.parse.quote(tag, safe=""))


def _ollama(client: ExternalClient, item: Item) -> dict[str, Sized]:
    """The summed layer sizes of the model's registry manifest."""
    where = f"ollama {item.filename}"
    url = ollama_manifest_url(item.filename)
    if url is None:
        return {item.key: (None, f"{where}: not a model of registry.ollama.ai")}
    try:
        data = client.get_json(url, headers={"Accept": MANIFEST_ACCEPT})
    except (HttpError, ValueError) as error:
        return {item.key: (None, f"{where}: {_failure(error)}")}
    layers = data.get("layers") if isinstance(data, dict) else None
    sizes = [_size(layer.get("size")) if isinstance(layer, dict) else None for layer in layers or ()]
    if not sizes or None in sizes:
        return {item.key: (None, f"{where}: no layer sizes in the manifest")}
    return {item.key: (sum(s for s in sizes if s is not None), "")}


def _bundled(config_dir: Path, item: Item) -> Sized:
    size = _file_size(config_dir / item.source_id)
    return (size, "") if size is not None else (None, f"bundled {item.source_id}: no such file in the config folder")


def fetch_sizes(wanted: Sequence[Item], client: ExternalClient, config_dir: Path, workers: int = WORKERS) -> dict[str, Sized]:
    """The source size of each item, WORKERS requests at a time; an item no source answers for is unknown."""
    sizes: dict[str, Sized] = {}
    tasks: list[Callable[[], dict[str, Sized]]] = []
    hf: dict[str, list[Item]] = {}
    civitai: dict[str, list[Item]] = {}
    for item in wanted:
        if item.source == "hf":
            hf.setdefault(hf_repo_path(item)[0], []).append(item)
        elif item.source == "civitai":
            civitai.setdefault(item.source_id.partition("::")[0], []).append(item)
        elif item.source == "url":
            tasks.append(lambda item=item: _url(client, item))
        elif item.source == OLLAMA:
            tasks.append(lambda item=item: _ollama(client, item))
        elif item.source == "bundled":
            sizes[item.key] = _bundled(config_dir, item)
        else:
            sizes[item.key] = (None, f"unknown source {item.source!r}")
    tasks += [lambda repo=repo, group=group: _hf(client, repo, group) for repo, group in hf.items()]
    tasks += [lambda vid=vid, group=group: _civitai(client, vid, group) for vid, group in civitai.items()]
    if tasks:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            for result in pool.map(lambda task: task(), tasks):
                sizes.update(result)
    return sizes


def url_hosts(wanted: Iterable[Item]) -> set[str]:
    """The hosts of the url items: where the ExternalClient may start besides SOURCE_HOSTS (Req 25.8)."""
    hosts = set()
    for item in wanted:
        if item.source == "url":
            try:
                host = urllib.parse.urlsplit(item.source_id).hostname
            except ValueError:
                host = None
            if host:
                hosts.add(host)
    return hosts


# ---- the estimate and its figures (26.3) ----


@dataclass(frozen=True)
class Row:
    item: Item
    state: str  # PRESENT, DOWNLOAD or UNKNOWN
    size: int | None  # bytes; None when UNKNOWN
    reason: str = ""


def gb(size: int) -> float:
    """bytes in GB of 10^9 bytes, rounded to one decimal."""
    return round(size / GB, 1)


@dataclass(frozen=True)
class Estimate:
    ids: tuple[str, ...]
    rows: tuple[Row, ...]
    workspace: Path
    free: int | None  # bytes free on the workspace; None: not measured

    def _sum(self, state: str) -> int:
        return sum(row.size or 0 for row in self.rows if row.state == state)

    @property
    def present(self) -> int:
        return self._sum(PRESENT)

    @property
    def download(self) -> int:
        return self._sum(DOWNLOAD)

    @property
    def total(self) -> int:
        return self.present + self.download

    @property
    def unknown(self) -> tuple[Row, ...]:
        return tuple(row for row in self.rows if row.state == UNKNOWN)

    @property
    def short(self) -> bool:
        """The size still to download, as printed, exceeds the measured free space as printed (Req 23.6), so
        the WARNING line never states two equal figures."""
        return self.free is not None and gb(self.download) > gb(self.free)

    def free_text(self) -> str:
        return NOT_MEASURED if self.free is None else f"{gb(self.free):.1f} GB"

    def brief(self) -> str:
        """The line the install hook shows (§C5)."""
        return (f"to download: {gb(self.download):.1f} GB ({len(self.unknown)} unknown), already present: "
                f"{gb(self.present):.1f} GB, free on {self.workspace}: {self.free_text()}")  # fmt: skip

    def warning(self) -> str | None:
        if not self.short:
            return None
        return (f"WARNING: the size still to download, {gb(self.download):.1f} GB, exceeds the free space on "
                f"{self.workspace}, {self.free_text()}")  # fmt: skip

    def report(self) -> str:
        """The full report: one row per item, then the four figures and the number of unknowns."""
        lines = [f"disk estimate for {', '.join(self.ids)}: {len(self.rows)} items"]
        for row in self.rows:
            size = f"{gb(row.size):7.1f} GB" if row.size is not None else " " * 10
            reason = f" ({row.reason})" if row.reason else ""
            lines.append(f"  {row.state:<8} {size}  {row.item.label}{reason}")
        lines += [
            f"size already present: {gb(self.present):.1f} GB",
            f"size still to download: {gb(self.download):.1f} GB",
            f"total download size: {gb(self.total):.1f} GB",
            f"free space on {self.workspace}: {self.free_text()}",
            f"unknown sizes: {len(self.unknown)} (left out of every figure)",
        ]
        return "\n".join(lines)


def estimate(files: Mapping[str, str | None], ids: Sequence[str], *, client: ExternalClient, config_dir: Path,
             workspace: Path, layout: Layout | None = None, installed: Mapping[str, int] | None = None,
             free: int | None = None, workers: int = WORKERS) -> Estimate:  # fmt: skip
    """The estimate over the ids' items: present ones at their installed size, the rest at their source size
    or unknown. layout None (no workspace folder) makes every item still to download."""
    found = items(files, ids)
    present = {item.key: present_size(item, layout, installed) for item in found}
    sizes = fetch_sizes([item for item in found if present[item.key] is None], client, config_dir, workers)
    rows = []
    for item in found:
        if present[item.key] is not None:
            rows.append(Row(item, PRESENT, present[item.key]))
            continue
        size, reason = sizes.get(item.key, (None, "no source answered"))
        rows.append(Row(item, DOWNLOAD, size) if size is not None else Row(item, UNKNOWN, None, reason))
    return Estimate(tuple(dict.fromkeys(ids)), tuple(rows), workspace, free if layout is not None else None)


# ---- the command (26.3, 26.4) ----


def read_config(config_dir: Path) -> dict[str, str | None]:
    """Each config file's text, None when it is missing; decoded as lines.py expects."""
    files: dict[str, str | None] = {}
    for name in CONFIG_FILES:
        try:
            files[name] = (config_dir / name).read_bytes().decode("utf-8", "surrogateescape")
        except FileNotFoundError:
            files[name] = None
    return files


def _valid_values(catalog_ids: Sequence[str]) -> str:
    return f"valid ids: {', '.join(catalog_ids) or 'none'}; base models: {', '.join(BASE_MODELS)}"


def check_ids(files: Mapping[str, str | None], ids: Sequence[str], config_dir: Path) -> None:
    """ValidationError naming each id the Catalog does not list, with the valid ids and base models."""
    text = files.get(CATALOG)
    if text is None:
        raise ValidationError([Problem(str(config_dir / CATALOG), None, None, "missing, so no id can be estimated")])
    known = [line.id for line in parse_catalog(text)]
    unknown = [ident for ident in dict.fromkeys(ids) if ident not in known]
    if unknown:
        raise ValidationError(
            Problem("command line", "--id", None, f"{ident!r} is not a Catalog id; {_valid_values(known)}")
            for ident in unknown
        )


def _free_space(workspace: Path) -> int | None:
    try:
        stats = os.statvfs(workspace)
    except OSError:
        return None
    return stats.f_bavail * stats.f_frsize


def run_disk(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None, transport: Transport | None = None,
             timing: Timing | None = None, out: TextIO | None = None) -> int:  # fmt: skip
    """disk --id X [--id Y ...] [--brief]. Exit 0 with the estimate, unknown sizes included; 1 for an unknown
    id or base model, before any request; 2 for a valid base model, whose estimate is Phase 2's."""
    environ = os.environ if environ is None else environ
    timing = timing or DEFAULT_TIMING
    out = out or sys.stdout
    paths = env.paths(environ)
    if args.base is not None:
        if args.base not in BASE_MODELS:
            files = read_config(paths.config_dir)
            known = [line.id for line in parse_catalog(files.get(CATALOG) or "")]
            raise ValidationError([Problem("command line", "--base", None,
                                           f"{args.base!r} is not a base model; {_valid_values(known)}")])  # fmt: skip
        raise EnvironmentFailure(f"--base {args.base}: the base-model estimate arrives with the Training_Kit (Phase 2)")
    files = read_config(paths.config_dir)
    check_ids(files, args.ids, paths.config_dir)
    layout = layout_of(paths, environ)
    installed = None
    if layout is not None:
        try:
            ollama = LoopbackClient(paths.ollama_url, "Ollama", setting="OLLAMA_URL")
        except HttpError:
            ollama = None
        installed = ollama_installed(ollama, timing) if ollama is not None else None
    wanted = items(files, args.ids)
    client = ExternalClient(extra_hosts=url_hosts(wanted), environ=environ, transport=transport, timing=timing)
    result = estimate(files, args.ids, client=client, config_dir=paths.config_dir, workspace=paths.workspace,
                      layout=layout, installed=installed,
                      free=_free_space(paths.workspace) if layout is not None else None)  # fmt: skip
    print(result.brief() if args.brief else result.report(), file=out)
    warning = result.warning()
    if warning is not None:
        print(warning, file=out)
    return Exit.OK
