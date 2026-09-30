"""Generates the Repo's two LUTs (task 14.4; design §M8; Req 14.10).

    python tests/tools/make_luts.py [--out DIR]

writes into DIR (default: config/influencer/luts):

- identity-33.cube: N = 33, the entry at red, green and blue lattice index (i, j, k) is (i, j, k)/32
- example-grade-33.cube: a warm, lifted-black film curve

Each file starts with the origin and licence comment, then a comment saying what it holds, then the
LUT_Printer's text. Every run on every machine writes the same bytes: the grade is computed in integer
arithmetic and rounded to a multiple of 1/1024, and such a number, like every i/32, is a float that '.17g'
prints exactly, so no platform's float rounding reaches the files.

The grade, per channel: y = black + (white − black)·s(x) at x = i/32, with the gentle S-curve
s(x) = 0.7·x + 0.3·(3x² − 2x³), which is 0 at 0, 1 at 1 and rises in between. The blacks are lifted and the
highlights rolled off, both warmer in red than in blue. Then a film-like 10 % desaturation towards
Rec. 709 luma: out = 0.9·y + 0.1·(0.2126·r + 0.7152·g + 0.0722·b), rounded half up to a multiple of
1/1024. Every entry lies between 0.04 and 0.99, and the black entry (0, 0, 0) is lifted to about 0.06.
"""
from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from influencer.atomic import write_atomic  # noqa: E402
from influencer.lut import Lut, print_lut  # noqa: E402

LUT_DIR = REPO / "config" / "influencer" / "luts"
ORIGIN = "# origin: generated for comfyui-vastai by tests/tools/make_luts.py; licence: CC0-1.0"
IDENTITY_NAME = "identity-33.cube"
GRADE_NAME = "example-grade-33.cube"
SIZE = 33
LAST = SIZE - 1

# s(i/32) = S(i) / CURVE_DENOMINATOR, with S(i) = 7168·i + 288·i² − 6·i³ (s = 0.7·x + 0.9·x² − 0.6·x³).
CURVE_DENOMINATOR = 10 * 32**3
BLACK = (62, 52, 44)  # the lifted blacks, red, green and blue, in thousandths
WHITE = (985, 962, 925)  # the rolled-off highlights, in thousandths
LUMA = (2126, 7152, 722)  # Rec. 709 luma weights, in ten-thousandths
KEEP = 9  # tenths of the channel kept by the desaturation; the rest is luma
QUANTUM = 1024  # every grade entry is a multiple of 1/QUANTUM


def lattice(size: int) -> Iterator[tuple[int, int, int]]:
    """Every (i, j, k) lattice index in `.cube` order: red changes fastest, then green, then blue."""
    for k in range(size):
        for j in range(size):
            for i in range(size):
                yield i, j, k


def identity() -> Lut:
    table = tuple((i / LAST, j / LAST, k / LAST) for i, j, k in lattice(SIZE))
    return Lut("comfyui-vastai identity 33", SIZE, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), table)


def _curve(index: int) -> int:
    return 7168 * index + 288 * index**2 - 6 * index**3


def example_grade() -> Lut:
    # y[c][n]: channel c of the film curve at lattice index n, over CURVE_DENOMINATOR·1000.
    y = [
        [BLACK[c] * CURVE_DENOMINATOR + (WHITE[c] - BLACK[c]) * _curve(n) for n in range(SIZE)] for c in range(3)
    ]
    # out = (KEEP·10000·y_c + luma) / (10·10000·CURVE_DENOMINATOR·1000), with luma = Σ LUMA_c·y_c
    denominator = 10 * 10000 * CURVE_DENOMINATOR * 1000
    keep = KEEP * 10000

    def quantised(numerator: int) -> float:  # numerator/denominator rounded half up to a multiple of 1/QUANTUM
        return ((2 * numerator * QUANTUM + denominator) // (2 * denominator)) / QUANTUM

    table = []
    for i, j, k in lattice(SIZE):
        red, green, blue = y[0][i], y[1][j], y[2][k]
        luma = LUMA[0] * red + LUMA[1] * green + LUMA[2] * blue
        table.append((quantised(keep * red + luma), quantised(keep * green + luma), quantised(keep * blue + luma)))
    return Lut("comfyui-vastai example grade 33", SIZE, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), tuple(table))


def cube_files() -> dict[str, str]:
    """The name and full text of each file."""
    return {
        IDENTITY_NAME: "\n".join(
            (ORIGIN, "# identity: the entry at red, green and blue lattice index (i, j, k) is (i, j, k)/32", "")
        )
        + print_lut(identity()),
        GRADE_NAME: "\n".join(
            (
                ORIGIN,
                "# example grade: a warm, lifted-black film curve with a 10 % desaturation, each entry a "
                "multiple of 1/1024 (see tests/tools/make_luts.py)",
                "",
            )
        )
        + print_lut(example_grade()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write identity-33.cube and example-grade-33.cube.")
    parser.add_argument("--out", type=Path, default=LUT_DIR, help=f"the folder to write to (default: {LUT_DIR})")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in cube_files().items():
        write_atomic(args.out / name, text)
        print(f"wrote {args.out / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
