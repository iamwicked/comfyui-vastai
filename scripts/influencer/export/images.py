"""The still-image export pipeline and its target sizes (design §C8; Req 13.1–13.3, 13.5–13.9, 14.1–14.4).

    target_size(post) -> (w, h)      1080×1350 for a 4:5 single or carousel Post, 1080×1440 for every other
                                     single or carousel Post, 1080×1920 for story Slides (and Reels)
    render(source, size, grading, week, post_id, slide) -> ExportedImage
    export_slide(source, post, week, slide, grading) -> ExportedImage      render at target_size(post)

render runs the §C8 steps in order: open the source with Pillow, apply its EXIF orientation, convert to
RGB, crop geometry.crop_box, resize to the target with Lanczos, copy the pixels alone into a fresh uint8
array, grade with lut.apply and grade.grain, then encode a fresh image as a quality 95, 4:4:4, baseline
JPEG with the SRGB_V2 profile and an empty EXIF, and add the XMP Digital Source Type packet after APP0.
No comment is set and the fresh image has an empty info, so no source metadata, ComfyUI's prompt and
workflow included, reaches the export (Req 13.8). A source narrower or shorter than the target still
exports, flagged UPSCALED (Req 13.5).

The same source, size, LUT, strength and ids give the same bytes in one environment (Req 14.4). Writing
the file, and staging a Post's files atomically, belong to the caller; this module only reads the source.
Pillow and NumPy.
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageOps

from influencer import grade, lut
from influencer.calendar_io import Post
from influencer.export import icc, xmp
from influencer.geometry import crop_box

FEED_SIZE = (1080, 1440)  # single and carousel Posts, 3:4
FEED_SIZE_4_5 = (1080, 1350)  # single and carousel Posts that set 4:5
VERTICAL_SIZE = (1080, 1920)  # story Slides and Reels
FEED_TYPES = ("single", "carousel")
UPSCALED = "upscaled at export"
JPEG_QUALITY = 95


def target_size(post: Post) -> tuple[int, int]:
    """The export width and height of every Slide of post (Req 13.1, 13.2): one size per Post, so every
    Slide of a carousel gets the same (Req 13.6)."""
    if post.type in FEED_TYPES:
        return FEED_SIZE_4_5 if post.aspect == "4:5" else FEED_SIZE
    return VERTICAL_SIZE


@dataclass(frozen=True)
class ExportedImage:
    """One exported Slide: the JPEG bytes, their width and height, and whether the source was smaller than
    the target on either side."""

    data: bytes
    size: tuple[int, int]
    upscaled: bool

    @property
    def flags(self) -> tuple[str, ...]:
        return (UPSCALED,) if self.upscaled else ()


def _pixels(source: str | os.PathLike[str], size: tuple[int, int]) -> tuple[np.ndarray, bool]:
    """Steps 1-4: the cropped and resized source as a fresh H×W×3 uint8 array, and the upscale flag."""
    tw, th = size
    with Image.open(source) as opened:
        oriented = ImageOps.exif_transpose(opened)  # a new image, the orientation applied
        rgb = oriented.convert("RGB")
    w, h = rgb.size
    x, y, cw, ch = crop_box(w, h, tw, th)
    resized = rgb.crop((x, y, x + cw, y + ch)).resize((tw, th), Image.LANCZOS)
    pixels = np.array(resized, dtype=np.uint8)  # the pixels only, in a new array
    return pixels, (w < tw or h < th)


def encode(pixels: np.ndarray) -> bytes:
    """Steps 6-7: an H×W×3 uint8 array as the exported JPEG bytes, the XMP packet inserted after APP0."""
    image = Image.fromarray(np.ascontiguousarray(pixels, dtype=np.uint8))
    if image.mode != "RGB":
        raise ValueError(f"encode needs an H×W×3 RGB array, not mode {image.mode}")
    buffer = io.BytesIO()
    image.save(
        buffer, "JPEG", quality=JPEG_QUALITY, subsampling=0, optimize=False, progressive=False,
        icc_profile=icc.SRGB_V2, exif=b"",
    )  # fmt: skip
    return xmp.insert(buffer.getvalue(), xmp.DIGITAL_SOURCE_TYPE_PACKET)


def render(
    source: str | os.PathLike[str], size: tuple[int, int], grading: grade.Grading, week: str, post_id: str, slide: int
) -> ExportedImage:
    """source exported at size (w, h), graded with grading's LUT and grain, the grain seeded by week,
    post_id and slide (§C8 steps 1-7). A source Pillow cannot read raises its OSError."""
    pixels, upscaled = _pixels(source, size)
    graded = lut.apply(grading.lut, pixels)
    grained = grade.grain(graded, grading.strength, week, post_id, slide)
    return ExportedImage(encode(grained), (size[0], size[1]), upscaled)


def export_slide(
    source: str | os.PathLike[str], post: Post, week: str, slide: int, grading: grade.Grading
) -> ExportedImage:
    """Slide slide of post exported at target_size(post)."""
    return render(source, target_size(post), grading, week, post.id, slide)
