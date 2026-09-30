"""Bash tests for the Provisioner additions of task 17 (design §C5).

Every run drives the repo's scripts (provision.sh, install-workflow.sh, common.sh) under /bin/bash with the
stub PATH of task 3.4 and the temp WORKSPACE, COMFYUI_DIR and CONFIG_DIR of task 3.1, so nothing is
downloaded, cloned, pulled or sent to an Instance. PYTHON is a stub too: it answers the Provisioner's
`-c sys.prefix` probe, the VOSR2 conversion, a node's install.py and the Disk_Estimator.

- Civitai early access (Req 2.7-2.9), the DeGrid collision guard (Req 3.4, 3.6), the Impact Pack SAM opt-out
  and the VOSR2 prefetch (Req 3.3), need_tool for influencer-video (Req 16.1, 3.6): task 17.7
- A second run downloads nothing (Req 3.7), and the disk-estimate hook (Req 23.3, 23.4): task 17.8
"""

from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

import pytest

from helpers.harness import REPO, TempEnv
from helpers.stubs import BASH, StubPath

pytestmark = pytest.mark.skipif(not os.access(BASH, os.X_OK), reason=f"{BASH} is missing; the scripts run under it")

PROVISION = "scripts/provision.sh"
INSTALL = "scripts/install-workflow.sh"
VID = "111222"  # the Civitai version id of the test line
CIVITAI_FILE = "civ.safetensors"
PTH = "dinov2_vitl14_pretrain.pth"
SAFETENSORS = "dinov2_vitl14.safetensors"
ESTIMATE = "to download: 102.1 GB (0 unknown), already present: 5.6 GB, free on /workspace: 412.3 GB"  # §C5

CATALOG = "influencer|yes||stills\ninfluencer-video|no||reels\nother|yes||other\n"
DEGRID = "https://github.com/lunaaispace-eng/ComfyUI-DeGrid||wf:influencer\n"
IMPACT = "https://github.com/ltdrdata/ComfyUI-Impact-Pack||wf:influencer\n"
VOSR2_NODE = "https://github.com/ylchen333/ComfyUI-VOSR2||wf:influencer\n"
CIVITAI_LINE = f"lora|{CIVITAI_FILE}|civitai|{VID}||wf:influencer\n"
VOSR2_LINE = f"vosr2|{PTH}|hf|CSWRY/VOSR::VOSR2/{PTH}|vosr2/VOSR2|wf:influencer\n"
OLLAMA_LINE = "qwen3:8b|wf:influencer\n"

# ---- the stubs' scripted behaviour ----

# curl: the Ollama health probe succeeds; a Civitai request with --range is the early-access probe, any other
# one the resumable download. Each writes its -o file and prints its HTTP code as -w '%{http_code}' would.
CURL_BODY = """\
__url="" __out="" __range=0 __prev=""
for __a in "$@"; do
  case "$__prev" in -o) __out="$__a" ;; esac
  case "$__a" in --range) __range=1 ;; http*) __url="$__a" ;; esac
  __prev="$__a"
done
case "$__url" in *127.0.0.1:11434*) exit 0 ;; esac
if [ "$__range" = 1 ]; then
  printf '%s' {probe_body} > "$__out"
  printf '%s' {probe_code}
else
  printf 'civitai-weights' >> "$__out"
  printf '%s' {download_code}
fi
"""
# hf download <repo> <path> --local-dir <stage>
HF_BODY = 'if [ "$1" = download ]; then mkdir -p "$(dirname "$5/$3")"; printf hf-weights > "$5/$3"; fi'
# git clone ... <dest> makes a checkout (Impact Pack gets an install.py); rev-parse prints a fixed commit.
GIT_BODY = """\
if [ "$1" = clone ]; then
  for __a in "$@"; do __last="$__a"; done
  mkdir -p "$__last/.git"
  case "$__last" in *Impact-Pack) : > "$__last/install.py" ;; esac
fi
if [ "$1" = -C ] && [ "$3" = rev-parse ]; then echo 0123456789abcdef0123456789abcdef01234567; fi
"""
OLLAMA_BODY = 'if [ "$1" = list ]; then printf "NAME ID SIZE MODIFIED\\nqwen3:8b abc 5.2GB now\\n"; fi'
APT_BODY = 'case " $* " in *" ffmpeg "*) status={ffmpeg_status} ;; esac'
PYTHON_BODY = """\
case "$1" in
  -c) case "$2" in *sys.prefix*) printf '%s\\n' {prefix} ;; esac ;;
  -) if [ "$#" -eq 3 ]; then printf converted > "$3"; fi ;;
  install.py)
    if [ -e "$COMFYUI_DIR/custom_nodes/skip_download_model" ]; then echo yes; else echo no; fi >> {marker_log} ;;
  -m) if [ "$2 $3" = "influencer disk" ]; then {disk}; fi ;;
esac
"""
# A timeout that passes its command through and records its budget (its first argument, in the call log).
# A command still running after 3 s gets SIGTERM, and the stub then exits 124 as timeout(1) does.
TIMEOUT_BODY = """\
shift
"$@" &
__pid=$!
( sleep 3; kill -TERM "$__pid" 2>/dev/null ) >/dev/null 2>&1 &
__watch=$!
if wait "$__pid"; then status=0; else status=$?; fi
kill "$__watch" 2>/dev/null || true
if [ "$status" -eq 143 ]; then status=124; fi
"""
DISK_OK = f"printf '%s\\n' {shlex.quote(ESTIMATE)}"
DISK_FAILS = "status=4"
DISK_HANGS = "exec sleep 30"


class Rig:
    """One temp env with its stub PATH, config lists and the environment of a run."""

    def __init__(self, stub_path: StubPath, temp_env: TempEnv, tmp_path: Path) -> None:
        self.stubs, self.env, self.tmp = stub_path, temp_env, tmp_path
        self.prefix = tmp_path / "venv-prefix"  # the stub Python's sys.prefix, where the dependency stamps go
        self.prefix.mkdir()
        self.marker_log = tmp_path / "marker-at-install.log"
        self.stubs.link("seq", "cksum")
        self.script_curl()
        self.stubs.stub("hf", body=HF_BODY)
        self.stubs.stub("git", body=GIT_BODY)
        self.stubs.stub("ollama", body=OLLAMA_BODY)
        self.script_apt()
        self.script_python()
        self.lists()

    # ---- scripting ----

    def script_curl(self, probe_code: str = "307", probe_body: str = "", download_code: str = "200") -> None:
        body = CURL_BODY.format(
            probe_code=shlex.quote(probe_code), probe_body=shlex.quote(probe_body),
            download_code=shlex.quote(download_code),
        )  # fmt: skip
        self.stubs.stub("curl", body=body, capture_stdin=True)

    def script_apt(self, ffmpeg_status: int = 0) -> None:
        self.stubs.stub("apt-get", body=APT_BODY.format(ffmpeg_status=ffmpeg_status))

    def script_python(self, disk: str = "status=1") -> None:
        body = PYTHON_BODY.format(
            prefix=shlex.quote(str(self.prefix)), marker_log=shlex.quote(str(self.marker_log)), disk=disk
        )
        self.stubs.stub("python", body=body)

    def lists(self, nodes: str = "", models: str = "", ollama: str = "") -> None:
        config = self.env.config_dir
        (config / "catalog.txt").write_text(CATALOG, encoding="utf-8")
        (config / "nodes.txt").write_text(nodes, encoding="utf-8")
        (config / "models.list").write_text(models, encoding="utf-8")
        (config / "ollama-models.txt").write_text(ollama, encoding="utf-8")

    # ---- paths ----

    @property
    def staging(self) -> Path:
        return self.env.workspace / ".staging" / "civitai"

    @property
    def models(self) -> Path:
        return self.env.workspace / "models"

    @property
    def custom_nodes(self) -> Path:
        return self.env.comfyui_dir / "custom_nodes"

    # ---- runs ----

    def variables(self, **extra: str) -> dict[str, str]:
        env = {"PYTHON": str(self.stubs.bin / "python"), "CIVITAI_TOKEN": "test-token", "HF_TOKEN": "test-hf"}
        env.update(extra)
        return env

    def provision(self, phases: str, workflow: str = "influencer") -> Result:
        result = self.stubs.run(PROVISION, extra_env=self.variables(WORKFLOW=workflow, PHASES=phases), timeout=90)
        return Result(result.returncode, result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace"))

    def install(self, workflow_id: str, script: str | Path = INSTALL) -> Result:
        result = self.stubs.run(script, workflow_id, extra_env=self.variables(), timeout=90)
        return Result(result.returncode, result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace"))

    def civitai_calls(self) -> list:
        return [call for call in self.stubs.calls("curl") if any("civitai.com" in arg for arg in call.args)]


class Result:
    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

    @property
    def output(self) -> str:
        return f"exit {self.returncode}\n--- stdout\n{self.stdout}\n--- stderr\n{self.stderr}"


@pytest.fixture
def rig(stub_path: StubPath, temp_env: TempEnv, tmp_path: Path) -> Rig:
    return Rig(stub_path, temp_env, tmp_path)


# ---- Civitai early access (Req 2.7, 2.8, 2.9) ----


def test_an_early_access_answer_keeps_no_file_and_fails_the_run(rig: Rig) -> None:
    rig.lists(models=CIVITAI_LINE)
    rig.script_curl(probe_code="403", probe_body='{"error":"This model is in Early Access until 2026-10-05"}')
    partial = rig.staging / f"{VID}.part"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"partial bytes from an earlier run")
    result = rig.provision("models")
    assert result.returncode != 0, result.output
    message = f"early access: {CIVITAI_FILE} (Civitai version {VID})"
    assert f"  - {message} — the version is in early access" in result.stderr, result.output  # the closing summary
    assert partial.read_bytes() == b"partial bytes from an earlier run"
    assert not (rig.models / "loras" / CIVITAI_FILE).exists()
    assert sorted(p.name for p in rig.staging.iterdir()) == [partial.name], "a file from the answer stayed"
    calls = rig.civitai_calls()
    assert len(calls) == 1, "the download ran after an early-access answer"
    probe = calls[0]
    assert "-L" not in probe.args and "0-65535" in probe.args, probe.args
    assert probe.stdin is not None and b"Authorization: Bearer test-token" in probe.stdin
    assert "test-token" not in " ".join(probe.args)


def test_a_206_probe_continues_into_the_download(rig: Rig) -> None:
    rig.lists(models=CIVITAI_LINE)
    rig.script_curl(probe_code="206", probe_body="first 64 KB of the file", download_code="206")
    result = rig.provision("models")
    assert result.returncode == 0, result.output
    assert (rig.models / "loras" / CIVITAI_FILE).read_bytes() == b"civitai-weights"
    assert len(rig.civitai_calls()) == 2
    assert not any(p.name.endswith(".probe") for p in rig.staging.iterdir())


def test_a_500_probe_keeps_todays_handling(rig: Rig) -> None:
    rig.lists(models=CIVITAI_LINE)
    rig.script_curl(probe_code="500", probe_body="server error", download_code="500")
    result = rig.provision("models")
    assert result.returncode == 3, result.output
    assert len(rig.civitai_calls()) == 2, "the download did not run after a 500 probe"
    assert f"Civitai returned HTTP 500 (curl exit 0) for version {VID}" in result.stderr, result.output
    assert f"Civitai download failed (version {VID})" in result.stderr, result.output
    assert "early access" not in result.stderr
    assert list(rig.staging.iterdir()) == []
    assert not (rig.models / "loras" / CIVITAI_FILE).exists()


# ---- the DeGrid collision guard (Req 3.4, 3.6) ----


def test_degrid_is_skipped_when_savesimple_exists_in_any_case(rig: Rig) -> None:
    rig.lists(nodes=DEGRID + VOSR2_NODE)
    clash = rig.custom_nodes / "comfyUI-saveSIMPLE"
    clash.mkdir(parents=True)
    result = rig.provision("nodes")
    assert result.returncode != 0, result.output
    assert f"  - skipped ComfyUI-DeGrid: node id VAEDeGrid collides with {clash}" in result.stderr, result.output
    git = rig.stubs.calls("git")
    assert not any("DeGrid" in arg for call in git for arg in call.args), [c.args for c in git]
    assert not (rig.custom_nodes / "ComfyUI-DeGrid").exists()
    assert any(call.args[0] == "clone" and "ComfyUI-VOSR2" in call.args[-1] for call in git), "VOSR2 not installed"
    assert (rig.custom_nodes / "ComfyUI-VOSR2" / ".git").is_dir()


# ---- the Impact Pack SAM opt-out (Req 3.3, 3.7) ----


def test_the_impact_pack_marker_exists_before_install_py_runs(rig: Rig) -> None:
    rig.lists(nodes=IMPACT)
    result = rig.provision("nodes")
    assert result.returncode == 0, result.output
    assert rig.marker_log.read_text(encoding="utf-8").split() == ["yes"], "install.py ran without the marker"
    marker = rig.custom_nodes / "skip_download_model"
    assert marker.is_file() and marker.stat().st_size == 0


# ---- the VOSR2 prefetch (Req 3.3, 3.7) ----


def test_the_vosr2_link_replaces_a_real_folder_without_losing_a_file(rig: Rig) -> None:
    old = rig.env.comfyui_dir / "models" / "vosr2"
    (old / "VOSR2").mkdir(parents=True)
    (old / "VOSR2" / "a.bin").write_bytes(b"only in the old folder")
    (old / "VOSR2" / "c.bin").write_bytes(b"old copy")
    target = rig.models / "vosr2"
    (target / "VOSR2").mkdir(parents=True)
    (target / "VOSR2" / "b.bin").write_bytes(b"only on the volume")
    (target / "VOSR2" / "c.bin").write_bytes(b"volume copy")
    result = rig.install("influencer")
    assert result.returncode == 0, result.output
    assert old.is_symlink() and os.readlink(old) == str(target), result.output
    assert (target / "VOSR2" / "a.bin").read_bytes() == b"only in the old folder"
    assert (target / "VOSR2" / "b.bin").read_bytes() == b"only on the volume"
    assert (target / "VOSR2" / "c.bin").read_bytes() == b"volume copy"  # no-clobber


def test_the_vosr2_conversion_runs_once_into_a_tmp_target(rig: Rig) -> None:
    rig.lists(models=VOSR2_LINE)
    folder = rig.models / "vosr2" / "VOSR2"
    first = rig.provision("models")
    assert first.returncode == 0, first.output
    conversions = [call for call in rig.stubs.calls("python") if call.args[:1] == ("-",) and len(call.args) == 3]
    assert len(conversions) == 1, [c.args for c in rig.stubs.calls("python")]
    assert conversions[0].args[1:] == (str(folder / PTH), str(folder / f"{SAFETENSORS}.tmp"))
    assert (folder / SAFETENSORS).read_bytes() == b"converted"
    assert (folder / PTH).read_bytes() == b"hf-weights", "the .pth must stay"
    assert not (folder / f"{SAFETENSORS}.tmp").exists()
    rig.stubs.clear()
    again = rig.provision("models")
    assert again.returncode == 0, again.output
    assert not any(c.args[:1] == ("-",) and len(c.args) == 3 for c in rig.stubs.calls("python")), "converted twice"
    assert rig.stubs.calls("hf") == []


# ---- need_tool and the per-id system tools (Req 16.1, 3.6) ----


def _ffmpeg_installs(rig: Rig) -> list:
    return [call for call in rig.stubs.calls("apt-get") if call.args[:1] == ("install",) and "ffmpeg" in call.args]


@pytest.mark.parametrize("workflow_id", ["influencer-video", "influencer", "other"])
def test_a_missing_ffmpeg_is_installed_for_influencer_video_only(rig: Rig, workflow_id: str) -> None:
    result = rig.install(workflow_id)
    assert result.returncode == 0, result.output
    assert len(_ffmpeg_installs(rig)) == (1 if workflow_id == "influencer-video" else 0), rig.stubs.log_lines()


def test_a_failed_ffmpeg_install_is_reported_with_a_non_zero_exit(rig: Rig) -> None:
    rig.script_apt(ffmpeg_status=100)
    result = rig.install("influencer-video")
    assert result.returncode != 0, result.output
    assert len(_ffmpeg_installs(rig)) == 1
    assert "could not install ffmpeg" in result.stderr, result.output
    assert "finished with problems" in result.stderr, result.output


# ---- a second run downloads nothing (Req 3.7) ----


def _tree(root: Path) -> dict[str, tuple[int, int, str]]:
    """Every path below root: (size, mtime in ns, link target or "")."""
    found = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            link = os.readlink(path) if os.path.islink(path) else ""
            found[os.path.relpath(path, root)] = (info.st_size, info.st_mtime_ns, link)
    return found


def test_a_second_run_downloads_nothing_and_exits_0(rig: Rig) -> None:
    rig.lists(nodes=DEGRID + IMPACT + VOSR2_NODE, models=CIVITAI_LINE + VOSR2_LINE, ollama=OLLAMA_LINE)
    first = rig.install("influencer")
    assert first.returncode == 0, first.output
    assert len(rig.civitai_calls()) == 2 and len(rig.stubs.calls("hf")) == 1  # the first run did download
    link = rig.env.comfyui_dir / "models" / "vosr2"
    assert link.is_symlink()
    link_before = (os.readlink(link), os.lstat(link).st_mtime_ns, os.lstat(link).st_ino)
    watched = (rig.models, rig.custom_nodes, rig.env.workspace / ".staging")
    before = [_tree(folder) for folder in watched]
    rig.stubs.clear()

    second = rig.install("influencer")
    assert second.returncode == 0, second.output
    assert rig.civitai_calls() == [], "a Civitai request (probe or download) on the second run"
    for name in ("wget", "hf"):
        assert rig.stubs.calls(name) == [], f"{name} was called on the second run"
    assert not [c for c in rig.stubs.calls("ollama") if c.args[:1] == ("pull",)], "an Ollama pull on the second run"
    assert not [c for c in rig.stubs.calls("git") if c.args[:1] == ("clone",)], "a clone on the second run"
    assert not [c for c in rig.stubs.calls("python") if c.args[:1] == ("-",) and len(c.args) == 3], "a conversion"
    assert (os.readlink(link), os.lstat(link).st_mtime_ns, os.lstat(link).st_ino) == link_before
    assert [_tree(folder) for folder in watched] == before, "the second run changed a model, node or staging file"
    assert "created custom_nodes/skip_download_model" not in second.stdout


# ---- the disk-estimate hook (Req 23.3, 23.4) ----


def _usable_timeout(rig: Rig) -> None:
    """Puts the timeout stub on the PATH, and skips unless it runs a command through."""
    rig.stubs.stub("timeout", body=TIMEOUT_BODY)
    probe = rig.tmp / "timeout-probe.sh"
    probe.write_text("timeout 120 bash -c 'echo through'\n", encoding="utf-8")
    result = rig.stubs.run(probe, timeout=30)
    if result.returncode != 0 or result.stdout.strip() != b"through":
        pytest.skip("neither the timeout stub nor a real timeout can run a command here")
    rig.stubs.clear()


def _install_steps(rig: Rig) -> list[tuple[str, str]]:
    """The run's stub calls without the estimator's own (timeout and python -m influencer disk)."""
    return [
        (call.name, call.args[0] if call.args else "")
        for call in rig.stubs.calls()
        if call.name != "timeout" and not (call.name == "python" and call.args[:1] == ("-m",))
    ]


def _repo_copy_without_launcher(root: Path) -> Path:
    """The repo's scripts and model-path file in a temp repo that lacks scripts/influencer.sh."""
    (root / "scripts").mkdir(parents=True)
    for path in (REPO / "scripts").iterdir():
        if path.is_file() and path.name != "influencer.sh":
            shutil.copy2(path, root / "scripts" / path.name)
    shutil.copy2(REPO / "extra_model_paths.yaml", root / "extra_model_paths.yaml")
    return root / "scripts" / "install-workflow.sh"


@pytest.fixture
def baseline(rig: Rig) -> tuple[Result, list[tuple[str, str]]]:
    """A run with a working estimator: its result and install steps, for the failure cases to match."""
    rig.lists(nodes=VOSR2_NODE, models=VOSR2_LINE)
    _usable_timeout(rig)
    rig.script_python(disk=DISK_OK)
    result = rig.install("influencer")
    return result, _install_steps(rig)


def test_the_hook_prints_the_estimate_before_the_install_phases(rig: Rig, baseline) -> None:
    result, _ = baseline
    assert result.returncode == 0, result.output
    assert f"[install-workflow] {ESTIMATE}\n" in result.stdout, result.output
    assert result.stdout.index(ESTIMATE) < result.stdout.index("[install-workflow] installing workflow: influencer")
    assert "disk estimate unavailable" not in result.stderr
    timeouts = rig.stubs.calls("timeout")
    assert len(timeouts) == 1 and timeouts[0].args[:2] == ("120", "bash"), [c.args for c in timeouts]
    assert timeouts[0].args[-4:] == ("disk", "--id", "influencer", "--brief")


@pytest.mark.parametrize("case", ["non-zero", "hangs", "no-launcher"])
def test_an_unavailable_estimate_warns_and_changes_nothing(
    rig: Rig, baseline, temp_env: TempEnv, tmp_path: Path, case: str
) -> None:
    base_result, base_steps = baseline
    # A fresh env, so the run starts from the same state as the baseline did.
    shutil.rmtree(temp_env.workspace)
    TempEnv.create(temp_env.root)
    rig.lists(nodes=VOSR2_NODE, models=VOSR2_LINE)
    rig.stubs.clear()
    script: str | Path = INSTALL
    if case == "non-zero":
        rig.script_python(disk=DISK_FAILS)
    elif case == "hangs":
        rig.script_python(disk=DISK_HANGS)
    else:
        script = _repo_copy_without_launcher(tmp_path / "repo-copy")
    result = rig.install("influencer", script)
    assert "[install-workflow][WARN] disk estimate unavailable (continuing)" in result.stderr, result.output
    assert ESTIMATE not in result.stdout
    assert result.returncode == base_result.returncode, result.output
    assert _install_steps(rig) == base_steps
