"""pytest configuration for the Check_Suite's tests (task 3.1, design Testing Strategy, Req 27.2).

- The socket guard: a session fixture, active in every test, that refuses any connection outside
  127.0.0.0/8 and ::1, and to loopback ports 18188 and 11434, so no test can reach the internet or a real
  ComfyUI or Ollama on the host.
- The Hypothesis `check` profile, loaded when HYPOTHESIS_PROFILE names it (scripts/check.sh does).
- Temp WORKSPACE, COMFYUI_DIR, CONFIG_DIR and Influencer_Root fixtures, a shortened `timing`, a freshly
  closed port, the bash stub PATH, and the fake ComfyUI and Ollama servers.
- The `instance` marker (skipped unless --run-instance) and the `ffmpeg` marker (skipped with a reason
  when ffmpeg or ffprobe is missing on the host).
- A time limit on every test's setup, call and teardown (default 120 s each; TESTS_TIME_LIMIT=<seconds>
  changes it, 0 turns it off, @pytest.mark.time_limit(seconds) sets one test's). A test that hangs fails
  with its traceback and the other threads' stacks, and the run goes on.

The pieces themselves live in helpers/, fakes/ and strategies.py, so a Hypothesis test can use them
without a function-scoped fixture.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from hypothesis import settings
from hypothesis.configuration import set_hypothesis_home_dir

from fakes.comfy import ComfyFake
from fakes.ollama import OllamaFake
from helpers import harness
from helpers.harness import MEDIA_TOOLS, REPO, SHORT_TIMING, SocketGuard, TempEnv, Timing, time_limit
from helpers.stubs import StubPath

# ---- Hypothesis ----
# The design (Testing Strategy, Correctness Properties) and task 3.1 ask for the `check` profile with
# max_examples=200, deadline=None, derandomize=True and "database in .hypothesis/". Hypothesis 6.168.3
# refuses that pairing (InvalidArgument: derandomize=True implies database=None), so `check` runs without
# an example database: a derandomized run replays the same examples every time anyway. The home folder is
# pinned to the git-ignored <repo>/.hypothesis/, so every file Hypothesis does write (the default
# profile's example database, its caches) lands there whatever the working folder is.
set_hypothesis_home_dir(REPO / ".hypothesis")
settings.register_profile("check", max_examples=200, deadline=None, derandomize=True)
if os.environ.get("HYPOTHESIS_PROFILE"):
    # pytest's own --hypothesis-profile option, when given, is applied after this and wins.
    settings.load_profile(os.environ["HYPOTHESIS_PROFILE"])


# ---- markers ----


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-instance",
        action="store_true",
        default=False,
        help="run the @pytest.mark.instance acceptance checks (REFERENCE.md §13); they need a Vast Instance",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "instance: Instance acceptance check (REFERENCE.md §13); skipped unless --run-instance"
    )
    config.addinivalue_line(
        "markers", "ffmpeg: needs ffmpeg and ffprobe; skipped with a reason when either is missing on the host"
    )
    config.addinivalue_line(
        "markers",
        f"time_limit(seconds): this test's limit per setup, call and teardown (default "
        f"{harness.DEFAULT_TIME_LIMIT_S:g} s, or {harness.TIME_LIMIT_ENV}; 0 for none)",
    )
    config.stash[_TIME_LIMIT] = harness.default_time_limit()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    run_instance = config.getoption("--run-instance", default=False)
    missing_media = harness.missing_tools(MEDIA_TOOLS)
    for item in items:
        if item.get_closest_marker("instance") is not None and not run_instance:
            item.add_marker(pytest.mark.skip(reason="Instance acceptance check: run with --run-instance on an Instance"))
        if item.get_closest_marker("ffmpeg") is not None and missing_media:
            item.add_marker(pytest.mark.skip(reason=f"{' and '.join(missing_media)} missing on this host"))


# ---- the per-test time limit ----

_TIME_LIMIT = pytest.StashKey[float]()


def item_time_limit(item: pytest.Item) -> float:
    """The limit for each phase of item: its time_limit marker, else TESTS_TIME_LIMIT, else the default."""
    marker = item.get_closest_marker("time_limit")
    if marker is None:
        return item.config.stash.get(_TIME_LIMIT, harness.DEFAULT_TIME_LIMIT_S)
    if len(marker.args) != 1 or marker.kwargs or not isinstance(marker.args[0], (int, float)):
        raise pytest.UsageError(f"{item.nodeid}: use @pytest.mark.time_limit(<seconds>)")
    return float(marker.args[0])


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Iterator[None]:
    with time_limit(item_time_limit(item), f"the setup of {item.nodeid}"):
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    with time_limit(item_time_limit(item), item.nodeid):
        return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Iterator[None]:
    with time_limit(item_time_limit(item), f"the teardown of {item.nodeid}"):
        return (yield)


# ---- the socket guard ----


@pytest.fixture(scope="session", autouse=True)
def socket_guard() -> Iterator[SocketGuard]:
    """Active for every test: AF_INET/AF_INET6 connections outside loopback, or to loopback port 18188 or
    11434, raise NetworkRefused."""
    guard = SocketGuard()
    guard.install()
    yield guard
    guard.uninstall()


# ---- temp env and time limits ----


@pytest.fixture
def temp_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TempEnv:
    """A fresh env below tmp_path; its variables are also set in os.environ for in-process code."""
    env = TempEnv.create(tmp_path / "env")
    for name, value in env.variables().items():
        monkeypatch.setenv(name, value)
    return env


@pytest.fixture
def workspace(temp_env: TempEnv) -> Path:  # WORKSPACE
    return temp_env.workspace


@pytest.fixture
def comfyui_dir(temp_env: TempEnv) -> Path:  # COMFYUI_DIR
    return temp_env.comfyui_dir


@pytest.fixture
def config_dir(temp_env: TempEnv) -> Path:  # CONFIG_DIR
    return temp_env.config_dir


@pytest.fixture
def influencer_root(temp_env: TempEnv) -> Path:  # Influencer_Root
    return temp_env.influencer_root


@pytest.fixture
def timing() -> Timing:
    """The stage time limits, shortened so a timeout test takes well under a few seconds."""
    return SHORT_TIMING


@pytest.fixture
def closed_port() -> int:
    """A loopback port nothing listens on (never 18188 or 11434), for "unreachable" tests."""
    return harness.closed_port()


@pytest.fixture
def ffmpeg() -> str:
    """The host's ffmpeg, or a skip naming it (the design's ffmpeg-missing skip)."""
    harness.require_tool("ffprobe")
    return harness.require_tool("ffmpeg")


# ---- the bash stub PATH ----


@pytest.fixture
def stub_path(tmp_path: Path, temp_env: TempEnv) -> StubPath:
    """A PATH of recording stubs (curl, wget, hf, git, ollama, apt-get, rclone, nvidia-smi, uv) and links to
    the real basic tools; stub_path.run(script, ...) runs a repo script with it under /bin/bash."""
    return StubPath(tmp_path / "stubs", temp_env)


# ---- fake servers ----


@pytest.fixture
def comfy_fake(temp_env: TempEnv, monkeypatch: pytest.MonkeyPatch) -> Iterator[ComfyFake]:
    """A running fake ComfyUI_API writing into the temp ComfyUI's output folder; COMFYUI_URL points at it."""
    with ComfyFake(temp_env.comfyui_dir / "output", temp_env.comfyui_dir / "input") as fake:
        monkeypatch.setenv("COMFYUI_URL", fake.url)
        yield fake
        assert not fake.errors, "the ComfyUI fake failed:\n" + "\n".join(fake.errors)


@pytest.fixture
def ollama_fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[OllamaFake]:
    """A running fake Ollama_API; OLLAMA_URL points at it."""
    with OllamaFake() as fake:
        monkeypatch.setenv("OLLAMA_URL", fake.url)
        yield fake
        assert not fake.errors, "the Ollama fake failed:\n" + "\n".join(fake.errors)
