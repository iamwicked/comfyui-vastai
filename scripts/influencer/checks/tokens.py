"""The Config_Checker's token scan (design §C10 row "Tokens"; Req 26.9).

Every file git tracks is scanned twice: its index blob, read through `git cat-file --batch`, so a newly
staged file is covered, and its working-tree copy, so an unstaged edit is covered too. A file git treats as
binary (a NUL byte in its first 8000 bytes, as git's own test, or the `binary` or `-diff` attribute) is
skipped. The three Req 26.9 families:

- Hugging Face: `hf_` followed by 30 or more letters or digits
- GitHub: `ghp_`, `gho_`, `ghu_`, `ghs_` or `ghr_` followed by 36 letters or digits, or `github_pat_`
  followed by 22 or more letters, digits or underscores
- Civitai and Vast.ai: exactly 32 or exactly 64 hex characters with no letter or digit on either side, on a
  line that also holds `civitai`, `vast`, `token` or `bearer` in any letter case

A finding gives the file path, the 1-based line number, the family and where the copy is (index, working
tree), and never the matched string: nothing in this module puts a match into a message.

    find_tokens(data) -> [(line, family)]      pure, over one file's bytes
    scan(git, repo) -> [Problem]               every tracked file; git runs the read-only git commands
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from subprocess import CompletedProcess

from influencer.errors import Problem

HF, GITHUB, HEX = "Hugging Face", "GitHub", "Civitai or Vast.ai"

_FAMILIES: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (HF, re.compile(rb"hf_[A-Za-z0-9]{30}")),
    (GITHUB, re.compile(rb"gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22}")),
)
_HEX = re.compile(rb"(?<![A-Za-z0-9])(?:[0-9A-Fa-f]{64}|[0-9A-Fa-f]{32})(?![A-Za-z0-9])")
_HEX_CONTEXT = re.compile(rb"civitai|vast|token|bearer", re.I)

BINARY_PROBE = 8000  # git's buffer_is_binary() looks at this many leading bytes
INDEX, WORK_TREE = "the index", "the working tree"
GITLINK = "160000"

Git = Callable[..., "CompletedProcess[bytes]"]  # Context.git: (*args, input=...) -> CompletedProcess


def is_binary(data: bytes) -> bool:
    """git's heuristic: a NUL byte in the first 8000 bytes."""
    return b"\0" in data[:BINARY_PROBE]


def find_tokens(data: bytes) -> list[tuple[int, str]]:
    """(1-based line number, family) of every line holding a token, each family once per line, in line
    order. The matched text itself is never returned."""
    found = []
    for number, line in enumerate(data.split(b"\n"), 1):
        for family, pattern in _FAMILIES:
            if pattern.search(line):
                found.append((number, family))
        if _HEX_CONTEXT.search(line) and _HEX.search(line):
            found.append((number, HEX))
    return found


def _git_ok(result: CompletedProcess[bytes], what: str) -> bytes:
    if result.returncode != 0:
        raise RuntimeError(f"git {what} failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result.stdout


def _index_entries(git: Git) -> list[tuple[str, str, str]]:
    """(mode, object id, path) of every index entry, gitlinks left out."""
    out = _git_ok(git("ls-files", "-z", "--stage", "--full-name"), "ls-files --stage")
    entries = []
    for record in out.split(b"\0"):
        if not record:
            continue
        info, _, path = record.partition(b"\t")
        mode, object_id, _stage = info.decode("ascii").split(" ")
        if mode != GITLINK:
            entries.append((mode, object_id, os.fsdecode(path)))
    return entries


def _binary_by_attribute(git: Git, paths: Iterable[str]) -> set[str]:
    """The paths whose `binary` attribute is set or whose `diff` attribute is unset."""
    listed = list(dict.fromkeys(paths))
    if not listed:
        return set()
    stdin = b"".join(os.fsencode(path) + b"\0" for path in listed)
    out = _git_ok(git("check-attr", "-z", "--stdin", "binary", "diff", input=stdin), "check-attr")
    parts = out.split(b"\0")
    binary = set()
    for index in range(0, len(parts) - 2, 3):
        path, attribute, value = parts[index : index + 3]
        if (attribute, value) in ((b"binary", b"set"), (b"diff", b"unset")):
            binary.add(os.fsdecode(path))
    return binary


def _blobs(git: Git, object_ids: list[str]) -> dict[str, bytes]:
    """object id -> content, through one `git cat-file --batch`."""
    if not object_ids:
        return {}
    unique = list(dict.fromkeys(object_ids))
    stdin = b"".join(object_id.encode("ascii") + b"\n" for object_id in unique)
    out = _git_ok(git("cat-file", "--batch", input=stdin), "cat-file --batch")
    blobs: dict[str, bytes] = {}
    position = 0
    for object_id in unique:
        end = out.index(b"\n", position)
        header = out[position:end].split(b" ")
        position = end + 1
        if len(header) != 3:  # "<id> missing"
            continue
        size = int(header[2])
        blobs[object_id] = out[position : position + size]
        position += size + 1  # the content and its trailing newline
    return blobs


def _work_tree_copy(repo: Path, path: str) -> bytes | None:
    """The working-tree file's bytes; None for a deleted file, a folder or a symlink (its target is the
    index blob's content, scanned there)."""
    full = repo / path
    if full.is_symlink() or not full.is_file():
        return None
    try:
        return full.read_bytes()
    except OSError:
        return None


def scan(git: Git, repo: Path) -> list[Problem]:
    """A Problem for each (file, line, family) holding a token, in path order, saying whether the index
    blob, the working-tree copy or both hold it. The reason never contains the match."""
    entries = _index_entries(git)
    skip = _binary_by_attribute(git, (path for _, _, path in entries))
    blobs = _blobs(git, [object_id for _, object_id, path in entries if path not in skip])
    places: dict[tuple[str, int, str], list[str]] = {}
    for _mode, object_id, path in entries:
        if path in skip:
            continue
        copies = ((INDEX, blobs.get(object_id)), (WORK_TREE, _work_tree_copy(repo, path)))
        for where, data in copies:
            if data is None or is_binary(data):
                continue
            for number, family in find_tokens(data):
                seen = places.setdefault((path, number, family), [])
                if where not in seen:
                    seen.append(where)
    return [
        Problem(path, None, number, f"holds a {family} token in {' and '.join(where)}; remove it and revoke the token")
        for (path, number, family), where in sorted(places.items())
    ]
