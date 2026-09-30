"""The bash stub PATH (task 3.4, design Testing Strategy "Bash tests", Req 27.2).

A StubPath is a folder that a script under test gets as its whole PATH. It holds:

- stubs for curl, wget, hf, git, ollama, apt-get, rclone, nvidia-smi and uv. Each records its call and exits
  with a scripted status, so nothing is downloaded, cloned, pulled or installed.
- links to the real host tools the script needs (dirname, sed, awk, ...), found on the PATH of the process
  running the tests.

Every call is recorded twice under <root>/state/:
- calls.log, the log file: one line per call, the stub's name and its arguments shell-quoted (log_lines())
- calls/<n>/, the exact record: argv (NUL-separated), working folder, exit status and, when asked, stdin
  (calls())

Adding another stub is one line, for example `stubs.stub("flock")` or `stubs.stub("timeout", 124)`.
StubPath.run() runs a repo script under a chosen bash (default /bin/bash, bash 3.2 on macOS) with that PATH,
the TempEnv variables, and HOME and TMPDIR inside the temp env. Nothing else is inherited from the caller, so
BASH_ENV, a GIT_DIR or a real token in the maintainer's shell cannot reach the run. The run has empty stdin,
no controlling terminal and a timeout, and a timeout kills every process the script started
(helpers.harness.run_bounded).

A stub reads stdin only when capture_stdin=True. Reading it by default would drain the input of a
`while read ...; done < file` loop around the call, and change the behaviour of the script under test.

Every stub is a symlink to one stub program per test process. macOS checks each new executable file the
first time it runs (about 0.3 s), so a program per stub would add that to every stub of every test.
"""
from __future__ import annotations

import atexit
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from helpers.harness import REPO, TempEnv, run_bounded

BASH = "/bin/bash"

# The binaries that would reach the network or change the host.
DEFAULT_STUBS = ("curl", "wget", "hf", "git", "ollama", "apt-get", "rclone", "nvidia-smi", "uv")

# Real tools linked by default: the POSIX utilities the repo's scripts call. A host that lacks one simply
# does not get the link; StubPath.link() is strict instead.
DEFAULT_TOOLS = (
    "awk", "basename", "cat", "chmod", "cmp", "cp", "cut", "date", "df", "dirname", "du", "env", "find",
    "grep", "head", "id", "ln", "ls", "mkdir", "mktemp", "mv", "od", "readlink", "rm", "rmdir", "sed",
    "sleep", "sort", "stat", "tail", "tee", "touch", "tr", "uname", "uniq", "wc", "xargs",
)  # fmt: skip

# Where link() looks besides the caller's PATH.
_FALLBACK_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

STUB_ERROR_STATUS = 97  # a stub that cannot record its call exits with this and a message on stderr

# The stub program: bash 3.2 safe, and it calls mkdir and cat by absolute path, so it works whatever PATH the
# script under test hands it. $0 is the symlink it was called through, <root>/bin/<name>, so the name is its
# last part and the state is <root>/state. The scripted files live in state/stubs/<name>/: status (one exit
# status per line; the Nth call takes line N, later calls the last line), stdout, stderr, body.sh and
# capture_stdin. Taking a call number retries mkdir a bounded number of times, so a missing state folder
# fails the call instead of looping forever.
_STUB_PROGRAM = """#!/bin/bash
# The stub program of tests/helpers/stubs.py: records this call, then exits with its scripted status.
case "$0" in */*) __stub_bin="${{0%/*}}" ;; *) __stub_bin=. ;; esac
__stub_name="${{0##*/}}"
__stub_state="$__stub_bin/../state"
__stub_conf="$__stub_state/stubs/$__stub_name"
__stub_fail() {{ printf 'stub %s: %s\\n' "$__stub_name" "$1" >&2; exit {error}; }}
[ -d "$__stub_conf" ] || __stub_fail "no scripted state in $__stub_conf"
__stub_seq() {{ # $1: folder; creates its first free numbered entry and prints the number
  local n=1 e tries=0
  [ -d "$1" ] || return 1
  for e in "$1"/*; do [ -e "$e" ] && n=$((n + 1)); done
  until {mkdir_q} "$1/$n" 2>/dev/null; do
    n=$((n + 1))
    tries=$((tries + 1))
    [ "$tries" -le 1000 ] || return 1
  done
  printf '%s' "$n"
}}
__stub_n="$(__stub_seq "$__stub_state/calls")" || __stub_fail "cannot record the call in $__stub_state/calls"
__stub_call="$__stub_state/calls/$__stub_n"
__stub_index="$(__stub_seq "$__stub_conf/count")" || __stub_fail "cannot count the call in $__stub_conf/count"
printf '%s' "$__stub_name" > "$__stub_call/name"
if [ "$#" -gt 0 ]; then printf '%s\\0' "$@" > "$__stub_call/argv"; else : > "$__stub_call/argv"; fi
printf '%s' "$PWD" > "$__stub_call/cwd"
__stub_line="$__stub_name"
for __stub_a in "$@"; do printf -v __stub_q '%q' "$__stub_a"; __stub_line="$__stub_line $__stub_q"; done
printf '%s\\n' "$__stub_line" >> "$__stub_state/calls.log"
trap 'printf "%s" "$?" > "$__stub_call/status"' EXIT
if [ -f "$__stub_conf/capture_stdin" ]; then {cat_q} > "$__stub_call/stdin"; fi
status=0
__stub_i=0
while IFS= read -r __stub_s || [ -n "$__stub_s" ]; do
  __stub_i=$((__stub_i + 1))
  status="$__stub_s"
  [ "$__stub_i" -ge "$__stub_index" ] && break
done < "$__stub_conf/status"
if [ -f "$__stub_conf/stdout" ]; then {cat_q} "$__stub_conf/stdout"; fi
if [ -f "$__stub_conf/stderr" ]; then {cat_q} "$__stub_conf/stderr" >&2; fi
if [ -f "$__stub_conf/body.sh" ]; then . "$__stub_conf/body.sh"; fi
exit "$status"
"""

_programs: dict[tuple[str, str], Path] = {}
_programs_lock = threading.Lock()


def stub_program(mkdir: str, cat: str) -> Path:
    """The stub program of this process for these mkdir and cat paths, written on first use into a private
    temp folder (mode 0700) that is removed at exit."""
    key = (mkdir, cat)
    with _programs_lock:
        program = _programs.get(key)
        if program is None or not program.is_file():
            folder = Path(tempfile.mkdtemp(prefix="test-stub-program-"))
            atexit.register(shutil.rmtree, folder, True)
            program = folder / "stub"
            text = _STUB_PROGRAM.format(error=STUB_ERROR_STATUS, mkdir_q=shlex.quote(mkdir), cat_q=shlex.quote(cat))
            program.write_text(text, encoding="utf-8")
            program.chmod(0o755)
            _programs[key] = program
    return program


@dataclass(frozen=True)
class Call:
    """One recorded stub call."""

    name: str
    args: tuple[str, ...]
    cwd: str
    stdin: bytes | None  # None unless the stub was made with capture_stdin=True
    status: int | None  # None while the call is still running


def _as_bytes(data: str | bytes) -> bytes:
    return data.encode("utf-8") if isinstance(data, str) else data


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


class StubPath:
    """A PATH folder of recording stubs and links to real tools (see the module docstring)."""

    def __init__(
        self,
        root: Path,
        env: TempEnv,
        *,
        stubs: Iterable[str] = DEFAULT_STUBS,
        tools: Iterable[str] = DEFAULT_TOOLS,
        host_path: str | None = None,
    ) -> None:
        self.root = Path(root).absolute()  # the stubs find their state through the absolute PATH entry
        self.env = env
        self.bin = self.root / "bin"  # the PATH of a run
        self.state = self.root / "state"  # the call log and the scripted answers
        self.host_path = (host_path if host_path is not None else os.environ.get("PATH", ""))
        self.host_path = f"{self.host_path}{os.pathsep}{_FALLBACK_PATH}" if self.host_path else _FALLBACK_PATH
        mkdir = shutil.which("mkdir", path=self.host_path)
        cat = shutil.which("cat", path=self.host_path)
        if mkdir is None or cat is None:
            raise FileNotFoundError("the stubs need mkdir and cat on the host")
        self._program = stub_program(os.path.abspath(mkdir), os.path.abspath(cat))
        for folder in (self.bin, self.state / "calls", self.state / "stubs"):
            folder.mkdir(parents=True, exist_ok=True)
        self.log.touch()
        for name in stubs:
            self.stub(name)
        for tool in tools:
            if shutil.which(tool, path=self.host_path) is not None:
                self.link(tool)

    # ---- building the PATH ----

    def stub(
        self,
        name: str,
        status: int | Sequence[int] = 0,
        *,
        stdout: str | bytes = b"",
        stderr: str | bytes = b"",
        body: str | None = None,
        capture_stdin: bool = False,
    ) -> Path:
        """Adds or re-scripts the stub `name`, and restarts its status sequence.

        status is one exit status, or one per call in order (the last one repeats). stdout and stderr are
        printed by every call. body is bash that runs after the call is recorded and the output printed,
        with "$@" and $status set; it may print, create files, change $status or exit.
        """
        if "/" in name or not name or name in (".", ".."):
            raise ValueError(f"not a command name: {name!r}")
        statuses = [status] if isinstance(status, int) else list(status)
        if not statuses or not all(isinstance(s, int) and 0 <= s <= 255 for s in statuses):
            raise ValueError(f"exit statuses must be 0-255: {status!r}")
        conf = self.state / "stubs" / name
        if conf.exists():
            shutil.rmtree(conf)
        (conf / "count").mkdir(parents=True)
        (conf / "status").write_text("".join(f"{s}\n" for s in statuses), encoding="ascii")
        if stdout:
            (conf / "stdout").write_bytes(_as_bytes(stdout))
        if stderr:
            (conf / "stderr").write_bytes(_as_bytes(stderr))
        if body is not None:
            (conf / "body.sh").write_text(body + "\n", encoding="utf-8")
        if capture_stdin:
            (conf / "capture_stdin").touch()
        target = self.bin / name
        self._symlink(name, str(self._program))
        return target

    def is_stub(self, name: str) -> bool:
        return (self.state / "stubs" / name).is_dir()

    def link(self, *names: str) -> None:
        """Links real host tools into the PATH; a name that is missing on the host, or a stub, is an error."""
        for name in names:
            if self.is_stub(name):
                raise ValueError(f"{name} is a stub in this PATH; it cannot also be the real tool")
            real = shutil.which(name, path=self.host_path)
            if real is None:
                raise FileNotFoundError(f"{name} is not on this host's PATH")
            self._symlink(name, os.path.abspath(real))

    def _symlink(self, name: str, target: str) -> None:
        link = self.bin / name
        if link.is_symlink() or link.exists():
            if link.is_symlink() and os.readlink(link) == target:
                return
            link.unlink()
        link.symlink_to(target)

    # ---- the call log ----

    @property
    def log(self) -> Path:
        """The log file: one line per call, `<name> <arg> ...`, each argument quoted as bash's printf %q."""
        return self.state / "calls.log"

    def log_lines(self) -> list[str]:
        """The log file's lines, in the order the calls were recorded."""
        return _decode(self.log.read_bytes()).splitlines()

    def calls(self, name: str | None = None) -> list[Call]:
        """Every recorded call in the order the calls started, optionally only those of one stub."""
        found = []
        for entry in (self.state / "calls").iterdir():
            if not entry.name.isdigit() or not (entry / "name").is_file():
                continue  # a call that is just starting
            call_name = _decode((entry / "name").read_bytes())
            if name is not None and call_name != name:
                continue
            argv = (entry / "argv").read_bytes() if (entry / "argv").is_file() else b""
            args = tuple(_decode(a) for a in argv.split(b"\0")[:-1]) if argv else ()
            stdin = (entry / "stdin").read_bytes() if (entry / "stdin").is_file() else None
            status_file = entry / "status"
            status_text = status_file.read_text(encoding="ascii").strip() if status_file.is_file() else ""
            cwd = _decode((entry / "cwd").read_bytes()) if (entry / "cwd").is_file() else ""
            found.append(
                (int(entry.name), Call(call_name, args, cwd, stdin, int(status_text) if status_text else None))
            )
        return [call for _, call in sorted(found, key=lambda item: item[0])]

    def clear(self) -> None:
        """Forgets the recorded calls, empties the log file and restarts every status sequence."""
        shutil.rmtree(self.state / "calls")
        (self.state / "calls").mkdir()
        self.log.write_bytes(b"")
        for conf in (self.state / "stubs").iterdir():
            shutil.rmtree(conf / "count")
            (conf / "count").mkdir()

    # ---- running a script ----

    def environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """The whole environment of a run: PATH, HOME, TMPDIR, the TempEnv variables, then extra."""
        env = {"PATH": str(self.bin), "HOME": str(self.env.home), "TMPDIR": str(self.env.tmp)}
        env.update(self.env.variables())
        env.update(extra or {})
        return env

    def run(
        self,
        script: str | os.PathLike[str],
        *args: str | os.PathLike[str],
        bash: str = BASH,
        cwd: str | os.PathLike[str] | None = None,
        extra_env: Mapping[str, str] | None = None,
        input: bytes | None = None,
        timeout: float = 120,
    ) -> subprocess.CompletedProcess[bytes]:
        """Runs `bash script args...` with this PATH and returns the finished process (bytes output).

        A relative script path is taken from the repo root. The run starts in the temp env's root unless cwd
        is given, so a stray relative write cannot land in the checkout. `bash` on the PATH is linked to the
        chosen bash, so a nested `bash x.sh` runs the same version. stdin is empty unless input is given.
        After timeout seconds every process of the run is killed and subprocess.TimeoutExpired is raised.
        """
        script_path = Path(script)
        if not script_path.is_absolute():
            script_path = REPO / script_path
        bash_path = bash if os.path.isabs(bash) else shutil.which(bash, path=self.host_path)
        if bash_path is None or not os.access(bash_path, os.X_OK):
            raise FileNotFoundError(f"no executable bash at {bash}")
        self._symlink("bash", bash_path)
        return run_bounded(
            [bash_path, script_path, *args],
            cwd=cwd if cwd is not None else self.env.root,
            env=self.environment(extra_env),
            input=input,
            timeout=timeout,
        )
