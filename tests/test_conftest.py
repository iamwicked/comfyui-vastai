"""Tests of tests/conftest.py and tests/helpers/harness.py (task 3.1): the socket guard, the Hypothesis
profiles, the temp env, the shortened Timing, the closed port, and the instance and ffmpeg markers."""
from __future__ import annotations

import dataclasses
import errno
import os
import shutil
import socket
import tempfile
from pathlib import Path

import pytest
from hypothesis import settings
from hypothesis.configuration import storage_directory

from helpers import harness
from helpers.harness import REPO, RESERVED_PORTS, SHORT_TIMING, TIMING_FIELDS, NetworkRefused, Timing, is_loopback

# ---- the socket guard ----


def _listener(family: int = socket.AF_INET, host: str = "127.0.0.1") -> socket.socket:
    server = socket.socket(family, socket.SOCK_STREAM)
    server.bind((host, 0))
    server.listen(8)
    return server


def _has_ipv6_loopback() -> bool:
    if not socket.has_ipv6:
        return False
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


def test_the_guard_is_active(socket_guard):
    assert socket_guard.installed
    assert "connect" in socket.socket.__dict__ and "connect_ex" in socket.socket.__dict__


def test_a_public_address_is_refused(socket_guard):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(NetworkRefused, match=r"'192\.0\.2\.1' port 80"):
            sock.connect(("192.0.2.1", 80))
    assert socket_guard.refused[-1] == "'192.0.2.1' port 80"


def test_connect_ex_and_create_connection_are_refused():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(NetworkRefused):
            sock.connect_ex(("198.51.100.7", 443))
    with pytest.raises(NetworkRefused):
        socket.create_connection(("203.0.113.9", 443), timeout=1)


def test_a_refusal_cannot_be_swallowed_by_the_code_under_test():
    assert issubclass(NetworkRefused, pytest.fail.Exception)
    assert not issubclass(NetworkRefused, Exception)

    def code_under_test() -> str:
        try:
            with socket.create_connection(("192.0.2.1", 80), timeout=1):
                return "connected"
        except OSError:
            return "swallowed"
        except Exception:
            return "swallowed"

    with pytest.raises(NetworkRefused):
        code_under_test()


def test_a_host_name_is_refused_without_a_lookup():
    # .invalid never resolves: had the guard let it through, connect would raise socket.gaierror instead
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(NetworkRefused, match="example.invalid"):
            sock.connect(("example.invalid", 443))


@pytest.mark.skipif(not _has_ipv6_loopback(), reason="no IPv6 loopback on this host")
def test_ipv6_outside_loopback_is_refused_and_loopback_allowed():
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
        with pytest.raises(NetworkRefused):
            sock.connect(("2001:db8::1", 80, 0, 0))
    with _listener(socket.AF_INET6, "::1") as server, socket.socket(socket.AF_INET6) as client:
        client.connect(server.getsockname()[:2])


@pytest.mark.parametrize("port", sorted(RESERVED_PORTS))
def test_the_real_comfyui_and_ollama_ports_are_refused_on_loopback(port):
    # a real ComfyUI_API (18188) or Ollama_API (11434) on the host listens on loopback
    targets = [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET, "localhost")]
    if _has_ipv6_loopback():
        targets.append((socket.AF_INET6, "::1"))
    for family, host in targets:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            with pytest.raises(NetworkRefused, match=f"port {port}: a real ComfyUI or Ollama"):
                sock.connect((host, port))
            with pytest.raises(NetworkRefused, match=f"port {port}"):
                sock.connect_ex((host, port))
    with pytest.raises(NetworkRefused):
        socket.create_connection(("127.0.0.1", port), timeout=1)


def test_loopback_connections_are_allowed():
    with _listener() as server:
        port = server.getsockname()[1]
        with socket.socket() as client:
            client.connect(("127.0.0.1", port))
        with socket.create_connection(("localhost", port), timeout=2):
            pass
        with socket.socket() as client:
            assert client.connect_ex(("127.0.0.1", port)) == 0


def test_unix_sockets_are_allowed():
    folder = tempfile.mkdtemp(prefix="guard-", dir="/tmp")  # short: AF_UNIX paths are limited to ~104 bytes
    try:
        path = os.path.join(folder, "s")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            server.listen(1)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(path)
    finally:
        shutil.rmtree(folder)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.255.3.9", True),
        ("::1", True),
        ("::1%lo0", True),
        ("::ffff:127.0.0.1", True),
        ("localhost", True),
        ("LOCALHOST.", True),
        (b"127.0.0.1", True),
        ("0.0.0.0", False),
        ("", False),
        ("::", False),
        ("10.0.0.1", False),
        ("128.0.0.1", False),
        ("::ffff:10.0.0.1", False),
        ("fe80::1%lo0", False),
        ("127.1", False),
        ("localhost.example", False),
        ("example.com", False),
        (None, False),
    ],
)
def test_is_loopback(host, expected):
    assert is_loopback(host) is expected


def test_uninstall_restores_the_socket_class():
    guard = harness.SocketGuard()
    before = {name: socket.socket.__dict__.get(name) for name in ("connect", "connect_ex")}
    guard.install()
    assert socket.socket.connect is not before["connect"]
    guard.uninstall()
    assert {name: socket.socket.__dict__.get(name) for name in ("connect", "connect_ex")} == before


# ---- Hypothesis ----


def test_the_check_profile():
    profile = settings.get_profile("check")
    assert (profile.max_examples, profile.deadline, profile.derandomize) == (200, None, True)
    assert profile.database is None  # derandomize=True implies database=None in Hypothesis 6.168.3


def test_the_profile_comes_from_hypothesis_profile(request):
    wanted = request.config.getoption("hypothesis_profile", default=None) or os.environ.get("HYPOTHESIS_PROFILE")
    assert settings.get_current_profile_name() == (wanted or "default")


def test_hypothesis_writes_below_the_repo_dot_hypothesis():
    assert storage_directory("examples", intent_to_write=False).path == REPO / ".hypothesis" / "examples"


# ---- temp env ----


def test_the_temp_env_layout(temp_env, tmp_path):
    for folder in (
        temp_env.workspace,
        temp_env.comfyui_dir / "input",
        temp_env.comfyui_dir / "output",
        temp_env.config_dir,
        temp_env.influencer_root,
        temp_env.home,
        temp_env.tmp,
    ):
        assert folder.is_dir()
        assert tmp_path in folder.parents
    assert temp_env.comfyui_dir.parent == temp_env.workspace
    assert temp_env.influencer_root == temp_env.influencer_home / temp_env.slug
    assert temp_env.output_root == temp_env.comfyui_dir / "output" / "influencer" / temp_env.slug
    assert list(temp_env.config_dir.iterdir()) == []


def test_the_temp_env_variables_are_set(temp_env):
    assert set(temp_env.variables()) == {"WORKSPACE", "COMFYUI_DIR", "CONFIG_DIR", "INFLUENCER_HOME", "INFLUENCER_OUTPUT"}
    for name, value in temp_env.variables().items():
        assert os.environ[name] == value
    assert os.environ["INFLUENCER_OUTPUT"] == str(temp_env.output_parent)


def test_the_path_fixtures_share_one_env(temp_env, workspace, comfyui_dir, config_dir, influencer_root):
    assert (workspace, comfyui_dir, config_dir, influencer_root) == (
        temp_env.workspace,
        temp_env.comfyui_dir,
        temp_env.config_dir,
        temp_env.influencer_root,
    )


# ---- Timing ----


def test_timing_defaults_are_the_designs_figures():
    assert dataclasses.asdict(Timing()) == {
        "request_s": 10,
        "object_info_s": 30,
        "upload_s": 60,
        "submit_s": 30,
        "plan_attempt_s": 600,
        "caption_attempt_s": 120,
        "preflight_deadline_s": 55,
        "poll_s": 2,
        "silence_s": 10,
        "silence_budget_s": 300,
        "interrupt_grace_s": 30,
        "external_request_s": 10,
    }


def test_the_timing_fixture_is_shortened_and_frozen(timing):
    assert timing == SHORT_TIMING
    for name in TIMING_FIELDS:
        assert 0 < getattr(timing, name) < getattr(Timing(), name) and getattr(timing, name) <= 2
    assert timing.poll_s < timing.silence_s < timing.silence_budget_s
    assert timing.request_s < timing.object_info_s < timing.preflight_deadline_s
    with pytest.raises(dataclasses.FrozenInstanceError):
        timing.poll_s = 1  # type: ignore[misc]


# ---- the closed port ----


def test_the_closed_port_refuses_connections(closed_port):
    assert closed_port not in RESERVED_PORTS
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        assert sock.connect_ex(("127.0.0.1", closed_port)) == errno.ECONNREFUSED


def test_the_closed_port_is_never_a_reserved_one(monkeypatch):
    handed_out = iter([18188, 11434, 18188, 50123])
    monkeypatch.setattr(harness, "_free_port", lambda: next(handed_out))
    assert harness.closed_port() == 50123


# ---- markers and tools ----


@pytest.mark.instance
def test_instance_checks_are_skipped_by_default(request):
    if not request.config.getoption("--run-instance"):
        pytest.fail("an @pytest.mark.instance test ran without --run-instance")


@pytest.mark.ffmpeg
def test_the_ffmpeg_marker_runs_only_with_ffmpeg():
    assert shutil.which("ffmpeg") and shutil.which("ffprobe")


def test_require_tool_skips_with_the_tool_name():
    with pytest.raises(pytest.skip.Exception, match="no-such-tool-7c1 is missing on this host"):
        harness.require_tool("no-such-tool-7c1")
    assert harness.require_tool("sh") == shutil.which("sh")
    assert harness.missing_tools(["sh", "no-such-tool-7c1"]) == ["no-such-tool-7c1"]


def test_repo_is_the_checkout():
    assert (REPO / "scripts" / "provision.sh").is_file()
    assert Path(__file__).resolve().parent == REPO / "tests"
