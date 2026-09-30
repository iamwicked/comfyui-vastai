"""ProcessLock: at most one process per lock file (design §C6; Req 10.10, 18.12, 20.8).

The lock is fcntl.flock(LOCK_EX | LOCK_NB) on the lock file, and the holder writes its PID into the file.
The kernel drops the lock when the holder exits or is killed, so a crashed run never blocks the next one;
the file itself stays, and is never deleted, because deleting a lock file lets two processes hold locks
on two different files of one name.

    with ProcessLock(week / ".lock"):
        ...                              # LockHeld when another process holds it

A refused acquire raises LockHeld, whose pid is the holder's PID as the lock file records it; __main__ turns
it into exit status 4 and reports the PID. ProcessLock.holder_pid() reads the same value at any time. The
lock file's folder must exist. The descriptor is not inherited by child processes.
"""
from __future__ import annotations

import errno
import fcntl
import os
import time
from pathlib import Path

# A holder writes its PID just after taking the lock, so an empty file is re-read for a short while.
_PID_READ_ATTEMPTS = 10
_PID_READ_PAUSE_S = 0.02
_BUSY = frozenset({errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES})


class LockHeld(Exception):
    """Another process holds the lock at path; pid is its PID, or None when the lock file names none."""

    def __init__(self, path: str | os.PathLike[str], pid: int | None) -> None:
        super().__init__(path, pid)
        self.path = Path(path)
        self.pid = pid

    def __str__(self) -> str:
        holder = f"PID {self.pid}" if self.pid is not None else "PID unknown"
        return f"another process holds the lock {self.path} ({holder})"


def read_pid(path: str | os.PathLike[str]) -> int | None:
    """The PID recorded in a lock file, or None when the file is missing, empty or holds no PID."""
    try:
        text = Path(path).read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None
    return int(text) if text.isascii() and text.isdigit() and int(text) > 0 else None


class ProcessLock:
    """An exclusive, non-blocking lock on one file; see the module docstring."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    @property
    def held(self) -> bool:
        """True between a successful acquire() and release()."""
        return self._fd is not None

    def acquire(self) -> ProcessLock:
        """Takes the lock and records this process's PID in the file, or raises LockHeld."""
        if self._fd is not None:
            raise RuntimeError(f"{self.path} is already held by this ProcessLock")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)  # no O_TRUNC: the holder's PID stays
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno in _BUSY:
                raise LockHeld(self.path, self.holder_pid()) from None
            raise
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{os.getpid()}\n".encode("ascii"), 0)
        except BaseException:
            os.close(fd)  # closing the only descriptor releases the lock
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        """Clears the recorded PID and drops the lock; does nothing when the lock is not held."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)
        finally:
            os.close(fd)

    def holder_pid(self) -> int | None:
        """The PID recorded in the lock file: the holder's while another process holds the lock. An empty
        file is re-read briefly, because a new holder writes its PID just after taking the lock."""
        for attempt in range(_PID_READ_ATTEMPTS):
            pid = read_pid(self.path)
            if pid is not None or attempt == _PID_READ_ATTEMPTS - 1:
                return pid
            time.sleep(_PID_READ_PAUSE_S)
        return None

    def __enter__(self) -> ProcessLock:
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()
