"""Tests of the token scan, scripts/influencer/checks/tokens.py, through the Config_Checker's tokens group
(task 25.7, 25.9; design §C10; Req 26.9).

- Property 44: a token of any family at any line of a committed or staged file is reported by file and line,
  and the report never contains it; near misses and hex strings without a keyword are not reported
- an unstaged edit is found in the working tree, a binary file is skipped, find_tokens on its own

Every token here is generated or built in the test; no assertion shows a matched string, so a failing run
cannot print one either.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer.checks import config_checker as C  # noqa: E402
from influencer.checks import tokens as T  # noqa: E402
from strategies import texts  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is missing; the scan reads the index with git")

FILE = "notes.txt"
ALNUM = "A-Za-z0-9"

# One strategy per Req 26.9 format, each with the keyword line context the hex family needs.
TOKENS = {
    "hf": st.from_regex(rf"hf_[{ALNUM}]{{30,40}}", fullmatch=True),
    "ghp": st.from_regex(rf"gh[pousr]_[{ALNUM}]{{36}}", fullmatch=True),
    "pat": st.from_regex(rf"github_pat_[{ALNUM}_]{{22,40}}", fullmatch=True),
    "hex32": st.from_regex(r"[0-9a-fA-F]{32}", fullmatch=True),
    "hex64": st.from_regex(r"[0-9a-fA-F]{64}", fullmatch=True),
}
KEYWORDS = ("civitai", "VAST", "Token", "bearer", "CIVITAI_TOKEN=")

# Strings one character short of a format, and hex strings on a line with no keyword: never reported.
NEAR_MISSES = st.one_of(
    st.from_regex(rf"hf_[{ALNUM}]{{29}}", fullmatch=True),
    st.from_regex(rf"gh[pousr]_[{ALNUM}]{{35}}", fullmatch=True),
    st.from_regex(rf"github_pat_[{ALNUM}_]{{21}}", fullmatch=True),
    st.from_regex(r"[0-9a-f]{31}", fullmatch=True).map(lambda h: f"token {h}"),
    st.from_regex(r"[0-9a-f]{63}", fullmatch=True).map(lambda h: f"bearer {h}"),
    st.from_regex(r"[0-9a-f]{32}|[0-9a-f]{64}", fullmatch=True).map(lambda h: f"checksum {h}"),
)
CONTEXT = texts(single_line=True, max_size=30).map(lambda t: t.replace("\0", ""))


def git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CEILING_DIRECTORIES": str(home.parent),
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }  # fmt: skip


def git(repo: Path, environ: dict[str, str], *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=repo, env=environ, capture_output=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def repo_with(root: Path, content: bytes, *, commit: bool) -> tuple[Path, dict[str, str]]:
    repo = root / "repo"
    repo.mkdir()
    environ = git_env(repo)
    git(repo, environ, "init", "-q")
    (repo / FILE).write_bytes(content)
    git(repo, environ, "add", "--", FILE)
    if commit:
        git(repo, environ, "commit", "-q", "--no-verify", "-m", "x")
    return repo, environ


def token_report(repo: Path, environ: dict[str, str]) -> C.Report:
    return C.run_checks(C.Context(repo, repo / "config", repo / "safety", environ), ["tokens"])


# ---- Property 44 ----


@st.composite
def token_files(draw: st.DrawFn) -> tuple[str, bytes, int]:
    """(token, file content, the token's 1-based line): context lines, one near-miss line, one token line."""
    family = draw(st.sampled_from(sorted(TOKENS)))
    token = draw(TOKENS[family])
    line = f"{draw(st.sampled_from(KEYWORDS))} {token}" if family.startswith("hex") else f"key = {token} end"
    lines = draw(st.lists(CONTEXT, max_size=6))
    lines.insert(draw(st.integers(0, len(lines))), draw(NEAR_MISSES))
    at = draw(st.integers(0, len(lines)))
    lines.insert(at, line)
    return token, "\n".join(lines).encode("utf-8") + b"\n", at + 1


# Feature: ai-influencer-pipeline, Property 44: Tokens in tracked files are found and never echoed
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(case=token_files(), commit=st.booleans())
def test_property_44_tokens_are_found_and_never_echoed(case: tuple[str, bytes, int], commit: bool) -> None:
    """**Validates: Requirements 26.9**"""
    token, content, line = case
    with tempfile.TemporaryDirectory(prefix="tokens-") as folder:
        repo, environ = repo_with(Path(folder), content, commit=commit)
        report = token_report(repo, environ)
    reported = [(f.problem.path, f.problem.line) for f in report.findings]
    assert reported == [(FILE, line)]
    echoed = token in report.text()
    assert not echoed, "the report echoes the token"
    assert report.exit_status != 0


# ---- examples ----


def _hf(length: int = 34) -> str:
    return "hf_" + ("Ab3" * 20)[:length]


def test_an_unstaged_edit_is_found_in_the_working_tree(tmp_path: Path) -> None:
    repo, environ = repo_with(tmp_path, b"clean\n", commit=True)
    (repo / FILE).write_text(f"clean\nexport HF_TOKEN={_hf()}\n", encoding="utf-8")
    report = token_report(repo, environ)
    assert [(f.problem.path, f.problem.line) for f in report.findings] == [(FILE, 2)]
    assert "the working tree" in report.findings[0].problem.reason
    assert "the index" not in report.findings[0].problem.reason


def test_a_staged_token_is_found_in_both_copies(tmp_path: Path) -> None:
    repo, environ = repo_with(tmp_path, f"a\nb {_hf()}\n".encode(), commit=False)
    [finding] = token_report(repo, environ).findings
    assert (finding.problem.line, "the index and the working tree" in finding.problem.reason) == (2, True)


def test_a_binary_file_is_skipped(tmp_path: Path) -> None:
    repo, environ = repo_with(tmp_path, b"\0\x01bin " + _hf().encode() + b"\n", commit=False)
    assert token_report(repo, environ).findings == ()


def test_find_tokens_families_and_boundaries() -> None:
    hex32 = "0123456789abcdef" * 2
    data = "\n".join([
        f"x {_hf(30)}",                       # 1 hf, exactly 30
        f"x {_hf(29)} y",                     # 2 one short
        "ghp_" + "a1" * 18,                   # 3 github, 36
        "github_pat_" + "a_" * 11,            # 4 github_pat_, 22
        f"Bearer {hex32}",                    # 5 hex with a keyword
        f"sha {hex32}",                       # 6 hex, no keyword
        f"token x{hex32}",                    # 7 a letter before it
        f"vast {hex32}0",                     # 8 33 hex characters
    ]).encode()  # fmt: skip
    assert T.find_tokens(data) == [(1, T.HF), (3, T.GITHUB), (4, T.GITHUB), (5, T.HEX)]
