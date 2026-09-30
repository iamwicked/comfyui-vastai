"""The harness that tests/conftest.py turns into fixtures (task 3.1, design Testing Strategy, Req 27.2).

- SocketGuard: refuses every AF_INET and AF_INET6 connection outside 127.0.0.0/8 and ::1, and to the
  loopback ports 18188 and 11434 where a real ComfyUI_API or Ollama_API listens
- TempEnv: a temp WORKSPACE, COMFYUI_DIR, CONFIG_DIR and Influencer_Root for one test
- closed_port(): a loopback port that nothing listens on, never 18188 or 11434
- require_tool() and missing_tools(): host tools, and the skip that names a missing one (ffmpeg)
- run_bounded(): a subprocess with no terminal, empty stdin, a timeout, and no child left behind
- time_limit(): the per-test time limit that conftest.py puts on every setup, call and teardown
- Timing: the stage time limits, which the `timing` fixture shortens

Every piece is plain Python, so a test can also use it outside a fixture, for example inside a Hypothesis
test, where function-scoped fixtures are not allowed.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import traceback
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# ComfyUI_API and Ollama_API on the Instance and on the Mac. An "unreachable" test must never pick them,
# because a real server may be listening there.
RESERVED_PORTS = frozenset({18188, 11434})

# ---- socket guard ----

_LOOPBACK_V4 = ipaddress.IPv4Network("127.0.0.0/8")
_LOOPBACK_V6 = ipaddress.IPv6Address("::1")
_GUARDED_FAMILIES = frozenset({socket.AF_INET, socket.AF_INET6})


class NetworkRefused(pytest.fail.Exception):
    """A test tried to open a connection outside loopback.

    It derives from pytest's Failed, which is a BaseException, so a broad `except OSError` or
    `except Exception` in the code under test cannot swallow the refusal and carry on.
    """


def is_loopback(host: object) -> bool:
    """True when host is 127.0.0.0/8, ::1 (an IPv4-mapped 127.x address included) or the name localhost.

    Any other name is refused without being resolved, so the guard never sends a DNS query. Shorthand forms
    that only inet_aton accepts, such as "127.1", are refused as well.
    """
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    name = host.split("%", 1)[0]  # an IPv6 zone such as ::1%lo0
    if name.rstrip(".").lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped in _LOOPBACK_V4
        return address == _LOOPBACK_V6
    return address in _LOOPBACK_V4


class SocketGuard:
    """Wraps socket.socket.connect and connect_ex for AF_INET and AF_INET6 sockets.

    A non-loopback address raises NetworkRefused before the real call, so no packet leaves the host. So
    does a loopback address on a RESERVED_PORTS port, so no test reaches a real ComfyUI or Ollama running
    on the host (the fakes listen on ports the OS hands out, never those two). AF_UNIX and every other
    family pass through unchanged. The guard covers this process only: a subprocess (bash, curl, git) is
    kept offline by the stub PATH of helpers/stubs.py instead. DNS lookups made directly through
    getaddrinfo, and UDP sendto without connect, are not intercepted.
    """

    _METHODS = ("connect", "connect_ex")

    def __init__(self) -> None:
        self.refused: list[str] = []  # every refused address, in order
        self._saved: dict[str, object] | None = None

    @property
    def installed(self) -> bool:
        return self._saved is not None

    def check(self, sock: socket.socket, address: object) -> None:
        if sock.family not in _GUARDED_FAMILIES:
            return
        host = address[0] if isinstance(address, tuple) and address else address
        port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
        loopback = is_loopback(host)
        if loopback and port not in RESERVED_PORTS:
            return
        where = repr(host) + (f" port {port}" if port is not None else "")
        self.refused.append(where)
        if loopback:
            reason = f"not {where}: a real ComfyUI or Ollama may listen there; use comfy_fake or ollama_fake"
        else:
            reason = f"only to 127.0.0.0/8 or ::1, not {where}"
        raise NetworkRefused(f"socket guard: tests may connect {reason}", pytrace=True)

    def install(self) -> None:
        if self._saved is not None:
            return
        cls = socket.socket
        # Remember what the class itself defines (normally nothing: both methods come from _socket.socket),
        # so uninstall() restores the exact previous state.
        self._saved = {name: cls.__dict__.get(name) for name in self._METHODS}
        real_connect, real_connect_ex = cls.connect, cls.connect_ex
        guard = self

        def connect(sock: socket.socket, address: object) -> None:
            guard.check(sock, address)
            return real_connect(sock, address)

        def connect_ex(sock: socket.socket, address: object) -> int:
            guard.check(sock, address)
            return real_connect_ex(sock, address)

        cls.connect = connect  # type: ignore[method-assign]
        cls.connect_ex = connect_ex  # type: ignore[method-assign]

    def uninstall(self) -> None:
        if self._saved is None:
            return
        for name, original in self._saved.items():
            if original is None:
                delattr(socket.socket, name)
            else:
                setattr(socket.socket, name, original)
        self._saved = None


# ---- temp env ----


@dataclass(frozen=True)
class TempEnv:
    """The folders a test runs against, all below one temp root, laid out like the Instance.

    COMFYUI_DIR sits inside WORKSPACE, and INFLUENCER_OUTPUT is ComfyUI's own output/influencer folder, so
    files the fake ComfyUI saves land in the Output_Root exactly as SaveImage does on the Instance.
    """

    root: Path
    slug: str = "test-persona"

    @property
    def workspace(self) -> Path:  # WORKSPACE
        return self.root / "workspace"

    @property
    def comfyui_dir(self) -> Path:  # COMFYUI_DIR
        return self.workspace / "ComfyUI"

    @property
    def config_dir(self) -> Path:  # CONFIG_DIR, empty until a test writes a configuration into it
        return self.root / "config"

    @property
    def influencer_home(self) -> Path:  # INFLUENCER_HOME
        return self.root / "influencer"

    @property
    def influencer_root(self) -> Path:  # Influencer_Root: <home>/<slug>
        return self.influencer_home / self.slug

    @property
    def output_parent(self) -> Path:  # INFLUENCER_OUTPUT, the parent of <slug>
        return self.comfyui_dir / "output" / "influencer"

    @property
    def output_root(self) -> Path:  # Output_Root, created by the tools themselves
        return self.output_parent / self.slug

    @property
    def home(self) -> Path:  # HOME of a script run
        return self.root / "home"

    @property
    def tmp(self) -> Path:  # TMPDIR of a script run
        return self.root / "tmp"

    def variables(self) -> dict[str, str]:
        """The environment variables that point the repo's scripts and package at this env."""
        return {
            "WORKSPACE": str(self.workspace),
            "COMFYUI_DIR": str(self.comfyui_dir),
            "CONFIG_DIR": str(self.config_dir),
            "INFLUENCER_HOME": str(self.influencer_home),
            "INFLUENCER_OUTPUT": str(self.output_parent),
        }

    @classmethod
    def create(cls, root: Path, slug: str = "test-persona") -> TempEnv:
        env = cls(Path(root), slug)
        for folder in (
            env.workspace,
            env.comfyui_dir / "input",
            env.comfyui_dir / "output",
            env.config_dir,
            env.influencer_root,
            env.home,
            env.tmp,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        return env


# ---- ports and tools ----


def _free_port() -> int:
    """A port the OS just handed out on 127.0.0.1, closed again before returning."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def closed_port(avoid: Iterable[int] = RESERVED_PORTS) -> int:
    """A freshly closed loopback port for "unreachable" tests: connecting to it is refused at once."""
    avoid = frozenset(avoid)
    for _ in range(100):
        port = _free_port()
        if port not in avoid:
            return port
    raise RuntimeError(f"the OS kept handing out reserved ports {sorted(avoid)}")


def missing_tools(names: Iterable[str]) -> list[str]:
    """The names among names that are not on this host's PATH."""
    return [name for name in names if shutil.which(name) is None]


def require_tool(name: str, reason: str | None = None) -> str:
    """The path of a host tool, or a skip of the calling test that names the missing tool."""
    path = shutil.which(name)
    if path is None:
        pytest.skip(reason or f"{name} is missing on this host")
    return path


MEDIA_TOOLS = ("ffmpeg", "ffprobe")  # what the media-dependent (ffmpeg, video) tests need


# ---- subprocesses ----

KILL_WAIT_S = 5.0  # after a timeout: how long to wait for the pipes to close once the group is killed


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)  # the child leads its own group (start_new_session)
    except (ProcessLookupError, PermissionError):
        pass  # the whole group has already exited
    process.wait()


def run_bounded(
    argv: Sequence[str | os.PathLike[str]],
    *,
    timeout: float,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    input: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """subprocess.run for scripts under test, bounded in every way a script can hang a test run.

    - stdin is empty (/dev/null) unless input is given, so a `read` or a tool that reads stdin sees EOF.
    - The command starts a new session with no controlling terminal, so a tool that opens /dev/tty to
      prompt (git credentials, sudo, ssh) fails at once instead of waiting on the maintainer's terminal.
    - On timeout, or when the caller is interrupted (a time_limit() expiry included), the whole process
      group is killed, so no background child keeps running or keeps the output pipes open. A process
      that left the group (setsid) is not killed; the pipes are then read for KILL_WAIT_S at most.
    Returns the finished process with bytes output; raises subprocess.TimeoutExpired on timeout.
    """
    with subprocess.Popen(
        [os.fspath(arg) for arg in argv],
        cwd=os.fspath(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            try:
                stdout, stderr = process.communicate(timeout=KILL_WAIT_S)
            except subprocess.TimeoutExpired as late:  # a process outside the group holds the pipes
                stdout, stderr = late.stdout or b"", late.stderr or b""
            raise subprocess.TimeoutExpired(process.args, timeout, stdout, stderr) from None
        except BaseException:
            _kill_group(process)
            raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


# ---- the per-test time limit ----

DEFAULT_TIME_LIMIT_S = 120.0  # per setup, call and teardown of one test
TIME_LIMIT_ENV = "TESTS_TIME_LIMIT"  # seconds; 0 turns the limit off


class TimeLimitExceeded(BaseException):
    """A test phase ran past its time limit.

    A BaseException that is neither an Exception nor pytest's Failed, so Hypothesis does not treat it as a
    failing example and does not shrink it: a shrink would rerun the hang without any limit.
    """


def default_time_limit(environ: Mapping[str, str] = os.environ) -> float:
    """DEFAULT_TIME_LIMIT_S, or the seconds that TESTS_TIME_LIMIT names (0 or less: no limit)."""
    text = environ.get(TIME_LIMIT_ENV, "").strip()
    if not text:
        return DEFAULT_TIME_LIMIT_S
    try:
        return float(text)
    except ValueError:
        raise pytest.UsageError(f"{TIME_LIMIT_ENV} must be a number of seconds, not {text!r}") from None


def _other_thread_stacks() -> str:
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    me = threading.get_ident()
    stacks = [
        f"--- thread {names.get(ident, ident)} ---\n" + "".join(traceback.format_stack(frame))
        for ident, frame in sys._current_frames().items()
        if ident != me
    ]
    return "\n".join(stacks) or "(no other threads)"


@contextmanager
def time_limit(seconds: float | None, what: str = "the test") -> Iterator[None]:
    """Raises TimeLimitExceeded in the main thread once seconds have passed, so a hang fails the test with
    the main thread's traceback and every other thread's stack instead of stalling the run.

    Uses SIGALRM, which interrupts sleeps, lock and join waits, socket reads and subprocess waits. A call
    blocked inside C code that ignores signals (a DNS lookup) is interrupted when it returns. Without a
    limit (None or <= 0), off the main thread, or on a platform without setitimer, it does nothing.
    """
    usable = hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
    if not seconds or seconds <= 0 or not usable:
        yield
        return

    def expired(signum: int, frame: object) -> None:
        raise TimeLimitExceeded(
            f"{what} ran past its {seconds:g} s time limit (raise it with @pytest.mark.time_limit(seconds) "
            f"or {TIME_LIMIT_ENV}=<seconds>, 0 for none).\nOther threads:\n{_other_thread_stacks()}"
        )

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL if previous is None else previous)


# ---- Timing ----
# The package's own Timing (scripts/influencer/timing.py, task 15) holds the design's figures, so the
# `timing` fixture builds that class, and package code accepts what the fixture hands out.

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer.timing import Timing  # noqa: E402

# What the `timing` fixture hands out: the same ordering as the defaults (poll < silence < budget,
# request < object_info < preflight deadline), short enough that no test waits more than a few seconds.
SHORT_TIMING = Timing(
    request_s=0.5,
    object_info_s=1.0,
    upload_s=1.0,
    submit_s=1.0,
    plan_attempt_s=1.0,
    caption_attempt_s=0.5,
    preflight_deadline_s=2.0,
    poll_s=0.05,
    silence_s=0.2,
    silence_budget_s=1.0,
    interrupt_grace_s=0.3,
    external_request_s=0.5,
)

TIMING_FIELDS = tuple(field.name for field in dataclasses.fields(Timing))
