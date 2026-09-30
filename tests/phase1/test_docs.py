"""The Phase 1 docs: README.md, REFERENCE.md §13 (the Runbook), CHANGELOG.md and .gitignore (tasks 27.9 and
27.10, design §C12; Req 25.4, 28.1, 28.2, 28.5, 28.8, 28.10, 28.13, 28.14).

The phase-marker rule (Req 28.13) is checked per Markdown block: a paragraph, a list item, a table row, a
heading or a fenced code block. A block that names a Phase 2-4 command, stage, tool or id must sit under a
"Not yet available — arrives in Phase N" marker with N at least that phase: in the block itself, in one of
its enclosing headings, or, for a code block, in the block just before it. A block that names only Phase 1
commands carries no marker.
"""

from __future__ import annotations

import datetime
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import calendar_io  # noqa: E402
from influencer.export.publish import DISCLOSURE_CHECKLIST  # noqa: E402
from influencer.persona import read_persona  # noqa: E402

README = REPO / "README.md"
REFERENCE = REPO / "REFERENCE.md"
CHANGELOG = REPO / "CHANGELOG.md"
GITIGNORE = REPO / ".gitignore"
EXAMPLES = REPO / "config" / "influencer" / "examples"

RUNBOOK_HEADING = "## 13. AI influencer pipeline (Runbook)"
MARKER = re.compile(r"Not yet available — arrives in Phase (\d)")

# Req 28.1, in order.
STAGES = (
    "Persona_File",
    "Closed-model character design (manual)",
    "Dataset curation",
    "Character_LoRA training and Test_Grid checkpoint choice",
    "Weekly session",
    "Export",
    "QC",
    "Manual publishing",
)

# Req 28.2, in order: a phrase each step's text holds, and the command it names (None: manual).
CHECKLIST = (
    ("Write the week's Content_Calendar on the Mac", "influencer.sh new-calendar"),
    ("Run the `plan` stage on the Mac against the Mac's local Ollama", "influencer.sh plan"),
    ("Review and edit the Prompt_Sheet", None),
    ("Start the Instance", None),
    ("Copy the Persona_File, Content_Calendar and Prompt_Sheet into the Instance's", None),
    ("Run the `render` stage", "influencer.sh render"),
    ("Record the Picks in the Prompt_Sheet", None),
    ("Run the `finish` stage", "influencer.sh finish"),
    ("Run the `reels` stage, in weeks with reel Posts", "influencer.sh reels"),
    ("Run the export stage", "influencer.sh export"),
    ("Review the QC_Sheets", None),
    ("Copy the week's Output_Root files, Prompt_Sheet and Manifest from the Instance to the", None),
    ("Stop the Instance by hand", None),
)

# What each later phase adds that the docs may name (design §C6, §C9, Phases table).
LATER = (
    (2, re.compile(r"\btrain\.sh\b|\bTraining_Kit\b|\bTest_Grid\b|disk --base\b")),
    (2, re.compile(r"influencer\.sh (dataset|refine|grid|train-config|train-publish)\b")),
    (2, re.compile(r"`(refine|grid|dataset check|dataset captions|train-config|train-publish)`")),
    (3, re.compile(r"\binfluencer-video\b|`reels`|influencer\.sh reels\b")),
    (4, re.compile(r"\binfluencer-nsfw\b|\bcharacter-design\b|\bSource_Checker\b|`sources`|influencer\.sh sources\b")),
)
PHASE1 = re.compile(
    r"influencer\.sh (new-calendar|plan|render|finish|export|persona check|calendar check|check-config|disk --id)\b"
    r"|`(new-calendar|plan|render|finish|export|persona check|calendar check|check-config)`"
    r"|install-workflow\.sh influencer(?![-\w])|\bcheck\.sh\b|\bsave-progress\.sh\b|provision\.sh --(status|selection)"
)

LICENCE_GROUPS = (
    "Krea 2 Community License v1",
    "Krea 2 Acceptable Use Policy",
    "MiniMax H3 Community License Agreement",
    "Llama 3.1 Community License",
    "Civitai permissions",
    "No conditions on outputs",
    "Excluded from monetised posts",
)
LICENCE_DATE = "2026-09-28"
SOURCES_COMMAND = (
    "bash scripts/influencer.sh sources --id influencer --id influencer-video --id influencer-nsfw --id character-design"
)
GITIGNORE_PATTERNS = ("/influencer/", "/models/loras/characters/", "/.check-venv/", ".pytest_cache/", ".hypothesis/")


# ---- reading the Markdown ----


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def runbook() -> str:
    """REFERENCE.md §13, up to the next level-2 heading."""
    full = text(REFERENCE)
    assert RUNBOOK_HEADING in full, f"REFERENCE.md lacks {RUNBOOK_HEADING!r}"
    body = full.split(RUNBOOK_HEADING, 1)[1]
    return RUNBOOK_HEADING + re.split(r"\n## (?!#)", body, maxsplit=1)[0]


def subsections(section: str) -> dict[str, str]:
    """The ### subsections of §13 by title (the text after "### 13.N "), each up to the next ### heading."""
    parts = re.split(r"^### 13\.\d+ ", section, flags=re.M)[1:]
    return {part.split("\n", 1)[0]: part for part in parts}


def subsection(title_start: str) -> str:
    found = [body for title, body in subsections(runbook()).items() if title.startswith(title_start)]
    assert len(found) == 1, f"§13 needs one subsection titled {title_start!r}"
    return found[0]


def readme_section() -> str:
    body = text(README).split("\n## AI influencer pipeline\n", 1)
    assert len(body) == 2, "README.md lacks its AI influencer pipeline section"
    return "## AI influencer pipeline\n" + body[1].split("\n## ", 1)[0]


def v7_entry() -> str:
    full = text(CHANGELOG)
    match = re.search(r"^## v7 — .*?(?=^## v6)", full, flags=re.M | re.S)
    assert match, "CHANGELOG.md needs a v7 entry directly above v6"
    return match.group(0)


@dataclass
class Block:
    line: int
    text: str
    code: bool = False


_ITEM = re.compile(r"^\s*(?:[-*] |\d+\. |\|)")


def blocks(markdown: str) -> list[tuple[Block, frozenset[int]]]:
    """Each block with the marker phases in scope: its own, its enclosing headings' and, for a code block,
    the block before it."""
    found: list[tuple[Block, frozenset[int]]] = []
    headings: list[tuple[int, frozenset[int]]] = []
    current: Block | None = None
    previous: Block | None = None
    fence: Block | None = None

    def phases(block: Block | None) -> frozenset[int]:
        return frozenset(int(n) for n in MARKER.findall(block.text)) if block else frozenset()

    def close() -> None:
        nonlocal current, previous
        if current is not None:
            scope = phases(current).union(*(marks for _, marks in headings))
            if current.code:
                scope |= phases(previous)
            found.append((current, scope))
            previous = current
        current = None

    for number, line in enumerate(markdown.split("\n"), 1):
        if fence is not None:
            fence.text += "\n" + line
            if line.strip().startswith("```"):
                current, fence = fence, None
                close()
            continue
        if line.strip().startswith("```"):
            close()
            fence = Block(number, line, code=True)
            continue
        heading = re.match(r"^(#+) ", line)
        if heading:
            close()
            level = len(heading.group(1))
            headings = [(lvl, marks) for lvl, marks in headings if lvl < level]
            current = Block(number, line)
            headings.append((level, phases(current)))
            close()
            continue
        if not line.strip():
            close()
        elif current is None or _ITEM.match(line):
            close()
            current = Block(number, line)
        else:
            current.text += "\n" + line
    close()
    return found


def later_phase(block: Block) -> int:
    return max((phase for phase, pattern in LATER if pattern.search(block.text)), default=1)


DOCS = {
    "README.md": readme_section,
    "REFERENCE.md": lambda: text(REFERENCE),
    "CHANGELOG.md v7": v7_entry,
}


# ---- REFERENCE.md §13 ----


def test_the_runbook_follows_the_twelve_sections_and_holds_the_stages_in_order() -> None:
    titles = list(subsections(runbook()))
    assert len(titles) >= len(STAGES)
    for number, (title, stage) in enumerate(zip(titles, STAGES), 1):
        assert title.startswith(stage), (number, title, stage)
    full = text(REFERENCE)
    assert full.index("## 12. Problem log") < full.index(RUNBOOK_HEADING)
    numbers = [int(n) for n in re.findall(r"^### 13\.(\d+) ", runbook(), flags=re.M)]
    assert numbers == list(range(1, len(numbers) + 1))


@pytest.mark.parametrize("stage", STAGES)
def test_each_stage_names_its_commands_or_says_it_is_manual_and_its_files(stage: str) -> None:
    body = subsection(stage)
    opening = body.split("\n\n", 2)[1]
    assert re.search(r"Command|Manual|Install", opening), opening
    assert "Reads:" in body and "Writes:" in body


def checklist_items() -> list[str]:
    body = subsection("Weekly session")
    assert "One-time Mac prerequisites:" in body and "Weekly checklist:" in body
    prerequisites, checklist = body.split("Weekly checklist:", 1)
    prerequisites = prerequisites.split("One-time Mac prerequisites:", 1)[1]
    assert "Python 3.10 or later" in prerequisites and "`plan` stage" in prerequisites
    assert "Ollama" in prerequisites and "`influencer/`" in prerequisites
    items = re.split(r"^(\d+)\. ", checklist, flags=re.M)
    numbers, texts = [int(n) for n in items[1::2]], items[2::2]
    assert numbers == list(range(1, len(numbers) + 1))
    return texts


def test_the_weekly_checklist_holds_the_steps_in_order_and_ends_with_stopping_the_instance() -> None:
    texts = checklist_items()
    assert len(texts) == len(CHECKLIST)
    for step, (phrase, command) in zip(texts, CHECKLIST):
        assert phrase in step, (phrase, step)
        if command is None:
            assert "manual" in step, step
        else:
            assert command in step, (command, step)
    assert texts[-1].startswith("Stop the Instance by hand")


def test_the_start_step_gives_the_hardware_guidance_and_the_reel_host_rule_under_phase_3() -> None:
    start = checklist_items()[3]
    main, _, rule = start.partition("\n   - ")
    for fact in ("80 GB", "141-192 GB", "H200", "B200", "sm_100", "open item 12"):
        assert fact in main, fact
    assert not MARKER.search(main)
    assert "outside the H3-excluded territories" in rule and "Vast's offer list" in rule
    assert MARKER.findall(rule) == ["3"]


def test_the_runbook_disclosure_checklist_is_the_publish_sheets_item_by_item() -> None:
    body = subsection("Manual publishing")
    items = [line[len("- [ ] "):] for line in body.split("\n") if line.startswith("- [ ] ")]
    assert len(items) == len(DISCLOSURE_CHECKLIST) == 6
    for number, (runbook_item, sheet_item) in enumerate(zip(items, DISCLOSURE_CHECKLIST), 1):
        assert runbook_item == sheet_item, number


def test_the_qc_section_names_the_review_as_the_krea_content_filter() -> None:
    body = subsection("QC")
    assert "The QC_Sheet review is the human review that the Krea 2 Community License accepts as a" in body
    assert "content filter (4.2)" in body and "before it is posted" in body


def test_the_csv_section_documents_every_column_and_one_valid_row_per_post_type(tmp_path: Path) -> None:
    body = subsection("Content_Calendar CSV format")
    rows = {match.group(1): match.group(2) for match in re.finditer(r"^\| `(\w+)` \| (yes|no) \|", body, re.M)}
    assert list(rows) == list(calendar_io.COLUMNS)
    assert [c for c, required in rows.items() if required == "yes"] == list(calendar_io.REQUIRED_COLUMNS)
    assert "`;`" in body and "line break" in body  # how a list value is written in one cell
    example = re.search(r"```csv\n(.*?)```", body, re.S)
    assert example, "the CSV section needs a ```csv example"
    path = tmp_path / "calendar.csv"
    path.write_text(example.group(1), encoding="utf-8")
    persona = read_persona(EXAMPLES / "persona.yaml", EXAMPLES, check_files=False)
    result = calendar_io.read_calendar(path, persona, None, check_files=False)
    assert result.calendar is not None and not result.rejected, result.all_problems()
    assert sorted(post.type for post in result.calendar.posts) == ["carousel", "reel", "single", "story"]


def test_the_licences_section_lists_every_group_with_its_date_and_the_phase_4_command() -> None:
    body = subsection("Licences")
    assert "before the first monetised post" in body.split("\n\n", 2)[1]
    for group in LICENCE_GROUPS:
        paragraph = re.search(rf"^\*\*{re.escape(group)}[^\n]*(?:\n(?!\n)[^\n]*)*", body, re.M)
        assert paragraph, group
        assert f"(checked {LICENCE_DATE})" in paragraph.group(0), group
    scopes = [scope for block, scope in blocks(body) if block.code and SOURCES_COMMAND in block.text]
    assert len(scopes) == 1 and 4 in scopes[0], scopes  # the sources command sits under its Phase 4 marker
    for fact in ("US$1,000,000", "(4.2)", "(4.3)", "4.1(c)", "(9.1)", "(9.2)", "V.4", "V.3", "IV.1", "IV.2",
                 "AUP item 5", "AUP item 12", "700M", "Built with Llama", "Qwen Research License"):  # fmt: skip
        assert fact in body, fact


def test_the_disk_table_marks_what_is_not_yet_measured() -> None:
    body = subsection("Disk table")
    assert re.search(r"^\| `influencer` \| 107\.7, of which 102\.1 new \| 2026-09-28 \|", body, re.M)
    for row in ("`influencer-video`", "`influencer-nsfw`", "`character-design`", "`krea/Krea-2-Raw`", "`all`"):
        line = next(line for line in body.split("\n") if line.startswith("| ") and row in line)
        assert "not yet measured" in line, row


# ---- the phase markers (Req 28.13) ----


@pytest.mark.parametrize("doc", list(DOCS))
def test_every_later_phase_name_carries_its_marker_and_no_phase_1_command_does(doc: str) -> None:
    unmarked, marked_phase1 = [], []
    for block, scope in blocks(DOCS[doc]()):
        needed = later_phase(block)
        if needed > 1 and max(scope, default=1) < needed:
            unmarked.append((block.line, needed, block.text[:120]))
        if needed == 1 and PHASE1.search(block.text) and MARKER.search(block.text):
            marked_phase1.append((block.line, block.text[:120]))
    assert unmarked == [] and marked_phase1 == []


def test_the_markers_name_a_later_phase_only() -> None:
    for doc, read in DOCS.items():
        assert set(MARKER.findall(read())) <= {"2", "3", "4"}, doc


# ---- README.md, CHANGELOG.md, .gitignore ----


def test_the_readme_section_lists_the_four_ids_and_links_the_runbook() -> None:
    body = readme_section()
    for influencer_id in ("influencer", "influencer-video", "influencer-nsfw", "character-design"):
        assert f"| `{influencer_id}` |" in body, influencer_id
    assert "install-workflow.sh influencer\n" in body
    assert "(REFERENCE.md#13-ai-influencer-pipeline-runbook)" in body


def test_the_changelog_has_a_dated_v7_entry_above_v6_and_reads_v7() -> None:
    full = text(CHANGELOG)
    date = re.search(r"^## v7 — (\d{4}-\d{2}-\d{2})$", full, re.M)
    assert date and datetime.date.fromisoformat(date.group(1))
    assert full.index("\n## v7 — ") < full.index("\n## v6")
    assert "The current version is **v7**." in full.split("\n## ", 1)[0]
    entry = v7_entry()
    for name in ("influencer", "influencer-video", "influencer-nsfw", "character-design", "Export_Tool",
                 "Config_Checker", "Disk_Estimator", "Check_Suite", "--selection", "Training_Kit", "`reels`",
                 "Source_Checker"):  # fmt: skip
        assert name in entry, name


def test_gitignore_holds_the_five_patterns() -> None:
    lines = text(GITIGNORE).split("\n")
    for pattern in GITIGNORE_PATTERNS:
        assert pattern in lines, pattern
    assert "__pycache__/" in lines


@pytest.mark.skipif(shutil.which("git") is None, reason="git is missing; the check asks git check-ignore")
def test_git_ignores_the_root_folders_but_not_the_package() -> None:
    def ignored(path: str) -> bool:
        run = subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", "--no-index", path],
                             capture_output=True, timeout=30)  # fmt: skip
        assert run.returncode in (0, 1), run.stderr
        return run.returncode == 0

    assert not ignored("scripts/influencer/")
    assert not ignored("scripts/influencer/__main__.py")
    assert ignored("influencer/mara/persona.yaml")
    assert ignored("models/loras/characters/mara/mara_v1.safetensors")
