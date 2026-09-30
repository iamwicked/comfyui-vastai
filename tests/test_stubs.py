"""Tests of the bash stub PATH (task 3.4): tests/helpers/stubs.py and the stub_path fixture."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from helpers.stubs import BASH, DEFAULT_STUBS, StubPath
from strategies import ConfigLists, write_config

pytestmark = pytest.mark.skipif(not os.access(BASH, os.X_OK), reason=f"{BASH} is missing; the stubs run under it")


def _script(folder: Path, text: str) -> Path:
    path = folder / "script.sh"
    path.write_text(text, encoding="utf-8")
    return path


def test_every_default_stub_records_its_arguments(stub_path, tmp_path):
    lines = "".join(f"{name} 'a b' '' $'x\\ny' 'ünï'\n" for name in DEFAULT_STUBS)
    result = stub_path.run(_script(tmp_path, lines + "curl\n"))
    assert result.returncode == 0, result.stderr
    calls = stub_path.calls()
    assert [call.name for call in calls] == [*DEFAULT_STUBS, "curl"]
    for call in calls[:-1]:
        assert (call.args, call.status, call.stdin) == (("a b", "", "x\ny", "ünï"), 0, None)
        assert call.cwd == os.path.realpath(stub_path.env.root)
    assert calls[-1].args == ()
    assert [call.name for call in stub_path.calls("curl")] == ["curl", "curl"]


def test_scripted_statuses_follow_the_call_order(stub_path, tmp_path):
    stub_path.stub("curl", [7, 22, 0])
    script = 'for i in 1 2 3 4; do curl "$i"; echo "$?"; done\n'
    result = stub_path.run(_script(tmp_path, script))
    assert result.stdout == b"7\n22\n0\n0\n"
    assert [(c.args, c.status) for c in stub_path.calls("curl")] == [
        (("1",), 7),
        (("2",), 22),
        (("3",), 0),
        (("4",), 0),
    ]
    stub_path.stub("curl", 5)  # re-scripting restarts the sequence
    stub_path.clear()
    assert stub_path.run(_script(tmp_path, "curl x\n")).returncode == 5
    assert [c.status for c in stub_path.calls()] == [5]


def test_scripted_output_and_body(stub_path, tmp_path):
    stub_path.stub(
        "nvidia-smi",
        stdout="9.0, 12.6\n",
        stderr="stub warning\n",
        body='printf "%s\\n" "body saw $1"; status=3',
    )
    result = stub_path.run(_script(tmp_path, "nvidia-smi --query-gpu=compute_cap\n"))
    assert result.returncode == 3
    assert result.stdout == b"9.0, 12.6\nbody saw --query-gpu=compute_cap\n"
    assert result.stderr == b"stub warning\n"
    assert stub_path.calls()[0].status == 3


def test_stdin_is_recorded_only_when_asked(stub_path, tmp_path):
    stub_path.stub("uv", capture_stdin=True)
    (tmp_path / "list.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    script = (
        "printf 'pkg==1\\n' | uv pip install -r -\n"
        f"while IFS= read -r line; do git clone \"$line\"; done < '{tmp_path / 'list.txt'}'\n"
    )
    result = stub_path.run(_script(tmp_path, script))
    assert result.returncode == 0, result.stderr
    assert stub_path.calls("uv")[0].stdin == b"pkg==1\n"
    # the log file: one line per call, the stub's name and its arguments, in call order
    assert stub_path.log_lines() == ["uv pip install -r -", "git clone one", "git clone two", "git clone three"]
    # git does not read stdin, so the loop around it still sees every line
    assert [(c.args, c.stdin) for c in stub_path.calls("git")] == [
        (("clone", "one"), None),
        (("clone", "two"), None),
        (("clone", "three"), None),
    ]


def test_adding_a_stub_is_one_line(stub_path, tmp_path):
    stub_path.stub("flock", 1)
    stub_path.stub("timeout", 124)
    result = stub_path.run(_script(tmp_path, 'flock -n 9; echo "$?"; timeout 5 sleep 60; echo "$?"\n'))
    assert result.stdout == b"1\n124\n"
    assert [(c.name, c.args) for c in stub_path.calls()] == [("flock", ("-n", "9")), ("timeout", ("5", "sleep", "60"))]


def test_the_path_holds_only_stubs_and_linked_tools(stub_path, tmp_path):
    script = "command -v python3 || echo none; command -v sed; command -v curl; command -v bash\n"
    result = stub_path.run(_script(tmp_path, script))
    assert result.stdout.decode().splitlines() == [
        "none",
        str(stub_path.bin / "sed"),
        str(stub_path.bin / "curl"),
        str(stub_path.bin / "bash"),
    ]
    assert os.path.realpath(stub_path.bin / "bash") == os.path.realpath(BASH)
    stub_path.link("python3")
    assert stub_path.run(_script(tmp_path, "command -v python3\n")).stdout.decode().strip() == str(
        stub_path.bin / "python3"
    )


def test_a_stub_name_cannot_be_linked_and_a_missing_tool_is_an_error(stub_path):
    with pytest.raises(ValueError, match="curl is a stub"):
        stub_path.link("curl")
    with pytest.raises(FileNotFoundError, match="no-such-tool-7c1"):
        stub_path.link("no-such-tool-7c1")
    with pytest.raises(ValueError):
        stub_path.stub("sub/dir")
    with pytest.raises(ValueError):
        stub_path.stub("curl", 256)


def test_a_run_gets_the_temp_env_and_nothing_else(stub_path, temp_env, tmp_path, monkeypatch):
    monkeypatch.setenv("LEAK_CHECK", "from the caller")
    script = (
        'printf "%s\\n" "$BASH" "$HOME" "$TMPDIR" "$WORKSPACE" "$COMFYUI_DIR" "$CONFIG_DIR" '
        '"$INFLUENCER_HOME" "$INFLUENCER_OUTPUT" "${LEAK_CHECK-unset}" "$EXTRA" "$PWD"\n'
    )
    result = stub_path.run(_script(tmp_path, script), extra_env={"EXTRA": "given"})
    assert result.stdout.decode().splitlines() == [
        BASH,
        str(temp_env.home),
        str(temp_env.tmp),
        str(temp_env.workspace),
        str(temp_env.comfyui_dir),
        str(temp_env.config_dir),
        str(temp_env.influencer_home),
        str(temp_env.output_parent),
        "unset",
        "given",
        os.path.realpath(temp_env.root),
    ]


def test_input_goes_to_the_script(stub_path, tmp_path):
    result = stub_path.run(_script(tmp_path, "IFS= read -r line; echo \"got $line\"\n"), input=b"hello\n")
    assert result.stdout == b"got hello\n"


def test_runs_a_repo_script_against_a_written_config(stub_path, temp_env):
    config = ConfigLists(
        catalog=("# ids", "amber|yes||Amber stills", "", "moss-2|no|0.30.0|Moss — opt-in"),
        models=("lora|a b.safetensors|civitai|123|loras/looks|wf:amber",),
    )
    write_config(temp_env.config_dir, config)
    result = stub_path.run("scripts/provision.sh", "--list-workflows")
    assert result.returncode == 0, result.stderr
    lines = [line.split() for line in result.stdout.decode().splitlines()]
    assert lines == [["amber", "Amber", "stills"], ["moss-2", "Moss", "—", "opt-in", "[opt-in,", "not", "in", "all]"]]
    assert stub_path.calls() == []


def test_a_second_stub_path_is_independent(tmp_path, temp_env):
    first = StubPath(tmp_path / "first", temp_env, stubs=("curl",), tools=())
    second = StubPath(tmp_path / "second", temp_env, stubs=("curl",), tools=())
    first.run(_script(tmp_path, "curl one\n"))
    assert [c.args for c in first.calls()] == [("one",)]
    assert second.calls() == []
    assert sorted(p.name for p in first.bin.iterdir()) == ["bash", "curl"]
