"""write_atomic: replace a file so that every reader sees its old bytes or its new bytes, never a part
(design §C6; the Manifest's crash-safe writes of Req 10.9 go through it).

The data goes into a temp file in the target's own folder, which is fsynced, closed and moved over the
target with os.replace; the folder is fsynced last so the rename itself is on disk. On any failure the
temp file is removed and the target is left as it was. A process killed mid-write can leave one
`.<name>.<random>.tmp` file behind, never a partial target.

A new file gets the usual permissions (0o666 less the umask); a replaced file keeps its permission bits.
The folder must exist: write_atomic creates no folders.
"""
from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path

TEMP_SUFFIX = ".tmp"
_TEMP_ATTEMPTS = 100
# Folder fsync is refused by some filesystems (FUSE mounts, some network shares); the rename stands anyway.
_FSYNC_UNSUPPORTED = frozenset({errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP})


def temp_name(target: str) -> str:
    """A fresh temp file name for target: `.<target>.<16 hex digits>.tmp`."""
    return f".{target}.{secrets.token_hex(8)}{TEMP_SUFFIX}"


def _create_temp(folder: Path, target: str) -> tuple[int, Path]:
    for _ in range(_TEMP_ATTEMPTS):
        temp = folder / temp_name(target)
        try:
            return os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666), temp
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, f"no free temp file name after {_TEMP_ATTEMPTS} tries", str(folder))


def _fsync_folder(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    except OSError as error:
        if error.errno not in _FSYNC_UNSUPPORTED:
            raise
    finally:
        os.close(fd)


def write_atomic(path: str | os.PathLike[str], data: bytes | bytearray | memoryview | str) -> None:
    """Writes data (bytes, or str as UTF-8) to path atomically; see the module docstring."""
    if isinstance(data, str):
        payload = data.encode("utf-8")
    elif isinstance(data, (bytes, bytearray, memoryview)):
        payload = bytes(data)
    else:
        raise TypeError(f"write_atomic takes bytes or str, not {type(data).__name__}")
    target = Path(path)
    folder = target.parent
    try:
        info = os.stat(target)
        mode = stat.S_IMODE(info.st_mode) if stat.S_ISREG(info.st_mode) else None
    except FileNotFoundError:
        mode = None
    fd, temp = _create_temp(folder, target.name)
    try:
        with os.fdopen(fd, "wb") as handle:
            if mode is not None:
                os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
    _fsync_folder(folder)
