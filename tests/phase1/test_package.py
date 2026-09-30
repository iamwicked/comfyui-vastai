"""Unit tests of the package skeleton, scripts/influencer/ (task 5, design §C6, §A11; Req 25.6, 25.7, 25.10).

- the parser: every Phase 1 command, the common options, usage errors, and exit statuses 0 to 4
- env.py: the Paths table defaults, the overrides, Instance or Mac, and the Influencer_Root rule
- errors.py: Problem, ValidationError and their formatting
- yamlio.py: the §A11 styles, the round trip, and load_yaml's Problems
- atomic.py and locks.py, with real subprocesses as the concurrent writer and the lock holder
- the guard: Python 3.9 (the Mac's /usr/bin/python3) and a venv without PyYAML, Pillow or NumPy each exit 2
  with a message naming the version or the packages, and an audit hook in the run sees no file written,
  no socket and no process started
"""
from __future__ import annotations

import dataclasses
import os
import platform
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from helpers.harness import REPO, run_bounded
from strategies import texts

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

import influencer  # noqa: E402
from influencer import __main__ as cli  # noqa: E402
from influencer import env  # noqa: E402
from influencer.atomic import write_atomic  # noqa: E402
from influencer.errors import EXIT_MEANINGS, EnvironmentFailure, Exit, Problem, ValidationError  # noqa: E402
from influencer.locks import LockHeld, ProcessLock, read_pid  # noqa: E402
from influencer.yamlio import dump_yaml, load_yaml  # noqa: E402

LAUNCHER = SCRIPTS / "influencer.sh"
BASH = "/bin/bash"
MAC_PYTHON = Path("/usr/bin/python3")

# Every Phase 1 command: the argv that reaches its handler, and the task that implements it.
PHASE_1 = {
    "new-calendar": (["new-calendar", "--week", "2026-W40"], 9),
    "plan": (["plan", "--week", "2026-W40"], 21),
    "render": (["render", "--week", "2026-W40"], 20),
    "finish": (["finish", "--week", "2026-W40"], 20),
    "export": (["export", "--week", "2026-W40"], 23),
    "persona check": (["persona", "check"], 8),
    "calendar check": (["calendar", "check", "--week", "2026-W40"], 9),
    "check-config": (["check-config"], 25),
    "disk": (["disk", "--id", "influencer"], 26),
}


@pytest.fixture(autouse=True)
def _plain_usage_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """Usage text without colour (Python 3.14) and wide enough not to wrap."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")


def _with_handler(name: str, handler: cli.Handler) -> tuple[cli.Command, ...]:
    return tuple(dataclasses.replace(c, handler=handler) if c.name == name else c for c in cli.COMMANDS)


def _child_env(**extra: str) -> dict[str, str]:
    """The environment of a Python subprocess that imports the package from this checkout."""
    environment = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(SCRIPTS), "PYTHONDONTWRITEBYTECODE": "1"}
    environment.update(extra)
    return environment


def _wait_for(condition: Callable[[], bool], what: str, process: subprocess.Popen[bytes], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if process.poll() is not None:
            pytest.fail(f"the subprocess exited ({process.returncode}) before {what}: {process.stderr.read()!r}")
        if time.monotonic() > deadline:
            pytest.fail(f"{what} did not happen within {timeout:g} s")
        time.sleep(0.02)


def _kill(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


# ---- the parser (task 5.2) ----


def test_the_table_holds_exactly_the_phase_1_commands_and_their_tasks() -> None:
    assert {c.name: c.task for c in cli.COMMANDS} == {name: task for name, (_, task) in PHASE_1.items()}
    assert set(env.PACKAGES) == {"yaml", "PIL", "numpy"}
    assert all(set(c.needs) <= set(env.PACKAGES) for c in cli.COMMANDS)
    assert [c.needs for c in cli.COMMANDS if c.name == "disk"] == [()]  # stdlib only (§C5)


@pytest.mark.parametrize(
    ("argv", "name", "values"),
    [
        (["new-calendar", "--week", "2026-W40", "--csv"], "new-calendar", {"week": "2026-W40", "csv": True}),
        (["plan", "--week", "W", "--replan", "p1, p2"], "plan", {"replan": ["p1", "p2"]}),
        (["plan", "--week", "W"], "plan", {"replan": []}),
        (["render", "--week", "W", "--dry-run", "--only", "a,b"], "render", {"dry_run": True, "only": ["a", "b"]}),
        (["render", "--week", "W"], "render", {"dry_run": False, "only": None}),
        (["finish", "--week", "W", "--dry-run"], "finish", {"dry_run": True}),
        (["export", "--week", "W", "--external", "r1=/a b.mp4", "r2=x", "--external", "r3=y"], "export",
         {"external": [("r1", "/a b.mp4"), ("r2", "x"), ("r3", "y")]}),
        (["export", "--week", "W"], "export", {"external": []}),
        (["persona", "check"], "persona check", {}),
        (["calendar", "check", "--week", "W"], "calendar check", {"week": "W"}),
        (["check-config"], "check-config", {}),
        (["disk", "--id", "influencer", "--id", "flux", "--brief"], "disk",
         {"ids": ["influencer", "flux"], "base": None, "brief": True}),
        (["disk", "--base", "krea/Krea-2-Raw"], "disk", {"ids": None, "base": "krea/Krea-2-Raw", "brief": False}),
    ],
)  # fmt: skip
def test_each_phase_1_command_parses_to_its_table_entry(argv: list[str], name: str, values: dict) -> None:
    args = cli.build_parser().parse_args(argv)
    assert args.command_spec.name == name
    assert (args.persona, args.home) == (None, None)
    assert {key: getattr(args, key) for key in values} == values


@pytest.mark.parametrize(
    "argv",
    [
        ["--persona", "mara-lind", "--home", "/h", "plan", "--week", "W"],
        ["plan", "--week", "W", "--persona", "mara-lind", "--home", "/h"],
        ["--persona", "zoe", "--home", "/h", "persona", "check", "--persona", "mara-lind"],
    ],
    ids=["before", "after", "after-wins"],
)
def test_persona_and_home_go_before_or_after_the_command(argv: list[str]) -> None:
    args = cli.build_parser().parse_args(argv)
    assert (args.persona, args.home) == ("mara-lind", "/h")


@pytest.mark.parametrize(
    "argv",
    [
        [], ["bogus"], ["persona"], ["persona", "bogus"], ["plan"], ["plan", "--week"],
        ["plan", "--week", "W", "--replan", "a,,b"], ["render", "--week", "W", "--bogus"],
        ["export", "--week", "W", "--external", "no-equals"], ["disk"], ["disk", "--id", "a", "--base", "b"],
        ["check-config", "--persona", "Not_A_Slug"], ["--persona", "../escape", "check-config"],
        ["check-config", "--pers", "mara"], ["check-config", "extra"],
    ],
)  # fmt: skip
def test_an_unknown_command_or_a_usage_error_exits_1_with_the_usage_text(argv: list[str], capsys) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(argv)
    assert exited.value.code == Exit.USAGE
    stderr = capsys.readouterr().err
    assert stderr.startswith("usage: influencer.sh") and "error:" in stderr, stderr


def test_help_exits_0_and_lists_every_command_and_exit_status(capsys) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["--help"])
    assert exited.value.code == Exit.OK
    out = capsys.readouterr().out
    for word in ("new-calendar", "plan", "render", "finish", "export", "persona", "calendar", "check-config", "disk"):
        assert f"\n    {word} " in out, word
    for code, meaning in EXIT_MEANINGS.items():
        assert f"\n  {int(code)}  {meaning}\n" in out + "\n"


@pytest.mark.parametrize("name", PHASE_1)
def test_a_command_without_its_handler_exits_2_naming_its_task(name: str, capsys) -> None:
    """A command whose task has not landed never passes; once the task attaches its handler, the handler
    must import and be callable."""
    argv, task = PHASE_1[name]
    [command] = [c for c in cli.COMMANDS if c.name == name]
    if command.handler is not None:
        assert callable(cli._handler(command))
        return
    assert cli.main(argv) == Exit.ENVIRONMENT
    assert capsys.readouterr() == ("", f"{name}: not implemented yet (task {task})\n")


@pytest.mark.parametrize("status", [Exit.OK, Exit.FAILED, 0, 3])
def test_the_handler_status_is_the_exit_status(status: int) -> None:
    seen = []

    def handler(args) -> int:
        seen.append((args.command_spec.name, args.week, args.persona))
        return status

    assert cli.main(["finish", "--week", "W", "--persona", "mara-lind"], _with_handler("finish", handler)) == status
    assert seen == [("finish", "W", "mara-lind")]


def test_a_validation_error_exits_1_with_every_problem(capsys) -> None:
    problems = [Problem("/r/persona.yaml", "age", None, "must be 25 to 99"), Problem("/r/persona.yaml", None, 7, "bad")]

    def handler(args) -> int:
        raise ValidationError(problems)

    assert cli.main(["persona", "check"], _with_handler("persona check", handler)) == Exit.USAGE
    assert capsys.readouterr().err == "/r/persona.yaml: age: must be 25 to 99\n/r/persona.yaml: line 7: bad\n"


def test_an_environment_failure_exits_2(capsys) -> None:
    def handler(args) -> int:
        raise EnvironmentFailure("ComfyUI at http://127.0.0.1:18188 did not answer")

    assert cli.main(["render", "--week", "W"], _with_handler("render", handler)) == Exit.ENVIRONMENT
    assert capsys.readouterr().err == "render: ComfyUI at http://127.0.0.1:18188 did not answer\n"


def test_a_held_lock_exits_4_with_the_holders_pid(tmp_path: Path, capsys) -> None:
    path = tmp_path / ".lock"

    def handler(args) -> int:
        with ProcessLock(path):
            return Exit.OK

    with ProcessLock(path):  # flock refuses a second open file description, even in this process
        status = cli.main(["plan", "--week", "W"], _with_handler("plan", handler))
    assert status == Exit.LOCKED
    assert capsys.readouterr().err == f"plan: another process holds the lock {path} (PID {os.getpid()})\n"


def test_the_exit_statuses_are_defined_once() -> None:
    assert [(e.name, int(e)) for e in Exit] == [("OK", 0), ("USAGE", 1), ("ENVIRONMENT", 2), ("FAILED", 3), ("LOCKED", 4)]
    assert list(EXIT_MEANINGS) == list(Exit)
    assert influencer.GUARD_EXIT == Exit.ENVIRONMENT  # the guard cannot import errors.py, so it keeps a copy


# ---- env.py (task 5.3) ----


def test_the_mac_layout_applies_without_the_workspace_folder(tmp_path: Path) -> None:
    missing = tmp_path / "no-workspace"
    paths = env.paths({"WORKSPACE": str(missing)})
    assert not paths.on_instance
    assert (paths.home, paths.output_parent) == (REPO / "influencer", REPO / "influencer" / "_output")
    assert (paths.config_dir, paths.repo) == (REPO / "config", REPO)
    assert (paths.comfyui_url, paths.ollama_url) == ("http://127.0.0.1:18188", "http://127.0.0.1:11434")
    with pytest.raises(EnvironmentFailure, match=str(missing)):
        paths.require_workspace()
    assert not missing.exists()  # reported, never created


def test_the_instance_layout_applies_with_the_workspace_folder(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = env.paths({"WORKSPACE": str(workspace)})
    assert paths.on_instance and paths.require_workspace() == workspace
    assert paths.home == workspace / "influencer"
    assert paths.models_dir == workspace / "models"
    assert paths.comfyui_dir == Path("/workspace/ComfyUI")  # as scripts/common.sh: not derived from WORKSPACE
    assert paths.output_parent == Path("/workspace/ComfyUI/output/influencer")


def test_the_package_paths_match_the_test_harness_layout(temp_env) -> None:
    (temp_env.influencer_root / "persona.yaml").write_text(f"slug: {temp_env.slug}\n", encoding="utf-8")
    paths = env.paths()  # temp_env set WORKSPACE, COMFYUI_DIR, CONFIG_DIR, INFLUENCER_HOME, INFLUENCER_OUTPUT
    assert paths.on_instance
    assert (paths.workspace, paths.comfyui_dir, paths.config_dir) == (temp_env.workspace, temp_env.comfyui_dir,
                                                                      temp_env.config_dir)  # fmt: skip
    assert paths.influencer_root() == temp_env.influencer_root == paths.home / temp_env.slug
    assert paths.output_root(temp_env.slug) == temp_env.output_root


def test_every_override_and_its_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    environ = {
        "WORKSPACE": str(tmp_path / "none"),
        "COMFYUI_DIR": str(tmp_path / "comfy"),
        "MODELS_DIR": str(tmp_path / "models"),
        "MODELS_PATH": str(tmp_path / "models-path"),
        "CONFIG_DIR": str(tmp_path / "config"),
        "INFLUENCER_HOME": str(tmp_path / "home"),
        "INFLUENCER_OUTPUT": str(tmp_path / "out"),
        "COMFYUI_URL": "http://127.0.0.1:8188",
        "OLLAMA_URL": "http://[::1]:11435",
    }
    paths = env.paths(environ)
    assert (paths.comfyui_dir, paths.models_dir, paths.config_dir) == (tmp_path / "comfy", tmp_path / "models",
                                                                       tmp_path / "config")  # fmt: skip
    assert (paths.home, paths.output_parent) == (tmp_path / "home", tmp_path / "out")
    assert (paths.comfyui_url, paths.ollama_url) == ("http://127.0.0.1:8188", "http://[::1]:11435")
    assert env.paths(environ, home=tmp_path / "flag").home == tmp_path / "flag"  # --home beats INFLUENCER_HOME
    assert env.paths({**environ, "MODELS_DIR": ""}).models_dir == tmp_path / "models-path"  # empty is unset
    assert env.paths({"WORKSPACE": str(tmp_path / "none"), "INFLUENCER_HOME": ""}).home == REPO / "influencer"
    monkeypatch.chdir(tmp_path)
    assert env.paths({**environ, "INFLUENCER_HOME": "relative"}).home == tmp_path / "relative"


def test_the_influencer_root_needs_a_slug_unless_the_home_holds_one_persona(tmp_path: Path) -> None:
    home = tmp_path / "home"
    paths = env.paths({"WORKSPACE": str(tmp_path / "none"), "INFLUENCER_HOME": str(home)})
    with pytest.raises(ValidationError, match="holds no persona folder"):
        paths.influencer_root()
    for folder, persona in (("mara-lind", True), ("_output", False), ("notes", False), ("Upper", True)):
        (home / folder).mkdir(parents=True)
        if persona:
            (home / folder / "persona.yaml").write_text("slug: x\n", encoding="utf-8")
    assert paths.influencer_root() == home / "mara-lind"  # _output, notes and Upper are not personas
    assert paths.influencer_root("zoe") == home / "zoe"
    (home / "zoe").mkdir()
    (home / "zoe" / "persona.yaml").write_text("", encoding="utf-8")
    with pytest.raises(ValidationError, match=r"holds 2 personas \(mara-lind, zoe\)"):
        paths.influencer_root()
    for bad in ("../escape", "", "a" * 33, "Upper"):
        with pytest.raises(ValidationError, match="--persona"):
            paths.influencer_root(bad)
    assert paths.output_root("mara-lind") == paths.output_parent / "mara-lind"


def test_the_import_check_names_each_missing_package() -> None:
    assert env.import_error("export", ("yaml", "PIL", "numpy")) is None  # this environment has all three
    message = env.import_error("export", ("yaml", "no_such_module_for_this_test", "numpy"))
    assert message is not None
    assert message.startswith("export: needs no_such_module_for_this_test, which Python ")
    assert "ModuleNotFoundError" in message and "INFLUENCER_PYTHON" in message


# ---- errors.py (task 5.4) ----


def test_problems_format_as_path_line_field_and_reason() -> None:
    problems = [
        Problem("persona.yaml", "age", None, "must be 25 to 99"),
        Problem("persona.yaml", None, 7, "YAML syntax error"),
        Problem("calendar.csv", "posts[2].date", 4, "not in the week"),
        Problem("persona.yaml", None, None, "cannot read the file"),
    ]
    lines = [
        "persona.yaml: age: must be 25 to 99",
        "persona.yaml: line 7: YAML syntax error",
        "calendar.csv: line 4: posts[2].date: not in the week",
        "persona.yaml: cannot read the file",
    ]
    assert [str(problem) for problem in problems] == lines
    error = ValidationError(iter(problems))
    assert error.problems == problems and str(error) == "\n".join(lines)
    with pytest.raises(dataclasses.FrozenInstanceError):
        problems[0].reason = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError):
        ValidationError([])


# ---- yamlio.py (task 5.5, design §A11) ----

YAML_VALUES = st.recursive(
    texts(),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(texts(), children, max_size=4),
    max_leaves=12,
)


@given(YAML_VALUES)
def test_dump_yaml_output_reads_back_equal(value: object) -> None:
    assert yaml.load(dump_yaml(value), Loader=yaml.SafeLoader) == value


def test_dump_yaml_prints_the_design_styles() -> None:
    value = {
        "voice": "Warm, dry.\nShort sentences.\n",
        "identity": "A woman\nwith freckles",
        "niche": "slow travel, specialty coffee",
        "emoji": "\U0001f600 hello",
        **{f"k{i}": v for i, v in enumerate(["no", "on", "y", "2026", "1e3", "0o17", "~", "2026-09-28", "", "#x",
                                             "a: b", "a #b", " lead", "trail ", "null", "- item"])},
        "tab": "a\tb\nc",
        "blank": "a\n\n",
        "list": ["plain", "no"],
        "long": "word " * 40 + "end",
    }  # fmt: skip
    text = dump_yaml(value)
    assert text.startswith(
        "voice: |\n  Warm, dry.\n  Short sentences.\n"
        "identity: |-\n  A woman\n  with freckles\n"
        "niche: slow travel, specialty coffee\n"
        "emoji: \U0001f600 hello\n"
        'k0: "no"\nk1: "on"\nk2: "y"\nk3: "2026"\nk4: "1e3"\nk5: "0o17"\nk6: "~"\nk7: "2026-09-28"\nk8: ""\n'
        'k9: "#x"\nk10: "a: b"\nk11: "a #b"\nk12: " lead"\nk13: "trail "\nk14: "null"\nk15: "- item"\n'
        'tab: "a\\tb\\nc"\nblank: "a\\n\\n"\n'
        'list:\n- plain\n- "no"\n'
    )
    assert f"long: {'word ' * 40}end\n" in text  # never wrapped
    assert yaml.safe_load(text) == value
    assert yaml.safe_dump("no") == "'no'\n"  # PyYAML's own SafeDumper is left as it was


def test_load_yaml_reads_a_printed_file(tmp_path: Path) -> None:
    value = {"name": "Mara Lind", "voice": "Warm.\nDry.\n", "themes": ["café", "no", "\U0001f469\U0001f3fd\u200d\U0001f4bb"]}
    path = tmp_path / "persona.yaml"
    write_atomic(path, dump_yaml(value))
    assert load_yaml(path) == value
    (tmp_path / "empty.yaml").write_bytes(b"")
    assert load_yaml(tmp_path / "empty.yaml") is None


@pytest.mark.parametrize(
    ("data", "line", "reason"),
    [
        (b"slug: mara\nage: 29\n  name: x\n", 3, "YAML syntax error (column 7): mapping values are not allowed here"),
        (b"slug: mara\nthemes: [a, b\nname: x\n", 3,
         "YAML syntax error (column 5): while parsing a flow sequence; expected ',' or ']', but got ':'"),
        (b"slug: mara\r\nname: \xff\n", 2, "not UTF-8 text: byte 0xff at byte offset 18"),
        (b"slug: mara\nname: a\x1bb\n", 2, "character U+001B is not allowed in YAML"),
    ],
    ids=["syntax", "unclosed", "utf-8", "control"],
)
def test_load_yaml_reports_a_file_that_does_not_load_with_its_line(tmp_path: Path, data: bytes, line: int, reason: str) -> None:
    path = tmp_path / "persona.yaml"
    path.write_bytes(data)
    with pytest.raises(ValidationError) as error:
        load_yaml(path)
    [problem] = error.value.problems
    assert (problem.path, problem.field, problem.line) == (str(path), None, line)
    assert problem.reason.startswith(reason), problem.reason


def test_load_yaml_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as error:
        load_yaml(tmp_path / "missing.yaml")
    assert error.value.problems == [Problem(str(tmp_path / "missing.yaml"), None, None,
                                            "cannot read the file: No such file or directory")]  # fmt: skip


# ---- atomic.py (task 5.6) ----


def test_write_atomic_writes_replaces_and_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "manifest.json"
    write_atomic(target, "first ✓\n")
    assert target.read_bytes() == "first ✓\n".encode("utf-8")
    target.chmod(0o640)
    write_atomic(target, b"second")
    assert target.read_bytes() == b"second"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640  # a replaced file keeps its permission bits
    reference = tmp_path / "reference"
    reference.write_bytes(b"")  # open() applies the umask the same way
    write_atomic(tmp_path / "new", bytearray(b"x"))
    assert stat.S_IMODE((tmp_path / "new").stat().st_mode) == stat.S_IMODE(reference.stat().st_mode)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["manifest.json", "new", "reference"]


def test_a_failed_write_removes_its_temp_file_and_leaves_the_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "kept").write_text("kept", encoding="utf-8")
    with pytest.raises(OSError):
        write_atomic(target, b"data")  # the temp file is written, then os.replace fails
    assert [p.name for p in tmp_path.iterdir()] == ["target"]
    assert [p.name for p in target.iterdir()] == ["kept"]
    with pytest.raises(TypeError):
        write_atomic(tmp_path / "number", 5)  # type: ignore[arg-type]
    with pytest.raises(FileNotFoundError):
        write_atomic(tmp_path / "no-folder" / "file", b"x")
    assert [p.name for p in tmp_path.iterdir()] == ["target"]


WRITER = """
import sys
from influencer.atomic import write_atomic
payloads = (b"a" * int(sys.argv[2]), b"b" * int(sys.argv[2]))
for i in range(1, 1000000):
    write_atomic(sys.argv[1], payloads[i % 2])
"""


@pytest.mark.time_limit(60)
def test_a_reader_never_sees_a_partial_target_and_a_killed_writer_leaves_a_whole_one(tmp_path: Path) -> None:
    size = 256 * 1024
    old, new = b"a" * size, b"b" * size
    target = tmp_path / "state.bin"
    write_atomic(target, old)
    writer = subprocess.Popen([sys.executable, "-c", WRITER, str(target), str(size)], env=_child_env(),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              start_new_session=True)  # fmt: skip
    seen = set()
    try:
        _wait_for(lambda: target.read_bytes() == new, "the writer's first replacement", writer)
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            data = target.read_bytes()
            assert data in (old, new), f"read {len(data)} bytes that are neither version of the file"
            seen.add(data[:1])
        assert writer.poll() is None, "the writer stopped while the reader was reading"
    finally:
        _kill(writer)
    assert seen == {b"a", b"b"}  # the reader really ran across the writer's replacements
    assert target.read_bytes() in (old, new)
    assert len([p for p in tmp_path.iterdir() if p != target]) <= 1  # at most the killed write's temp file


# ---- locks.py (task 5.7) ----

HOLDER = """
import sys, time
from influencer.locks import ProcessLock
lock = ProcessLock(sys.argv[1]).acquire()
time.sleep(60)
"""


@pytest.mark.time_limit(60)
def test_a_second_holder_is_refused_with_the_pid_until_the_holder_is_killed(tmp_path: Path) -> None:
    path = tmp_path / ".lock"
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(path)], env=_child_env(),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              start_new_session=True)  # fmt: skip
    try:
        _wait_for(lambda: read_pid(path) == holder.pid, "the holder's PID in the lock file", holder)
        with pytest.raises(LockHeld) as refused:
            ProcessLock(path).acquire()
        assert (refused.value.path, refused.value.pid) == (path, holder.pid)
        assert str(refused.value) == f"another process holds the lock {path} (PID {holder.pid})"
        assert ProcessLock(path).holder_pid() == holder.pid
    finally:
        _kill(holder)  # SIGKILL: the holder releases nothing itself, the kernel drops the lock
    lock = ProcessLock(path)
    with lock:
        assert lock.held and read_pid(path) == os.getpid()
        with pytest.raises(RuntimeError):
            lock.acquire()
    assert not lock.held and read_pid(path) is None  # release clears the recorded PID
    lock.release()  # releasing twice does nothing
    assert path.exists()  # the lock file is never deleted


# ---- the guard (task 5.8) ----

# Put first on a subprocess's PYTHONPATH: Python imports it at startup, and the audit hook ends the run
# with status 99 at the first file opened for writing, file-system change, socket or process start.
AUDIT_SITECUSTOMIZE = '''\
import os
import sys

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_EVENTS = frozenset((
    "os.mkdir", "os.rename", "os.remove", "os.rmdir", "os.symlink", "os.link", "os.truncate", "os.chmod",
    "os.chown", "os.utime", "shutil.copyfile", "shutil.copytree", "shutil.move", "shutil.rmtree",
    "tempfile.mkstemp", "tempfile.mkdtemp", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
    "os.fork", "os.forkpty", "subprocess.Popen", "pty.spawn",
))


def _audit(event, args):
    if event == "open":
        mode = args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 else None
        if not ((isinstance(flags, int) and flags & _WRITE_FLAGS)
                or (isinstance(mode, str) and any(c in mode for c in "wax+"))):
            return
        what = "open for writing %r" % (args[0],)
    elif event in _EVENTS or event.startswith("socket."):
        what = "%s %r" % (event, args)
    else:
        return
    sys.stderr.write("AUDIT: %s\\n" % what)
    sys.stderr.flush()
    os._exit(99)


sys.addaudithook(_audit)
'''


@pytest.fixture(scope="module")
def audit_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("audit")
    (folder / "sitecustomize.py").write_text(AUDIT_SITECUSTOMIZE, encoding="utf-8")
    return folder


@pytest.fixture(scope="module")
def bare_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A venv holding no package at all, so PyYAML, Pillow and NumPy are really missing."""
    root = tmp_path_factory.mktemp("bare")
    made = run_bounded([sys.executable, "-m", "venv", "--without-pip", root / "venv"], timeout=120)
    assert made.returncode == 0, made.stderr.decode()
    python = root / "venv" / "bin" / "python"
    probe = "import importlib.util as u, sys; sys.exit(sum(u.find_spec(m) is not None for m in sys.argv[1:]))"
    found = run_bounded([python, "-c", probe, "yaml", "PIL", "numpy"], env={"PATH": "/usr/bin:/bin"}, timeout=60)
    assert found.returncode == 0, "the bare venv can import PyYAML, Pillow or NumPy"
    return python


@pytest.mark.parametrize("which", ["bare venv", "Python 3.9"])
def test_the_audit_hook_stops_a_run_that_writes_or_connects(bare_python: Path, audit_dir: Path, tmp_path: Path,
                                                            which: str) -> None:  # fmt: skip
    """The guard tests below rely on the hook, so it must really load and fire in both interpreters."""
    python = bare_python
    if which == "Python 3.9":
        _mac_python_39()
        python = MAC_PYTHON
    environment = _child_env(PYTHONPATH=str(audit_dir), HOME=str(tmp_path))
    for code in (f"open({str(tmp_path / 'written')!r}, 'w')", "import socket; socket.socket()"):
        result = run_bounded([python, "-c", code], cwd=tmp_path, env=environment, timeout=60)
        assert result.returncode == 99 and result.stderr.startswith(b"AUDIT: "), result.stderr.decode()
    assert not (tmp_path / "written").exists()


def _guarded(python: Path, argv: list[str], audit: Path, tmp_path: Path, via: str = "python -m") -> tuple[int, str, str]:
    """Runs `<python> -m influencer <argv>`, or with via="launcher" `bash scripts/influencer.sh <argv>`
    with INFLUENCER_PYTHON=<python>, under the audit hook and from an empty working folder."""
    (tmp_path / "cwd").mkdir(exist_ok=True)
    (tmp_path / "home").mkdir(exist_ok=True)
    extra = {"HOME": str(tmp_path / "home"), "NO_COLOR": "1"}
    if via == "launcher":  # the launcher puts scripts/ in front of the audit folder
        command = [BASH, str(LAUNCHER), *argv]
        environment = _child_env(PYTHONPATH=str(audit), INFLUENCER_PYTHON=str(python), **extra)
    else:
        command = [str(python), "-m", "influencer", *argv]
        environment = _child_env(PYTHONPATH=f"{audit}{os.pathsep}{SCRIPTS}", **extra)
    result = run_bounded(command, cwd=tmp_path / "cwd", env=environment, timeout=60)
    return result.returncode, result.stdout.decode("utf-8"), result.stderr.decode("utf-8")


@pytest.mark.parametrize("via", ["python -m", "launcher"])
@pytest.mark.parametrize(
    ("argv", "packages"),
    [(["check-config"], ["PyYAML"]), (["export", "--week", "2026-W40"], ["PyYAML", "Pillow", "NumPy"])],
    ids=["check-config", "export"],
)
def test_a_missing_package_exits_2_naming_it_before_any_write_or_socket(
    bare_python: Path, audit_dir: Path, tmp_path: Path, via: str, argv: list[str], packages: list[str]
) -> None:
    status, stdout, stderr = _guarded(bare_python, argv, audit_dir, tmp_path, via)
    assert (status, stdout) == (Exit.ENVIRONMENT, ""), stderr
    assert "AUDIT:" not in stderr and "Traceback" not in stderr, stderr
    listed = packages[0] if len(packages) == 1 else ", ".join(packages[:-1]) + " and " + packages[-1]
    assert stderr.startswith(f"{argv[0]}: needs {listed}, which Python "), stderr
    for package in packages:
        assert f"\n  {package}: ModuleNotFoundError" in stderr, stderr


def test_disk_needs_only_the_standard_library(bare_python: Path, audit_dir: Path, tmp_path: Path) -> None:
    """disk passes the import check without PyYAML, Pillow or NumPy (§C5) and runs: an id the Catalog does
    not list exits 1 before any write or socket (Req 23.8)."""
    status, stdout, stderr = _guarded(bare_python, ["disk", "--id", "no-such-id"], audit_dir, tmp_path)
    assert (status, stdout) == (Exit.USAGE, ""), stderr
    assert "AUDIT:" not in stderr and "Traceback" not in stderr, stderr
    assert "'no-such-id' is not a Catalog id; valid ids: " in stderr, stderr


def test_the_version_guard_names_the_running_version() -> None:
    message = influencer.python_version_error((3, 9, 6, "final", 0), "/usr/bin/python3")
    assert message is not None
    assert message.startswith("influencer: Python 3.10 or later is required, but this is Python 3.9.6 (/usr/bin/python3).")
    assert "INFLUENCER_PYTHON" in message
    for version in ((3, 10, 0), (3, 14, 7), (4, 0, 0)):
        assert influencer.python_version_error(version, "/x") is None
    assert influencer.python_version_error() is None  # this interpreter passes


def _mac_python_39() -> str:
    """The Mac's /usr/bin/python3 version, or a skip with the reason it cannot serve as the old Python."""
    if not MAC_PYTHON.exists():
        pytest.skip(f"{MAC_PYTHON} does not exist on this host")
    if platform.system() == "Darwin":
        # Without the Command Line Tools, /usr/bin/python3 only offers to install them (a dialog).
        tools = run_bounded(["/usr/bin/xcode-select", "-p"], env={"PATH": "/usr/bin:/bin"}, timeout=30)
        folder = tools.stdout.decode().strip()
        if tools.returncode != 0 or not Path(folder, "usr", "bin", "python3").exists():
            pytest.skip(f"{MAC_PYTHON} is the Command Line Tools installer stub on this Mac")
    probe = run_bounded([MAC_PYTHON, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
                        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"}, timeout=60)  # fmt: skip
    version = probe.stdout.decode().strip()
    if probe.returncode != 0 or not version:
        pytest.skip(f"{MAC_PYTHON} does not run: {probe.stderr.decode().strip()}")
    if tuple(int(part) for part in version.split(".")[:2]) >= (3, 10):
        pytest.skip(f"{MAC_PYTHON} is Python {version}, not older than 3.10")
    return version


@pytest.mark.parametrize("via", ["python -m", "launcher"])
def test_python_3_9_gets_the_version_message_before_any_write_or_socket(audit_dir: Path, tmp_path: Path, via: str) -> None:
    version = _mac_python_39()
    status, stdout, stderr = _guarded(MAC_PYTHON, ["check-config"], audit_dir, tmp_path, via)
    assert (status, stdout) == (Exit.ENVIRONMENT, ""), stderr
    assert stderr.startswith(f"influencer: Python 3.10 or later is required, but this is Python {version} ("), stderr
    assert "SyntaxError" not in stderr and "Traceback" not in stderr and "AUDIT:" not in stderr, stderr


def test_the_guard_modules_parse_under_python_3_9() -> None:
    """__init__.py runs the guard and env.py (with errors.py) the import check; all three must parse on an
    old Python, so no newer syntax slips in."""
    _mac_python_39()
    files = [SCRIPTS / "influencer" / name for name in ("__init__.py", "env.py", "errors.py")]
    code = "import sys\nfor f in sys.argv[1:]:\n    compile(open(f, encoding='utf-8').read(), f, 'exec')\n"
    result = run_bounded([MAC_PYTHON, "-c", code, *files], env={"PATH": "/usr/bin:/bin",
                                                                  "PYTHONDONTWRITEBYTECODE": "1"}, timeout=60)  # fmt: skip
    assert result.returncode == 0, result.stderr.decode()
