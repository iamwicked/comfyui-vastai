"""Bash tests of the launcher, scripts/influencer.sh (task 5.1, design §C6, Req 25.6, 25.7).

The interpreter tests run a copy of the launcher in a throwaway repo under tmp_path, so the checkout's
own .check-venv never takes part, with the ComfyUI_Env path /venv/main/bin/python pointed at a temp path,
so a host that has /venv/main behaves like one that does not. Every interpreter there is a stub: it
answers the launcher's version probe (`-c <code>`) by running that code in the real Python with a faked
sys.version_info, and otherwise prints which stub ran, its arguments and PYTHONPATH. A run's whole
environment is PATH (the stubs only), HOME and TMPDIR, plus what a test adds, under /bin/bash (bash 3.2 on
macOS). The last tests run the real launcher on the real package.
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

from helpers.harness import REPO, run_bounded

BASH = "/bin/bash"
LAUNCHER = REPO / "scripts" / "influencer.sh"
COMFYUI_ENV_LINE = "COMFYUI_ENV_PYTHON=/venv/main/bin/python\n"
BAD_WEEK = "command line: --week: '2026-W99' names week 99, but 2026 has the ISO weeks 01 to 53\n"

pytestmark = pytest.mark.skipif(not os.access(BASH, os.X_OK), reason="/bin/bash is missing; the launcher is tested under it")

# A stub interpreter. printf is a shell builtin, so the stub needs nothing on PATH.
STUB = """#!/bin/sh
printf '%s\\n' "$0 $*" >> {log}
if [ "$1" = -c ]; then
  exec {python} -c '
import sys
from collections import namedtuple
fields = "major minor micro releaselevel serial"
sys.version_info = namedtuple("version_info", fields)({major}, {minor}, {micro}, "final", 0)
exec(compile(sys.argv.pop(1), "<string>", "exec"))' "$2"
fi
printf 'picked %s\\n' "$0"
for arg in "$@"; do printf 'arg=%s\\n' "$arg"; done
printf 'PYTHONPATH=%s\\n' "${{PYTHONPATH-<unset>}}"
"""

ARGS = ("check-config", "a b", "*", "")  # passed through untouched, empty and glob-like ones included


class Sandbox(NamedTuple):
    """A throwaway repo holding a copy of the launcher, and the folders of one run."""

    root: Path
    repo: Path
    launcher: Path
    comfyui_env: Path  # stands in for /venv/main/bin/python
    check_env: Path  # <repo>/.check-venv/bin/python
    bin: Path  # the run's whole PATH
    log: Path  # one line per stub call: "<stub path> <arguments>"

    def stub(self, path: Path, version: tuple[int, int, int]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        major, minor, micro = version
        path.write_text(
            STUB.format(log=shlex.quote(str(self.log)), python=shlex.quote(sys.executable), major=major,
                        minor=minor, micro=micro),
            encoding="utf-8",
        )  # fmt: skip
        path.chmod(0o755)
        return path

    def run(self, *args: str, extra_env: dict[str, str] | None = None, cwd: Path | None = None):
        environment = {"PATH": str(self.bin), "HOME": str(self.root / "home"), "TMPDIR": str(self.root / "tmp")}
        environment.update(extra_env or {})
        return run_bounded([BASH, self.launcher, *args], cwd=cwd or self.root / "elsewhere", env=environment,
                           timeout=60)  # fmt: skip

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    repo = tmp_path / "repo"
    launcher = repo / "scripts" / "influencer.sh"
    launcher.parent.mkdir(parents=True)
    text = LAUNCHER.read_text(encoding="utf-8")
    assert text.count(COMFYUI_ENV_LINE) == 1, "the launcher must name the ComfyUI_Env in one assignment"
    comfyui_env = tmp_path / "venv-main" / "bin" / "python"
    launcher.write_text(text.replace(COMFYUI_ENV_LINE, f"COMFYUI_ENV_PYTHON={comfyui_env}\n"), encoding="utf-8")
    for folder in ("bin", "home", "tmp", "elsewhere"):
        (tmp_path / folder).mkdir()
    return Sandbox(tmp_path, repo, launcher, comfyui_env, repo / ".check-venv" / "bin" / "python",
                   tmp_path / "bin", tmp_path / "calls.log")  # fmt: skip


def _picked(result) -> tuple[str, list[str], str]:
    """The stub that ran, the arguments it got, and its PYTHONPATH, from its output."""
    lines = result.stdout.decode("utf-8").splitlines()
    assert lines and lines[0].startswith("picked "), result.stdout + result.stderr
    args = [line[len("arg="):] for line in lines if line.startswith("arg=")]
    [pythonpath] = [line[len("PYTHONPATH="):] for line in lines if line.startswith("PYTHONPATH=")]
    return lines[0][len("picked "):], args, pythonpath


# ---- the four interpreter cases (design §C6) ----


@pytest.mark.parametrize("case", ["INFLUENCER_PYTHON", "ComfyUI_Env", "check-venv", "PATH"])
def test_the_launcher_takes_the_first_interpreter_in_the_design_order(sandbox: Sandbox, case: str) -> None:
    """Each case has every later candidate present too, so the order itself is what is tested. On PATH,
    a python3.12 that is really 3.9 is probed and skipped, and python3, a 3.11, is taken."""
    path_39 = sandbox.stub(sandbox.bin / "python3.12", (3, 9, 6))
    path_311 = sandbox.stub(sandbox.bin / "python3", (3, 11, 9))
    later = [path_311]
    extra = {}
    if case in ("INFLUENCER_PYTHON", "ComfyUI_Env", "check-venv"):
        later.insert(0, sandbox.stub(sandbox.check_env, (3, 14, 0)))
    if case in ("INFLUENCER_PYTHON", "ComfyUI_Env"):
        later.insert(0, sandbox.stub(sandbox.comfyui_env, (3, 12, 3)))
    if case == "INFLUENCER_PYTHON":
        chosen = sandbox.stub(sandbox.root / "chosen" / "python", (3, 13, 1))
        extra["INFLUENCER_PYTHON"] = str(chosen)
    else:
        chosen = later.pop(0)
    result = sandbox.run(*ARGS, extra_env=extra)
    assert result.returncode == 0, result.stderr.decode()
    picked, args, pythonpath = _picked(result)
    assert (picked, args) == (str(chosen), ["-m", "influencer", *ARGS])
    assert pythonpath == str(sandbox.repo / "scripts")
    calls = [line.split(" ", 2)[:2] for line in sandbox.calls()]  # [stub, first argument] per call
    if case == "PATH":  # both PATH stubs were probed in order, and only the 3.11 ran the package
        assert calls == [[str(path_39), "-c"], [str(path_311), "-c"], [str(path_311), "-m"]]
    else:  # the first three steps take their interpreter without probing or running any other
        assert calls == [[str(chosen), "-m"]]


def test_with_only_a_python_3_9_the_launcher_exits_2_and_runs_nothing(sandbox: Sandbox) -> None:
    old = sandbox.stub(sandbox.bin / "python3", (3, 9, 6))  # like the Mac's /usr/bin/python3
    result = sandbox.run("check-config")
    stderr = result.stderr.decode()
    assert result.returncode == 2, stderr
    assert result.stdout == b""
    assert "no Python 3.10 or later" in stderr and "INFLUENCER_PYTHON" in stderr, stderr
    assert [line.split(" ", 2)[:2] for line in sandbox.calls()] == [[str(old), "-c"]]  # probed, never run


@pytest.mark.parametrize("value", ["missing", "folder", "not-on-path"])
def test_a_wrong_influencer_python_exits_2_without_falling_through(sandbox: Sandbox, value: str) -> None:
    sandbox.stub(sandbox.check_env, (3, 14, 0))
    (sandbox.root / "folder").mkdir()
    given = {"missing": str(sandbox.root / "no-such-python"), "folder": str(sandbox.root / "folder")}.get(value, value)
    result = sandbox.run("check-config", extra_env={"INFLUENCER_PYTHON": given})
    stderr = result.stderr.decode()
    assert result.returncode == 2, stderr
    assert f"INFLUENCER_PYTHON is '{given}'" in stderr, stderr
    assert sandbox.calls() == []  # the .check-venv stub was never tried


def test_a_bare_influencer_python_name_is_looked_up_on_path(sandbox: Sandbox) -> None:
    named = sandbox.stub(sandbox.bin / "my-python", (3, 12, 0))
    result = sandbox.run("disk", "--id", "x", extra_env={"INFLUENCER_PYTHON": "my-python"})
    assert result.returncode == 0, result.stderr.decode()
    assert _picked(result)[:2] == (str(named), ["-m", "influencer", "disk", "--id", "x"])


# ---- PYTHONPATH and the working folder ----


@pytest.mark.parametrize(("given", "expected"), [(None, ""), ("", ""), ("/x/y:/z", ":/x/y:/z")],
                         ids=["unset", "empty", "set"])  # fmt: skip
def test_pythonpath_starts_with_the_absolute_scripts_folder(sandbox: Sandbox, given: str | None, expected: str) -> None:
    """Prepended to an existing PYTHONPATH, and absolute whatever the working folder and however the
    launcher is named: by absolute path from another folder, or as scripts/influencer.sh from the repo."""
    sandbox.stub(sandbox.check_env, (3, 14, 0))
    extra = {} if given is None else {"PYTHONPATH": given}
    scripts = str(sandbox.repo / "scripts")
    assert _picked(sandbox.run("check-config", extra_env=extra))[2] == scripts + expected
    environment = {"PATH": str(sandbox.bin), "HOME": str(sandbox.root / "home"), **extra}
    relative = run_bounded([BASH, "scripts/influencer.sh", "check-config"], cwd=sandbox.repo, env=environment,
                           timeout=60)  # fmt: skip
    assert relative.returncode == 0, relative.stderr.decode()
    assert _picked(relative)[2] == scripts + expected


# ---- the real launcher on the real package ----


class RealRun(NamedTuple):
    returncode: int
    stdout: str
    stderr: str


def _real_run(tmp_path: Path, *args: str) -> RealRun:
    """The checkout's launcher with this Python, from a folder that holds an influencer/ folder, as the
    Mac's repo root does (its Influencer home) when the Runbook runs the launcher from there."""
    cwd = tmp_path / "cwd"
    (cwd / "influencer" / "mara-lind").mkdir(parents=True, exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "INFLUENCER_PYTHON": sys.executable,
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
        "COLUMNS": "200",
    }
    result = run_bounded([BASH, LAUNCHER, *args], cwd=cwd, env=environment, timeout=60)
    return RealRun(result.returncode, result.stdout.decode("utf-8"), result.stderr.decode("utf-8"))


def test_the_real_launcher_runs_the_package_from_any_folder(tmp_path: Path) -> None:
    """render reaches the package's own Week_Id check (exit 1, before any file is read), so the checkout's
    scripts/ folder won over the influencer/ folder in the working folder."""
    run = _real_run(tmp_path, "render", "--week", "2026-W99")
    assert (run.returncode, run.stdout, run.stderr) == (1, "", BAD_WEEK)


@pytest.mark.parametrize(("args", "status"), [(("--help",), 0), (("bogus",), 1), (("plan",), 1)])
def test_the_real_launcher_passes_the_package_exit_status_through(tmp_path: Path, args: tuple[str, ...], status: int) -> None:
    run = _real_run(tmp_path, *args)
    assert run.returncode == status, run.stderr
    shown = run.stdout if status == 0 else run.stderr
    assert shown.startswith("usage: influencer.sh"), shown
