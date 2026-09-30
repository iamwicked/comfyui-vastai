"""Bash tests for scripts/provision.sh and its Selection_Snapshot (design §M10, §M11).

As in the design's Testing Strategy, the tests run shell scripts in subprocesses, under
/bin/bash (bash 3.2 on macOS) unless a test says otherwise, and check exit status, output and
the files a run leaves behind.
"""

from __future__ import annotations

import io
import itertools
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

from helpers.harness import REPO, TempEnv, run_bounded
from helpers.stubs import BASH, StubPath

# ---- tests/tools/snapshot_v6_selection.sh, the Selection_Snapshot generator (Req 1.4, 27.6) ----
# The _snap helpers below serve only the generator's tests.

_SNAP_REPO = Path(__file__).resolve().parents[2]
_SNAP_BASH = "/bin/bash"
_SNAP_GENERATOR = "tests/tools/snapshot_v6_selection.sh"  # repo-relative, as in its usage line
_SNAP_FIXTURE = "tests/fixtures/selection-v6.txt"
_SNAP_TEMP_PREFIX = "snapshot_v6_selection."  # the name the generator gives its own temp dir
# Left out of the work-tree listing: git's internals, and the env and caches that pytest and
# the Check_Suite write while tests run.
_SNAP_UNLISTED = frozenset({".git", ".check-venv", ".pytest_cache", ".hypothesis", "__pycache__"})


class _SnapTree(NamedTuple):
    status: str  # git status --porcelain=v1 --untracked-files=all
    paths: dict[str, tuple[int, int]]  # relative path -> (size, mtime in ns)


class _SnapRun(NamedTuple):
    result: subprocess.CompletedProcess[bytes]
    home: Path  # the run's HOME, empty before the run
    tmpdir: Path  # the run's TMPDIR, empty before the run
    before: _SnapTree
    after: _SnapTree


def _snap_sandbox(parent: Path) -> tuple[Path, Path]:
    """Creates the empty dirs parent/home and parent/tmp, a run's HOME and TMPDIR."""
    home, tmpdir = parent / "home", parent / "tmp"
    home.mkdir()
    tmpdir.mkdir()
    return home, tmpdir


def _snap_run(
    script: str | Path, *, cwd: Path, home: Path, tmpdir: Path, **env: str
) -> subprocess.CompletedProcess[bytes]:
    """Runs script under /bin/bash with PATH, HOME, TMPDIR and env as its whole environment.

    Nothing else is inherited, so the caller's settings (BASH_ENV, SHELLOPTS, a GIT_DIR set by a
    git hook, ~/.gitconfig) cannot change the run.
    """
    run_env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(home), "TMPDIR": str(tmpdir)}
    run_env.update(env)
    return subprocess.run(
        [_SNAP_BASH, os.fspath(script)],
        cwd=cwd,
        env=run_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=300,
        check=False,
    )


def _snap_work_tree() -> _SnapTree:
    """The checkout's git status, and the size and mtime of every path outside _SNAP_UNLISTED.

    Directories are listed too: making or removing an entry changes its directory's mtime, so a
    file that a run creates and then deletes still shows.
    """
    status = subprocess.run(
        # --no-optional-locks: git status does not refresh .git/index, so looking writes nothing
        ["git", "--no-optional-locks", "-C", str(_SNAP_REPO), "status", "--porcelain=v1",
         "--untracked-files=all"],
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=60,
    ).stdout.decode("utf-8", "backslashreplace")
    info = os.lstat(_SNAP_REPO)
    paths = {".": (info.st_size, info.st_mtime_ns)}
    for dirpath, dirnames, filenames in os.walk(_SNAP_REPO):
        dirnames[:] = [name for name in dirnames if name not in _SNAP_UNLISTED]
        for name in dirnames + [name for name in filenames if name not in _SNAP_UNLISTED]:
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            paths[os.path.relpath(path, _SNAP_REPO)] = (info.st_size, info.st_mtime_ns)
    return _SnapTree(status, paths)


def _snap_first_difference(expected: bytes, actual: bytes) -> str:
    """Names the first line, counting from 1, at which actual differs from expected."""

    def show(line: bytes | None) -> str:
        if line is None:
            return "nothing (it ends before this line)"
        return repr(line.decode("utf-8", "backslashreplace"))

    pairs = itertools.zip_longest(io.BytesIO(expected).readlines(), io.BytesIO(actual).readlines())
    for number, (want, got) in enumerate(pairs, start=1):
        if want != got:
            return f"line {number}: the fixture has {show(want)}, the generator printed {show(got)}"
    return "no line differs"


@pytest.mark.skipif(
    shutil.which("git") is None,
    reason="git is missing; the generator reads commit a746fda with git show",
)
@pytest.mark.skipif(
    shutil.which(_SNAP_BASH) is None,
    reason="/bin/bash is missing; the generator is tested under it",
)
class TestSelectionSnapshotGenerator:
    """The generator prints the Selection_Snapshot of commit a746fda (design §M11)."""

    @pytest.fixture(scope="class")
    @classmethod
    def generator_run(cls, tmp_path_factory: pytest.TempPathFactory) -> _SnapRun:
        """One run from the repo root, as in the generator's usage line, with an empty HOME and an
        empty TMPDIR. The tests share it because a run takes several seconds under bash 3.2."""
        home, tmpdir = _snap_sandbox(tmp_path_factory.mktemp("snapshot-run"))
        before = _snap_work_tree()
        result = _snap_run(_SNAP_GENERATOR, cwd=_SNAP_REPO, home=home, tmpdir=tmpdir)
        after = _snap_work_tree()
        return _SnapRun(result, home, tmpdir, before, after)

    def test_exits_0_with_empty_stderr(self, generator_run: _SnapRun) -> None:
        result = generator_run.result
        stderr = result.stderr.decode("utf-8", "backslashreplace")
        assert result.returncode == 0, f"exit status {result.returncode}; stderr:\n{stderr}"
        assert stderr == "", "the generator wrote to stderr"

    def test_reproduces_the_fixture_byte_for_byte(self, generator_run: _SnapRun) -> None:
        # Every input comes from commit a746fda, not from the work tree, so every run prints
        # the committed bytes.
        expected = (_SNAP_REPO / _SNAP_FIXTURE).read_bytes()
        actual = generator_run.result.stdout
        if actual != expected:
            pytest.fail(
                f"the generator's stdout differs from {_SNAP_FIXTURE}, first at "
                + _snap_first_difference(expected, actual)
            )

    def test_leaves_the_work_tree_unchanged(self, generator_run: _SnapRun) -> None:
        before, after = generator_run.before, generator_run.after
        assert after.status == before.status, "git status changed during the run"
        assert after.paths == before.paths, "a path in the work tree changed during the run"

    def test_creates_nothing_outside_the_temp_folder(
        self, generator_run: _SnapRun, tmp_path: Path
    ) -> None:
        assert os.listdir(generator_run.home) == [], "the run wrote into HOME"
        leftovers = [
            name for name in os.listdir(generator_run.tmpdir) if name.startswith(_SNAP_TEMP_PREFIX)
        ]
        assert leftovers == [], "the generator did not remove its temp dir from TMPDIR"
        # That check counts only if the temp dir is made under TMPDIR: with TMPDIR naming a
        # file, the generator must fail to make it, and fail before printing anything.
        not_a_dir = tmp_path / "not-a-dir"
        not_a_dir.touch()
        home = tmp_path / "home"
        home.mkdir()
        result = _snap_run(_SNAP_GENERATOR, cwd=_SNAP_REPO, home=home, tmpdir=not_a_dir)
        stderr = result.stderr.decode("utf-8", "backslashreplace")
        assert result.returncode != 0, "the generator ignored a TMPDIR that names a file"
        assert result.stdout == b"", "the generator printed on stdout although it failed"
        assert str(not_a_dir / _SNAP_TEMP_PREFIX) in stderr, stderr

    def test_fails_cleanly_outside_a_git_clone(self, tmp_path: Path) -> None:
        script = tmp_path / "copy" / "snapshot_v6_selection.sh"
        script.parent.mkdir()
        shutil.copyfile(_SNAP_REPO / _SNAP_GENERATOR, script)
        home, tmpdir = _snap_sandbox(tmp_path)
        # git searches for a repo from the copy's dir upwards but never enters tmp_path, so the
        # copy is in no clone wherever the system temp folder is
        result = _snap_run(
            script,
            cwd=script.parent,
            home=home,
            tmpdir=tmpdir,
            GIT_CEILING_DIRECTORIES=str(tmp_path),
        )
        stderr = result.stderr.decode("utf-8", "backslashreplace")
        assert result.returncode != 0, "the generator exited 0 outside a git clone"
        assert result.stdout == b"", "the generator printed on stdout outside a git clone"
        assert "snapshot_v6_selection: cannot read commit" in stderr, stderr


# ---- scripts/provision.sh --selection, the read-only Selection mode (Req 1.4, 1.7, 1.9, 1.10, 27.6) ----
# The _sel helpers below serve only the Selection-mode tests.

_SEL_SCRIPT = "scripts/provision.sh"  # repo-relative, as in its usage lines
_SEL_CONFIG = REPO / "config"  # the CONFIG_DIR that a run from this checkout reads by default
_SEL_FIXTURE = "tests/fixtures/selection-v6.txt"
_SEL_SECTIONS = ("[models.list]", "[nodes.txt]", "[ollama-models.txt]")  # design §M10, in this order
_SEL_MACOS_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"  # macOS's own command-line tools only (Req 1.7)
_SEL_UTF8 = "en_US.UTF-8" if sys.platform == "darwin" else "C.UTF-8"
# Req 1.4's Existing_Ids, in the Selection_Snapshot's order; `all` is the full run.
_SEL_EXISTING_IDS = (
    "qwen21", "qwen21-edit", "krea2", "edit-2509", "krea-style", "wan-i2v", "minimax-h3", "wan-animate",
    "global", "flux",
)  # fmt: skip
_SEL_NEWER_BASHES = ("/opt/homebrew/bin/bash", "/usr/local/bin/bash")  # looked at besides the host PATH's bash
_SEL_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Left out of the checkout listing: git's internals, and what pytest and the Check_Suite write while tests run.
_SEL_UNLISTED = frozenset({".git", ".check-venv", ".pytest_cache", ".hypothesis", "__pycache__"})
# Req 1.10's runs: each branch of selected() (a full run, global, one id) and the unknown-id branch, with the
# exit status each must end with. Every id takes the same code path; krea2 selects lines from all three lists.
_SEL_READ_ONLY_RUNS = (("all", 0), ("global", 0), ("krea2", 0), ("no-such-id", 1))


def _sel_catalog_ids() -> list[str]:
    """The ids that provision.sh's catalog_ids prints: the text before the first `|` of each Catalog line that
    is neither blank nor a comment (its grep -vE '^[[:space:]]*(#|$)' in the C locale)."""
    text = (_SEL_CONFIG / "catalog.txt").read_bytes().decode("utf-8", "surrogateescape")
    ids = []
    for line in text.split("\n"):
        body = line.lstrip(" \t\v\f\r")
        if body and not body.startswith("#"):
            ids.append(line.split("|", 1)[0])
    return ids


# Req 1.7's runs: every Catalog id, then `global` (which the Catalog lists anyway) and the full run.
_SEL_RUNS = tuple(dict.fromkeys([*_sel_catalog_ids(), "global", "all"]))


class _SelRunner:
    """Runs `bash scripts/provision.sh --selection <run>` from the checkout and keeps every result, so the tests
    share the runs (about half a second each under bash 3.2).

    A run's whole environment is PATH, an empty HOME and TMPDIR, and for the "host" variant a UTF-8 locale.
    Nothing else is inherited, so a WORKSPACE, CONFIG_DIR or WORKFLOW in the maintainer's shell cannot reach a
    run: CONFIG_DIR is the checkout's config/, and WORKSPACE is /workspace, which the Mac does not have.
    """

    def __init__(self, root: Path) -> None:
        self.home = root / "home"
        self.tmpdir = root / "tmp"
        self.home.mkdir()
        self.tmpdir.mkdir()
        self._results: dict[tuple[str, str, str], subprocess.CompletedProcess[bytes]] = {}

    def environment(self, variant: str) -> dict[str, str]:
        env = {"PATH": _SEL_MACOS_PATH, "HOME": str(self.home), "TMPDIR": str(self.tmpdir)}
        if variant == "host":  # the PATH of the process running the tests, in a UTF-8 locale
            env.update(PATH=os.environ.get("PATH") or os.defpath, LANG=_SEL_UTF8, LC_ALL=_SEL_UTF8)
        elif variant != "macos":
            raise ValueError(f"unknown variant {variant!r}")
        return env

    def run(
        self, run_id: str, *, bash: str = BASH, variant: str = "macos"
    ) -> subprocess.CompletedProcess[bytes]:
        key = (bash, variant, run_id)
        if key not in self._results:
            self._results[key] = run_bounded(
                [bash, _SEL_SCRIPT, "--selection", run_id],
                cwd=REPO,
                env=self.environment(variant),
                timeout=60,
            )
        return self._results[key]


def _sel_sections(output: bytes, what: str) -> dict[str, list[str]]:
    """The sections of a Selection printout: header -> lines, in order.

    Fails the calling test, naming `what`, unless the printout has the design §M10 layout: the three headers
    in their fixed order, each followed by its lines, and a newline at the end.
    """
    text = output.decode("utf-8", "surrogateescape")
    lines = text[:-1].split("\n") if text.endswith("\n") else []
    headers = [line for line in lines if line in _SEL_SECTIONS]
    if not lines or lines[0] != _SEL_SECTIONS[0] or headers != list(_SEL_SECTIONS):
        pytest.fail(f"{what} does not have the §M10 layout (three headers in order, a final newline):\n{text}")
    sections: dict[str, list[str]] = {}
    items: list[str] = []
    for line in lines:
        if line in _SEL_SECTIONS:
            items = sections.setdefault(line, [])
        else:
            items.append(line)
    return sections


def _sel_differences(expected: dict[str, list[str]], actual: dict[str, list[str]]) -> list[str]:
    """Each file, node pack or Ollama model line that actual has and expected lacks, or the reverse, in
    printout order; the order of the lines themselves does not count (Req 1.4)."""
    problems = []
    for section in _SEL_SECTIONS:
        want, got = set(expected[section]), set(actual[section])
        problems += [f"  {section} extra:   {item}" for item in dict.fromkeys(actual[section]) if item not in want]
        problems += [f"  {section} missing: {item}" for item in dict.fromkeys(expected[section]) if item not in got]
    return problems


def _sel_first_difference(first: bytes, second: bytes, first_name: str, second_name: str) -> str:
    """Names the first line, counting from 1, at which second differs from first."""

    def show(line: bytes | None) -> str:
        if line is None:
            return "nothing (it ends before this line)"
        return repr(line.decode("utf-8", "backslashreplace"))

    pairs = itertools.zip_longest(io.BytesIO(first).readlines(), io.BytesIO(second).readlines())
    for number, (one, two) in enumerate(pairs, start=1):
        if one != two:
            return f"line {number}: {first_name} printed {show(one)}, {second_name} printed {show(two)}"
    return "no line differs"


def _sel_bash_version(bash: str) -> tuple[int, ...] | None:
    """The version of the bash at `bash` as (major, minor, patch), or None when it does not run."""
    script = 'printf "%s %s %s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}" "${BASH_VERSINFO[2]}"'
    try:
        result = run_bounded([bash, "-c", script], env={"PATH": _SEL_MACOS_PATH}, timeout=30)
        return tuple(int(part) for part in result.stdout.split()) if result.returncode == 0 else None
    except (OSError, ValueError):
        return None


def _sel_tree(
    root: Path, *, unlisted_names: frozenset[str] = frozenset(), unlisted: tuple[Path, ...] = ()
) -> dict[str, tuple[int, int]]:
    """Every path below root, root included, with its size and mtime in ns (lstat), except the names in
    unlisted_names and the folders in unlisted, with everything below them.

    Directories are listed too: making or removing an entry changes its directory's mtime, so a file that a
    run creates and then deletes still shows.
    """
    skipped = {os.path.normpath(folder) for folder in unlisted}
    info = os.lstat(root)
    paths = {".": (info.st_size, info.st_mtime_ns)}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in unlisted_names and os.path.normpath(os.path.join(dirpath, name)) not in skipped
        ]
        for name in dirnames + [name for name in filenames if name not in unlisted_names]:
            path = os.path.join(dirpath, name)
            info = os.lstat(path)
            paths[os.path.relpath(path, root)] = (info.st_size, info.st_mtime_ns)
    return paths


def _sel_tree_changes(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]]) -> list[str]:
    return sorted(
        [f"created {path}" for path in after.keys() - before.keys()]
        + [f"deleted {path}" for path in before.keys() - after.keys()]
        + [f"changed {path}" for path in before.keys() & after.keys() if before[path] != after[path]]
    )


def _sel_sandbox_profile(tmpdir: Path) -> str:
    """A sandbox-exec profile refusing every network operation and every file write outside tmpdir and /dev
    (bash opens /dev/null for writing). The sandbox checks resolved paths, so tmpdir is resolved first."""
    allowed = os.path.realpath(tmpdir).replace("\\", "\\\\").replace('"', '\\"')
    return (
        "(version 1)(allow default)(deny network*)(deny file-write*)"
        f'(allow file-write* (subpath "/dev") (subpath "{allowed}"))'
    )


def _sel_influencer_selection(runner: _SelRunner) -> dict[str, list[str]]:
    """The influencer Selection that Req 1.4 adds to the full run's snapshot lines.

    Until task 16 adds the `influencer` Catalog line, `--selection influencer` is an unknown id and the
    Selection is empty. That holds only while the Catalog lacks the line: once it lists the id, the run must
    succeed and its lines count.
    """
    result = runner.run("influencer")
    stderr = result.stderr.decode("utf-8", "backslashreplace")
    if "influencer" not in _sel_catalog_ids():
        assert result.returncode == 1, (
            f"the Catalog has no influencer line, yet --selection influencer exited {result.returncode}; "
            f"stderr:\n{stderr}"
        )
        return {section: [] for section in _SEL_SECTIONS}
    assert result.returncode == 0, f"--selection influencer exited {result.returncode}; stderr:\n{stderr}"
    return _sel_sections(result.stdout, "--selection influencer")


@pytest.fixture(scope="module")
def sel_runner(tmp_path_factory: pytest.TempPathFactory) -> _SelRunner:
    if not os.access(BASH, os.X_OK):
        pytest.skip(f"{BASH} is missing; the Selection mode is tested under it")
    return _SelRunner(tmp_path_factory.mktemp("selection-runs"))


@pytest.fixture(scope="module")
def sel_newer_bash() -> str:
    """The newest bash on this host that is newer than /bin/bash, standing in for the Instance's bash 5, or a
    skip that says there is none."""
    base = _sel_bash_version(BASH)
    found = []
    for path in dict.fromkeys([*_SEL_NEWER_BASHES, shutil.which("bash") or ""]):
        if not path or not os.access(path, os.X_OK) or os.path.realpath(path) == os.path.realpath(BASH):
            continue
        version = _sel_bash_version(path)
        if version is not None and base is not None and version > base:
            found.append((version, path))
    if not found:
        shown = ".".join(str(part) for part in base) if base else "unknown"
        pytest.skip(
            f"no bash newer than {BASH} ({shown}) on this host, so the comparison with the Instance's bash 5 "
            "is left out (brew install bash provides one)"
        )
    return max(found)[1]


@pytest.fixture(scope="module")
def sel_snapshot() -> dict[str, dict[str, list[str]]]:
    """The runs that tests/fixtures/selection-v6.txt records: run id -> §M10 section -> lines."""
    text = (REPO / _SEL_FIXTURE).read_bytes().decode("utf-8")
    if not text.startswith("# Selection_Snapshot of v6 (a746fda…)") or not text.endswith("\n"):
        pytest.fail(f"{_SEL_FIXTURE} is not a Selection_Snapshot")
    blocks: dict[str, list[str]] = {}
    block: list[str] = []
    for line in text[:-1].split("\n")[1:]:
        if line.startswith("### run: "):
            block = blocks.setdefault(line[len("### run: "):], [])
        elif not blocks:
            pytest.fail(f"{_SEL_FIXTURE} has a line before its first run: {line!r}")
        else:
            block.append(line)
    assert list(blocks) == [*_SEL_EXISTING_IDS, "all"], f"{_SEL_FIXTURE} records the runs {list(blocks)}"
    return {
        run_id: _sel_sections("".join(f"{line}\n" for line in lines).encode(), f"{_SEL_FIXTURE}, run {run_id}")
        for run_id, lines in blocks.items()
    }


@pytest.mark.parametrize("run_id", _SEL_RUNS)
def test_selection_prints_the_same_bytes_in_both_environments(run_id: str, sel_runner: _SelRunner) -> None:
    """Req 1.7 under /bin/bash: the macOS run (macOS's own tools on PATH, no /workspace) and a second run with
    the host's PATH in a UTF-8 locale both exit 0 and print the same bytes, in the §M10 layout."""
    if os.path.lexists("/workspace"):
        pytest.skip("/workspace exists on this host, and Req 1.7's macOS run has none")
    macos = sel_runner.run(run_id)
    host = sel_runner.run(run_id, variant="host")
    for name, result in (("the macOS run", macos), ("the run with the host's PATH and a UTF-8 locale", host)):
        stderr = result.stderr.decode("utf-8", "backslashreplace")
        assert result.returncode == 0, f"{name} of --selection {run_id} exited {result.returncode}:\n{stderr}"
        assert stderr == "", f"{name} of --selection {run_id} wrote to stderr:\n{stderr}"
    _sel_sections(macos.stdout, f"--selection {run_id}")
    if host.stdout != macos.stdout:
        pytest.fail(
            f"--selection {run_id} printed other bytes with the host's PATH and a UTF-8 locale, first at "
            + _sel_first_difference(macos.stdout, host.stdout, "the macOS run", "the host run")
        )
    assert os.listdir(sel_runner.home) == [], "a run wrote into HOME"
    assert os.listdir(sel_runner.tmpdir) == [], "a run left files in TMPDIR"


def test_selection_prints_the_same_bytes_under_a_newer_bash(sel_runner: _SelRunner, sel_newer_bash: str) -> None:
    """Req 1.7: a newer bash, standing in for the Instance's, exits 0 and prints the bytes /bin/bash prints, for
    every Catalog id, global and the full run, each with the macOS run's environment."""
    problems = []
    for run_id in _SEL_RUNS:
        old, new = sel_runner.run(run_id), sel_runner.run(run_id, bash=sel_newer_bash)
        if (old.returncode, new.returncode) != (0, 0):
            problems.append(
                f"--selection {run_id}: exit {old.returncode} under {BASH}, {new.returncode} under {sel_newer_bash}"
            )
        elif new.stdout != old.stdout:
            difference = _sel_first_difference(old.stdout, new.stdout, BASH, sel_newer_bash)
            problems.append(f"--selection {run_id}: first at {difference}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    "bad_id",
    ["no-such-id", "krea", "krea*", "qwen21\nqwen21-edit", ""],
    ids=["unlisted", "prefix-of-two-ids", "glob", "two-ids-on-two-lines", "empty"],
)
def test_selection_of_an_unknown_id_fails_and_prints_no_selection(bad_id: str, sel_runner: _SelRunner) -> None:
    """Req 1.9: exit 1, nothing on stdout, and on stderr the error naming the id, then the Catalog's ids."""
    result = sel_runner.run(bad_id)
    stderr = result.stderr.decode("utf-8", "backslashreplace")
    assert result.returncode == 1, f"exit status {result.returncode}; stderr:\n{stderr}"
    assert result.stdout == b"", "an unknown id printed on stdout"
    lead, found, listing = stderr.partition(f"unknown workflow '{bad_id}' — available:\n")
    assert found and "\n" not in lead, f"stderr does not open with the error naming {bad_id!r}:\n{stderr}"
    listed = [line.split()[0] for line in listing.splitlines() if line.strip()]
    assert listed == _sel_catalog_ids(), f"stderr does not list the Catalog ids after the error:\n{stderr}"


def test_selection_calls_no_network_tool_and_changes_no_file(
    stub_path: StubPath, temp_env: TempEnv, sel_runner: _SelRunner
) -> None:
    """Req 1.10 with the stub PATH of task 3.4: no stub is called (so no curl, wget, hf, git or ollama), the
    checkout and the temp env (WORKSPACE, COMFYUI_DIR, HOME, ...) keep every name, size and mtime, and TMPDIR
    holds nothing afterwards."""
    expected = {run_id: sel_runner.run(run_id).stdout for run_id, status in _SEL_READ_ONLY_RUNS if status == 0}
    env_unlisted = (temp_env.tmp,)  # these runs' system temp folder, where Req 1.10 allows files
    env_before = _sel_tree(temp_env.root, unlisted=env_unlisted)
    repo_before = _sel_tree(REPO, unlisted_names=_SEL_UNLISTED)
    results = {
        run_id: stub_path.run(
            _SEL_SCRIPT, "--selection", run_id, extra_env={"CONFIG_DIR": str(_SEL_CONFIG)}, timeout=60
        )
        for run_id, _ in _SEL_READ_ONLY_RUNS
    }
    repo_after = _sel_tree(REPO, unlisted_names=_SEL_UNLISTED)
    env_after = _sel_tree(temp_env.root, unlisted=env_unlisted)
    for run_id, status in _SEL_READ_ONLY_RUNS:
        result = results[run_id]
        stderr = result.stderr.decode("utf-8", "backslashreplace")
        assert result.returncode == status, f"--selection {run_id} exited {result.returncode}:\n{stderr}"
        if status == 0:
            assert result.stdout == expected[run_id], f"--selection {run_id} printed other bytes with the stubs"
    assert stub_path.calls() == [], "--selection called:\n" + "\n".join(stub_path.log_lines())
    changes = _sel_tree_changes(repo_before, repo_after)
    assert not changes, (
        "the checkout changed during the runs (a process editing it meanwhile shows here too):\n"
        + "\n".join(changes)
    )
    changes = _sel_tree_changes(env_before, env_after)
    assert not changes, "the temp env changed during the runs:\n" + "\n".join(changes)
    assert os.listdir(temp_env.tmp) == [], f"leftovers in TMPDIR: {sorted(os.listdir(temp_env.tmp))}"


def test_selection_runs_in_a_sandbox_without_network_or_writes(tmp_path: Path, sel_runner: _SelRunner) -> None:
    """Req 1.10 on macOS: under sandbox-exec, with every network operation and every file write outside TMPDIR
    and /dev refused, --selection ends with the same status and prints the same bytes as without it. A bash
    3.2 here-string fails in this sandbox, because bash 3.2 writes it to a temp file in /var/tmp or /tmp."""
    if sys.platform != "darwin" or not os.access(_SEL_SANDBOX_EXEC, os.X_OK):
        pytest.skip(f"{_SEL_SANDBOX_EXEC} is missing (it is macOS's)")
    home, tmpdir = tmp_path / "home", tmp_path / "tmp"
    home.mkdir()
    tmpdir.mkdir()
    profile = _sel_sandbox_profile(tmpdir)
    env = {"PATH": _SEL_MACOS_PATH, "HOME": str(home), "TMPDIR": str(tmpdir)}

    def sandboxed(*argv: str) -> subprocess.CompletedProcess[bytes]:
        return run_bounded([_SEL_SANDBOX_EXEC, "-p", profile, *argv], cwd=REPO, env=env, timeout=60)

    probe = sandboxed("/usr/bin/true")
    if probe.returncode != 0:
        pytest.skip("sandbox-exec cannot apply a profile here: " + probe.stderr.decode("utf-8", "replace").strip())
    # The runs below prove something only if the profile bites: a write outside TMPDIR is refused.
    outside = tmp_path / "outside-tmpdir"
    control = sandboxed(BASH, "-c", ': > "$1"', "control", str(outside))
    assert control.returncode != 0 and not outside.exists(), "the sandbox let a write outside TMPDIR through"
    for run_id, status in _SEL_READ_ONLY_RUNS:
        result = sandboxed(BASH, _SEL_SCRIPT, "--selection", run_id)
        stderr = result.stderr.decode("utf-8", "backslashreplace")
        assert result.returncode == status, f"sandboxed --selection {run_id} exited {result.returncode}:\n{stderr}"
        want = sel_runner.run(run_id).stdout if status == 0 else b""
        assert result.stdout == want, f"sandboxed --selection {run_id} printed other bytes"
    assert os.listdir(home) == [], "a sandboxed run wrote into HOME"
    assert os.listdir(tmpdir) == [], f"leftovers in TMPDIR: {sorted(os.listdir(tmpdir))}"


@pytest.mark.parametrize("run_id", (*_SEL_EXISTING_IDS, "all"))
def test_selection_matches_the_selection_snapshot(
    run_id: str, sel_runner: _SelRunner, sel_snapshot: dict[str, dict[str, list[str]]]
) -> None:
    """Req 1.4 and 27.6: for each Existing_Id and global, --selection prints the lines that the
    Selection_Snapshot records for that run; for the full run, the snapshot's full-run lines plus the
    influencer Selection. Line order is ignored, and neither side carries a wf: tag, so only tags may differ."""
    result = sel_runner.run(run_id)
    stderr = result.stderr.decode("utf-8", "backslashreplace")
    assert result.returncode == 0, f"--selection {run_id} exited {result.returncode}; stderr:\n{stderr}"
    actual = _sel_sections(result.stdout, f"--selection {run_id}")
    expected = sel_snapshot[run_id]
    source = f"the run {run_id!r} of {_SEL_FIXTURE}"
    if run_id == "all":
        influencer = _sel_influencer_selection(sel_runner)
        expected = {section: expected[section] + influencer[section] for section in _SEL_SECTIONS}
        source += " plus the influencer Selection"
    problems = _sel_differences(expected, actual)
    if problems:
        pytest.fail(f"run {run_id!r}: --selection {run_id} differs from {source}:\n" + "\n".join(problems))
