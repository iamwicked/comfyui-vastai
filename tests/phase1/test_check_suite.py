"""Bash tests of the Check_Suite's own step reporting (scripts/check.sh; design §C11, Req 27).

Every run executes check.sh under /bin/bash in a throwaway copy of the repo under tmp_path, never
in this checkout, so the real .check-venv is never rebuilt or touched. A copy is a git work tree
holding check.sh, a stand-in requirements file, a stub Config_Checker, one tiny test of its own
and a few shell scripts. Its .check-venv is a stub whose bin/python execs the Python running these
tests, and its stamp matches that Python, so check.sh reuses it without an interpreter search.

A run's whole environment is PATH, HOME and TMPDIR. PATH is a folder of links to the commands
check.sh runs, plus the run's stubs (shellcheck, uname, Python 3.9 stand-ins), and HOME is empty,
so no run can find a real interpreter or shellcheck, build an env, or reach the network. Each
scenario runs once, and every test that needs it shares that run.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest

REPO = Path(__file__).resolve().parents[2]
BASH = "/bin/bash"
CHECK = "scripts/check.sh"  # repo-relative, as in its usage line
# The commands check.sh runs besides bash builtins, Python and shellcheck (dirname finds the root).
TOOLS = ("dirname", "git", "sed", "grep", "sort", "tee", "uname", "rm", "mv")

pytestmark = [
    pytest.mark.skipif(
        not os.access(BASH, os.X_OK), reason="/bin/bash is missing; check.sh is tested under it"
    ),
    pytest.mark.skipif(
        shutil.which("git") is None,
        reason="git is missing; check.sh lists the shell scripts with git ls-files",
    ),
]

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"
NO_TESTS = "no tests"  # the copy's tests/ holds no test file, so pytest exits 5
STEPS = ("config", "tests", "bash -n", "shellcheck")
HEADERS = tuple(f"[check] {number}/4 {step}:" for number, step in enumerate(STEPS, start=1))
SUMMARY_LINE = re.compile(r"(config|tests|bash -n|shellcheck): (passed|failed|skipped)")
LISTING = re.compile(r"\[check\] shell scripts \((\d+)\): (.*)")
PYTEST_RESULT = re.compile(r"1 (passed|failed) in ")  # pytest's closing line for the one test
INSTALL_HINTS = {"Darwin": "brew install shellcheck", "Linux": "sudo apt-get install -y shellcheck"}
NO_PYTHON = "Python 3.10 or later is required"

# ---- the copy ----

# The shell scripts check.sh must find in every copy (Req 27.1).
SCRIPTS = ("bin/tool", "scripts/check.sh", "untracked.sh")
COPY_FILES = {
    ".gitignore": "/.check-venv/\n/ignored/\n__pycache__/\n.pytest_cache/\n",
    "tests/requirements-check.txt": "# stand-in: these runs reuse a stub .check-venv\n",
    "scripts/influencer/__init__.py": "",
    "bin/tool": "#!/usr/bin/env bash\necho tool\n",  # tracked; bash shebang, no .sh suffix
    "untracked.sh": "echo untracked\n",  # never added to the index, and not ignored
    # Not shell scripts, and neither parses, so bash -n fails if check.sh takes one of them.
    "bin/pytool": '#!/usr/bin/env python3\nprint("python")\n',
    "ignored/skip.sh": "if then\n",
}
TRACKED = (".gitignore", "bin", "scripts", "tests")  # what the copy's git add stages
# Added when bash -n must fail: one untracked and one tracked shell script, neither parses.
BROKEN_FILES = {"broken.sh": "echo 'never closed\n", "bin/broken": "#!/bin/bash\nif true; then\n"}
TEST_FILES = {
    PASSED: "def test_passes():\n    pass\n",
    FAILED: "def test_fails():\n    assert False, 'fails on purpose'\n",
}

# ---- the stubs ----

CONFIG_STUB = '''\
"""Stub Config_Checker: exits {code} for check-config and 2 for any other arguments."""
import sys

print("stub-config:", *sys.argv[1:])
sys.exit({code} if sys.argv[1:] == ["check-config"] else 2)
'''
VENV_PYTHON_STUB = '#!/bin/sh\nexec {python} "$@"\n'
SHELLCHECK_STUB = '#!/bin/sh\necho "stub-shellcheck: $*"\nexit {code}\n'
UNAME_STUB = "#!/bin/sh\necho {name}\n"
# Logs each call, answers check.sh's -c version probe as Python 3.9.6 would, and refuses anything
# else, so a build that picked it would stop at `-m venv`, before pip could reach the network.
OLD_PYTHON_STUB = """\
#!/bin/sh
echo "$0" >> {log}
if [ "$#" -ne 2 ] || [ "$1" != -c ]; then
  echo "python 3.9 stub: refusing $*" >&2
  exit 1
fi
exec {python} -c 'import sys
sys.version_info = (3, 9, 6, "final", 0)
exec(compile(sys.argv.pop(1), "<string>", "exec"))' "$2"
"""


class Scenario(NamedTuple):
    """One check.sh run, described by what each step's stub or input makes the step report."""

    config: str = PASSED  # the stub Config_Checker exits 0 (PASSED) or 1 (FAILED)
    tests: str = PASSED  # the copy's one test passes or fails; NO_TESTS: it has none
    bash_n: str = PASSED  # FAILED adds BROKEN_FILES
    shellcheck: str = PASSED  # a stub shellcheck exits 0 or 1; SKIPPED: none on PATH
    python: bool = True  # False: no .check-venv, and only Python 3.9 stand-ins to find
    uname: str | None = None  # what a stub uname answers; None: the host's uname runs
    dirname: bool = True  # False: PATH has no dirname

    def results(self) -> tuple[str, str, str, str]:
        """The result check.sh must report for each step, in STEPS order."""
        config = self.config if self.python else FAILED
        tests = PASSED if self.python and self.tests == PASSED else FAILED
        return (config, tests, self.bash_n, self.shellcheck)


class Run(NamedTuple):
    """What one check.sh run did."""

    returncode: int
    output: str  # stdout and stderr together, in the order they were written
    lines: list[str]
    root: Path  # holds repo/ (the copy), bin/ (PATH), home/ (HOME) and tmp/ (TMPDIR)
    probes: list[str]  # the Python 3.9 stand-ins that check.sh ran, in order


def _summary(*results: str) -> list[str]:
    return [f"{step}: {result}" for step, result in zip(STEPS, results)]


def _id(scenario: Scenario) -> str:
    return "-".join(scenario[:4])  # e.g. passed-failed-passed-skipped


def _host_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        pytest.fail(f"{name} is not on PATH, and check.sh runs it")
    return path


def _write(path: Path, text: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def _git(repo: Path, env: dict[str, str], *args: str) -> None:
    result = subprocess.run(
        [_host_tool("git"), *args],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "backslashreplace")


def _run_check(scenario: Scenario, root: Path) -> Run:
    """Builds the copy, PATH and HOME for scenario under root, then runs check.sh in the copy."""
    repo, bin_dir, home, tmpdir = (root / name for name in ("repo", "bin", "home", "tmp"))
    for folder in (bin_dir, home, tmpdir):
        folder.mkdir()
    env = {"PATH": str(bin_dir), "HOME": str(home), "TMPDIR": str(tmpdir)}
    python, log = shlex.quote(sys.executable), root / "probes.log"

    for tool in TOOLS:
        if not (tool == "uname" and scenario.uname) and not (tool == "dirname" and not scenario.dirname):
            (bin_dir / tool).symlink_to(_host_tool(tool))
    if scenario.uname:
        _write(bin_dir / "uname", UNAME_STUB.format(name=scenario.uname), executable=True)
    if scenario.shellcheck != SKIPPED:
        code = 0 if scenario.shellcheck == PASSED else 1
        _write(bin_dir / "shellcheck", SHELLCHECK_STUB.format(code=code), executable=True)

    files = dict(COPY_FILES)
    code = 0 if scenario.config == PASSED else 1
    files["scripts/influencer/__main__.py"] = CONFIG_STUB.format(code=code)
    if scenario.tests in TEST_FILES:
        files["tests/test_tiny.py"] = TEST_FILES[scenario.tests]
    if scenario.bash_n == FAILED:
        files.update(BROKEN_FILES)
    for name, text in files.items():
        _write(repo / name, text)
    shutil.copyfile(REPO / CHECK, repo / CHECK)
    _git(repo, env, "init", "-q")
    _git(repo, env, "add", "--", *TRACKED)

    if scenario.python:
        venv_python = VENV_PYTHON_STUB.format(python=python)
        _write(repo / ".check-venv/bin/python", venv_python, executable=True)
        digest = hashlib.sha256((repo / "tests/requirements-check.txt").read_bytes()).hexdigest()
        version = "%d.%d.%d" % sys.version_info[:3]
        stamp = f"requirements-sha256 {digest}\npython {sys.implementation.name}-{version}\n"
        _write(repo / ".check-venv/stamp", stamp)
    else:
        stub = OLD_PYTHON_STUB.format(log=shlex.quote(str(log)), python=python)
        for path in (bin_dir / "python3", home / ".local/bin/python3"):
            _write(path, stub, executable=True)

    result = subprocess.run(
        [BASH, CHECK],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    output = result.stdout.decode("utf-8", "backslashreplace")
    if scenario.python and "[check] env: reusing .check-venv" not in output:
        pytest.fail(f"check.sh did not reuse the stub .check-venv, so the run is void:\n{output}")
    probes = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return Run(result.returncode, output, output.splitlines(), root, probes)


@pytest.fixture(scope="module")
def check_run(tmp_path_factory: pytest.TempPathFactory) -> Callable[[Scenario], Run]:
    """Runs check.sh for a scenario in a fresh copy, once; later requests share that run."""
    runs: dict[Scenario, Run] = {}

    def run(scenario: Scenario) -> Run:
        if scenario not in runs:
            runs[scenario] = _run_check(scenario, tmp_path_factory.mktemp("check-run"))
        return runs[scenario]

    return run


def _step_starts(run: Run) -> list[int]:
    """The line numbers of the four step headers. Fails the test unless each header appears once,
    in step order, before the summary."""
    starts = []
    for header in HEADERS:
        found = [number for number, line in enumerate(run.lines) if line.startswith(header)]
        assert len(found) == 1, f"{header!r} appears {len(found)} times\n{run.output}"
        starts.append(found[0])
    assert starts == sorted(starts), f"the steps ran out of order\n{run.output}"
    assert starts[-1] < len(run.lines) - 4, f"no summary after the last step\n{run.output}"
    return starts


def _section(run: Run, step: str) -> list[str]:
    """What a step printed: the lines after its header, up to the next header or the summary."""
    ends = [*_step_starts(run), len(run.lines) - 4]
    index = STEPS.index(step)
    return run.lines[ends[index] + 1 : ends[index + 1]]


def _shellcheck_calls(run: Run) -> list[list[str]]:
    """The arguments of each call to the stub shellcheck, which prints them on one line."""
    section = _section(run, "shellcheck")
    return [line.split()[1:] for line in section if line.startswith("stub-shellcheck:")]


# ---- the summary and exit status (Req 27.7) ----

COMBOS = [  # every mix of step results: only shellcheck can be skipped
    Scenario(config, tests, bash_n, shellcheck)
    for config in (PASSED, FAILED)
    for tests in (PASSED, FAILED)
    for bash_n in (PASSED, FAILED)
    for shellcheck in (PASSED, FAILED, SKIPPED)
]


@pytest.mark.parametrize("scenario", COMBOS, ids=_id)
def test_summary_follows_every_step_and_sets_the_exit_status(
    check_run: Callable[[Scenario], Run], scenario: Scenario
) -> None:
    """After the last step, one `<step>: <result>` line per step, in step order; then exit 1 if a
    step failed and 0 otherwise, a skipped step not counting as failed (Req 27.7)."""
    run = check_run(scenario)
    results = scenario.results()
    assert run.lines[-4:] == _summary(*results), run.output
    early = [line for line in run.lines[:-4] if SUMMARY_LINE.fullmatch(line)]
    assert early == [], f"summary lines before the last step ended: {early}\n{run.output}"
    _step_starts(run)  # every step ran, in step order, before the summary
    assert run.returncode == (1 if FAILED in results else 0), run.output


# ---- a failing early step (Req 27.8) ----


@pytest.mark.parametrize(
    "scenario",
    [Scenario(config=FAILED), Scenario(tests=FAILED), Scenario(config=FAILED, tests=FAILED)],
    ids=_id,
)
def test_a_failing_config_or_tests_step_does_not_stop_the_later_steps(
    check_run: Callable[[Scenario], Run], scenario: Scenario
) -> None:
    """pytest runs after config fails, and bash -n and shellcheck run and pass after either
    fails (Req 27.8)."""
    run = check_run(scenario)
    assert any(PYTEST_RESULT.match(line) for line in _section(run, "tests")), run.output
    assert len(_shellcheck_calls(run)) == 1, run.output
    assert run.lines[-4:] == _summary(*scenario.results()), run.output
    assert run.returncode == 1, run.output


# ---- a missing shellcheck (Req 27.4) ----


@pytest.mark.parametrize("uname", [None, "Darwin", "Linux"], ids=["host-uname", "Darwin", "Linux"])
def test_missing_shellcheck_is_skipped_with_the_install_command_for_the_os(
    check_run: Callable[[Scenario], Run], uname: str | None
) -> None:
    """With no shellcheck on PATH the step is skipped and prints the install command for the OS that
    uname names; the other steps still run, and a skip alone leaves exit 0 (Req 27.4, 27.7)."""
    os_name = uname or platform.system()
    if os_name not in INSTALL_HINTS:
        pytest.skip(f"the design gives install commands for macOS and Linux only, not {os_name}")
    run = check_run(Scenario(shellcheck=SKIPPED, uname=uname))
    assert any(INSTALL_HINTS[os_name] in line for line in _section(run, "shellcheck")), run.output
    for other, hint in INSTALL_HINTS.items():
        if other != os_name:
            assert hint not in run.output, run.output
    assert "stub-config: check-config" in _section(run, "config"), run.output
    assert any(PYTEST_RESULT.match(line) for line in _section(run, "tests")), run.output
    assert run.lines[-4:] == _summary(PASSED, PASSED, PASSED, SKIPPED), run.output
    assert run.returncode == 0, run.output


# ---- no Python 3.10+ (Req 27.9) ----


def test_without_python_3_10_config_and_tests_fail_and_the_later_steps_run(
    check_run: Callable[[Scenario], Run],
) -> None:
    """No .check-venv, and only Python 3.9 on PATH and in ~/.local/bin: config and tests fail,
    each stating that Python 3.10 or later is required, and bash -n and shellcheck still run
    (Req 27.9, 27.8)."""
    run = check_run(Scenario(python=False))
    for step in ("config", "tests"):
        assert any(NO_PYTHON in line for line in _section(run, step)), f"{step}:\n{run.output}"
    assert len(_shellcheck_calls(run)) == 1, run.output
    assert run.lines[-4:] == _summary(FAILED, FAILED, PASSED, PASSED), run.output
    assert run.returncode == 1, run.output
    # Both stand-ins were probed and turned down for their version, and no env was built.
    assert run.probes == [str(run.root / "bin/python3"), str(run.root / "home/.local/bin/python3")]
    assert not (run.root / "repo/.check-venv").exists()


# ---- bash -n and shell-script discovery (Req 27.1) ----


def test_bash_n_counts_the_scripts_that_do_not_parse(check_run: Callable[[Scenario], Run]) -> None:
    """bash -n fails with the number of scripts that do not parse, naming each, and never sees
    the unparsable files that are not shell scripts (Req 27.1, 27.7)."""
    run = check_run(Scenario(bash_n=FAILED))
    section = _section(run, "bash -n")
    count = f"{len(BROKEN_FILES)} of {len(SCRIPTS) + len(BROKEN_FILES)} scripts do not parse"
    assert any(count in line for line in section), run.output
    for name in BROKEN_FILES:
        assert any(line.startswith(f"{name}: ") for line in section), f"{name}:\n{run.output}"
    for name in ("bin/pytool", "ignored/skip.sh"):
        assert not any(name in line for line in section), f"{name}:\n{run.output}"
    assert run.lines[-4:] == _summary(PASSED, PASSED, FAILED, PASSED), run.output


def test_discovery_takes_tracked_untracked_and_shebang_scripts_but_not_ignored_ones(
    check_run: Callable[[Scenario], Run],
) -> None:
    """A shell script is a tracked, or untracked but not ignored, file whose name ends in .sh or
    whose first line is a bash or sh shebang; shellcheck gets exactly those (Req 27.1)."""
    run = check_run(Scenario())
    listings = [match for match in map(LISTING.fullmatch, run.lines) if match]
    assert len(listings) == 1, run.output
    count, names = int(listings[0].group(1)), listings[0].group(2).split()
    assert (count, sorted(names)) == (len(SCRIPTS), sorted(SCRIPTS)), run.output
    calls = _shellcheck_calls(run)
    assert len(calls) == 1, run.output
    assert calls[0][0] == "--" and sorted(calls[0][1:]) == sorted(SCRIPTS), run.output
    # bin/pytool and ignored/skip.sh do not parse, so bash -n passing shows it skipped them too.
    assert run.lines[-4:] == _summary(PASSED, PASSED, PASSED, PASSED), run.output


# ---- pytest collecting nothing (Req 27.7) ----


def test_a_tests_step_that_collects_no_tests_fails(check_run: Callable[[Scenario], Run]) -> None:
    """pytest's exit 5, no tests collected, reports the tests step failed (Req 27.7)."""
    run = check_run(Scenario(tests=NO_TESTS))
    assert any(line.startswith("no tests ran") for line in _section(run, "tests")), run.output
    assert run.lines[-4:] == _summary(PASSED, FAILED, PASSED, PASSED), run.output
    assert run.returncode == 1, run.output


# ---- the root lookup without dirname (Req 27.1, 27.7) ----

ROOT_ERROR = "check.sh: cannot find the repo root"


def test_without_dirname_a_misplaced_check_sh_stops_before_any_step(tmp_path: Path) -> None:
    """With no dirname on PATH, check.sh finds its root by parameter expansion. A check.sh that does
    not sit at <root>/scripts/check.sh stops non-zero with a message naming the problem, and runs no
    step (it would once have taken / as the root)."""
    copy, bin_dir, home, tmpdir = (tmp_path / name for name in ("copy", "bin", "home", "tmp"))
    for folder in (copy, bin_dir, home, tmpdir):
        folder.mkdir()
    for tool in TOOLS:
        if tool != "dirname":
            (bin_dir / tool).symlink_to(_host_tool(tool))
    shutil.copyfile(REPO / CHECK, copy / "check.sh")
    result = subprocess.run(
        [BASH, "check.sh"],
        cwd=copy,
        env={"PATH": str(bin_dir), "HOME": str(home), "TMPDIR": str(tmpdir)},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    output = result.stdout.decode("utf-8", "backslashreplace")
    assert result.returncode != 0, output
    assert ROOT_ERROR in output and "scripts/check.sh does not exist" in output, output
    assert not any(line.startswith("[check]") for line in output.splitlines()), f"a step ran:\n{output}"
    assert not SUMMARY_LINE.search(output), output


def test_without_dirname_check_sh_still_finds_its_root(check_run: Callable[[Scenario], Run]) -> None:
    """The same PATH without dirname, from a proper copy: the root is the copy, not /, and every step
    runs and passes (_run_check fails the run when the copy's .check-venv is not the one reused)."""
    run = check_run(Scenario(dirname=False))
    assert ROOT_ERROR not in run.output, run.output
    assert run.lines[-4:] == _summary(PASSED, PASSED, PASSED, PASSED), run.output
    assert run.returncode == 0, run.output
