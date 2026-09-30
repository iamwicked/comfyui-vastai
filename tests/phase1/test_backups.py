"""Bash tests for the backup copies of save-progress.sh (task 27.8, design §C12; Req 25.5).

Every run drives scripts/save-progress.sh under /bin/bash with the stub PATH of task 3.4 (rclone and git are
recording stubs) and the temp WORKSPACE of task 3.1, so nothing is copied, committed or pushed. WORKFLOWS_REPO
stays unset, so the git half of the script only logs that it skips.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

import pytest

from helpers.harness import REPO, TempEnv
from helpers.stubs import BASH, StubPath

pytestmark = pytest.mark.skipif(not os.access(BASH, os.X_OK), reason=f"{BASH} is missing; the script runs under it")

SCRIPT = "scripts/save-progress.sh"
REMOTE = "remote:comfyui-output"
STATE = f"{REMOTE}.state"


class Run:
    def __init__(self, returncode: int, stdout: str, stderr: str, copies: list[tuple[str, ...]]) -> None:
        self.returncode, self.stdout, self.stderr, self.copies = returncode, stdout, stderr, copies

    @property
    def output(self) -> str:
        return f"exit {self.returncode}\n--- stdout\n{self.stdout}\n--- stderr\n{self.stderr}"

    @property
    def targets(self) -> list[str]:
        return [args[2] for args in self.copies]


def save(stubs: StubPath, **extra: str) -> Run:
    result = stubs.run(SCRIPT, extra_env=extra, timeout=60)
    copies = [call.args for call in stubs.calls("rclone")]
    assert all(args[:1] == ("copy",) and args[-1] == "--progress" and len(args) == 4 for args in copies), copies
    return Run(result.returncode, result.stdout.decode("utf-8", "replace"), result.stderr.decode("utf-8", "replace"),
               copies)  # fmt: skip


def roots(env: TempEnv, *names: str) -> list[Path]:
    made = []
    for name in names:
        folder = env.workspace / "influencer" / name
        (folder / "weeks").mkdir(parents=True)
        (folder / "persona.yaml").write_text("slug: x\n", encoding="utf-8")
        made.append(folder)
    return made


def characters(env: TempEnv) -> Path:
    folder = env.workspace / "models" / "loras" / "characters"
    (folder / "mara").mkdir(parents=True)
    (folder / "mara" / "mara_v1.safetensors").write_bytes(b"lora")
    return folder


def test_each_root_and_the_characters_get_their_own_copy(stub_path: StubPath, temp_env: TempEnv) -> None:
    alpha, beta = roots(temp_env, "alpha", "beta")
    chars = characters(temp_env)
    (temp_env.workspace / "influencer" / "notes.txt").write_text("a file, not a root", encoding="utf-8")
    run = save(stub_path, RCLONE_REMOTE=REMOTE + "/")  # a trailing slash on the remote is dropped
    assert run.returncode == 0, run.output
    outputs = (str(temp_env.comfyui_dir / "output"), REMOTE + "/")
    assert [args[1:3] for args in run.copies] == [
        outputs,
        (f"{alpha}/", f"{STATE}/influencer/alpha"),
        (f"{beta}/", f"{STATE}/influencer/beta"),
        (str(chars), f"{STATE}/characters"),
    ]
    state_targets = run.targets[1:]
    assert len(set(state_targets)) == len(state_targets)
    assert all(target.rstrip("/") != REMOTE for target in state_targets)
    assert all(not target.startswith(REMOTE + "/") for target in state_targets)  # never inside the outputs copy


def test_missing_folders_add_no_copy(stub_path: StubPath, temp_env: TempEnv) -> None:
    (temp_env.workspace / "influencer").mkdir()  # an empty home: no root, no call
    run = save(stub_path, RCLONE_REMOTE=REMOTE)
    assert run.returncode == 0, run.output
    assert run.targets == [REMOTE]


def test_a_failed_copy_is_logged_and_the_rest_still_run(stub_path: StubPath, temp_env: TempEnv) -> None:
    alpha, beta = roots(temp_env, "alpha", "beta")
    chars = characters(temp_env)
    stub_path.stub("rclone", body='case "$2" in */alpha/) status=1 ;; esac')
    run = save(stub_path, RCLONE_REMOTE=REMOTE)
    assert f"rclone copy failed: {alpha}/" in run.stdout, run.output
    assert run.targets == [REMOTE, f"{STATE}/influencer/alpha", f"{STATE}/influencer/beta", f"{STATE}/characters"]
    assert run.returncode == 0, run.output  # the state copies never change the exit status
    assert "done." in run.stdout and "with problems" not in run.stdout
    # A failing outputs copy still sets the status as before, and the state copies still run.
    stub_path.clear()
    outputs = shlex.quote(str(temp_env.comfyui_dir / "output"))
    stub_path.stub("rclone", body=f'case "$2" in {outputs}) status=1 ;; esac')
    failed = save(stub_path, RCLONE_REMOTE=REMOTE)
    assert failed.returncode == 1, failed.output
    assert failed.targets == run.targets
    assert chars.is_dir() and beta.is_dir()


def test_no_remote_makes_no_rclone_call(stub_path: StubPath, temp_env: TempEnv) -> None:
    roots(temp_env, "alpha")
    characters(temp_env)
    run = save(stub_path)
    assert run.returncode == 0, run.output
    assert stub_path.calls("rclone") == [] and stub_path.calls("git") == []
    assert "RCLONE_REMOTE not set" in run.stdout and "WORKFLOWS_REPO not set" in run.stdout


def test_the_state_copies_stay_inside_the_rclone_block() -> None:
    text = (REPO / SCRIPT).read_text(encoding="utf-8")
    git_half, _, rclone_half = text.partition("# ---- 2. Outputs -> cloud via rclone ----")
    assert rclone_half, "the rclone block's header is missing"
    assert ".state" not in git_half.split("set -euo pipefail", 1)[1]
    block = rclone_half.split('\nelse\n  log "RCLONE_REMOTE not set', 1)[0]
    assert 'state="${RCLONE_REMOTE%/}.state"' in block
    assert len(re.findall(r'rclone copy "', block)) == 3  # outputs, each root, the characters
