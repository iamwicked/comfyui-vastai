"""Image geometry: the export crop box, the refine size and the finish upscale factor (design §A7, §C7,
Decision 27; Req 12.4, 13.3, 13.4, 18.7).

- crop_box: the largest centred box of a target's ratio inside a source (§A7). The Export_Tool cuts it
  before its Lanczos resize to the export size (Req 13.3), and the refine stage before scaling a Dataset
  image to its refine size.
- refine_size: the size the Refine_Template renders a Dataset image at (§C7, Req 18.7).
- finish_factor: the integer `{{upscale}}` of a Finish Job (Decision 27, Req 12.4).

Integer arithmetic only, so no result depends on float rounding. A ratio a/b of positive integers is
rounded half up as (2·a + b) // (2·b), and rounded up as -(-a // b). The Reel geometry of §A8
(reel_frames, reel_size) and the Reel first-frame crop join this module with Phase 3.

Standard library only; nothing here reads or writes a file.
"""
from __future__ import annotations

import operator

# The Refine_Template's source preparation (§C7): the longer side at most 1920 px, never scaled up, and
# both sides multiples of 16.
REFINE_MAX_SIDE = 1920
REFINE_MULTIPLE = 16

# The finished image is at least twice the export size on both sides (Req 12.4), so the factor is never
# below 2 (Decision 27).
FINISH_MIN_FACTOR = 2


def _pixels(name: str, value: int) -> int:
    """value as a whole number of pixels, at least 1: a TypeError for a float or a str, a ValueError for a
    number below 1."""
    number = operator.index(value)
    if number < 1:
        raise ValueError(f"{name} must be at least 1 pixel, not {number}")
    return number


def crop_box(w: int, h: int, tw: int, th: int) -> tuple[int, int, int, int]:
    """(x, y, cw, ch): the largest box of the target ratio tw:th centred in a w×h source (§A7, Req 13.3).

    A source at least as wide as the target (w·th >= h·tw) keeps its full height, and the box width is
    h·tw/th rounded half up; any other source keeps its full width, and the box height is w·th/tw rounded
    half up. The rounded side is clamped to 1 .. the source side, though for the export targets
    (1080×1440, 1080×1350, 1080×1920) neither clamp ever binds. x and y centre the box, rounding down. So
    the box lies inside the source, is at least 1×1, spans the full width or height, is within ½ px of the
    target ratio on its free side, and its opposite margins differ by at most 1 px (Req 13.4).

    The engine crops with Pillow as image.crop((x, y, x + cw, y + ch)).
    """
    w, h = _pixels("the source width", w), _pixels("the source height", h)
    tw, th = _pixels("the target width", tw), _pixels("the target height", th)
    if w * th >= h * tw:  # the source is at least as wide as the target
        ch = h
        cw = min(w, max(1, (2 * h * tw + th) // (2 * th)))
    else:
        cw = w
        ch = min(h, max(1, (2 * w * th + tw) // (2 * tw)))
    return (w - cw) // 2, (h - ch) // 2, cw, ch


def refine_size(w: int, h: int) -> tuple[int, int]:
    """(rw, rh): the size the refine stage renders a w×h Dataset image at (§C7, Req 18.7).

    The source's own size is scaled down so that its longer side is REFINE_MAX_SIDE (a source whose longer
    side is at most that keeps its size, as it is never scaled up), each side rounded half up. Each side is
    then cut down to a multiple of REFINE_MULTIPLE. The ratio is kept up to that last cut, which takes less
    than 16 px from each side.

    The engine cuts crop_box(w, h, rw, rh) from the source, a box at least rw×rh, and resizes it to exactly
    rw×rh, so the refined image keeps the source's framing and is never enlarged.

    Raises ValueError when a side would end below REFINE_MULTIPLE, which only upscaling could reach.
    """
    w, h = _pixels("the source width", w), _pixels("the source height", h)
    sw, sh = w, h
    longer = max(w, h)
    if longer > REFINE_MAX_SIDE:  # each side × REFINE_MAX_SIDE / longer, rounded half up
        sw = (2 * w * REFINE_MAX_SIDE + longer) // (2 * longer)
        sh = (2 * h * REFINE_MAX_SIDE + longer) // (2 * longer)
    rw, rh = sw - sw % REFINE_MULTIPLE, sh - sh % REFINE_MULTIPLE
    if rw < REFINE_MULTIPLE or rh < REFINE_MULTIPLE:
        raise ValueError(
            f"a {w}×{h} image is too small to refine: scaled to at most {REFINE_MAX_SIDE} px it is {sw}×{sh}, "
            f"and each side needs at least {REFINE_MULTIPLE} px"
        )
    return rw, rh


def finish_factor(w: int, h: int, export_w: int, export_h: int) -> int:
    """The integer upscale factor of a Finish Job for a w×h Pick and an export_w×export_h export size
    (Decision 27): the smallest f >= 2 with f·w >= 2·export_w and f·h >= 2·export_h.

    VOSR2 scales the Pick to exactly f·w × f·h before restoring it, so the finished image keeps the Pick's
    ratio and is at least twice the export size on both sides (Req 12.4): 2 for every Still Pick and 3 for
    both Edit Picks. The result is not clamped. finish.json bounds `upscale` to 2–4, so a Pick that needs
    more fails the Template_Filler's bounds check by name instead of finishing below the floor.
    """
    w, h = _pixels("the Pick width", w), _pixels("the Pick height", h)
    export_w, export_h = _pixels("the export width", export_w), _pixels("the export height", export_h)
    return max(FINISH_MIN_FACTOR, -(-2 * export_w // w), -(-2 * export_h // h))
