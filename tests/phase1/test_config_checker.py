"""Tests of the Config_Checker, scripts/influencer/checks/config_checker.py (task 25; design §C10; Req 26,
2.2-2.6, 25.4).

- Property 43: every injected config fault is reported with its file, line and rule; a fault-free
  configuration reports nothing and exits 0
- the runner: one report across groups, a group that raises is reported and the rest still run
- the groups a generated configuration cannot reach: presence, private data, provenance, ignore
- the Repo's own checkout reports nothing and `check-config` exits 0, as the Check_Suite's config step asserts
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

from helpers.harness import REPO, run_bounded
from helpers.stubs import StubPath

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer.checks import config_checker as C  # noqa: E402
from influencer.errors import Problem  # noqa: E402
from strategies import ConfigLists, config_lists, write_config  # noqa: E402
from strategies_config import faults, inject  # noqa: E402

SAFETY = REPO / "config" / "influencer" / "safety"
EXAMPLES = REPO / "config" / "influencer" / "examples"
LICENCES = REPO / "config" / "influencer" / "licences"
# The groups that read only the Catalog, the Config_Lists and the term lists (Property 43). The Selection
# equivalence on generated lists is Property 42's, and the git and file groups have unit tests below.
CONFIG_GROUPS = ("syntax", "tags", "catalog", "install-paths", "nodes", "patches", "nsfw", "terms", "provenance")

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is missing; the checker reads the repo with git")


def git_env(home: Path) -> dict[str, str]:
    """An environment in which git reads no user or system config and cannot climb above home."""
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


def temp_repo(root: Path, files: dict[str, str | bytes], *, stage: bool = True) -> tuple[Path, dict[str, str]]:
    """A git work tree at root/repo holding files, each staged (git tracks the index, so staged counts)."""
    repo = root / "repo"
    repo.mkdir(parents=True)
    environ = git_env(repo)
    git(repo, environ, "init", "-q")
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    if stage and files:
        git(repo, environ, "add", "-f", "--", *files)
    return repo, environ


def context(repo: Path, environ: dict[str, str] | None = None, *, config_dir: Path | None = None,
            safety_dir: Path = SAFETY) -> C.Context:  # fmt: skip
    return C.Context(repo, config_dir or repo / "config", safety_dir, environ or git_env(repo))


def valid_config() -> dict[str, str]:
    """A small valid configuration with one influencer-only line and its Req 2.4 block."""
    return {
        "config/catalog.txt": "base|yes||base id\ninfluencer|yes||stills\n",
        "config/models.list": (
            "# section\n"
            "lora|base.safetensors|hf|owner/repo::base.safetensors||wf:base\n"
            "\n"
            "# source: https://huggingface.co/owner/repo\n"
            "# licence: apache-2.0\n"
            "# verified: 2026-09-28\n"
            "lora|mine.safetensors|hf|owner/repo::mine.safetensors||wf:influencer\n"
        ),
        "config/nodes.txt": "https://github.com/owner/pack||wf:base\n",
        "config/ollama-models.txt": "qwen3:8b|wf:base\n",
        # presence also requires the Noncommercial_List and the conditions map (task 25.13)
        "config/influencer/licences/noncommercial.txt": (LICENCES / "noncommercial.txt").read_text(encoding="utf-8"),
        "config/influencer/licences/conditions.txt": (LICENCES / "conditions.txt").read_text(encoding="utf-8"),
    }


def keys(report: C.Report) -> set[tuple[str, str, int | None]]:
    return {(f.group, f.problem.path, f.problem.line) for f in report.findings}


# ---- Property 43 ----


# Feature: ai-influencer-pipeline, Property 43: The Config_Checker reports every injected fault
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(config=config_lists(), kinds=st.sets(faults("config"), max_size=5))
def test_property_43_every_injected_fault_is_reported(config: ConfigLists, kinds: set[str]) -> None:
    """**Validates: Requirements 26.1, 26.2, 26.3, 26.4, 26.5, 26.7, 26.15, 26.16**"""
    faulty, expected = inject(config, kinds)
    with tempfile.TemporaryDirectory(prefix="config-checker-") as folder:
        config_dir = write_config(Path(folder) / "config", faulty)
        ctx = C.Context(Path(folder), config_dir, SAFETY, dict(os.environ))
        report = C.run_checks(ctx, CONFIG_GROUPS)
    text = report.text()
    if not expected:
        assert report.findings == (), text
        assert report.exit_status == 0
        return
    assert report.exit_status != 0, text
    found = keys(report)
    wanted = {(e.group, f"config/{e.file}", e.line) for e in expected}  # the report is relative to the repo
    assert wanted <= found, text
    assert found <= wanted, f"a finding outside the injected faults:\n{text}"
    for e in expected:
        rendered = f"[{e.group}] config/{e.file}: line {e.line}:"
        lines = [line for line in text.splitlines() if line.startswith(rendered)]
        assert lines, text
        if e.also is not None:
            assert any(f"line {e.also}" in line or f"lines {e.also} " in line for line in lines), text


# ---- the runner ----


def test_one_report_with_path_line_and_rule_and_a_raising_group_is_its_own_failure(monkeypatch, tmp_path: Path) -> None:
    def broken(ctx: C.Context) -> list[Problem]:
        raise OSError("cannot read the thing")

    def one(ctx: C.Context) -> list[Problem]:
        return [Problem("config/models.list", "kind", 7, "'widget' is not a models.list kind")]

    groups = (C.Group("first", "Req x", broken), C.Group("second", "Req y", one))
    monkeypatch.setattr(C, "GROUPS", groups)
    monkeypatch.setattr(C, "GROUP_NAMES", ("first", "second"))
    report = C.run_checks(C.Context(tmp_path, tmp_path / "config", SAFETY))
    assert [f.group for f in report.findings] == ["first", "second"]
    assert "OSError: cannot read the thing" in report.findings[0].problem.reason
    lines = report.text().splitlines()
    assert lines[1] == "[second] config/models.list: line 7: kind: 'widget' is not a models.list kind"
    assert lines[-1] == "check-config: 2 failures (first 1, second 1)"
    assert report.exit_status != 0


def test_a_valid_config_passes_every_config_group_and_exits_0(tmp_path: Path) -> None:
    config_dir = write_config(tmp_path / "config", {k.split("/", 1)[1]: v for k, v in valid_config().items()})
    report = C.run_checks(C.Context(tmp_path, config_dir, SAFETY), CONFIG_GROUPS)
    assert report.findings == (), report.text()
    assert report.exit_status == 0
    assert report.text() == f"check-config: passed, no failure in {len(CONFIG_GROUPS)} rule groups\n"


def test_the_selection_differences_name_each_item() -> None:
    bash = b"[models.list]\na|b\nc|d\n[nodes.txt]\n[ollama-models.txt]\n"
    python = b"[models.list]\na|b\n[nodes.txt]\nn|\n[ollama-models.txt]\n"
    found = C.selection_differences("x", bash, python)
    assert found == [
        "[models.list] 'c|d' is in provision.sh's Selection only",
        "[nodes.txt] 'n|' is in the Python Selection only",
    ]


# ---- presence ----


def test_no_git_on_the_stub_path_is_named(tmp_path: Path, temp_env) -> None:
    stubs = StubPath(tmp_path / "stubs", temp_env, stubs=())
    assert shutil.which("git", path=str(stubs.bin)) is None
    config_dir = write_config(tmp_path / "config", {k.split("/", 1)[1]: v for k, v in valid_config().items()})
    ctx = C.Context(tmp_path, config_dir, SAFETY, {"PATH": str(stubs.bin)})
    report = C.run_checks(ctx, ["presence", "tokens", "private-data", "ignore"])
    assert [(f.group, f.problem.path) for f in report.findings] == [("presence", "git")], report.text()
    assert "git is not installed" in report.findings[0].problem.reason


@needs_git
def test_a_folder_that_is_no_work_tree_is_named(tmp_path: Path) -> None:
    folder = tmp_path / "plain"
    config_dir = write_config(folder / "config", {k.split("/", 1)[1]: v for k, v in valid_config().items()})
    report = C.run_checks(context(folder, config_dir=config_dir), ["presence"])
    assert [f.problem.reason for f in report.findings] == ["the Repo checkout is not a git work tree"]
    assert report.findings[0].problem.path == str(folder)


@needs_git
def test_a_missing_catalog_and_a_term_list_of_only_comments_are_named(tmp_path: Path) -> None:
    files = {k: v for k, v in valid_config().items() if not k.endswith("catalog.txt")}
    repo, environ = temp_repo(tmp_path, files)
    safety = tmp_path / "safety"
    safety.mkdir()
    shutil.copyfile(SAFETY / "resource-terms.txt", safety / "resource-terms.txt")
    (safety / "minor-terms.txt").write_text("# only a comment\n\n# and another\n", encoding="utf-8")
    report = C.run_checks(context(repo, environ, safety_dir=safety), ["presence"])
    reasons = {(f.problem.path, f.problem.reason) for f in report.findings}
    assert reasons == {
        ("config/catalog.txt", "is missing"),
        (str(safety / "minor-terms.txt"), "the term list lists no terms"),
    }, report.text()


# ---- private data ----


@needs_git
def test_tracked_private_files_outside_the_examples_fail_and_the_examples_do_not(tmp_path: Path) -> None:
    files = {
        "notes/persona.yaml": (EXAMPLES / "persona.yaml").read_bytes(),
        "data/week.csv": (EXAMPLES / "calendar.csv").read_bytes(),
        "config/influencer/examples/persona.yaml": (EXAMPLES / "persona.yaml").read_bytes(),
        "config/influencer/examples/calendar.csv": (EXAMPLES / "calendar.csv").read_bytes(),
        "config/influencer/examples/calendar.yaml": (EXAMPLES / "calendar.yaml").read_bytes(),
        "other.yaml": "name: not a persona\nversion: 1\n",
    }
    repo, environ = temp_repo(tmp_path, files)
    report = C.run_checks(context(repo, environ), ["private-data"])
    assert {f.problem.path for f in report.findings} == {"notes/persona.yaml", "data/week.csv"}, report.text()
    assert all(f.group == "private-data" for f in report.findings)


# ---- provenance ----


PROVENANCE_CASES = {
    "no block": (
        "lora|base.safetensors|hf|owner/repo::base.safetensors||wf:base\n"
        "lora|new.safetensors|hf|owner/repo::new.safetensors||wf:influencer\n",
        {2}, "has no '# source:' line",
    ),
    "hf repo only": (
        "# source: https://huggingface.co/owner/repo\n# licence: apache-2.0\n# verified: 2026-09-28\n"
        "lora|new.safetensors|hf|owner/repo||wf:influencer\n",
        {4}, "names only the repo",
    ),
    "github workflow without sha": (
        "# source: https://github.com/owner/repo\n# licence: MIT\n# verified: 2026-09-28\n"
        "workflow|w.json|url|https://raw.githubusercontent.com/owner/repo/main/w.json||wf:influencer\n",
        {4}, "40-character commit SHA",
    ),
    "commented out without reason": (
        "lora|base.safetensors|hf|owner/repo::base.safetensors||wf:base\n"
        "# lora|gone.safetensors|civitai|123|loras/looks|wf:influencer\n",
        {2}, "directly under a comment",
    ),
}


@pytest.mark.parametrize("case", PROVENANCE_CASES)
def test_provenance_failures(case: str, tmp_path: Path) -> None:
    models, lines, reason = PROVENANCE_CASES[case]
    files = {k.split("/", 1)[1]: v for k, v in valid_config().items()}
    files["models.list"] = models
    config_dir = write_config(tmp_path / "config", files)
    report = C.run_checks(C.Context(tmp_path, config_dir, SAFETY), ["provenance"])
    assert {f.problem.line for f in report.findings} == lines, report.text()
    assert any(reason in f.problem.reason for f in report.findings), report.text()


def test_a_commented_out_line_under_its_reason_and_block_passes(tmp_path: Path) -> None:
    files = {k.split("/", 1)[1]: v for k, v in valid_config().items()}
    files["models.list"] = (
        "# source: https://civitai.com/models/1\n# licence: Civitai permissions: commercial use RentCivit\n"
        "# verified: 2026-09-28\n# Commented out: fails the Licence_Gate.\n"
        "# lora|gone.safetensors|civitai|123|loras/looks|wf:influencer\n"
    )
    config_dir = write_config(tmp_path / "config", files)
    report = C.run_checks(C.Context(tmp_path, config_dir, SAFETY), ["provenance"])
    assert report.findings == (), report.text()


# ---- ignore ----


@needs_git
def test_a_gitignore_pattern_that_matches_a_tracked_file_is_reported(tmp_path: Path) -> None:
    repo, environ = temp_repo(tmp_path, {".gitignore": "*.log\n", "keep.txt": "x\n", "run.log": "y\n"})
    report = C.run_checks(context(repo, environ), ["ignore"])
    assert [(f.group, f.problem.path) for f in report.findings] == [("ignore", "run.log")], report.text()


# ---- the Repo's own checkout ----


@needs_git
def test_the_repo_checkout_reports_nothing_and_check_config_exits_0(tmp_path: Path) -> None:
    environ = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(SCRIPTS),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = run_bounded([sys.executable, "-m", "influencer", "check-config"], cwd=REPO, env=environ, timeout=240)
    out = result.stdout.decode("utf-8", "replace")
    assert result.returncode == 0, out + result.stderr.decode("utf-8", "replace")
    assert out == f"check-config: passed, no failure in {len(C.GROUPS)} rule groups\n"
