"""Tests of scripts/influencer/geometry.py (task 14, design §A7, §C7, Decision 27; Req 12.4, 13.3, 13.4,
18.7).

- Property 29: the export crop box, for every source size from 1 to 16,384 and each export target
- crop_box pins for the three targets at a square, a very wide and a very tall source, and at 1×1, 16,384×1
  and 1×16,384 sources
- finish_factor: the five §C7 rows, a Pick already twice its export size, one where only the width forces
  3, f − 1 missing a side whenever f > 2, and no clamping
- refine_size: pins, the too-small error, and its invariants over a sweep of sizes
"""
from __future__ import annotations

import sys
from fractions import Fraction

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer.geometry import crop_box, finish_factor, refine_size  # noqa: E402

TARGETS = ((1080, 1440), (1080, 1350), (1080, 1920))  # the Feed_Sizes and the Vertical_Size (Req 13.4)


# ---- crop_box ----


# Feature: ai-influencer-pipeline, Property 29: Export crop box
@example(w=1, h=1, target=(1080, 1440))
@example(w=16384, h=1, target=(1080, 1920))
@example(w=1, h=16384, target=(1080, 1920))
@example(w=16384, h=16384, target=(1080, 1350))
@given(w=st.integers(1, 16384), h=st.integers(1, 16384), target=st.sampled_from(TARGETS))
def test_export_crop_box(w: int, h: int, target: tuple[int, int]) -> None:
    """The crop box lies inside the source, is at least 1×1, spans the full width or the full height,
    matches the target ratio to within 1 px on its free side, and its opposite margins differ by at most
    1 px.

    **Validates: Requirements 13.4**
    """
    tw, th = target
    box = crop_box(w, h, tw, th)
    assert all(type(value) is int for value in box)
    x, y, cw, ch = box
    assert cw >= 1 and ch >= 1
    assert x >= 0 and y >= 0 and x + cw <= w and y + ch <= h
    assert cw == w or ch == h
    # |cw − ch·tw/th| ≤ 1 and |ch − cw·th/tw| ≤ 1, multiplied out so the comparison is exact
    width_fits = abs(cw * th - ch * tw) <= th
    height_fits = abs(ch * tw - cw * th) <= tw
    if ch == h and cw != w:  # the full height: the width is the free side
        assert width_fits
    elif cw == w and ch != h:  # the full width: the height is
        assert height_fits
    else:  # the whole source
        assert width_fits or height_fits
    assert abs(x - (w - x - cw)) <= 1
    assert abs(y - (h - y - ch)) <= 1


# (source, target) -> (x, y, cw, ch), worked out by hand from §A7's arithmetic
CROP_PINS = {
    ((4096, 4096), (1080, 1440)): (512, 0, 3072, 4096),
    ((16384, 1024), (1080, 1440)): (7808, 0, 768, 1024),
    ((1024, 16384), (1080, 1440)): (0, 7509, 1024, 1365),  # 1365.33 rounds down; margins 7509 and 7510
    ((1, 1), (1080, 1440)): (0, 0, 1, 1),
    ((16384, 1), (1080, 1440)): (8191, 0, 1, 1),
    ((1, 16384), (1080, 1440)): (0, 8191, 1, 1),
    ((4096, 4096), (1080, 1350)): (409, 0, 3277, 4096),  # 3276.8 rounds up; margins 409 and 410
    ((16384, 1024), (1080, 1350)): (7782, 0, 819, 1024),
    ((1024, 16384), (1080, 1350)): (0, 7552, 1024, 1280),
    ((1, 1), (1080, 1350)): (0, 0, 1, 1),
    ((16384, 1), (1080, 1350)): (8191, 0, 1, 1),
    ((1, 16384), (1080, 1350)): (0, 8191, 1, 1),
    ((4096, 4096), (1080, 1920)): (896, 0, 2304, 4096),
    ((16384, 1024), (1080, 1920)): (7904, 0, 576, 1024),
    ((1024, 16384), (1080, 1920)): (0, 7282, 1024, 1820),
    ((1, 1), (1080, 1920)): (0, 0, 1, 1),  # 0.5625 rounds up to 1
    ((16384, 1), (1080, 1920)): (8191, 0, 1, 1),
    ((1, 16384), (1080, 1920)): (0, 8191, 1, 2),  # 1.78 rounds up to 2
    # Finished Picks already at the export ratio keep every pixel
    ((2880, 3840), (1080, 1440)): (0, 0, 2880, 3840),
    ((3072, 3840), (1080, 1350)): (0, 0, 3072, 3840),
    ((2160, 3840), (1080, 1920)): (0, 0, 2160, 3840),
}


@pytest.mark.parametrize(("source", "target"), list(CROP_PINS))
def test_crop_box_pins(source: tuple[int, int], target: tuple[int, int]) -> None:
    assert crop_box(*source, *target) == CROP_PINS[(source, target)]


def test_crop_box_takes_whole_pixels_only() -> None:
    with pytest.raises(ValueError, match="at least 1 pixel"):
        crop_box(0, 10, 1080, 1440)
    with pytest.raises(ValueError, match="at least 1 pixel"):
        crop_box(10, 10, 1080, -1)
    with pytest.raises(TypeError):
        crop_box(10.0, 10, 1080, 1440)  # type: ignore[arg-type]


# ---- finish_factor (Decision 27) ----

FINISH_ROWS = [
    ((1440, 1920), (1080, 1440), 2),  # the §C7 table: `still` 3:4
    ((1536, 1920), (1080, 1350), 2),  # `still` 4:5
    ((1080, 1920), (1080, 1920), 2),  # `still` 9:16 story
    ((864, 1152), (1080, 1440), 3),  # `edit` 3:4
    ((896, 1120), (1080, 1350), 3),  # `edit` 4:5
    ((2160, 2880), (1080, 1440), 2),  # already twice the export size
    ((4320, 5760), (1080, 1440), 2),  # far above it: still 2, the smallest factor
    ((1000, 1920), (1080, 1440), 3),  # only the width forces 3
]


@pytest.mark.parametrize(("pick", "export", "factor"), FINISH_ROWS)
def test_finish_factor_is_the_smallest_that_doubles_the_export_size(
    pick: tuple[int, int], export: tuple[int, int], factor: int
) -> None:
    (w, h), (export_w, export_h) = pick, export
    f = finish_factor(w, h, export_w, export_h)
    assert f == factor
    assert f * w >= 2 * export_w and f * h >= 2 * export_h  # meets Req 12.4's floor
    if f > 2:  # and f − 1 would miss it on a side
        assert (f - 1) * w < 2 * export_w or (f - 1) * h < 2 * export_h


def test_only_the_width_forces_a_factor_of_3() -> None:
    assert 2 * 1920 >= 2 * 1440  # the height alone would allow 2
    assert 2 * 1000 < 2 * 1080  # the width does not
    assert finish_factor(1000, 1920, 1080, 1440) == 3


def test_finish_factor_is_not_clamped() -> None:
    # finish.json bounds `upscale` to 2–4, so the Filler rejects this by name instead of finishing it small
    assert finish_factor(400, 600, 1080, 1440) == 6
    assert finish_factor(1, 1, 1080, 1920) == 3840


def test_finish_factor_takes_whole_pixels_only() -> None:
    with pytest.raises(ValueError, match="the Pick width must be at least 1 pixel"):
        finish_factor(0, 1920, 1080, 1440)
    with pytest.raises(TypeError):
        finish_factor(1440, 1920.5, 1080, 1440)  # type: ignore[arg-type]


# ---- refine_size (§C7, Req 18.7) ----

REFINE_PINS = [
    ((4000, 3000), (1920, 1440)),
    ((3000, 4000), (1440, 1920)),
    ((4032, 3024), (1920, 1440)),
    ((6000, 4000), (1920, 1280)),
    ((5000, 3333), (1920, 1280)),  # 1279.87 rounds up to 1280, a multiple of 16
    ((2000, 1999), (1920, 1904)),  # 1919.04 rounds to 1919, cut down to 1904
    ((1920, 1080), (1920, 1072)),  # not scaled; 1080 is cut down to 1072
    ((1080, 1350), (1072, 1344)),
    ((1024, 1024), (1024, 1024)),
    ((1000, 1000), (992, 992)),  # never scaled up
    ((17, 17), (16, 16)),
]


@pytest.mark.parametrize(("source", "refined"), REFINE_PINS)
def test_refine_size_pins(source: tuple[int, int], refined: tuple[int, int]) -> None:
    assert refine_size(*source) == refined


@pytest.mark.parametrize("source", [(15, 1000), (1000, 15), (16384, 16)])
def test_refine_size_refuses_sources_that_would_need_upscaling(source: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="too small to refine"):
        refine_size(*source)


SWEEP = (16, 17, 31, 100, 511, 1023, 1024, 1080, 1350, 1440, 1919, 1920, 1921, 2048, 3000, 4032, 8191, 16384)


def test_refine_size_keeps_the_ratio_and_never_enlarges() -> None:
    half = Fraction(1, 2)
    for w in SWEEP:
        for h in SWEEP:
            scale = min(Fraction(1), Fraction(1920, max(w, h)))  # down to a 1920 px longer side, never up
            if min(w, h) * scale < 16 - half:  # the shorter side would round to less than 16 px
                with pytest.raises(ValueError):
                    refine_size(w, h)
                continue
            rw, rh = refine_size(w, h)
            assert rw % 16 == 0 and rh % 16 == 0 and max(rw, rh) <= 1920, (w, h)
            # each side is the scaled side rounded (within ½ px), then cut down by at most 15 px
            assert -half <= w * scale - rw <= 15 + half and -half <= h * scale - rh <= 15 + half, (w, h)
            _, _, cw, ch = crop_box(w, h, rw, rh)
            assert cw >= rw and ch >= rh, (w, h)  # the resize to rw×rh never enlarges the crop
