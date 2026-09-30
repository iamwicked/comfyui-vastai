"""Tests of the Disk_Estimator, scripts/influencer/checks/disk.py (task 26; design §C10, §C5; Req 23.1, 23.2,
23.3, 23.5, 23.6, 23.8).

- Property 45: over config_lists() and a fake size map, each distinct install path or Ollama model is
  listed once, unknown items are listed and left out of every figure, and the figures add up
- each size source through a recorded transport, the three kinds of unknown, the present split in a temp
  WORKSPACE, the Mac's "not measured", --brief and its WARNING line, and the unknown-id exit

Every external request goes through an injected transport, so no test reaches the network.
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import sys
import tempfile
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.harness import REPO, SHORT_TIMING
from strategies import ConfigLists, config_lists, install_path

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import __main__ as cli  # noqa: E402
from influencer.checks import disk as D  # noqa: E402
from influencer.errors import Exit, ValidationError  # noqa: E402
from influencer.http import ExternalClient, Incoming, Outgoing  # noqa: E402
from influencer.lines import CATALOG, MODELS, NODES, OLLAMA  # noqa: E402

WORKFLOWS_FOLDER = "comfyui-user/default/workflows"  # provision.sh's WORKFLOWS_DIR below the workspace

HF_CANARY = "hf_DiskCanary0123456789abcdefXYZ"
CIVITAI_CANARY = "DiskCanaryCivitai0123456789abcd"

Answer = Callable[[Outgoing], Incoming]


def recorder(answer: Answer) -> tuple[list[Outgoing], Callable[[Outgoing], Incoming]]:
    """A transport that records every exchange and answers it with answer(outgoing)."""
    sent: list[Outgoing] = []

    def transport(outgoing: Outgoing) -> Incoming:
        sent.append(outgoing)
        return answer(outgoing)

    return sent, transport


def json_answer(data: object, status: int = 200) -> Incoming:
    return Incoming(status, {"content-type": "application/json"}, json.dumps(data).encode())


def files(models: str = "", ollama: str = "", catalog: str = "stills|yes||stills\nother|no||other\n") -> dict[str, str]:
    return {CATALOG: catalog, MODELS: models, NODES: "", OLLAMA: ollama}


def run(config: Mapping[str, str], ids: list[str], transport, *, workspace: Path | None = None,
        environ: Mapping[str, str] | None = None, free: int | None = None, layout: D.Layout | None = None,
        installed: Mapping[str, int] | None = None) -> D.Estimate:  # fmt: skip
    """estimate() with an ExternalClient over transport and the shortened Timing."""
    wanted = D.items(config, ids)
    client = ExternalClient(extra_hosts=D.url_hosts(wanted), environ=environ or {}, transport=transport,
                            timing=SHORT_TIMING)  # fmt: skip
    return D.estimate(config, ids, client=client, config_dir=Path("/nonexistent-config"),
                      workspace=workspace or Path("/workspace"), layout=layout, installed=installed, free=free)


# ---- Property 45 ----

REFUSED, TIMEOUT, MISSING = "refused", "timeout", "missing"
UNKNOWNS = (REFUSED, TIMEOUT, MISSING)
SIZES = st.integers(0, 3 * 10**8).map(lambda kb: kb * 1024)  # whole KB, so Civitai's sizeKB is exact


def _tag_ids(field: str) -> list[str]:
    return field[3:].replace(" ", "").split(",") if field.startswith("wf:") else []


def expected_items(config: ConfigLists, ids: list[str]) -> dict[str, tuple[str, ...]]:
    """The oracle, from the generated lines: item key -> the model line's fields (() for Ollama)."""
    found: dict[str, tuple[str, ...]] = {}
    for _, fields in config.active(MODELS):
        kind, filename, _source, _source_id, subdir, tag = fields
        if set(_tag_ids(tag)) & set(ids):
            path = install_path(kind, filename, subdir).replace("<workflows>", WORKFLOWS_FOLDER, 1)
            found.setdefault(path, fields)
    for _, fields in config.active(OLLAMA):
        if len(fields) == 2 and set(_tag_ids(fields[1])) & set(ids):
            found.setdefault(f"ollama:{D.ollama_key(fields[0])}", ())
    return found


def request_key(key: str, fields: tuple[str, ...]) -> str:
    """Which request answers the item: an hf path of a repo, a Civitai version, a URL, an Ollama model."""
    if not fields:
        return key
    source, source_id, filename = fields[2], fields[3], fields[1]
    if source == "hf":
        repo, sep, path = source_id.partition("::")
        return f"hf:{repo}:{path if sep else filename}"
    if source == "civitai":
        return f"civitai:{source_id.partition('::')[0]}"
    return f"url:{source_id}"


def fake_sources(outcomes: Mapping[str, object]) -> Answer:
    """Answers each request from outcomes (request key -> bytes or one of UNKNOWNS; absent: MISSING). An hf
    batch is refused or times out when any of its paths does."""

    def outcome(key: str) -> object:
        return outcomes.get(key, MISSING)

    def fail(what: object) -> Incoming | None:
        if what == TIMEOUT:
            raise TimeoutError("timed out")
        return Incoming(403, {}, b"forbidden") if what == REFUSED else None

    def answer(outgoing: Outgoing) -> Incoming:
        parts = urllib.parse.urlsplit(outgoing.url)
        if outgoing.method == "POST":  # hf paths-info
            repo = urllib.parse.unquote(parts.path[len("/api/models/"):-len("/paths-info/main")])
            paths = urllib.parse.parse_qs(outgoing.body.decode(), keep_blank_values=True)["paths"]
            what = [outcome(f"hf:{repo}:{path}") for path in paths]
            for bad in (REFUSED, TIMEOUT):
                if bad in what:
                    return fail(bad)  # type: ignore[return-value]
            listing = [{"type": "file", "path": p, "size": s} for p, s in zip(paths, what) if isinstance(s, int)]
            return json_answer(listing)
        if parts.hostname == "civitai.com":
            what = outcome(f"civitai:{parts.path.rsplit('/', 1)[1]}")
            failed = fail(what)
            if failed is not None:
                return failed
            file = {"name": "x.zip", "primary": True, "type": "Archive"}
            if isinstance(what, int):
                file["sizeKB"] = what / 1024
            return json_answer({"files": [file]})
        if parts.hostname == "registry.ollama.ai":
            name, tag = parts.path[len("/v2/"):].split("/manifests/")
            what = outcome(f"ollama:{name.removeprefix('library/')}:{tag}")
            failed = fail(what)
            if failed is not None:
                return failed
            if not isinstance(what, int):
                return json_answer({"layers": [{"digest": "sha256:1"}]})
            return json_answer({"layers": [{"size": what // 3}, {"size": what // 3}, {"size": what - 2 * (what // 3)}]})
        what = outcome(f"url:{outgoing.url}")  # a HEAD
        failed = fail(what)
        if failed is not None:
            return failed
        return Incoming(200, {"content-length": str(what)} if isinstance(what, int) else {})

    return answer


def _probe_keys(key: str, fields: tuple[str, ...]) -> tuple[str, ...]:
    """The file-system names presence depends on, case-folded, since macOS folders ignore case."""
    if not fields:
        return ()
    if fields[0] == "workflow":
        name = fields[1][:-4] if fields[1].endswith(".zip") else fields[1]
        return (key.casefold(), key.rsplit("/", 1)[0].casefold() + "/.provisioned-" + name.casefold())
    return (key.casefold(),)


@st.composite
def disk_cases(draw: st.DrawFn):
    config = draw(config_lists(max_lines=6))
    ids = draw(st.lists(st.sampled_from(config.ids), min_size=1, max_size=4))
    expected = expected_items(config, ids)
    keys = sorted({request_key(key, fields) for key, fields in expected.items()})
    outcomes = draw(st.dictionaries(st.sampled_from(keys), st.one_of(SIZES, st.sampled_from(UNKNOWNS)))) if keys else {}
    # presence: only items whose file names no other item shares ignoring case can be made present
    probes: dict[str, int] = {}
    for key, fields in expected.items():
        for probe in _probe_keys(key, fields):
            probes[probe] = probes.get(probe, 0) + 1
    unique = sorted(key for key, fields in expected.items()
                    if fields and all(probes[p] == 1 for p in _probe_keys(key, fields)))  # fmt: skip
    ollama = sorted(key for key, fields in expected.items() if not fields)
    present = {key: draw(st.integers(1, 64)) for key in draw(st.lists(st.sampled_from(unique), unique=True))} if unique else {}
    installed = {key: draw(st.integers(0, 10**10)) for key in draw(st.lists(st.sampled_from(ollama), unique=True))} if ollama else {}
    on_instance = draw(st.booleans())
    return config, ids, expected, outcomes, present, installed, on_instance


def _unknown_by_oracle(rkey: str, outcomes: Mapping[str, object]) -> bool:
    if outcomes.get(rkey, MISSING) in UNKNOWNS:
        return True
    if rkey.startswith("hf:"):  # a refused or timed-out batch makes every path of the repository unknown
        repo = rkey.split(":", 2)[1]
        return any(other.split(":", 2)[1] == repo and outcomes[other] in (REFUSED, TIMEOUT)
                   for other in outcomes if other.startswith("hf:"))  # fmt: skip
    return False


def _figure_ok(figure: float, size: int) -> bool:
    return abs(figure - size / 10**9) <= 0.05 + 1e-9 and abs(figure * 10 - round(figure * 10)) < 1e-6


# Feature: ai-influencer-pipeline, Property 45: Disk figures count each item once
@settings(deadline=None)
@given(case=disk_cases())
def test_disk_figures_count_each_item_once(case) -> None:
    """Validates: Requirements 23.1, 23.2"""
    config, ids, expected, outcomes, present, installed, on_instance = case
    sent, transport = recorder(fake_sources(outcomes))
    with tempfile.TemporaryDirectory() as folder:
        workspace = Path(folder)
        layout = None
        ollama_installed = None
        if on_instance:
            layout = D.Layout(workspace / "models", workspace / "comfyui-user" / "default" / "workflows")
            for key, size in present.items():
                target = workspace / key
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"x" * size)
                if expected[key][0] == "workflow":
                    name = key.rsplit("/", 1)[1]
                    (target.parent / f".provisioned-{name[:-4] if name.endswith('.zip') else name}").touch()
            ollama_installed = {key.removeprefix("ollama:"): size for key, size in installed.items()}
        result = run(config.files(), ids, transport, workspace=workspace, layout=layout, installed=ollama_installed,
                     free=10**12)  # fmt: skip

    keys = [row.item.key for row in result.rows]
    assert len(keys) == len(set(keys)) and set(keys) == set(expected)  # each distinct item once
    present_bytes = download_bytes = 0
    for row in result.rows:
        key = row.item.key
        if on_instance and key in present:
            assert (row.state, row.size) == (D.PRESENT, present[key]), row
            present_bytes += present[key]
        elif on_instance and key in installed:
            assert (row.state, row.size) == (D.PRESENT, installed[key]), row
            present_bytes += installed[key]
        elif _unknown_by_oracle(request_key(key, expected[key]), outcomes):
            assert (row.state, row.size) == (D.UNKNOWN, None) and row.reason, row
        else:
            assert (row.state, row.size) == (D.DOWNLOAD, outcomes[request_key(key, expected[key])]), row
            download_bytes += row.size or 0
    assert (result.present, result.download) == (present_bytes, download_bytes)
    assert result.total == present_bytes + download_bytes  # summed before rounding
    for size in (result.present, result.download, result.total):
        assert _figure_ok(D.gb(size), size)
    unknown = [row for row in result.rows if row.state == D.UNKNOWN]
    assert f"({len(unknown)} unknown)" in result.brief()
    report = result.report()
    assert f"total download size: {D.gb(result.total):.1f} GB" in report
    for row in unknown:
        assert row.item.label in report
    assert result.free_text() == ("1000.0 GB" if on_instance else "not measured")
    assert all(outgoing.timeout == SHORT_TIMING.external_request_s for outgoing in sent)


# ---- the size sources ----


def test_two_paths_of_one_hf_repository_go_in_one_request_with_the_token_header() -> None:
    models = ("diffusion|a.safetensors|hf|Org/Repo::diffusion_models/a.safetensors||wf:stills\n"
              "vae|b.safetensors|hf|Org/Repo||wf:stills\n")  # fmt: skip
    listing = [
        {"type": "file", "path": "diffusion_models/a.safetensors", "size": 135, "lfs": {"size": 20 * 10**9}},
        {"type": "file", "path": "b.safetensors", "size": 3 * 10**8},
    ]
    sent, transport = recorder(lambda outgoing: json_answer(listing))
    result = run(files(models), ["stills"], transport, environ={"HF_TOKEN": HF_CANARY})
    assert len(sent) == 1
    [request] = sent
    assert (request.method, request.url) == ("POST", "https://huggingface.co/api/models/Org/Repo/paths-info/main")
    assert urllib.parse.parse_qs(request.body.decode())["paths"] == ["diffusion_models/a.safetensors", "b.safetensors"]
    assert request.headers["Authorization"] == f"Bearer {HF_CANARY}"
    assert HF_CANARY not in request.url
    assert [(row.state, row.size) for row in result.rows] == [(D.DOWNLOAD, 20 * 10**9), (D.DOWNLOAD, 3 * 10**8)]


def test_a_civitai_member_line_takes_the_archive_size() -> None:
    models = ("lora|inner.safetensors|civitai|123::inner.safetensors||wf:stills\n"
              "lora|plain.safetensors|civitai|456||wf:stills\n")  # fmt: skip
    versions = {
        "123": {"files": [{"name": "inner-preview.safetensors", "primary": True, "sizeKB": 1000.0},
                          {"name": "bundle.zip", "type": "Archive", "sizeKB": 4000.5}]},
        "456": {"files": [{"name": "other.pt", "sizeKB": 1.0}, {"name": "plain.safetensors", "primary": True, "sizeKB": 2048}]},
    }  # fmt: skip
    sent, transport = recorder(lambda outgoing: json_answer(versions[outgoing.url.rsplit("/", 1)[1]]))
    result = run(files(models), ["stills"], transport, environ={"CIVITAI_TOKEN": CIVITAI_CANARY})
    assert sorted(outgoing.url for outgoing in sent) == [
        "https://civitai.com/api/v1/model-versions/123", "https://civitai.com/api/v1/model-versions/456"]
    assert all(outgoing.headers["Authorization"] == f"Bearer {CIVITAI_CANARY}" for outgoing in sent)
    assert [row.size for row in result.rows] == [round(4000.5 * 1024), 2048 * 1024]


def test_a_url_redirect_chain_ends_in_a_content_length() -> None:
    models = "upscale|up.pth|url|https://files.example/get/up.pth?sig=abc||wf:stills\n"
    chain = {
        "https://files.example/get/up.pth?sig=abc": Incoming(302, {"location": "https://cdn.example/blob/1?X-Sig=s"}),
        "https://cdn.example/blob/1?X-Sig=s": Incoming(200, {"content-length": "67108864"}),
    }
    sent, transport = recorder(lambda outgoing: chain[outgoing.url])
    result = run(files(models), ["stills"], transport, environ={"HF_TOKEN": HF_CANARY})
    assert [(outgoing.method, outgoing.url) for outgoing in sent] == [("HEAD", url) for url in chain]
    assert all("Authorization" not in outgoing.headers for outgoing in sent)  # no token to another host
    assert [(row.state, row.size) for row in result.rows] == [(D.DOWNLOAD, 67108864)]


def test_a_bundled_file_counts_its_local_size(tmp_path: Path) -> None:
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "still.json").write_bytes(b"{}" * 500)
    config = files("workflow|still.json|bundled|workflows/still.json||wf:stills\n")
    sent, transport = recorder(lambda outgoing: pytest.fail("a bundled file needs no request"))
    client = ExternalClient(environ={}, transport=transport, timing=SHORT_TIMING)
    result = D.estimate(config, ["stills"], client=client, config_dir=tmp_path, workspace=Path("/workspace"))
    assert sent == [] and [(row.state, row.size) for row in result.rows] == [(D.DOWNLOAD, 1000)]


def test_an_ollama_manifest_sums_its_three_layers() -> None:
    manifest = {"config": {"size": 487}, "layers": [{"size": 5 * 10**9}, {"size": 11338}, {"size": 1024}]}
    sent, transport = recorder(lambda outgoing: json_answer(manifest))
    result = run(files(ollama="qwen3:8b|wf:stills\nqwen3:8b|wf:stills,other\n"), ["stills", "other"], transport)
    assert [outgoing.url for outgoing in sent] == ["https://registry.ollama.ai/v2/library/qwen3/manifests/8b"]
    assert [(row.item.label, row.size) for row in result.rows] == [("ollama qwen3:8b", 5 * 10**9 + 11338 + 1024)]


# ---- unknown sizes ----

KNOWN = "vae|known.safetensors|hf|Org/Known::known.safetensors||wf:stills\n"


def _known_answer(outgoing: Outgoing) -> Incoming | None:
    if "/Org/Known/" in outgoing.url:
        return json_answer([{"type": "file", "path": "known.safetensors", "size": 7 * 10**9}])
    return None


def _timeout(outgoing: Outgoing) -> Incoming:
    raise TimeoutError("timed out")


@pytest.mark.parametrize(
    ("line", "answer", "reason"),
    [
        ("lora|r.safetensors|civitai|99||wf:stills\n", lambda o: Incoming(403, {}, b'{"error": "denied"}'), "HTTP 403"),
        ("lora|t.safetensors|url|https://slow.example/t.safetensors?token=zzz||wf:stills\n", _timeout, "no answer within "),
        ("lora|n.safetensors|hf|Org/Other::n.safetensors||wf:stills\n", lambda o: json_answer([{"path": "n.safetensors"}]),
         "no size for n.safetensors"),
    ],
    ids=["refused", "timeout", "no-size"],
)  # fmt: skip
def test_an_unknown_item_is_listed_and_leaves_the_figures_unchanged(line: str, answer: Answer, reason: str) -> None:
    def both(outgoing: Outgoing) -> Incoming:
        return _known_answer(outgoing) or answer(outgoing)

    _, alone = recorder(both)
    baseline = run(files(KNOWN), ["stills"], alone, free=10**12, layout=None)
    sent, transport = recorder(both)
    result = run(files(KNOWN + line), ["stills"], transport, free=10**12)
    [unknown] = result.unknown
    assert reason in unknown.reason and unknown.size is None
    assert (result.present, result.download, result.total) == (baseline.present, baseline.download, baseline.total)
    assert "(1 unknown)" in result.brief() and unknown.item.label in result.report()
    assert "?" not in result.report()  # reported URLs drop their query strings, token=zzz included
    assert all(outgoing.timeout == SHORT_TIMING.external_request_s for outgoing in sent)


# ---- presence, free space, --brief and the unknown id (the command) ----

INSTANCE_MODELS = (
    "diffusion|here.safetensors|hf|Org/Repo::here.safetensors||wf:stills\n"
    "vae|empty.safetensors|hf|Org/Repo::empty.safetensors||wf:stills\n"
    "lora|away.safetensors|hf|Org/Repo::away.safetensors|loras/looks|wf:stills\n"
    "workflow|flow.json|url|https://files.example/flow.json||wf:stills\n"
    "workflow|pack.zip|url|https://files.example/pack.zip||wf:stills\n"
)
LISTING = [{"type": "file", "path": name, "size": size}
           for name, size in (("here.safetensors", 1), ("empty.safetensors", 2 * 10**9), ("away.safetensors", 3 * 10**9))]  # fmt: skip


def _instance_answer(outgoing: Outgoing) -> Incoming:
    if outgoing.method == "POST":
        return json_answer(LISTING)
    return Incoming(200, {"content-length": str(10**6)})


def _config(folder: Path, models: str = INSTANCE_MODELS, ollama: str = "") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in files(models, ollama).items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def _args(*ids: str, brief: bool = False, base: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(ids=list(ids) or None, base=base, brief=brief, persona=None, home=None)


def _run_disk(args: argparse.Namespace, environ: Mapping[str, str], transport) -> str:
    out = io.StringIO()
    assert D.run_disk(args, environ=environ, transport=transport, timing=SHORT_TIMING, out=out) == Exit.OK
    return out.getvalue()


def test_a_workspace_with_some_files_present_splits_present_and_to_download(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    models = workspace / "models"
    (models / "diffusion_models").mkdir(parents=True)
    (models / "diffusion_models" / "here.safetensors").write_bytes(b"x" * 4000)
    (models / "vae").mkdir()
    (models / "vae" / "empty.safetensors").write_bytes(b"")  # empty: the Provisioner downloads it again
    flows = workspace / "comfyui-user" / "default" / "workflows"
    flows.mkdir(parents=True)
    (flows / "flow.json").write_bytes(b"{}" * 50)
    (flows / ".provisioned-flow.json").touch()
    (flows / "pack.zip").write_bytes(b"z" * 999)  # no marker: still to download
    monkeypatch.setattr(D, "_free_space", lambda path: 50 * 10**9)
    environ = {"WORKSPACE": str(workspace), "CONFIG_DIR": str(_config(tmp_path / "config")),
               "OLLAMA_URL": "http://127.0.0.1:1"}  # fmt: skip
    sent, transport = recorder(_instance_answer)
    report = _run_disk(_args("stills"), environ, transport)
    assert "size already present: 0.0 GB" in report and "size still to download: 5.0 GB" in report
    rows = {line.split()[-1]: line.split()[0] for line in report.splitlines() if line.startswith("  ")}
    assert rows == {
        "models/diffusion_models/here.safetensors": "present",
        "models/vae/empty.safetensors": "download",
        "models/loras/looks/away.safetensors": "download",
        "comfyui-user/default/workflows/flow.json": "present",
        "comfyui-user/default/workflows/pack.zip": "download",
    }
    assert f"free space on {workspace}: 50.0 GB" in report
    [batch] = [outgoing for outgoing in sent if outgoing.method == "POST"]
    assert urllib.parse.parse_qs(batch.body.decode())["paths"] == ["empty.safetensors", "away.safetensors"]
    assert [outgoing.url for outgoing in sent if outgoing.method == "HEAD"] == ["https://files.example/pack.zip"]


def test_an_ollama_model_that_api_tags_lists_is_present(tmp_path: Path, ollama_fake, monkeypatch) -> None:
    ollama_fake.add_model("qwen3:8b")
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(D, "_free_space", lambda path: 10**12)
    config = _config(tmp_path / "config", models="", ollama="qwen3:8b|wf:stills\ngemma3|wf:stills\n")
    environ = {"WORKSPACE": str(tmp_path / "workspace"), "CONFIG_DIR": str(config), "OLLAMA_URL": ollama_fake.url}
    sent, transport = recorder(lambda outgoing: json_answer({"layers": [{"size": 3 * 10**9}]}))
    report = _run_disk(_args("stills"), environ, transport)
    assert [outgoing.url for outgoing in sent] == ["https://registry.ollama.ai/v2/library/gemma3/manifests/latest"]
    assert "  present " in report and "ollama qwen3:8b" in report
    assert "size still to download: 3.0 GB" in report


def test_without_a_workspace_everything_is_to_download_and_free_space_is_not_measured(tmp_path: Path) -> None:
    environ = {"WORKSPACE": str(tmp_path / "no-workspace"), "CONFIG_DIR": str(_config(tmp_path / "config"))}
    sent, transport = recorder(_instance_answer)
    line = _run_disk(_args("stills", brief=True), environ, transport)
    assert line == (f"to download: 5.0 GB (0 unknown), already present: 0.0 GB, "
                    f"free on {tmp_path / 'no-workspace'}: not measured\n")  # fmt: skip
    assert len(sent) == 3  # one paths-info batch, two HEADs


@pytest.mark.parametrize(("free", "warned"), [(4 * 10**9, True), (4_940_000_000, True), (5 * 10**9, False), (6 * 10**9, False)])
def test_brief_prints_one_line_and_a_warning_exactly_when_short(tmp_path: Path, monkeypatch, free: int, warned: bool) -> None:
    (tmp_path / "workspace").mkdir()
    monkeypatch.setattr(D, "_free_space", lambda path: free)
    environ = {"WORKSPACE": str(tmp_path / "workspace"), "CONFIG_DIR": str(_config(tmp_path / "config")),
               "OLLAMA_URL": "http://127.0.0.1:1"}  # fmt: skip
    _, transport = recorder(_instance_answer)
    lines = _run_disk(_args("stills", brief=True), environ, transport).splitlines()
    assert lines[0].startswith("to download: 5.0 GB (0 unknown), already present: 0.0 GB, free on ")
    assert lines[0].endswith(f": {D.gb(free):.1f} GB")
    if warned:
        assert lines[1:] == [f"WARNING: the size still to download, 5.0 GB, exceeds the free space on "
                             f"{tmp_path / 'workspace'}, {D.gb(free):.1f} GB"]  # fmt: skip
    else:
        assert lines[1:] == []


def test_the_brief_line_has_the_install_hooks_wording() -> None:
    """The line the §C5 hook shows, as tests/phase1/test_provisioner_additions.py stubs it."""
    source = (Path(__file__).parent / "test_provisioner_additions.py").read_text(encoding="utf-8")
    [ESTIMATE] = [ast.literal_eval(node.value) for node in ast.parse(source).body
                  if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == ["ESTIMATE"]]  # fmt: skip
    row = D.Row(D.Item("k", "k", D.MODEL, "hf", "a/b", "f"), D.DOWNLOAD, 102_100_000_000)
    kept = D.Row(D.Item("p", "p", D.MODEL, "hf", "a/b", "g"), D.PRESENT, 5_600_000_000)
    assert D.Estimate(("influencer",), (row, kept), Path("/workspace"), 412_300_000_000).brief() == ESTIMATE


def test_an_unknown_id_exits_1_listing_the_valid_values_before_any_request(tmp_path: Path, capsys, monkeypatch) -> None:
    environ = {"WORKSPACE": str(tmp_path / "no-workspace"), "CONFIG_DIR": str(_config(tmp_path / "config"))}
    sent, transport = recorder(_instance_answer)
    with pytest.raises(ValidationError) as raised:
        D.run_disk(_args("stills", "nope"), environ=environ, transport=transport, timing=SHORT_TIMING, out=io.StringIO())
    assert sent == []
    [problem] = raised.value.problems
    assert "'nope' is not a Catalog id; valid ids: stills, other; base models: krea/Krea-2-Raw, MiniMaxAI/MiniMax-H3" in str(problem)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    assert cli.main(["disk", "--id", "nope"]) == Exit.USAGE
    out, err = capsys.readouterr()
    assert out == "" and "'nope' is not a Catalog id" in err and "GB" not in err
    assert cli.main(["disk", "--base", "other/Model"]) == Exit.USAGE
    assert "'other/Model' is not a base model; valid ids: stills, other" in capsys.readouterr().err
