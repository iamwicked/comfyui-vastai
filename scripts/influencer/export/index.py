"""The week index: <Output_Root>/<W>/index.md (design §C8; Req 15.4, 15.6, 15.8).

    build_index(week, calendar, results, sheet, output_root) -> WeekIndex   the index as data
    print_index(index) -> str                                              the Markdown text
    write_index(run, results) -> Path                                      through write_atomic

One row per Post of the Content_Calendar, in calendar order and exactly once, Posts with no export
included: the status (a calendar Post without a PostResult is waiting), the QC_Sheet and Publish_Sheet
paths relative to the index where the files exist, and each flag that applies:
- `review`: the Prompt_Sheet entry's review list is not empty
- `failed Jobs`: the Manifest records a failed Job of the Post
- `upscaled at export`: a Slide the run exported is flagged so (task 22.6)
The counts line gives the exported, failed and waiting numbers, which add up to the number of Posts.
write_atomic leaves the previous index readable when a run is interrupted.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from influencer import ids
from influencer.atomic import write_atomic
from influencer.export.images import UPSCALED
from influencer.export.run import EXPORTED, FAILED, STATUSES, WAITING

if TYPE_CHECKING:
    from influencer.calendar_io import Calendar
    from influencer.export.run import ExportRun, PostResult

FILE_NAME = "index.md"
REVIEW, FAILED_JOBS = "review", "failed Jobs"
FLAGS = (REVIEW, FAILED_JOBS, UPSCALED)


@dataclass(frozen=True)
class Row:
    post: str
    date: str
    platform: str
    status: str
    qc: str | None  # relative to the index's folder, when the file exists
    publish: str | None
    flags: tuple[str, ...]


@dataclass(frozen=True)
class WeekIndex:
    week: str
    rows: tuple[Row, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {status: sum(1 for row in self.rows if row.status == status) for status in STATUSES}


def _relative(week: str, path: str) -> str:
    return path[len(week) + 1:]  # the ids paths start with "<W>/"


def build_index(week: str, calendar: Calendar, results: Sequence[PostResult], sheet: Any,
                output_root: Path) -> WeekIndex:  # fmt: skip
    """The index of the calendar's Posts; results by Post id, a missing one counting as waiting."""
    by_post: Mapping[str, PostResult] = {result.post.id: result for result in results}
    rows = []
    for post in calendar.posts:
        result = by_post.get(post.id)
        status = result.status if result is not None else WAITING
        paths = []
        for path in (ids.qc_sheet_path(week, post.id, post.platform), ids.publish_sheet_path(week, post.id, post.platform)):
            paths.append(_relative(week, path) if (output_root / path).is_file() else None)
        entry = sheet.entry(post.id) if sheet is not None else None
        flags = []
        if entry is not None and entry.review:
            flags.append(REVIEW)
        if result is not None and result.failed_jobs:
            flags.append(FAILED_JOBS)
        if result is not None and any(UPSCALED in found for found in result.flags.values()):
            flags.append(UPSCALED)
        rows.append(Row(post.id, post.date, post.platform, status, paths[0], paths[1], tuple(flags)))
    return WeekIndex(week, tuple(rows))


def _cell(text: str | None) -> str:
    if text is None:
        return "-"
    return text.replace("\\", "\\\\").replace("|", "\\|")


def _link(path: str | None) -> str:
    return "-" if path is None else f"[{_cell(path)}](<{path}>)"


def print_index(index: WeekIndex) -> str:
    total = index.counts
    lines = [
        f"# Week {index.week}",
        "",
        f"{len(index.rows)} Posts: {total[EXPORTED]} exported, {total[FAILED]} failed, {total[WAITING]} waiting",
        "",
        "| Post | Date | Platform | Status | QC_Sheet | Publish_Sheet | Flags |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in index.rows:
        flags = ", ".join(row.flags) if row.flags else "-"
        lines.append(f"| {_cell(row.post)} | {row.date} | {row.platform} | {row.status} | {_link(row.qc)} "
                     f"| {_link(row.publish)} | {flags} |")  # fmt: skip
    return "\n".join(lines) + "\n"


def index_path(run: ExportRun) -> Path:
    return run.output_root / run.week / FILE_NAME


def write_index(run: ExportRun, results: Sequence[PostResult]) -> Path:
    """Writes the week index through write_atomic, creating <Output_Root>/<W>/ when needed."""
    path = index_path(run)
    os.makedirs(path.parent, exist_ok=True)
    index = build_index(run.week, run.calendar, results, run.sheet, run.output_root)
    write_atomic(path, print_index(index))
    return path
