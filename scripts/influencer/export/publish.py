"""The Publish_Sheet: one Markdown file per exported Post, <post>/<post-id>_publish.md (design §C8; Req
15.5, 15.9).

    layout(post, entry, staged) -> Layout        the sheet as data, from the Post's Prompt_Sheet entry
    print_publish(layout) -> str                 the Markdown text
    write_publish(run, post, temp, staged) -> str   written into the Post's temp folder; returns its name
    fenced(text) -> str, fenced_blocks(markdown) -> [(heading, text)]   the fenced blocks, both ways

The sheet holds, in order:
1. the exported files in upload order (ascending number), each with its Slide's alt text in a fenced block
2. the caption: captions.full_text of the Prompt_Sheet's caption and hashtags, disclosure line included as
   the Prompt_Sheet stores it, character for character in a fenced block
3. the Post's planned date from the Content_Calendar
4. for an Instagram Post only, DISCLOSURE_CHECKLIST as `- [ ]` items in the Req 15.9 order

A fence is max(3, L + 1) backticks, L the longest backtick run of its text, so no line of the text can
close it and the text reads back unchanged. The text sits between the opening fence's line break and the
line break before the closing fence; only "\\n" separates lines, so "\\r" and other breaks stay in the text.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from influencer import ids

if TYPE_CHECKING:
    from influencer.calendar_io import Post
    from influencer.export.run import ExportRun, Staged
    from influencer.prompt_sheet import Entry

# The disclosure checklist of Req 15.9, in its order; the Runbook's list (task 27.5) uses this constant.
DISCLOSURE_CHECKLIST = (
    '"AI-generated profile" label on',
    "AI info label on the post",
    "AI disclosure in the bio",
    "no health claims",
    "no DMs implying a real person",
    "Instagram's nudity rules followed",
)
SUFFIX = "_publish.md"
FILES_HEADING = "## Files in upload order"
CAPTION_HEADING = "## Caption"
DATE_HEADING = "## Planned date"
CHECKLIST_HEADING = "## Disclosure checklist"
MIN_FENCE = 3
_RUN = re.compile(r"`+")


@dataclass(frozen=True)
class Layout:
    post: str
    platform: str
    date: str
    files: tuple[tuple[str, str], ...]  # (exported file name, alt text) in upload order
    caption: str  # the full caption text
    checklist: tuple[str, ...]  # DISCLOSURE_CHECKLIST for Instagram, () for other


def fence_for(text: str) -> str:
    """max(3, L + 1) backticks, L the longest run of backticks in text."""
    longest = max((len(run) for run in _RUN.findall(text)), default=0)
    return "`" * max(MIN_FENCE, longest + 1)


def fenced(text: str) -> str:
    """text in a fenced block that reads back as text."""
    fence = fence_for(text)
    return f"{fence}\n{text}\n{fence}"


def fenced_blocks(markdown: str) -> list[tuple[str | None, str]]:
    """Every fenced block of markdown as (the last heading line before it, its text)."""
    lines = markdown.split("\n")
    blocks: list[tuple[str | None, str]] = []
    heading: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("#"):
            heading = line
        elif line and set(line) == {"`"} and len(line) >= MIN_FENCE:
            end = lines.index(line, index + 1)  # a ValueError for an unclosed block
            blocks.append((heading, "\n".join(lines[index + 1:end])))
            index = end
        index += 1
    return blocks


def caption_text(markdown: str) -> str:
    """The text of the fenced block under the Caption heading."""
    return next(text for heading, text in fenced_blocks(markdown) if heading == CAPTION_HEADING)


def layout(post: Post, entry: Entry | None, staged: Sequence[Staged]) -> Layout:
    """The Publish_Sheet of an exported Post from its Prompt_Sheet entry; a missing entry, caption or alt
    text is empty text."""
    from influencer.captions import full_text

    slides = entry.slides if entry is not None else ()
    files = tuple((item.name, slides[item.number - 1].alt if item.number <= len(slides) else "")
                  for item in sorted(staged, key=lambda item: item.number))  # fmt: skip
    caption = full_text(entry.caption or "", entry.hashtags) if entry is not None else ""
    checklist = DISCLOSURE_CHECKLIST if post.platform == "instagram" else ()
    return Layout(post.id, post.platform, post.date, files, caption, checklist)


def print_publish(sheet: Layout) -> str:
    parts = [f"# Publish {sheet.post} ({sheet.platform})", "", FILES_HEADING, ""]
    for number, (name, alt) in enumerate(sheet.files, 1):
        parts += [f"### {number}. {name}", "", "Alt text:", "", fenced(alt), ""]
    parts += [CAPTION_HEADING, "", fenced(sheet.caption), "", DATE_HEADING, "", sheet.date, ""]
    if sheet.checklist:
        parts += [CHECKLIST_HEADING, "", *(f"- [ ] {item}" for item in sheet.checklist), ""]
    return "\n".join(parts)


def sheet_name(run: ExportRun, post: Post) -> str:
    return Path(ids.publish_sheet_path(run.week, post.id, post.platform)).name


def write_publish(run: ExportRun, post: Post, temp: Path, staged: Sequence[Staged]) -> str:
    """Writes <post-id>_publish.md (UTF-8, no newline translation) into temp and returns its name."""
    name = sheet_name(run, post)
    entry = run.sheet.entry(post.id) if run.sheet is not None else None
    with open(temp / name, "xb") as handle:
        handle.write(print_publish(layout(post, entry, staged)).encode("utf-8"))
    return name
