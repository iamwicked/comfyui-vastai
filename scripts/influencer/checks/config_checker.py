"""The Config_Checker: `influencer.sh check-config`, step `config` of the Check_Suite (design §C10; Req 26,
2.2-2.6, 25.4).

Every rule group of the §C10 table runs in one pass over the Repo checkout and `git`, offline, and every
group's Problems join one report (Req 26.15). Each report line is

    [<group>] <file>: line <n>: <field>: <reason>

where the group names the rule, and the parts that do not apply are left out. A Problem that concerns two
lines (a repeated install path, id or repo) carries the later line and names the earlier one in its reason.
A group that raises is reported as a failure of its own, and the remaining groups still run. The run exits
0 only when it reported no failure (Req 26.16).

API, for the command and for the tests:

    Context(repo, config_dir, safety_dir, environ)   what a run reads; Context.for_repo() is the command's
    run_checks(ctx, groups=GROUP_NAMES) -> Report     the named groups, in GROUPS order
    Report.findings, .ok, .exit_status, .text()       the Findings (group, Problem) and the printout
    check_config(args) -> int                         the check-config handler of __main__.COMMANDS

The groups, in GROUPS order (design §C10 rows):

    presence       git, the work tree, the Catalog and Config_Lists, the two term lists, the Noncommercial_List
                   with an identifier and the conditions map (Req 26.17)
    licence        the Licence_Gate: every models.list and ollama-models.txt line tagged with an Influencer_Id
                   has a passing `# licence:` record in its Req 2.4 block (Req 26.18; licences.py)
    syntax         field counts, kinds, sources, source ids, filename, subdir, tag placement (Req 26.2)
    tags           every wf: id is a Catalog id; no empty id (Req 26.1)
    catalog        unique ids, id charset, in_all, min_comfyui (Req 26.4)
    install-paths  one install path, one source and source id (Req 26.3)
    nodes          nodes.txt URL shape and unique repo names (Req 26.5)
    patches        config/patches/: valid JSON, named for an active non-zip workflow line (Req 26.6)
    nsfw           the NSFW_Set's tags and the models/loras/nsfw/ placement both ways (Req 26.7)
    terms          the term lists against every Config_Lists line and template pack name (Req 26.8)
    tokens         the three token families in every tracked file, never echoed (Req 26.9; tokens.py)
    private-data   tracked Persona_File, Content_Calendar and Prompt_Sheet lookalikes (Req 26.10)
    templates      the Req 26.11 Render_Template rules (template_rules.py)
    examples       the Persona_Example, the example calendars, the example Test_Grid (Req 26.12)
    selection      the Python Selection model against `provision.sh --selection` (Req 26.13)
    provenance     source/licence/verified blocks, repo::path, pinned SHAs, commented-out lines (Req 2.2-2.6)
    licence-record the licence gate's records parse, mapped licences carry the map's conditions, unclassifiable
                   ones carry terms:, and no identifier is both listed and mapped (Req 2.4)
    graph          the §C10 extra graph rules (template_rules.py; Req 12.2, 8.6, 11.2, 12.4)
    ignore         no .gitignore pattern matches a tracked file (Req 25.4)

The licence groups read the same Req 2.4 blocks as provenance: comment_block() gives a line's block and
block_value() its nearest `# licence:` line; the parser and the gate are checks/licences.py's, shared with
the Source_Checker.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from influencer import lines as L
from influencer.checks import licences as LIC
from influencer.errors import Exit, Problem, ValidationError, format_problem

INFLUENCER_IDS = ("influencer", "influencer-video", "influencer-nsfw", "character-design")  # glossary
NSFW_ID = "influencer-nsfw"  # the NSFW_Set is the models.list lines tagged with it
NSFW_FOLDER = "models/loras/nsfw/"

KINDS = (L.WORKFLOW_KIND, *L.KIND_DIRS)  # the models.list kinds of Req 26.2
SOURCES = ("hf", "civitai", "url", "bundled")
TAG_INDEX = {L.MODELS: 5, L.NODES: 2, L.OLLAMA: 1}  # the 0-based index of each list's tag field

SAFETY_FOLDER = ("influencer", "safety")  # below the config folder
TEMPLATES_FOLDER = ("influencer", "templates")
EXAMPLES_FOLDER = ("influencer", "examples")
PATCHES_FOLDER = "patches"
PATCH_SUFFIX = ".patch.json"
PROVISION = ("scripts", "provision.sh")  # below the repo

GIT_TIMEOUT_S = 60
SELECTION_TIMEOUT_S = 120
SELECTION_WORKERS = 6


# ---- what a run reads ----


class GitUnavailable(Exception):
    """git is not on PATH, so the groups that need it leave the report to presence."""


@dataclass
class Context:
    """The Repo checkout, the config folder and the environment of one run. Files are read once and kept."""

    repo: Path
    config_dir: Path
    safety_dir: Path
    environ: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    _cache: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def for_repo(cls, environ: Mapping[str, str] | None = None) -> Context:
        """The command's Context: the checkout this module is in, and CONFIG_DIR (default <repo>/config)."""
        from influencer import env

        environ = dict(os.environ if environ is None else environ)
        paths = env.paths(environ)
        return cls(paths.repo, paths.config_dir, paths.config_dir.joinpath(*SAFETY_FOLDER), environ)

    def _cached(self, key: str, make: Callable[[], Any]) -> Any:
        if key not in self._cache:
            self._cache[key] = make()
        return self._cache[key]

    # ---- paths ----

    def show(self, path: str | os.PathLike[str]) -> str:
        """path as the report writes it: relative to the repo when inside it, else absolute."""
        absolute = Path(os.path.abspath(path))
        try:
            return absolute.relative_to(os.path.abspath(self.repo)).as_posix()
        except ValueError:
            return str(absolute)

    def config_path(self, name: str) -> Path:
        return self.config_dir / name

    def config_shown(self, name: str) -> str:
        return self.show(self.config_path(name))

    # ---- the Catalog and Config_Lists ----

    def config_text(self, name: str) -> str | None:
        """A config file's text as provision.sh reads its bytes (UTF-8, surrogateescape), None when absent."""

        def read() -> str | None:
            path = self.config_path(name)
            if not path.is_file():
                return None
            return path.read_bytes().decode("utf-8", "surrogateescape")

        return self._cached(f"text:{name}", read)

    def config_files(self) -> dict[str, str | None]:
        """catalog.txt and the three lists, as lines.select() and check_templates() take them."""
        return {name: self.config_text(name) for name in L.CONFIG_FILES}

    def catalog(self) -> list[L.CatalogLine]:
        return self._cached("catalog", lambda: L.parse_catalog(self.config_text(L.CATALOG) or ""))

    def models(self) -> list[L.ModelLine]:
        return self._cached("models", lambda: L.parse_models(self.config_text(L.MODELS) or ""))

    def nodes(self) -> list[L.NodeLine]:
        return self._cached("nodes", lambda: L.parse_nodes(self.config_text(L.NODES) or ""))

    def ollama(self) -> list[L.OllamaLine]:
        return self._cached("ollama", lambda: L.parse_ollama(self.config_text(L.OLLAMA) or ""))

    def list_lines(self, name: str) -> list[L.ListLine]:
        return {L.MODELS: self.models, L.NODES: self.nodes, L.OLLAMA: self.ollama}[name]()

    def physical(self, name: str) -> list[str]:
        """Every physical line of a config file, as lines.physical_lines() splits it."""
        return L.physical_lines(self.config_text(name) or "")

    # ---- git ----

    def git_program(self) -> str | None:
        return self._cached("git", lambda: shutil.which("git", path=self.environ.get("PATH", "")))

    def git(self, *args: str, cwd: Path | None = None, input: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        """git with args in cwd (default: the repo), read-only use only. Raises GitUnavailable without git."""
        program = self.git_program()
        if program is None:
            raise GitUnavailable("git is not on PATH")
        environ = dict(self.environ)
        environ.update({"GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"})
        return subprocess.run(
            [program, *args],
            cwd=cwd or self.repo,
            env=environ,
            input=input if input is not None else b"",
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )

    def work_tree(self) -> bool:
        """True when git runs and the repo is inside a git work tree."""

        def probe() -> bool:
            try:
                result = self.git("rev-parse", "--is-inside-work-tree")
            except (GitUnavailable, OSError, subprocess.SubprocessError):
                return False
            return result.returncode == 0 and result.stdout.strip() == b"true"

        return self._cached("work_tree", probe)

    def tracked(self) -> list[str]:
        """Every path git tracks in the repo (the index, so a newly staged file counts), repo-relative;
        [] when git or the work tree is missing, which presence reports."""

        def listing() -> list[str]:
            if not self.work_tree():
                return []
            result = self.git("ls-files", "-z", "--cached", "--full-name")
            if result.returncode != 0:
                raise RuntimeError(f"git ls-files failed: {result.stderr.decode('utf-8', 'replace').strip()}")
            return [os.fsdecode(path) for path in result.stdout.split(b"\0") if path]

        return self._cached("tracked", listing)


# ---- groups, findings and the report ----

Check = Callable[[Context], list[Problem]]


@dataclass(frozen=True)
class Group:
    """One rule group of the §C10 table: its name in the report, the requirement, and its check."""

    name: str
    requirement: str
    check: Check


@dataclass(frozen=True)
class Finding:
    """One failure of one group."""

    group: str
    problem: Problem

    def __str__(self) -> str:
        problem = self.problem
        prefix = f"{self.group}: "
        if problem.reason.startswith(prefix):  # template_rules.py names its rule in the reason already
            problem = Problem(problem.path, problem.field, problem.line, problem.reason[len(prefix):])
        return f"[{self.group}] {format_problem(problem)}"


@dataclass(frozen=True)
class Report:
    """Every Finding of one run, in group order, and the groups that ran."""

    findings: tuple[Finding, ...]
    groups: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def exit_status(self) -> int:
        """0 for a run with no failure, non-zero otherwise (Req 26.16)."""
        return int(Exit.OK if self.ok else Exit.USAGE)

    def by_group(self, name: str) -> list[Problem]:
        return [finding.problem for finding in self.findings if finding.group == name]

    def text(self) -> str:
        """The printout: one line per Finding, then a summary line."""
        out = [str(finding) for finding in self.findings]
        if self.ok:
            out.append(f"check-config: passed, no failure in {len(self.groups)} rule groups")
        else:
            counts: dict[str, int] = {}
            for finding in self.findings:
                counts[finding.group] = counts.get(finding.group, 0) + 1
            listed = ", ".join(f"{group} {count}" for group, count in counts.items())
            noun = "failure" if len(self.findings) == 1 else "failures"
            out.append(f"check-config: {len(self.findings)} {noun} ({listed})")
        return "\n".join(out) + "\n"


def run_checks(ctx: Context, groups: Iterable[str] | None = None) -> Report:
    """Runs the named groups (default: all) in GROUPS order and collects every Finding. A group that raises
    is reported as a failure of its own, naming the group and the error, and the run goes on (Req 26.15)."""
    wanted = None if groups is None else set(groups)
    if wanted is not None:
        unknown = wanted - set(GROUP_NAMES)
        if unknown:
            raise ValueError(f"unknown rule groups: {', '.join(sorted(unknown))}")
    findings: list[Finding] = []
    ran: list[str] = []
    for group in GROUPS:
        if wanted is not None and group.name not in wanted:
            continue
        ran.append(group.name)
        try:
            problems = group.check(ctx)
        except Exception as error:  # a broken file or a bug must not hide the other groups
            reason = f"the {group.name} check could not finish: {type(error).__name__}: {error}"
            problems = [Problem(f"check-config {group.name}", None, None, reason)]
        findings += (Finding(group.name, problem) for problem in problems)
    return Report(tuple(findings), tuple(ran))


def check_config(args: argparse.Namespace | None = None) -> int:
    """The check-config command: every group over the Repo checkout, the report on stdout, and exit 0 only
    for a run with no failure."""
    report = run_checks(Context.for_repo())
    sys.stdout.write(report.text())
    sys.stdout.flush()
    return report.exit_status


# ---- shared helpers ----


def _problem(path: str, field: str | None, line: int | None, reason: str) -> Problem:
    return Problem(path, field, line, reason)


def _is_commentish(line: str) -> bool:
    return L.trim(line).startswith("#")


def _comment_body(line: str) -> str:
    """A comment line's text after its leading `#`s and whitespace."""
    return L.trim(L.trim(line).lstrip("#"))


def commented_record(name: str, line: str) -> list[str] | None:
    """The fields of a commented-out record of the list `name` (a comment that, without its `#`, reads as
    one of the list's lines), else None."""
    if not _is_commentish(line):
        return None
    body = _comment_body(line)
    if body == "":
        return None
    fields = L.line_reader(body)
    first = L.trim(fields[0])
    if name == L.MODELS:
        return fields if len(fields) == 6 and first in KINDS else None
    if name == L.NODES:
        return fields if len(fields) <= 3 and re.fullmatch(r"https?://\S+", first) else None
    if name == L.OLLAMA:
        if len(fields) > 2 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*(:[A-Za-z0-9._-]+)?", first):
            return None
        return fields if len(fields) == 1 or L.trim(fields[1]) == "" or L.trim(fields[1]).startswith("wf:") else None
    return None


def comment_block(name: str, physical: Sequence[str], number: int) -> list[tuple[int, str]]:
    """The comment block Req 2.4 places for the line `number` (1-based) of the list `name`: the run of comment
    lines directly above it, or, for an active line, above the first line of its run of consecutive active
    lines. The run stops at a blank line, an active line or a commented-out record, so a block never reaches
    another line's. (line number, line) pairs in file order."""
    index = number - 1
    if L.is_active(physical[index]):
        while index > 0 and L.is_active(physical[index - 1]):
            index -= 1
    block: list[tuple[int, str]] = []
    above = index - 1
    while above >= 0:
        line = physical[above]
        if not _is_commentish(line) or commented_record(name, line) is not None:
            break
        block.append((above + 1, line))
        above -= 1
    return block[::-1]


_KEY = re.compile(r"(source|licence|verified):(.*)", re.S)


def block_key(line: str) -> tuple[str, str] | None:
    """(key, value) of a `# source:`, `# licence:` or `# verified:` comment line, else None."""
    match = _KEY.fullmatch(_comment_body(line))
    return (match[1], L.trim(match[2])) if match else None


def block_value(block: Sequence[tuple[int, str]], key: str) -> tuple[int, str] | None:
    """(line number, value) of the key in the block; when it appears twice, the one nearest the line."""
    for number, line in reversed(block):
        found = block_key(line)
        if found is not None and found[0] == key:
            return number, found[1]
    return None


def tagged_lines(ctx: Context) -> list[tuple[str, L.ListLine]]:
    """(list name, line) for every active line of the three lists, in list order."""
    return [(name, line) for name in L.LIST_FILES for line in ctx.list_lines(name)]


def influencer_only(tag: str) -> bool:
    """The tag lists at least one id and only Influencer_Ids."""
    ids = L.parse_tag(tag) if L.trim(tag).startswith("wf:") else ()
    return bool(ids) and all(ident in INFLUENCER_IDS for ident in ids)


# ---- presence (Req 26.17) ----


def _presence_git(ctx: Context) -> list[Problem]:
    if ctx.git_program() is None:
        return [_problem("git", None, None, "git is not installed: no git program on PATH")]
    if not ctx.work_tree():
        return [_problem(os.path.abspath(ctx.repo), None, None, "the Repo checkout is not a git work tree")]
    return []


def _presence_config(ctx: Context) -> list[Problem]:
    return [
        _problem(ctx.config_shown(name), None, None, "is missing")
        for name in L.CONFIG_FILES
        if not ctx.config_path(name).is_file()
    ]


def _presence_terms(ctx: Context) -> list[Problem]:
    from influencer import safety

    problems: list[Problem] = []
    for name in (safety.MINOR_TERMS, safety.RESOURCE_TERMS):
        try:
            safety.load_term_list(ctx.safety_dir / name)
        except ValidationError as error:
            problems += [Problem(ctx.show(p.path), p.field, p.line, p.reason) for p in error.problems]
    return problems


def _presence_licences(ctx: Context) -> list[Problem]:
    """The Noncommercial_List, with at least one identifier, and the conditions map (§M15)."""
    problems: list[Problem] = []
    for path, load in ((_noncommercial_path(ctx), LIC.read_noncommercial), (_conditions_path(ctx), LIC.read_conditions)):
        try:
            load(path)
        except ValidationError as error:
            problems += [Problem(ctx.show(p.path), p.field, p.line, p.reason) for p in error.problems]
    return problems


# Each item names what is missing.
PRESENCE_ITEMS: tuple[Check, ...] = (_presence_git, _presence_config, _presence_terms, _presence_licences)


def check_presence(ctx: Context) -> list[Problem]:
    return [problem for item in PRESENCE_ITEMS for problem in item(ctx)]


# ---- the Licence_Gate (Req 26.18) and the licence-record rule (Req 2.4); design §M15, checks/licences.py ----

LICENCE_LISTS = (L.MODELS, L.OLLAMA)  # the lists whose Influencer_Id lines carry a `# licence:` record


def _noncommercial_path(ctx: Context) -> Path:
    return LIC.licences_dir(ctx.config_dir) / LIC.NONCOMMERCIAL


def _conditions_path(ctx: Context) -> Path:
    return LIC.licences_dir(ctx.config_dir) / LIC.CONDITIONS


def _optional(load: Callable[[Path], Any], path: Path) -> Any:
    """A loader's result, or None when it fails: presence reports that failure."""
    try:
        return load(path)
    except ValidationError:
        return None


def lists_influencer_id(tag: str) -> bool:
    """The tag lists at least one Influencer_Id, shared lines included."""
    ids = L.parse_tag(tag) if L.trim(tag).startswith("wf:") else ()
    return any(ident in INFLUENCER_IDS for ident in ids)


def licence_lines(ctx: Context) -> list[tuple[str, L.ListLine, tuple[int, str] | None]]:
    """(list name, line, (record line number, record value) or None) for every active models.list and
    ollama-models.txt line whose tag lists an Influencer_Id. The record is the `# licence:` line of the Req 2.4
    block, the nearest one when the block holds two."""
    found = []
    for name in LICENCE_LISTS:
        physical = ctx.physical(name)
        for line in ctx.list_lines(name):
            if lists_influencer_id(line.tag):
                found.append((name, line, block_value(comment_block(name, physical, line.number), "licence")))
    return found


def check_licence(ctx: Context) -> list[Problem]:
    noncommercial = _optional(LIC.load_noncommercial, _noncommercial_path(ctx)) or frozenset()
    problems: list[Problem] = []
    for name, line, record in licence_lines(ctx):
        shown = ctx.config_shown(name)
        if record is None:
            problems.append(_problem(shown, "licence", line.number,
                                     "has no '# licence:' record in the comment block Req 2.4 places for it"))  # fmt: skip
            continue
        at, value = record
        try:
            parsed = LIC.parse_record(value)
        except LIC.RecordError:
            continue  # the licence-record group reports it
        result = LIC.gate(parsed, noncommercial)
        if not result.passed:
            problems.append(_problem(shown, "licence", line.number,
                                     f"fails the Licence_Gate: licence {value!r} (line {at}): {result.reason}"))  # fmt: skip
    return problems


# ---- syntax (Req 26.2) ----

_HF_REPO = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
_CIVITAI = re.compile(r"[0-9]+(?:::(.+))?", re.S)
_URL = re.compile(r"https://([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)(?:[/:?#]\S*)?")


def _relative_path_problem(path: str) -> str | None:
    """Why path is not a non-empty relative path without `..` segments, or None."""
    if path == "":
        return "is empty"
    if path.startswith("/"):
        return "is an absolute path"
    if ".." in path.split("/"):
        return "has a '..' segment"
    return None


def _source_id_problem(ctx: Context, source: str, source_id: str) -> str | None:
    """Why source_id is not the shape its source needs (Req 26.2), or None."""
    if source == "hf":
        repo, sep, path = source_id.partition("::")
        if not _HF_REPO.fullmatch(repo):
            return f"{source_id!r} is not an hf repo <owner>/<name> of letters, digits, '-', '_' and '.'"
        if sep:
            why = _relative_path_problem(path)
            if why:
                return f"the path after '::' in {source_id!r} {why}"
        return None
    if source == "civitai":
        match = _CIVITAI.fullmatch(source_id)
        if not match:
            return f"{source_id!r} is not a Civitai version id (digits, optionally ::<member>)"
        return None
    if source == "url":
        if not _URL.fullmatch(source_id):
            return f"{source_id!r} is not https:// and a host name with no whitespace"
        return None
    if source == "bundled":
        why = _relative_path_problem(source_id)
        if why:
            return f"the bundled path {source_id!r} {why}"
        target = ctx.config_dir / source_id
        try:
            mode = target.lstat().st_mode
        except OSError:
            return f"config/{source_id} does not exist"
        if not stat.S_ISREG(mode):
            return f"config/{source_id} is not a regular file"
        if not _git_tracks(ctx, target):
            return f"config/{source_id} is not a file git tracks"
        return None
    return None  # an unknown source is reported by itself


def _git_tracks(ctx: Context, path: Path) -> bool:
    try:
        result = ctx.git("ls-files", "-z", "--", path.name, cwd=path.parent)
    except (GitUnavailable, OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.split(b"\0")[0] == os.fsencode(path.name)


def _subdir_problem(subdir: str) -> str | None:
    if subdir == "":
        return None
    if any(segment in ("", ".", "..") for segment in subdir.split("/")):
        return f"{subdir!r} has an empty, '.' or '..' segment"
    return None


def _field_count_problem(name: str, count: int) -> str | None:
    low, high = L.FIELD_COUNTS[name]
    if low <= count <= high:
        return None
    wanted = str(low) if low == high else f"{low} to {high}"
    return f"has {count} fields; a {name} line has {wanted} (a trailing '|' adds an empty field)"


def check_syntax(ctx: Context) -> list[Problem]:
    problems: list[Problem] = []
    for line in ctx.catalog():
        why = _field_count_problem(L.CATALOG, len(line.fields))
        if why:
            problems.append(_problem(ctx.config_shown(L.CATALOG), "field count", line.number, why))
    for name in L.LIST_FILES:
        shown = ctx.config_shown(name)
        for record in ctx.list_lines(name):
            number, fields = record.number, record.fields
            why = _field_count_problem(name, len(fields))
            if why:
                problems.append(_problem(shown, "field count", number, why))
            tag_index = TAG_INDEX[name]
            for index in range(min(len(fields), L.FIELD_COUNTS[name][1])):
                value = L.trim(fields[index])
                if index == tag_index:
                    if value and not value.startswith("wf:"):
                        problems.append(_problem(shown, f"field {index + 1} (tag)", number,
                                                 f"{value!r} is neither empty nor a wf: tag"))  # fmt: skip
                elif value.startswith("wf:"):
                    problems.append(_problem(shown, f"field {index + 1}", number,
                                             f"starts with 'wf:', but only field {tag_index + 1} is the tag"))  # fmt: skip
            if isinstance(record, L.ModelLine):
                problems += _model_syntax(ctx, shown, record)
    return problems


def _model_syntax(ctx: Context, shown: str, line: L.ModelLine) -> list[Problem]:
    problems = []
    if line.kind not in KINDS:
        problems.append(_problem(shown, "kind", line.number, f"{line.kind!r} is not a models.list kind"))
    if line.source not in SOURCES:
        problems.append(_problem(shown, "source", line.number,
                                 f"{line.source!r} is not a source ({', '.join(SOURCES)})"))  # fmt: skip
    else:
        why = _source_id_problem(ctx, line.source, line.source_id)
        if why:
            problems.append(_problem(shown, "source_id", line.number, why))
    if line.filename == "":
        problems.append(_problem(shown, "filename", line.number, "is empty"))
    elif "/" in line.filename:
        problems.append(_problem(shown, "filename", line.number, f"{line.filename!r} contains '/'"))
    why = _subdir_problem(line.subdir)
    if why:
        problems.append(_problem(shown, "subdir", line.number, why))
    return problems


# ---- tags (Req 26.1) ----


def check_tags(ctx: Context) -> list[Problem]:
    """Every id of a wf: tag is a Catalog id exactly (spaces ignored, case kept), and none is empty. A tag
    field that does not start with wf: is the syntax group's."""
    known = {line.fields[0] for line in ctx.catalog()}
    problems = []
    for name, line in tagged_lines(ctx):
        if not line.tag.startswith("wf:"):
            continue
        for position, ident in enumerate(L.parse_tag(line.tag), 1):
            if ident == "":
                reason = f"id {position} of {line.tag!r} is empty"
            elif ident not in known:
                reason = f"the id {ident!r} is not a Catalog id"
            else:
                continue
            problems.append(_problem(ctx.config_shown(name), "tag", line.number, reason))
    return problems


# ---- the Catalog (Req 26.4) ----

_CATALOG_ID = re.compile(r"[a-z0-9-]+")
_MIN_COMFYUI = re.compile(r"[0-9]+(?:\.[0-9]+){1,2}")


def check_catalog(ctx: Context) -> list[Problem]:
    shown = ctx.config_shown(L.CATALOG)
    problems = []
    first: dict[str, int] = {}
    for line in ctx.catalog():
        fields = line.fields
        ident = fields[0]
        if not _CATALOG_ID.fullmatch(ident):
            why = "is empty" if ident == "" else f"{ident!r} has characters other than a-z, 0-9 and '-'"
            problems.append(_problem(shown, "id", line.number, why))
        if ident in first:
            problems.append(_problem(shown, "id", line.number, f"{ident!r} is also the id of line {first[ident]}"))
        else:
            first[ident] = line.number
        if len(fields) > 1 and fields[1] not in ("yes", "no"):
            problems.append(_problem(shown, "in_all", line.number, f"{fields[1]!r} is neither yes nor no"))
        if len(fields) > 2 and fields[2] != "" and not _MIN_COMFYUI.fullmatch(fields[2]):
            problems.append(_problem(shown, "min_comfyui", line.number,
                                     f"{fields[2]!r} is neither empty nor 2 or 3 groups of digits joined by dots"))  # fmt: skip
    return problems


# ---- install paths (Req 26.3) ----


def check_install_paths(ctx: Context) -> list[Problem]:
    shown = ctx.config_shown(L.MODELS)
    return [
        _problem(
            shown, "install path", later.number,
            f"lines {first.number} and {later.number} both install {L.install_path(later)}, line "
            f"{first.number} from {first.source} {first.source_id!r} and line {later.number} from "
            f"{later.source} {later.source_id!r}",
        )
        for first, later in L.install_path_conflicts(ctx.models())
    ]  # fmt: skip


# ---- nodes.txt (Req 26.5) ----

_NODE_URL = re.compile(r"https://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)")


def node_repo(url: str) -> str | None:
    """The repo name of a valid nodes.txt URL, lowercased and without .git; None for an invalid URL."""
    match = _NODE_URL.fullmatch(url)
    if not match:
        return None
    repo = match[2][:-4] if match[2].lower().endswith(".git") else match[2]
    return repo.lower() if repo else None


def check_nodes(ctx: Context) -> list[Problem]:
    shown = ctx.config_shown(L.NODES)
    problems = []
    first: dict[str, L.NodeLine] = {}
    for line in ctx.nodes():
        repo = node_repo(line.url)
        if repo is None:
            problems.append(_problem(shown, "url", line.number,
                                     f"{line.url!r} is not https://github.com/<owner>/<repo>[.git]"))  # fmt: skip
            continue
        if repo in first:
            earlier = first[repo]
            problems.append(_problem(
                shown, "url", line.number,
                f"{line.url!r} has the repo of line {earlier.number} ({earlier.url!r}), case and .git ignored",
            ))  # fmt: skip
        else:
            first[repo] = line
    return problems


# ---- patches (Req 26.6) ----


def check_patches(ctx: Context) -> list[Problem]:
    folder = ctx.config_dir / PATCHES_FOLDER
    if not folder.is_dir():
        return []
    workflows = {
        line.filename for line in ctx.models() if line.kind == L.WORKFLOW_KIND and not line.filename.endswith(".zip")
    }
    problems = []
    for path in sorted(folder.iterdir()):
        if path.is_dir():
            continue
        shown = ctx.show(path)
        try:
            json.loads(path.read_bytes().decode("utf-8-sig"))
        except UnicodeDecodeError:
            problems.append(_problem(shown, None, None, "is not UTF-8 text, so not valid JSON"))
        except json.JSONDecodeError as error:
            problems.append(_problem(shown, None, error.lineno, f"is not valid JSON: {error.msg}"))
        name = path.name
        target = name[: -len(PATCH_SUFFIX)] if name.endswith(PATCH_SUFFIX) else None
        if target is None:
            problems.append(_problem(shown, None, None, f"is not named <filename>{PATCH_SUFFIX}"))
        elif target not in workflows:
            problems.append(_problem(shown, None, None,
                                     f"{target!r} is not the filename of an active non-zip workflow line of models.list"))  # fmt: skip
    return problems


# ---- NSFW (Req 26.7) ----


def check_nsfw(ctx: Context) -> list[Problem]:
    shown = ctx.config_shown(L.MODELS)
    problems = []
    for line in ctx.models():
        in_set = L.trim(line.tag) != "" and L.tag_has(line.tag, NSFW_ID)
        target = L.install_path(line)
        inside = target.startswith(NSFW_FOLDER)
        if in_set:
            others = [ident for ident in L.parse_tag(line.tag) if ident != NSFW_ID]
            if others:
                listed = ", ".join(repr(ident) for ident in others)
                problems.append(_problem(shown, "tag", line.number,
                                         f"an NSFW_Set line lists only {NSFW_ID}, not also {listed}"))  # fmt: skip
            if line.kind == "lora" and not inside:
                problems.append(_problem(shown, "install path", line.number,
                                         f"the NSFW_Set LoRA installs to {target}, outside {NSFW_FOLDER}"))  # fmt: skip
        elif inside:
            problems.append(_problem(shown, "install path", line.number,
                                     f"installs to {target} inside {NSFW_FOLDER}, but is not an NSFW_Set line"))  # fmt: skip
    return problems


# ---- terms (Req 26.8) ----


def _guard(ctx: Context) -> Any:
    """The Config_Checker's pair of term lists (safety.py), without the age patterns: Req 26.8 matches terms.
    None when a list is missing or empty, which presence reports."""
    from influencer import safety

    def load() -> Any:
        try:
            return safety.load_guard(ctx.safety_dir, resources=True, ages=False)
        except ValidationError:
            return None

    return ctx._cached("guard", load)


def template_paths(ctx: Context) -> list[Path]:
    folder = ctx.config_dir.joinpath(*TEMPLATES_FOLDER)
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def check_terms(ctx: Context) -> list[Problem]:
    guard = _guard(ctx)
    if guard is None:
        return []
    problems = []
    for name in L.LIST_FILES:
        shown = ctx.config_shown(name)
        for number, line in enumerate(ctx.physical(name), 1):
            for term in guard.check_text(line):
                problems.append(_problem(shown, None, number, f"matches the resource term {term!r}"))
    for path in template_paths(ctx):
        try:
            meta = json.loads(path.read_bytes().decode("utf-8-sig")).get("meta", {})
            packs = meta.get("node_packs", {}) if isinstance(meta, dict) else {}
        except (ValueError, AttributeError):
            continue  # the templates group reports a file that is not a template
        if not isinstance(packs, dict):
            continue
        for class_type, pack in packs.items():
            if isinstance(pack, str):
                for term in guard.check_text(pack):
                    problems.append(_problem(ctx.show(path), f"meta.node_packs.{class_type}", None,
                                             f"the pack {pack!r} matches the resource term {term!r}"))  # fmt: skip
    return problems


# ---- tokens (Req 26.9) ----


def check_tokens(ctx: Context) -> list[Problem]:
    from influencer.checks import tokens

    if not ctx.work_tree():
        return []  # presence reports it
    return tokens.scan(ctx.git, ctx.repo)


# ---- private data (Req 26.10) ----

# The top-level keys that identify each private format, as its Reader requires them: the Persona_File's
# slug and Identity_Paragraph (persona.py), a Content_Calendar's week and posts (calendar_io.py), and a
# Prompt_Sheet's format and week (prompt_sheet.py). A mapping holding them is private whatever the rest of
# the file would make the Reader say.
PRIVATE_SHAPES = (
    ("a Persona_File", frozenset({"slug", "identity"})),
    ("a Content_Calendar", frozenset({"week", "posts"})),
    ("a Prompt_Sheet", frozenset({"format", "week"})),
)
YAML_SUFFIXES = (".yaml", ".yml")


def _examples_prefix(ctx: Context) -> str:
    return ctx.show(ctx.config_dir.joinpath(*EXAMPLES_FOLDER)).rstrip("/") + "/"


def private_shape(data: object) -> str | None:
    """The private format a parsed YAML document has the top-level structure of, or None."""
    if not isinstance(data, dict):
        return None
    keys = {key for key in data if isinstance(key, str)}
    for what, required in PRIVATE_SHAPES:
        if required <= keys:
            return what
    return None


def calendar_header(text: str) -> bool:
    """True when the first CSV row names every column a Content_Calendar CSV requires (Req 5.14)."""
    from influencer.calendar_io import REQUIRED_COLUMNS

    try:
        header = next(csv.reader(io.StringIO(text)), [])
    except csv.Error:
        return False
    names = {cell.strip().lstrip("\ufeff").strip() for cell in header}
    return set(REQUIRED_COLUMNS) <= names


def check_private_data(ctx: Context) -> list[Problem]:
    import yaml

    examples = _examples_prefix(ctx)
    problems = []
    for relative in ctx.tracked():
        suffix = PurePosixPath(relative).suffix.lower()
        if suffix not in (*YAML_SUFFIXES, ".csv") or relative.startswith(examples):
            continue
        path = ctx.repo / relative
        if path.is_symlink() or not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", "replace")
        if suffix == ".csv":
            if calendar_header(text):
                problems.append(_problem(relative, None, 1,
                                         "git tracks a CSV with the Content_Calendar header; keep calendars in the Influencer_Root"))  # fmt: skip
            continue
        try:
            data = yaml.load(text, Loader=yaml.SafeLoader)
        except yaml.YAMLError:
            continue  # not one document of YAML, so no Reader's structure
        what = private_shape(data)
        if what:
            problems.append(_problem(relative, None, None,
                                     f"git tracks a YAML file with the top-level structure of {what}; only {examples} may hold one"))  # fmt: skip
    return problems


# ---- examples (Req 26.12) ----

TEST_GRID_EXAMPLE = "test-grid.yaml"
TEST_GRID_PROMPTS = 20


def check_examples(ctx: Context) -> list[Problem]:
    from influencer import calendar_io, persona

    folder = ctx.config_dir.joinpath(*EXAMPLES_FOLDER)
    persona_path = folder / "persona.yaml"
    shown = ctx.show(persona_path)
    problems: list[Problem] = []
    example = None
    if not persona_path.is_file():
        problems.append(_problem(shown, None, None, "the Persona_Example is missing"))
    else:
        try:
            example = persona.read_persona(persona_path, None, check_files=False)
        except ValidationError as error:
            problems += [Problem(ctx.show(p.path), p.field, p.line, p.reason) for p in error.problems]
    calendars = sorted(
        path for path in folder.glob("calendar*") if path.suffix.lower() in (*YAML_SUFFIXES, ".csv")
    ) if folder.is_dir() else []  # fmt: skip
    for path in calendars:
        if example is None:
            problems.append(_problem(ctx.show(path), None, None, "not read: the Persona_Example is rejected"))
            continue
        result = calendar_io.read_calendar(path, example, None, check_files=False)
        problems += [Problem(ctx.show(p.path), p.field, p.line, p.reason) for p in result.all_problems()]
    grid = folder / TEST_GRID_EXAMPLE
    if grid.is_file():  # Phase 2 adds the example Test_Grid; until then there is nothing to check
        problems += _test_grid_problems(ctx.show(grid), grid)
    return problems


def _test_grid_problems(shown: str, path: Path) -> list[Problem]:
    from influencer.yamlio import load_yaml

    try:
        data = load_yaml(path)
    except ValidationError as error:
        return [Problem(shown, p.field, p.line, p.reason) for p in error.problems]
    prompts = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(prompts, list):
        return [_problem(shown, "prompts", None, "must be a list of prompts")]
    problems = []
    if len(prompts) != TEST_GRID_PROMPTS:
        problems.append(_problem(shown, "prompts", None,
                                 f"holds {len(prompts)} prompts; the Test_Grid holds exactly {TEST_GRID_PROMPTS}"))  # fmt: skip
    for number, prompt in enumerate(prompts, 1):
        seed = prompt.get("seed") if isinstance(prompt, dict) else None
        if type(seed) is not int:
            problems.append(_problem(shown, f"prompts[{number}].seed", None, "must be an integer seed"))
    return problems


# ---- templates and graph (Req 26.11; 12.2, 8.6, 11.2, 12.4) ----


def _template_problems(ctx: Context) -> list[Problem]:
    """check_templates() over every Render_Template, run once for both groups."""
    from influencer.checks.template_rules import check_templates

    def run() -> list[Problem]:
        texts = {ctx.show(path): path.read_bytes() for path in template_paths(ctx)}
        return check_templates(texts, ctx.config_files()) if texts else []

    return ctx._cached("templates", run)


def check_templates_group(ctx: Context) -> list[Problem]:
    from influencer.checks.template_rules import RULE_GRAPH

    return [p for p in _template_problems(ctx) if not p.reason.startswith(f"{RULE_GRAPH}:")]


def check_graph(ctx: Context) -> list[Problem]:
    from influencer.checks.template_rules import RULE_GRAPH

    return [p for p in _template_problems(ctx) if p.reason.startswith(f"{RULE_GRAPH}:")]


# ---- Selection (Req 26.13) ----

# One bash runs `provision.sh --selection <run>` for every run; a NUL byte, which no Selection holds, and the
# run's exit status follow each output.
_RUN_SELECTIONS = r"""
script=$1; shift
for run in "$@"; do
  "$BASH" "$script" --selection "$run"
  printf '\0%s\n' "$?"
done
"""


def _bash(ctx: Context) -> str | None:
    return "/bin/bash" if os.access("/bin/bash", os.X_OK) else shutil.which("bash", path=ctx.environ.get("PATH", ""))


def selection_runs(ctx: Context) -> list[str]:
    """Every Catalog id that --selection can name, then global and all, each once."""
    runs = [line.id for line in ctx.catalog() if line.id != "" and "\n" not in line.id]
    return list(dict.fromkeys([*runs, L.GLOBAL_RUN, L.FULL_RUN]))


def bash_selections(ctx: Context, runs: Sequence[str]) -> list[tuple[int, bytes]]:
    """(exit status, stdout) of `provision.sh --selection <run>` for each run, from the repo root, with PATH,
    CONFIG_DIR and a temp HOME, TMPDIR and WORKSPACE. The runs are split over SELECTION_WORKERS bash
    processes, since one --selection run forks a great deal and takes about a second on the Mac."""
    from concurrent.futures import ThreadPoolExecutor

    bash = _bash(ctx)
    if bash is None:
        raise RuntimeError("bash is not on PATH, so provision.sh --selection cannot run")
    workers = max(1, min(SELECTION_WORKERS, len(runs)))
    batches = [list(runs[index::workers]) for index in range(workers)]
    with tempfile.TemporaryDirectory(prefix="check-config-") as folder:
        sandbox = Path(folder)
        for name in ("home", "tmp"):
            (sandbox / name).mkdir()
        environ = {
            "PATH": ctx.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(sandbox / "home"),
            "TMPDIR": str(sandbox / "tmp"),
            "WORKSPACE": str(sandbox / "workspace"),
            "COMFYUI_DIR": str(sandbox / "workspace" / "ComfyUI"),
            "CONFIG_DIR": str(ctx.config_dir),
        }
        script = str(ctx.repo.joinpath(*PROVISION))

        def batch(names: list[str]) -> list[tuple[int, bytes]]:
            result = subprocess.run(
                [bash, "-c", _RUN_SELECTIONS, "run-selections", script, *names],
                cwd=ctx.repo, env=environ, stdin=subprocess.DEVNULL, capture_output=True,
                timeout=SELECTION_TIMEOUT_S, check=False,
            )  # fmt: skip
            chunks = result.stdout.split(b"\0")
            if len(chunks) != len(names) + 1:
                raise RuntimeError(f"provision.sh --selection printed {len(chunks) - 1} results for {len(names)} runs")
            results, output = [], chunks[0]
            for chunk in chunks[1:]:
                status, _, rest = chunk.partition(b"\n")
                results.append((int(status), output))
                output = rest
            return results

        with ThreadPoolExecutor(max_workers=workers) as pool:
            done = list(pool.map(batch, batches))
    by_run: dict[int, tuple[int, bytes]] = {}
    for index, results in enumerate(done):
        for offset, result in enumerate(results):
            by_run[index + offset * workers] = result
    return [by_run[index] for index in range(len(runs))]


def _sections(printout: bytes) -> dict[str, list[bytes]]:
    sections: dict[str, list[bytes]] = {}
    current = None
    for line in printout.split(b"\n")[:-1] if printout.endswith(b"\n") else printout.split(b"\n"):
        if line.startswith(b"[") and line.endswith(b"]"):
            current = line[1:-1].decode("utf-8", "replace")
            sections.setdefault(current, [])
        elif current is not None:
            sections[current].append(line)
    return sections


def selection_differences(run: str, bash: bytes, python: bytes) -> list[str]:
    """Each item on which the two printouts of one run differ."""
    ours, theirs = _sections(python), _sections(bash)
    found = []
    for section in dict.fromkeys([*ours, *theirs]):
        mine, other = ours.get(section, []), theirs.get(section, [])
        items = [f"[{section}] {item.decode('utf-8', 'replace')!r} is in provision.sh's Selection only"
                 for item in other if item not in mine]  # fmt: skip
        items += [f"[{section}] {item.decode('utf-8', 'replace')!r} is in the Python Selection only"
                  for item in mine if item not in other]  # fmt: skip
        if not items and mine != other:
            items.append(f"[{section}] the same items in another order, or one listed a different number of times")
        found += items
    return found or ["the printouts differ"]


def check_selection(ctx: Context) -> list[Problem]:
    if ctx.config_text(L.CATALOG) is None:
        return []  # presence reports it
    runs = selection_runs(ctx)
    files = ctx.config_files()
    problems = []
    for run, (status, output) in zip(runs, bash_selections(ctx, runs)):
        where = f"{'/'.join(PROVISION)} --selection {run}"
        python: bytes | None = None
        try:
            python = L.select(files, run).render().encode("utf-8", "surrogateescape")
        except ValidationError as error:
            reason = "; ".join(p.reason for p in error.problems)
        if python is None and status != 0:
            continue  # both reject the run
        if python is None:
            problems.append(_problem(where, None, None, f"provision.sh prints a Selection, the Python model rejects the run: {reason}"))
        elif status != 0:
            problems.append(_problem(where, None, None, f"provision.sh exits {status}, the Python model gives a Selection"))
        elif output != python:
            problems += [_problem(where, None, None, why) for why in selection_differences(run, output, python)]
    return problems


# ---- provenance (Req 2.2-2.6) ----

PROVENANCE_KEYS = ("source", "licence", "verified")
_SHA = re.compile(r"[0-9a-f]{40}")
GITHUB_HOSTS = ("github.com", "raw.githubusercontent.com")


def _is_reason_comment(name: str, line: str) -> bool:
    """A comment that can state a reason: not blank, not a keyed provenance line, not a commented record."""
    return (
        _is_commentish(line) and _comment_body(line) != ""
        and block_key(line) is None and commented_record(name, line) is None
    )  # fmt: skip


def _block_problems(shown: str, number: int, block: Sequence[tuple[int, str]]) -> list[Problem]:
    """The three keyed lines of an influencer-only line's block, and their forms (Req 2.4)."""
    problems = []
    for key in PROVENANCE_KEYS:
        found = block_value(block, key)
        if found is None:
            problems.append(_problem(shown, "provenance", number,
                                     f"the comment block above the line has no '# {key}:' line"))  # fmt: skip
            continue
        at, value = found
        if key == "source" and not re.fullmatch(r"https://\S+", value):
            problems.append(_problem(shown, "provenance", at, f"'# source:' must give the source page as an https:// URL"))
        elif key == "licence" and value == "":
            problems.append(_problem(shown, "provenance", at, "'# licence:' is empty; write 'none stated' when the page states none"))
        elif key == "verified" and not _is_date(value):
            problems.append(_problem(shown, "provenance", at, f"'# verified:' {value!r} is not a YYYY-MM-DD date"))
    return problems


def _is_date(value: str) -> bool:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _url_host(url: str) -> str:
    match = _URL.fullmatch(url)
    return match[1].lower() if match else ""


def check_provenance(ctx: Context) -> list[Problem]:
    problems: list[Problem] = []
    for name in L.LIST_FILES:
        shown = ctx.config_shown(name)
        physical = ctx.physical(name)
        for line in ctx.list_lines(name):
            if not influencer_only(line.tag):
                continue
            if not (isinstance(line, L.ModelLine) and line.source == "bundled"):
                problems += _block_problems(shown, line.number, comment_block(name, physical, line.number))
            if not isinstance(line, L.ModelLine):
                continue
            if line.source == "hf" and "::" not in line.source_id:
                problems.append(_problem(shown, "source_id", line.number,
                                         f"{line.source_id!r} names only the repo; write repo::path, also when the path is the filename"))  # fmt: skip
            if (
                line.kind == L.WORKFLOW_KIND and line.source == "url" and _url_host(line.source_id) in GITHUB_HOSTS
                and not any(_SHA.fullmatch(part) for part in line.source_id.split("/"))
            ):  # fmt: skip
                problems.append(_problem(shown, "source_id", line.number,
                                         "a GitHub workflow URL must pin a full 40-character commit SHA, not a branch or tag"))  # fmt: skip
        for number, text in enumerate(physical, 1):
            fields = commented_record(name, text)
            if fields is None or len(fields) <= TAG_INDEX[name] or not influencer_only(L.trim(fields[TAG_INDEX[name]])):
                continue
            if number == 1 or not _is_reason_comment(name, physical[number - 2]):
                problems.append(_problem(shown, "commented-out line", number,
                                         "must sit directly under a comment that states why it is commented out"))  # fmt: skip
            block = comment_block(name, physical, number)
            present = [key for key in PROVENANCE_KEYS if block_value(block, key) is not None]
            if present and len(present) < len(PROVENANCE_KEYS):
                missing = ", ".join(f"'# {key}:'" for key in PROVENANCE_KEYS if key not in present)
                problems.append(_problem(shown, "commented-out line", number,
                                         f"its provenance block lost {missing}"))  # fmt: skip
    return problems


# ---- licence records (Req 2.4; design §M15) ----


def _record_problems(shown: str, at: int, value: str, conditions: Mapping[str, tuple[int, str]] | None) -> list[Problem]:
    try:
        record = LIC.parse_record(value)
    except LIC.RecordError as error:
        return [_problem(shown, "licence", at, f"the '# licence:' record does not parse: {error}")]
    problems = []
    ident = record.identifier
    if conditions is not None and ident in conditions:
        wanted = conditions[ident][1]
        if record.conditions is None:
            problems.append(_problem(shown, "licence", at,
                                     f"a {ident} record carries the conditions map's text as '; conditions: '"))  # fmt: skip
        elif not LIC.same_text(record.conditions, wanted):
            problems.append(_problem(shown, "licence", at,
                                     f"the 'conditions:' text differs from the conditions map's text for {ident}"))  # fmt: skip
    if ident in LIC.UNCLASSIFIABLE:
        if record.terms is None:
            problems.append(_problem(shown, "licence", at,
                                     f"a '{ident}' record names the governing terms in '; terms: '"))  # fmt: skip
        elif ident == LIC.CIVITAI_MODEL_LICENSE and record.terms_civitai is None:
            problems.append(_problem(shown, "licence", at,
                                     "a 'civitai-model-license' record gives the listing's permissions as "
                                     "'; terms: Civitai permissions: ...'"))  # fmt: skip
    return problems


def check_licence_record(ctx: Context) -> list[Problem]:
    noncommercial = _optional(LIC.read_noncommercial, _noncommercial_path(ctx))
    conditions = _optional(LIC.read_conditions, _conditions_path(ctx))
    problems: list[Problem] = []
    seen: set[tuple[str, int]] = set()  # a run of lines shares one record, reported once
    for name, _line, record in licence_lines(ctx):
        if record is None or (name, record[0]) in seen:
            continue  # a missing record is the licence group's
        seen.add((name, record[0]))
        problems += _record_problems(ctx.config_shown(name), record[0], record[1], conditions)
    if noncommercial is not None and conditions is not None:
        listed = ctx.show(_noncommercial_path(ctx))
        for ident in sorted(set(noncommercial) & set(conditions)):
            problems.append(_problem(ctx.show(_conditions_path(ctx)), None, conditions[ident][0],
                                     f"{ident!r} is both in the conditions map and on the Noncommercial_List "
                                     f"({listed} line {noncommercial[ident]})"))  # fmt: skip
    return problems


# ---- ignore (Req 25.4) ----


def check_ignore(ctx: Context) -> list[Problem]:
    if not ctx.work_tree():
        return []  # presence reports it
    result = ctx.git("ls-files", "-z", "-ci", "--exclude-standard", "--full-name")
    if result.returncode != 0:
        raise RuntimeError(f"git ls-files -ci failed: {result.stderr.decode('utf-8', 'replace').strip()}")
    return [
        _problem(os.fsdecode(path), None, None, "git tracks this file, but a .gitignore pattern matches it")
        for path in result.stdout.split(b"\0") if path
    ]  # fmt: skip


# ---- the group table (design §C10 order) ----

GROUPS: tuple[Group, ...] = (
    Group("presence", "Req 26.17", check_presence),
    Group("licence", "Req 26.18", check_licence),
    Group("syntax", "Req 26.2", check_syntax),
    Group("tags", "Req 26.1", check_tags),
    Group("catalog", "Req 26.4", check_catalog),
    Group("install-paths", "Req 26.3", check_install_paths),
    Group("nodes", "Req 26.5", check_nodes),
    Group("patches", "Req 26.6", check_patches),
    Group("nsfw", "Req 26.7", check_nsfw),
    Group("terms", "Req 26.8", check_terms),
    Group("tokens", "Req 26.9", check_tokens),
    Group("private-data", "Req 26.10", check_private_data),
    Group("templates", "Req 26.11", check_templates_group),
    Group("examples", "Req 26.12", check_examples),
    Group("selection", "Req 26.13", check_selection),
    Group("provenance", "Req 2.2-2.6", check_provenance),
    Group("licence-record", "Req 2.4", check_licence_record),
    Group("graph", "Req 12.2, 8.6, 11.2, 12.4", check_graph),
    Group("ignore", "Req 25.4", check_ignore),
)
GROUP_NAMES: tuple[str, ...] = tuple(group.name for group in GROUPS)
