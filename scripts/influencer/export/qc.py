"""The QC_Sheet: one JPEG per exported Post, <post>/<post-id>_qc.jpg (design §C8; Req 15.1-15.3, 15.7).

    pillow_problem(version=None) -> str | None          why this Pillow cannot draw the sheet, or None
    layout(run, post, temp, staged) -> Layout            the sheet as data: header lines and rows of tiles
    draw(layout) -> bytes                                the Layout as JPEG bytes
    write_qc(run, post, temp, staged) -> str             both, written into the Post's temp folder

The sheet is built as a Layout first and drawn second, so tests assert rows, labels and order.

- The header lists the Persona_File's signature items and the checks face, hands, teeth, jewellery
  continuity and legible background text (Req 15.3).
- A still Post gets one row per Slide in ascending Slide number: the Hero_Image, then every rendered
  Candidate of the Slide in ascending Candidate number (Req 15.1). A rendered Candidate is the latest done
  render record of that Post, Slide and Candidate whose first output is on disk.
- Each Candidate's label: the Post id, the two-digit Slide number of the exported file, `c` and the
  Candidate number from 1, the seed the Manifest records and the Render_Template name, then `PICK` on the
  Slide's Pick (Req 15.7).
- A reel Post gets one row: the Hero_Image, then frames 1, floor(F/2)+1 and F of the exported Reel through
  video.grab_frame (Req 15.2). Without ffmpeg the row holds the Hero_Image and says why the frames are
  unavailable; with no exported Reel there is no row.
- Every image is scaled to a height of 1,024 px with its ratio kept. Text uses
  ImageFont.load_default(size=28), which needs Pillow 10.1 or later (pillow_problem).
"""
from __future__ import annotations

import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from influencer import ids
from influencer.manifest import DONE

if TYPE_CHECKING:
    from influencer.calendar_io import Post
    from influencer.export.run import ExportRun, Staged

MIN_PILLOW = (10, 1)
FONT_SIZE = 28
ROW_HEIGHT = 1024  # every image's height on the sheet (Req 15.1, 15.2)
CHECKS = ("face", "hands", "teeth", "jewellery continuity", "legible background text")  # Req 15.3
PICK_MARK = "PICK"
HERO_LABEL = "Hero_Image"
SUFFIX = "_qc.jpg"

_PAD, _GAP, _LINE = 24, 16, 40  # margins, space between tiles, one text line's height
_MIN_WIDTH = 1600  # the canvas is at least this wide, so the header lines fit
_BACKGROUND, _TEXT, _PICK = (255, 255, 255), (0, 0, 0), (200, 0, 0)
_QUALITY = 85


# ---- the Pillow check ----


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = []
    for part in version.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
        if len(digits) != len(part):
            break
    return tuple(parts)


def pillow_problem(version: str | None = None) -> str | None:
    """None when Pillow `version` (the running one when None) is 10.1 or later, else the report naming it."""
    if version is None:
        import PIL

        version = PIL.__version__
    if _version_tuple(version) >= MIN_PILLOW:
        return None
    wanted = ".".join(str(part) for part in MIN_PILLOW)
    return (f"the QC_Sheet needs Pillow {wanted} or later for ImageFont.load_default(size={FONT_SIZE}); "
            f"this is Pillow {version}, so no QC_Sheet is written")  # fmt: skip


# ---- the layout ----


@dataclass(frozen=True)
class Tile:
    """One image of a row: a file path or PNG bytes (a Reel frame), and its label."""

    source: Path | bytes
    label: str
    pick: bool = False


@dataclass(frozen=True)
class Row:
    """One row: its title, its tiles in order, and a note drawn after them (why frames are missing)."""

    title: str
    tiles: tuple[Tile, ...]
    note: str | None = None


@dataclass(frozen=True)
class Layout:
    post: str
    header: tuple[str, ...]
    rows: tuple[Row, ...]


def candidate_label(post_id: str, slide: int, candidate: int, seed: int | None, template: str,
                    pick: bool) -> str:  # fmt: skip
    """`<post> <NN> c<n> seed <seed> <template>`, then ` PICK` on the Pick (Req 15.7)."""
    shown = "?" if seed is None else str(seed)
    label = f"{post_id} {slide:02d} c{candidate} seed {shown} {template}"
    return f"{label} {PICK_MARK}" if pick else label


def header_lines(persona: object) -> tuple[str, ...]:
    items = tuple(getattr(persona, "signature_items", ()) or ())
    return (
        "Signature items: " + (", ".join(items) if items else "(none listed)"),
        "Check: " + ", ".join(CHECKS),
    )


def rendered_candidates(run: ExportRun, post: Post, slide: int) -> list[tuple[int, int | None, str, Path]]:
    """(candidate, seed, template, path) of each rendered Candidate of the Slide, ascending by Candidate: the
    latest done render record of each Candidate whose first output file is present."""
    from influencer import jobs as J

    found: dict[int, tuple[int | None, str, Path]] = {}
    for record in run.manifest.jobs:
        if (record.stage != J.RENDER or record.post != post.id or record.slide != slide
                or record.status != DONE or not record.outputs or record.candidate is None):  # fmt: skip
            continue
        path = run.files.output_dir / record.outputs[0].path
        if run.files.present(path):
            found[record.candidate] = (record.seed, record.template, path)
    return [(number, *found[number]) for number in sorted(found)]


def _pick(run: ExportRun, post: Post, slide: int) -> int | None:
    from influencer.prompt_sheet import pick_candidate

    entry = run.sheet.entry(post.id) if run.sheet is not None else None
    if entry is None or slide > len(entry.slides):
        return None
    try:
        return pick_candidate(entry.slides[slide - 1].pick, post.candidates)
    except ValueError:
        return None


def hero_tile(run: ExportRun) -> Tile:
    from influencer.persona import resolve_path

    return Tile(resolve_path(run.root, run.persona.hero_image), HERO_LABEL)


def still_rows(run: ExportRun, post: Post, staged: Sequence[Staged]) -> tuple[Row, ...]:
    hero = hero_tile(run)
    rows = []
    for number in sorted(item.number for item in staged):
        pick = _pick(run, post, number)
        tiles = [hero] + [
            Tile(path, candidate_label(post.id, number, candidate, seed, template, candidate == pick), candidate == pick)
            for candidate, seed, template, path in rendered_candidates(run, post, number)
        ]  # fmt: skip
        rows.append(Row(f"Slide {number:02d}", tuple(tiles)))
    return tuple(rows)


def frame_numbers(frames: int) -> tuple[int, int, int]:
    """1, floor(F/2)+1 and F (Req 15.2)."""
    return (1, frames // 2 + 1, frames)


def reel_row(run: ExportRun, post: Post, reel: Path) -> Row:
    """The Hero_Image and frames 1, floor(F/2)+1 and F of the exported Reel, or a note that ffmpeg is
    unavailable."""
    from influencer.export import video

    hero = hero_tile(run)
    title = f"Reel {reel.name}"
    probed = video.probe(reel)
    if isinstance(probed, video.Unavailable):
        return Row(title, (hero,), f"frames unavailable: {probed}")
    tiles = [hero]
    for number in frame_numbers(probed.frames):
        data = video.grab_frame(reel, number)
        if isinstance(data, video.Unavailable):
            return Row(title, (hero,), f"frames unavailable: {data}")
        tiles.append(Tile(data, f"{post.id} frame {number} of {probed.frames}"))
    return Row(title, tuple(tiles))


def layout(run: ExportRun, post: Post, temp: Path, staged: Sequence[Staged]) -> Layout:
    """The QC_Sheet of an exported Post as data; temp holds the staged files."""
    if post.type == "reel":
        rows = (reel_row(run, post, temp / staged[0].name),) if staged else ()
    else:
        rows = still_rows(run, post, staged)
    return Layout(post.id, header_lines(run.persona), rows)


# ---- drawing ----


def scaled_size(width: int, height: int) -> tuple[int, int]:
    """(width, height) scaled to a height of ROW_HEIGHT, the ratio kept."""
    return max(1, round(width * ROW_HEIGHT / height)), ROW_HEIGHT


def _open(source: Path | bytes):  # noqa: ANN202 - a PIL image
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as image:
        image = ImageOps.exif_transpose(image)
        return image.convert("RGB")


def draw(sheet: Layout) -> bytes:
    """The Layout as JPEG bytes. Raises OSError or ValueError for an image that cannot be read."""
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.load_default(size=FONT_SIZE)
    drawn_rows = []
    for row in sheet.rows:
        images = []
        for tile in row.tiles:
            image = _open(tile.source)
            images.append(image.resize(scaled_size(*image.size), Image.LANCZOS))
        drawn_rows.append(images)
    widths = [sum(image.width for image in images) + _GAP * max(0, len(images) - 1) for images in drawn_rows]
    widths.append(_MIN_WIDTH)  # room for the header and notes
    width = max(widths) + 2 * _PAD
    header_h = _PAD + _LINE * (len(sheet.header) + 1)
    row_h = _LINE + ROW_HEIGHT + _LINE + _GAP
    height = header_h + row_h * len(sheet.rows) + _PAD
    canvas = Image.new("RGB", (width, height), _BACKGROUND)
    pen = ImageDraw.Draw(canvas)
    y = _PAD
    pen.text((_PAD, y), f"QC {sheet.post}", fill=_TEXT, font=font)
    for line in sheet.header:
        y += _LINE
        pen.text((_PAD, y), line, fill=_TEXT, font=font)
    y = header_h
    for row, images in zip(sheet.rows, drawn_rows):
        title = row.title if row.note is None else f"{row.title}: {row.note}"
        pen.text((_PAD, y), title, fill=_TEXT, font=font)
        x = _PAD
        for tile, image in zip(row.tiles, images):
            canvas.paste(image, (x, y + _LINE))
            pen.text((x, y + _LINE + ROW_HEIGHT + 4), tile.label, fill=_PICK if tile.pick else _TEXT, font=font)
            x += image.width + _GAP
        y += row_h
    buffer = io.BytesIO()
    canvas.save(buffer, "JPEG", quality=_QUALITY)
    return buffer.getvalue()


def sheet_name(run: ExportRun, post: Post) -> str:
    return Path(ids.qc_sheet_path(run.week, post.id, post.platform)).name


def write_qc(run: ExportRun, post: Post, temp: Path, staged: Sequence[Staged]) -> str:
    """Writes <post-id>_qc.jpg into temp and returns its name."""
    name = sheet_name(run, post)
    data = draw(layout(run, post, temp, staged))
    with open(temp / name, "xb") as handle:
        handle.write(data)
    return name
