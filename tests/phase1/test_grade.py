"""Tests of the grade and the still-image export pipeline: lut.apply, scripts/influencer/grade.py and
scripts/influencer/export/{icc,xmp,images}.py (task 22; design §A9, §C8; Req 13, 14.1–14.8).

- Property 32: an identity LUT at grain 0.0 moves no channel by more than 1
- Property 33: more grain gives a larger standard deviation on mid-grey
- Property 34: two Slides, or two Reel frames, get different grain fields
- Property 35: exporting the same source twice gives byte-identical JPEG files
- the ICC profile, the XMP packet and its APP1 segment, source metadata left behind, EXIF orientation, the
  target sizes, the upscale flag, and the validation gate for a rejected LUT or grain strength
"""
from __future__ import annotations

import dataclasses
import io
import struct
import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from hypothesis.extra.numpy import arrays
from PIL import Image, ImageCms, PngImagePlugin

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import grade as G  # noqa: E402
from influencer import lut as L  # noqa: E402
from influencer.calendar_io import Post  # noqa: E402
from influencer.errors import ValidationError  # noqa: E402
from influencer.export import icc, images, xmp  # noqa: E402
from influencer.persona import Grade, Persona  # noqa: E402
from strategies_calendar import weeks  # noqa: E402
from strategies_ids import post_ids  # noqa: E402
from strategies_luts import luts  # noqa: E402

WEEK = "2026-W10"
# numpy and Pillow work in every example; the example count comes from the profile (100, or 200 in `check`)
IMAGE_SETTINGS = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])


def identity_lut(size: int, jitter_seed: int | None = None) -> L.Lut:
    """The identity Lut of size N, each entry moved by at most 1e-6 when jitter_seed is given."""
    axis = np.linspace(0.0, 1.0, size)
    b, g, r = np.meshgrid(axis, axis, axis, indexing="ij")  # red changes fastest in the flattened order
    table = np.stack([r, g, b], axis=-1).reshape(-1, 3)
    if jitter_seed is not None:
        table = table + np.random.default_rng(jitter_seed).uniform(-1e-6, 1e-6, table.shape)
    return L.Lut(None, size, L.DEFAULT_DOMAIN_MIN, L.DEFAULT_DOMAIN_MAX, tuple(map(tuple, table.tolist())))


def grading(lut: L.Lut | None = None, strength: float = 0.0) -> G.Grading:
    return G.Grading(lut if lut is not None else identity_lut(2), "<test>", strength)


def post(kind: str = "single", aspect: str | None = None, ident: str = "p1") -> Post:
    return Post(id=ident, type=kind, date="2026-03-02", aspect=aspect, location="x", outfit="y", slides=3)


def persona(lut: str = "luts/look.cube", grain: object = 0.3) -> Persona:
    return Persona(slug="mia", name="Mia", age=28, voice="warm", identity="Mia has dark hair.",
                   trigger_word="mia_tw", hero_image="hero.png", grade=Grade(lut, grain))  # type: ignore[arg-type]  # fmt: skip


def save_png(path: Path, pixels: np.ndarray, **info: str) -> Path:
    meta = PngImagePlugin.PngInfo()
    for key, value in info.items():
        meta.add_text(key, value)
    Image.fromarray(pixels).save(path, "PNG", pnginfo=meta)
    return path


def segments(data: bytes) -> list[tuple[int, bytes]]:
    """The (marker, payload) of each JPEG segment from SOI up to SOS."""
    assert data[:2] == b"\xff\xd8"
    found, at = [], 2
    while True:
        marker = data[at + 1]
        (length,) = struct.unpack(">H", data[at + 2 : at + 4])
        found.append((marker, data[at + 4 : at + 2 + length]))
        if marker == 0xDA:
            return found
        at += 2 + length


# ---- lut.apply and grade.grain ----


def test_apply_interpolates_trilinearly_between_corners():
    # a size-2 LUT that maps every channel c to 1 - c: the result is the exact inverse
    table = tuple((1.0 - r, 1.0 - g, 1.0 - b) for b in (0, 1) for g in (0, 1) for r in (0, 1))
    inverse = L.Lut(None, 2, L.DEFAULT_DOMAIN_MIN, L.DEFAULT_DOMAIN_MAX, table)
    pixels = np.array([[[0, 128, 255], [10, 20, 30]]], dtype=np.uint8)
    assert (L.apply(inverse, pixels) == 255 - pixels).all()


def test_apply_uses_the_domain_clamps_and_never_writes_its_input():
    lut = dataclasses.replace(identity_lut(3), domain_min=(0.5, 0.0, 0.0), domain_max=(1.0, 1.0, 2.0))
    pixels = np.array([[[0, 255, 255], [191, 0, 102]]], dtype=np.uint8)
    before = pixels.copy()
    out = L.apply(lut, pixels)
    assert (pixels == before).all()
    assert out.tolist() == [[[0, 255, 128], [127, 0, 51]]]
    # outputs beyond [0, 1], and tables of huge finite values, clamp without NaN
    big = L.Lut(None, 2, (-1e308,) * 3, (1e308,) * 3, ((1.7e308, -1.7e308, 2.0),) * 8)
    assert L.apply(big, pixels).tolist() == [[[255, 0, 255]] * 2]


def test_apply_rejects_other_arrays():
    with pytest.raises(ValueError, match="uint8"):
        L.apply(identity_lut(2), np.zeros((2, 2, 3), dtype=np.float64))
    with pytest.raises(ValueError, match="H×W×3"):
        L.apply(identity_lut(2), np.zeros((2, 2), dtype=np.uint8))


def test_grain_is_seeded_by_the_ids_alone_and_skipped_at_zero():
    grey = np.full((8, 8, 3), 128, dtype=np.uint8)
    once = G.grain(grey, 0.5, WEEK, "p1", 2)
    assert (once == G.grain(grey, 0.5, WEEK, "p1", 2)).all()
    assert (once[..., 0] == once[..., 1]).all() and (once[..., 1] == once[..., 2]).all()  # luma grain
    assert (grey == 128).all()
    zero = G.grain(grey, 0.0, WEEK, "p1", 2)
    assert zero is not grey and (zero == grey).all()
    assert G.grain_seed(WEEK, "p1", 2) != G.grain_seed(WEEK, "p2", 2) != G.grain_seed("2026-W11", "p2", 2)
    for bad in (-0.1, 1.1, "0.5", True, float("nan")):
        with pytest.raises(ValueError, match="grain strength"):
            G.grain(grey, bad, WEEK, "p1", 1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        G.grain_seed(WEEK, "other", 1)


# Feature: ai-influencer-pipeline, Property 32: Identity LUT leaves pixels unchanged
@IMAGE_SETTINGS
@given(
    size=st.integers(2, 65),
    jitter=st.integers(0, 2**32 - 1),
    pixels=st.tuples(st.integers(1, 64), st.integers(1, 64)).flatmap(
        lambda hw: arrays(np.uint8, (hw[0], hw[1], 3))
    ),
)
def test_identity_lut_leaves_pixels_unchanged(size, jitter, pixels):
    """**Validates: Requirements 14.5**"""
    out = G.grain(L.apply(identity_lut(size, jitter), pixels), 0.0, WEEK, "p1", 1)
    assert out.shape == pixels.shape
    assert int(np.abs(out.astype(np.int16) - pixels.astype(np.int16)).max()) <= 1


@st.composite
def strength_pairs(draw: st.DrawFn) -> tuple[float, float]:
    low = draw(st.floats(0.0, 0.99))
    high = draw(st.floats(min(1.0, low + 0.01), 1.0))
    return low, high


# Feature: ai-influencer-pipeline, Property 33: More grain gives more variance
@IMAGE_SETTINGS
@given(pair=strength_pairs(), week=weeks(), post_id=post_ids(), slide=st.integers(1, 20))
def test_more_grain_gives_more_variance(pair, week, post_id, slide):
    """**Validates: Requirements 14.2**"""
    s1, s2 = pair
    assert s2 - s1 >= 0.01 - 1e-12
    grey = L.apply(identity_lut(2), np.full((128, 128, 3), 128, dtype=np.uint8))
    low = G.grain(grey, s1, week, post_id, slide).astype(np.float64)
    high = G.grain(grey, s2, week, post_id, slide).astype(np.float64)
    assert high.std() > low.std()
    assert (G.grain(grey, 0.0, week, post_id, slide) == 128).all()


# Feature: ai-influencer-pipeline, Property 34: Grain patterns differ between Slides and between frames
@settings(deadline=None)
@given(
    week=weeks(),
    post_id=post_ids(),
    pair=st.one_of(
        st.lists(st.integers(1, 20), min_size=2, max_size=2, unique=True),
        st.lists(st.integers(1, 400), min_size=2, max_size=2, unique=True),
    ),
)
def test_grain_patterns_differ_between_slides_and_frames(week, post_id, pair):
    """**Validates: Requirements 14.3**"""
    a, b = pair
    assert not np.array_equal(G.grain_field(week, post_id, a, 64, 64), G.grain_field(week, post_id, b, 64, 64))


@st.composite
def sources(draw: st.DrawFn) -> tuple[np.ndarray, str]:
    """A small random source image (at most 256 px a side) and the mode it is saved in."""
    w, h = draw(st.integers(1, 256)), draw(st.integers(1, 256))
    pixels = np.random.default_rng(draw(st.integers(0, 2**32 - 1))).integers(0, 256, (h, w, 3), dtype=np.uint8)
    return pixels, draw(st.sampled_from(("RGB", "RGBA", "L", "P")))


# Feature: ai-influencer-pipeline, Property 35: Re-exports are byte-identical
@IMAGE_SETTINGS
@given(
    source=sources(),
    kind=st.sampled_from(("single", "carousel", "story")),
    aspect=st.sampled_from(("3:4", "4:5")),
    week=weeks(),
    post_id=post_ids(),
    slide=st.integers(1, 20),
    lut=luts(),
    strength=st.floats(0.0, 1.0),
)
def test_re_exports_are_byte_identical(source, kind, aspect, week, post_id, slide, lut, strength):
    """**Validates: Requirements 14.4**

    The export runs at 1/40 of target_size(post), 27 px wide, so each example stays fast; the full-size
    export is pinned by test_a_full_size_re_export_is_byte_identical."""
    pixels, mode = source
    tw, th = images.target_size(post(kind, aspect if kind != "story" else None, post_id))
    size = (tw // 40, th // 40)
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        path = root / "source.png"
        Image.fromarray(pixels).convert(mode).save(path, "PNG")
        target = root / week / post_id / f"{post_id}_{slide:02d}.jpg"
        target.parent.mkdir(parents=True)
        written = []
        for _ in range(2):
            target.write_bytes(images.render(path, size, grading(lut, strength), week, post_id, slide).data)
            written.append(target.read_bytes())
        assert written[0] == written[1]
        assert Image.open(target).size == size


# ---- the ICC profile ----


def test_the_profile_opens_in_imagecms_and_round_trips_mid_grey():
    profile = ImageCms.ImageCmsProfile(io.BytesIO(icc.SRGB_V2))
    assert ImageCms.getProfileDescription(profile).strip() == icc.DESCRIPTION
    srgb = ImageCms.createProfile("sRGB")
    grey = Image.new("RGB", (4, 4), (128, 128, 128))
    there = ImageCms.applyTransform(grey, ImageCms.buildTransform(profile, srgb, "RGB", "RGB"))
    back = ImageCms.applyTransform(there, ImageCms.buildTransform(srgb, profile, "RGB", "RGB"))
    for image in (there, back):
        assert all(abs(channel - 128) <= 1 for channel in image.getpixel((0, 0)))


def test_the_profile_is_fixed_and_holds_no_date_or_id():
    data = icc.build()
    assert data == icc.build() == icc.SRGB_V2
    assert struct.unpack(">I", data[:4])[0] == len(data)
    assert data[8:12] == b"\x02\x10\x00\x00" and data[12:24] == b"mntrRGB XYZ " and data[36:40] == b"acsp"
    assert data[24:36] == bytes(12)  # no creation date
    assert data[84:100] == bytes(16)  # no profile ID
    (count,) = struct.unpack(">I", data[128:132])
    tags = {data[132 + 12 * i : 136 + 12 * i] for i in range(count)}
    assert tags == {b"desc", b"cprt", b"wtpt", b"rXYZ", b"gXYZ", b"bXYZ", b"rTRC", b"gTRC", b"bTRC"}


# ---- the XMP packet ----


def test_insert_puts_one_app1_after_app0_and_reports_bad_bytes():
    jpeg = b"\xff\xd8\xff\xe0\x00\x04ab\xff\xd9"
    out = xmp.insert(jpeg, b"<x/>")
    assert out == b"\xff\xd8\xff\xe0\x00\x04ab\xff\xe1" + struct.pack(">H", 2 + 29 + 4) + xmp.XMP_NAMESPACE + b"<x/>\xff\xd9"
    for bad in (b"", b"\xff\xd8\xff\xe1\x00\x02", b"\x89PNG\r\n", b"\xff\xd8\xff\xe0\x00\x40ab"):
        with pytest.raises(ValueError):
            xmp.insert(bad, b"<x/>")
    with pytest.raises(ValueError, match="65533"):
        xmp.insert(jpeg, b"x" * (xmp.MAX_PAYLOAD - len(xmp.XMP_NAMESPACE) + 1))
    assert len(xmp.segment(b"x" * (xmp.MAX_PAYLOAD - len(xmp.XMP_NAMESPACE)))) == 65537
    assert xmp.TRAINED_ALGORITHMIC_MEDIA.encode() in xmp.DIGITAL_SOURCE_TYPE_PACKET
    assert b"Iptc4xmpExt:DigitalSourceType=" in xmp.DIGITAL_SOURCE_TYPE_PACKET


# ---- the pipeline ----


def test_the_export_carries_the_packet_and_none_of_the_source_metadata(tmp_path):
    pixels = np.random.default_rng(7).integers(0, 256, (60, 45, 3), dtype=np.uint8)
    png = save_png(tmp_path / "a.png", pixels, prompt='{"COMFY-PROMPT-7f3a": 1}', workflow='{"COMFY-WORKFLOW-9c1b": 2}')
    exif = Image.Exif()
    exif[0x010E] = "SOURCE-EXIF-MARKER-5d2e"  # ImageDescription
    jpg = tmp_path / "b.jpg"
    Image.fromarray(pixels).save(jpg, "JPEG", exif=exif.tobytes(), comment=b"SOURCE-COMMENT-4411")
    assert b"SOURCE-EXIF-MARKER-5d2e" in jpg.read_bytes()
    for source in (png, jpg):
        data = images.render(source, (27, 36), grading(strength=0.4), WEEK, "p1", 1).data
        found = segments(data)
        assert [marker for marker, _ in found[:2]] == [0xE0, 0xE1]
        assert [marker for marker, _ in found].count(0xE1) == 1
        assert found[1][1] == xmp.XMP_NAMESPACE + xmp.DIGITAL_SOURCE_TYPE_PACKET
        assert 0xFE not in [marker for marker, _ in found]  # no comment
        for marker in (b"COMFY-PROMPT-7f3a", b"COMFY-WORKFLOW-9c1b", b"SOURCE-EXIF-MARKER-5d2e",
                       b"SOURCE-COMMENT-4411", b"Exif\x00\x00", b"workflow"):  # fmt: skip
            assert marker not in data
        opened = Image.open(io.BytesIO(data))
        assert opened.info["xmp"] == xmp.DIGITAL_SOURCE_TYPE_PACKET
        assert opened.info["icc_profile"] == icc.SRGB_V2
        assert "prompt" not in opened.info and "workflow" not in opened.info and "exif" not in opened.info


def test_exif_orientation_is_applied_before_the_crop(tmp_path):
    stored = np.zeros((20, 40, 3), dtype=np.uint8)
    stored[:, :20] = (255, 0, 0)  # left half red
    stored[:, 20:] = (0, 0, 255)  # right half blue
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise to display: the stored left half becomes the top
    path = tmp_path / "rotated.jpg"
    Image.fromarray(stored).save(path, "JPEG", quality=100, exif=exif.tobytes())
    shown = np.array(Image.open(io.BytesIO(images.render(path, (27, 36), grading(), WEEK, "p1", 1).data)))
    assert shown.shape == (36, 27, 3)
    top, bottom = shown[3, 13].astype(int), shown[-4, 13].astype(int)
    assert top[0] > 200 and top[2] < 60, top
    assert bottom[2] > 200 and bottom[0] < 60, bottom


def test_target_sizes():
    assert images.target_size(post("single", "4:5")) == images.target_size(post("carousel", "4:5")) == (1080, 1350)
    assert images.target_size(post("single")) == images.target_size(post("carousel", "3:4")) == (1080, 1440)
    assert images.target_size(post("story")) == (1080, 1920)


@pytest.mark.parametrize(
    ("kind", "aspect", "source", "size", "upscaled"),
    [
        ("single", None, (900, 1200), (1080, 1440), True),
        ("carousel", None, (2160, 2880), (1080, 1440), False),
        ("carousel", "4:5", (2160, 2880), (1080, 1350), False),
        ("story", None, (1080, 1920), (1080, 1920), False),
        ("story", None, (2160, 2880), (1080, 1920), False),
    ],
)
def test_full_size_exports_and_the_upscale_flag(tmp_path, kind, aspect, source, size, upscaled):
    path = save_png(tmp_path / "s.png", np.full((source[1], source[0], 3), 90, dtype=np.uint8))
    exported = images.export_slide(path, post(kind, aspect), WEEK, 1, grading(identity_lut(17), 0.2))
    assert Image.open(io.BytesIO(exported.data)).size == size == exported.size
    assert exported.upscaled is upscaled
    assert exported.flags == (("upscaled at export",) if upscaled else ())


def test_a_full_size_re_export_is_byte_identical(tmp_path):
    pixels = np.random.default_rng(3).integers(0, 256, (300, 200, 3), dtype=np.uint8)
    path = save_png(tmp_path / "s.png", pixels)
    grade_ = grading(identity_lut(33, 5), 0.35)
    first = images.export_slide(path, post("carousel"), WEEK, 2, grade_).data
    assert first == images.export_slide(path, post("carousel"), WEEK, 2, grade_).data
    assert first != images.export_slide(path, post("carousel"), WEEK, 3, grade_).data  # another Slide's grain
    assert path.read_bytes() == save_png(tmp_path / "t.png", pixels).read_bytes()  # the source is unchanged


# ---- the validation gate ----


def write_lut(root: Path, text: str) -> None:
    (root / "luts").mkdir(parents=True, exist_ok=True)
    (root / "luts" / "look.cube").write_text(text, encoding="utf-8")


def test_load_grade_accepts_a_valid_grade(tmp_path):
    write_lut(tmp_path, L.print_lut(identity_lut(2)))
    loaded = G.load_grade(persona(grain=0.3), tmp_path)
    assert loaded.lut == identity_lut(2) and loaded.strength == 0.3
    assert loaded.lut_path == str(tmp_path / "luts" / "look.cube")
    assert G.load_grade(persona(grain=1), tmp_path).strength == 1.0


@pytest.mark.parametrize(
    ("lut_text", "grain", "expected"),
    [
        ("LUT_3D_SIZE 2\n0 0 0\n1 0 0\n", 0.3, [("look.cube", None, None, "expected 8 entries")]),
        (None, 0.3, [("look.cube", None, None, "missing")]),
        ("ok", -0.1, [("persona.yaml", "grade.grain", None, "out of range")]),
        ("ok", 1.1, [("persona.yaml", "grade.grain", None, "out of range")]),
        ("ok", "0.5", [("persona.yaml", "grade.grain", None, "not a number")]),
        ("ok", None, [("persona.yaml", "grade.grain", None, "is missing")]),
        ("LUT_1D_SIZE 4\nLUT_3D_SIZE 2\n" + "0 0 0\n" * 8, "x",
         [("persona.yaml", "grade.grain", None, "not a number"), ("look.cube", "LUT_1D_SIZE", 1, "1D LUT")]),
    ],
    ids=["count", "missing-file", "grain-negative", "grain-above-1", "grain-text", "grain-missing", "both"],
)  # fmt: skip
def test_a_rejected_grade_names_the_path_or_field_and_writes_nothing(tmp_path, lut_text, grain, expected):
    root = tmp_path / "root"
    output_root = tmp_path / "output"
    output_root.mkdir()
    root.mkdir()
    if lut_text is not None:
        write_lut(root, L.print_lut(identity_lut(2)) if lut_text == "ok" else lut_text)
    with pytest.raises(ValidationError) as caught:
        G.load_grade(persona(grain=grain), root)
    problems = caught.value.problems
    assert len(problems) == len(expected)
    for problem, (path_end, field, line, phrase) in zip(problems, expected):
        assert problem.path.endswith(path_end) and problem.field == field and problem.line == line
        assert phrase in problem.reason
    if expected[0][3] == "expected 8 entries":
        assert "found 2" in problems[0].reason
    assert list(output_root.iterdir()) == []


def test_a_lut_path_outside_the_root_is_rejected(tmp_path):
    with pytest.raises(ValidationError) as caught:
        G.load_grade(persona(lut="../look.cube"), tmp_path)
    [problem] = caught.value.problems
    assert problem.field == "grade.lut" and ".." in problem.reason
