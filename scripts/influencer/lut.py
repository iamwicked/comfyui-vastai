"""The LUT model, the LUT_Reader and the LUT_Printer for 3D `.cube` files (design §M8; Req 14.7–14.9).

A file is read as UTF-8 (a leading BOM is dropped) and split into lines on \\n only. Blanks are the ASCII
space, tab, \\r, \\f and \\v, so a CRLF file reads like an LF one. The grammar (Req 14.7):

- blank lines, and lines whose first non-blank character is `#`, anywhere
- keyword lines, before the first entry line, each at most once and in any order: `TITLE "…"`,
  `LUT_3D_SIZE N` with N a whole number from 2 to 65 (required), and `DOMAIN_MIN r g b` and
  `DOMAIN_MAX r g b` with 3 finite numbers each (the default domain is 0.0 to 1.0 in each channel)
- then N³ entry lines of exactly 3 finite numbers in `.cube` order: red changes fastest, then green, then
  blue

A number is a plain decimal such as `1`, `-0.5`, `.5`, `2.` or `1e-3`; one that overflows to infinity is not
finite. A TITLE's text runs from the first to the last `"` of its line, so it may itself contain `"`, and it
never contains \\n.

The LUT_Reader reports every fault of a file together, in one ValidationError with one Problem per fault,
each naming the file (Req 14.8):

- a `LUT_1D_SIZE` line (1D LUTs are not read), a keyword line after the first entry line and a malformed
  keyword line: on that line
- a missing LUT_3D_SIZE on no line, a repeated keyword on the line of the repeat, an N outside 2 to 65 on
  the LUT_3D_SIZE line
- a DOMAIN_MIN value not below the matching DOMAIN_MAX value: on the DOMAIN_MIN line, or on the DOMAIN_MAX
  line when the file has no DOMAIN_MIN, naming every such channel
- an entry line that is not exactly 3 finite numbers: on that line, as field `entry <n>`
- an entry count other than N³, with the expected and the actual count: on the line of the first entry
  beyond N³ when there are too many, on no line when there are too few. Every entry line counts, a
  malformed one included, so one bad entry line is one fault.

The LUT_Printer writes TITLE (when set), LUT_3D_SIZE, DOMAIN_MIN and DOMAIN_MAX, then the entries, with every
number formatted '.17g', so the LUT_Reader reads back an equal Lut (Req 14.9).

apply, the trilinear grading of §A9 (Req 14.1, 14.5), maps an 8-bit RGB image through a Lut for the
Export_Tool.

Standard library only, except apply, which imports NumPy when it is called, so the LUT_Reader stays usable
without it. Only load_lut reads a file; nothing here writes one.
"""
from __future__ import annotations

import codecs
import math
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from itertools import chain
from pathlib import Path

from influencer.errors import Problem, ValidationError

Rgb = tuple[float, float, float]

MIN_SIZE = 2
MAX_SIZE = 65
DEFAULT_DOMAIN_MIN: Rgb = (0.0, 0.0, 0.0)
DEFAULT_DOMAIN_MAX: Rgb = (1.0, 1.0, 1.0)

TITLE = "TITLE"
LUT_3D_SIZE = "LUT_3D_SIZE"
LUT_1D_SIZE = "LUT_1D_SIZE"
DOMAIN_MIN = "DOMAIN_MIN"
DOMAIN_MAX = "DOMAIN_MAX"
KEYWORDS = frozenset({TITLE, LUT_3D_SIZE, DOMAIN_MIN, DOMAIN_MAX})
CHANNELS = ("red", "green", "blue")

_BLANKS = " \t\r\f\v"
_NUMBER = r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
_THREE_NUMBERS = re.compile(rf"({_NUMBER})[ \t\r\f\v]+({_NUMBER})[ \t\r\f\v]+({_NUMBER})")
_WHOLE_NUMBER = re.compile(r"[+-]?[0-9]+")
_BLANK_RUN = re.compile(r"[ \t\r\f\v]+")
_ENTRY_START = frozenset("0123456789+-.")  # no keyword starts with these, so such a line is an entry line
_QUOTED = 60  # the characters of a faulty line that a Problem quotes
_REPR_ENTRIES = 1000  # a larger table is summarised in repr(), so a failing test prints a readable Lut


def _rgb(name: str, value: Iterable[float]) -> Rgb:
    triple = tuple(value)
    if len(triple) != 3 or not all(map(math.isfinite, triple)):
        raise ValueError(f"{name} must be 3 finite numbers, not {value!r}")
    return (triple[0], triple[1], triple[2])


@dataclass(frozen=True, repr=False)
class Lut:
    """A 3D LUT (§M8): a size N from 2 to 65, a domain with each domain_min value below its domain_max
    value, and N³ (r, g, b) entries of finite numbers in `.cube` order, so the entry for the red, green and
    blue lattice index (i, j, k) is table[i + N·j + N²·k]. title is None for a file without TITLE.

    The constructor checks all of this and raises ValueError, so every Lut prints to a file that the
    LUT_Reader reads back as an equal Lut.
    """

    title: str | None
    size: int
    domain_min: Rgb
    domain_max: Rgb
    table: tuple[Rgb, ...]

    def __post_init__(self) -> None:
        if self.title is not None and (not isinstance(self.title, str) or "\n" in self.title):
            raise ValueError(f"title must be None or text without a \\n, not {self.title!r}")
        size = self.size
        if isinstance(size, bool) or not isinstance(size, int) or not MIN_SIZE <= size <= MAX_SIZE:
            raise ValueError(f"size must be a whole number from {MIN_SIZE} to {MAX_SIZE}, not {size!r}")
        domain_min, domain_max = _rgb("domain_min", self.domain_min), _rgb("domain_max", self.domain_max)
        low = [CHANNELS[c] for c in range(3) if not domain_min[c] < domain_max[c]]
        if low:
            raise ValueError(f"domain_min is not below domain_max in {', '.join(low)}")
        table = tuple(map(tuple, self.table))
        if len(table) != size**3:
            raise ValueError(f"a size {size} LUT has {size**3} entries, not {len(table)}")
        if any(len(entry) != 3 for entry in table) or not all(map(math.isfinite, chain.from_iterable(table))):
            raise ValueError("every entry must be 3 finite numbers")
        object.__setattr__(self, "domain_min", domain_min)
        object.__setattr__(self, "domain_max", domain_max)
        object.__setattr__(self, "table", table)

    def __repr__(self) -> str:
        table = self.table
        if len(table) > _REPR_ENTRIES:
            shown = f"<{len(table)} entries: {table[0]!r}, {table[1]!r}, …, {table[-1]!r}>"
        else:
            shown = repr(table)
        return (
            f"Lut(title={self.title!r}, size={self.size!r}, domain_min={self.domain_min!r}, "
            f"domain_max={self.domain_max!r}, table={shown})"
        )


# ---- the LUT_Reader ----


def _quote(text: str) -> str:
    return repr(text[:_QUOTED]) + ("…" if len(text) > _QUOTED else "")


def _three_finite(text: str) -> Rgb | None:
    match = _THREE_NUMBERS.fullmatch(text)
    if match is None:
        return None
    triple = (float(match[1]), float(match[2]), float(match[3]))
    return triple if all(map(math.isfinite, triple)) else None


class _Header:
    """The keyword lines of one file as they are read: each keyword's first line, the values of the
    well-formed ones, and the Problems found so far."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.problems: list[Problem] = []
        self.first: dict[str, int] = {}  # keyword -> the line it first appears on
        self.title: str | None = None
        self.size: int | None = None
        self.domain: dict[str, Rgb] = {}  # DOMAIN_MIN, DOMAIN_MAX -> the values of a well-formed line
        self.first_entry = 0  # the line of the first entry line; 0 until one is read

    def problem(self, field: str | None, line: int | None, reason: str) -> None:
        self.problems.append(Problem(self.path, field, line, reason))

    def keyword(self, keyword: str, rest: str, number: int) -> None:
        """One keyword line: rest is the text after the keyword and its blanks."""
        if keyword in self.first:
            self.problem(keyword, number, f"repeated: the first {keyword} line is line {self.first[keyword]}")
            return
        self.first[keyword] = number
        if self.first_entry:
            reason = f"comes after the first entry, on line {self.first_entry}: keyword lines go before the entries"
            self.problem(keyword, number, reason)  # and its value is not read
        elif keyword == TITLE:
            if len(rest) >= 2 and rest[0] == '"' and rest[-1] == '"':
                self.title = rest[1:-1]
            else:
                self.problem(TITLE, number, 'needs its text in double quotes, as in TITLE "Warm grade"')
        elif keyword == LUT_3D_SIZE:
            self._size(rest, number)
        else:  # DOMAIN_MIN, DOMAIN_MAX
            triple = _three_finite(rest)
            if triple is None:
                self.problem(keyword, number, f"needs 3 finite numbers, not {_quote(rest)}")
            else:
                self.domain[keyword] = triple

    def _size(self, rest: str, number: int) -> None:
        if not _WHOLE_NUMBER.fullmatch(rest):
            reason = f"needs one whole number from {MIN_SIZE} to {MAX_SIZE}, not {_quote(rest)}"
            self.problem(LUT_3D_SIZE, number, reason)
            return
        digits = rest.lstrip("+-").lstrip("0")
        size = int(rest) if len(digits) <= 4 else None  # a longer one is far outside, and int() may refuse it
        if size is None or not MIN_SIZE <= size <= MAX_SIZE:
            shown = rest if len(rest) <= _QUOTED else rest[:_QUOTED] + "…"
            self.problem(LUT_3D_SIZE, number, f"{shown} is outside {MIN_SIZE} to {MAX_SIZE}")
        else:
            self.size = size

    def check_domain(self) -> None:
        """DOMAIN_MIN below DOMAIN_MAX in every channel, the defaults standing in for a missing line. Skipped
        when either line is malformed or misplaced, which is reported already."""
        if any(k in self.first and k not in self.domain for k in (DOMAIN_MIN, DOMAIN_MAX)):
            return
        low = self.domain.get(DOMAIN_MIN, DEFAULT_DOMAIN_MIN)
        high = self.domain.get(DOMAIN_MAX, DEFAULT_DOMAIN_MAX)
        bad = [c for c in range(3) if not low[c] < high[c]]
        if not bad:
            return
        detail = "; ".join(f"{CHANNELS[c]}: {low[c]!r} >= {high[c]!r}" for c in bad)
        if DOMAIN_MIN not in self.first:
            reason = f"the default DOMAIN_MIN is not below it in {detail}"
            self.problem(DOMAIN_MAX, self.first[DOMAIN_MAX], reason)
            return
        other = "the default DOMAIN_MAX"
        if DOMAIN_MAX in self.first:
            other = f"DOMAIN_MAX (line {self.first[DOMAIN_MAX]})"
        self.problem(DOMAIN_MIN, self.first[DOMAIN_MIN], f"not below {other} in {detail}")


def read_lut(text: str, *, path: str = "<lut>") -> Lut:
    """The LUT_Reader: the Lut in text, the content of the `.cube` file named path (path only names the file
    in Problems). Raises ValidationError with every fault the module docstring lists, in line order and then
    the faults on no line."""
    header = _Header(path)
    table: list[Rgb] = []
    count = 0  # entry lines, well-formed or not
    surplus_count = 0  # N³ + 1 once the entries start after a valid N: the first entry beyond the table
    surplus_line: int | None = None
    isfinite = math.isfinite
    lines = text.split("\n")
    if lines[-1] == "":  # a final \n ends the last line and starts no new one
        lines.pop()
    for number, raw in enumerate(lines, 1):
        line = raw.strip(_BLANKS)
        if not line:
            continue
        if line[0] not in _ENTRY_START:
            if line[0] == "#":
                continue
            keyword, _, rest = _BLANK_RUN.sub(" ", line, count=1).partition(" ")
            if keyword == LUT_1D_SIZE:
                reason = "a 1D LUT line: the LUT_Reader reads only 3D LUTs, sized by LUT_3D_SIZE"
                header.problem(LUT_1D_SIZE, number, reason)
                continue
            if keyword in KEYWORDS:
                header.keyword(keyword, rest, number)
                continue
        count += 1  # an entry line
        if not header.first_entry:
            header.first_entry = number
            surplus_count = header.size**3 + 1 if header.size is not None else 0
        if count == surplus_count:
            surplus_line = number
        match = _THREE_NUMBERS.fullmatch(line)
        if match is not None:
            entry = (float(match[1]), float(match[2]), float(match[3]))
            if isfinite(entry[0]) and isfinite(entry[1]) and isfinite(entry[2]):
                table.append(entry)
                continue
        header.problem(f"entry {count}", number, f"{_quote(line)} is not 3 finite numbers")
    header.check_domain()
    if LUT_3D_SIZE not in header.first:
        header.problem(LUT_3D_SIZE, None, "missing: a 3D LUT needs one LUT_3D_SIZE line")
    size = header.size
    if size is not None and count != size**3:
        reason = f"expected {size**3} entries ({size}³ for LUT_3D_SIZE {size}), found {count}"
        header.problem(None, surplus_line, reason)
    if header.problems or size is None:
        # sorted stably: the line faults in line order, then the faults on no line in the order found
        raise ValidationError(sorted(header.problems, key=lambda p: (p.line is None, p.line or 0)))
    return Lut(
        header.title,
        size,
        header.domain.get(DOMAIN_MIN, DEFAULT_DOMAIN_MIN),
        header.domain.get(DOMAIN_MAX, DEFAULT_DOMAIN_MAX),
        tuple(table),
    )


def load_lut(path: str | os.PathLike[str]) -> Lut:
    """The LUT_Reader on a file: its UTF-8 text (a leading BOM dropped) read with read_lut. A missing or
    unreadable file, or one that is not UTF-8, is one Problem naming the file."""
    shown = os.fspath(path)
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        raise ValidationError([Problem(shown, None, None, "the LUT file is missing")]) from None
    except OSError as error:
        reason = f"cannot read the LUT file: {error.strerror or error}"
        raise ValidationError([Problem(shown, None, None, reason)]) from None
    bom = len(codecs.BOM_UTF8) if data.startswith(codecs.BOM_UTF8) else 0
    try:
        text = data[bom:].decode("utf-8")
    except UnicodeDecodeError as error:
        start = bom + error.start  # an offset into the file, the BOM included
        line = data.count(b"\n", 0, start) + 1
        reason = f"not UTF-8 text: byte 0x{data[start]:02x} at byte offset {start}"
        raise ValidationError([Problem(shown, None, line, reason)]) from None
    return read_lut(text, path=shown)


# ---- the LUT_Printer ----


def _numbers(values: Rgb) -> str:
    return " ".join(format(value, ".17g") for value in values)


def print_lut(lut: Lut) -> str:
    """The LUT_Printer: lut as `.cube` text, one \\n after each line (Req 14.9). TITLE when set, then
    LUT_3D_SIZE, DOMAIN_MIN and DOMAIN_MAX, then the entries in `.cube` order, every number formatted '.17g'
    so that it reads back as exactly the same float."""
    lines = [] if lut.title is None else [f'{TITLE} "{lut.title}"']
    lines.append(f"{LUT_3D_SIZE} {lut.size}")
    lines.append(f"{DOMAIN_MIN} {_numbers(lut.domain_min)}")
    lines.append(f"{DOMAIN_MAX} {_numbers(lut.domain_max)}")
    lines.extend([f"{r:.17g} {g:.17g} {b:.17g}" for r, g, b in lut.table])
    lines.append("")
    return "\n".join(lines)


# ---- apply (§A9) ----

APPLY_ROWS = 256  # rows graded at a time: a 1080-wide chunk holds a few MB of float64


def apply(lut: Lut, pixels):  # noqa: ANN001, ANN201 - numpy types, imported lazily
    """pixels, an H×W×3 uint8 RGB array, graded through lut (§A9, Req 14.1): a new H×W×3 uint8 array.

    Per channel, in float64, x = clip((v/255 − dmin)/(dmax − dmin), 0, 1)·(N − 1), the floor index
    i0 = min(floor(x), N − 2) and the fraction f = x − i0; the 8 lattice corners of the table, reshaped
    [b][g][r] because red changes fastest, are interpolated trilinearly, the result clamped to [0, 1],
    scaled by 255 and rounded to the nearest integer (half to even). An identity LUT therefore keeps every
    channel within 1 of its input (Req 14.5).

    Two guards keep a Lut of any finite values from overflowing to inf or NaN, and change nothing for a
    table within [−1, 1] and a domain whose span is finite: a channel whose dmax − dmin overflows is
    normalised with every term halved, and a table holding values beyond ±1 is interpolated divided by its
    largest magnitude and multiplied back before the clamp.

    The work is done APPLY_ROWS rows at a time. pixels is never written to.
    """
    import numpy as np

    source = np.asarray(pixels)
    if source.dtype != np.uint8 or source.ndim != 3 or source.shape[2] != 3:
        raise ValueError(f"apply needs an H×W×3 uint8 RGB array, not {source.dtype} of shape {source.shape}")
    n = lut.size
    table = np.array(lut.table, dtype=np.float64).reshape(n, n, n, 3)  # [b][g][r][channel]
    scale = float(np.max(np.abs(table)))
    if scale > 1.0:
        table = table / scale
    else:
        scale = 1.0
    low = np.array(lut.domain_min, dtype=np.float64)
    high = np.array(lut.domain_max, dtype=np.float64)
    with np.errstate(over="ignore"):
        half = ~np.isfinite(high - low)
    k = np.where(half, 0.5, 1.0)  # 1 in every channel of a finite span, so the formula is exactly §A9's
    low_k, span_k = low * k, high * k - low * k
    height = source.shape[0]
    out = np.empty(source.shape, dtype=np.uint8)
    for start in range(0, height, APPLY_ROWS):
        block = source[start : start + APPLY_ROWS].astype(np.float64)  # a copy: the source stays as it was
        x = np.clip(((block / 255.0) * k - low_k) / span_k, 0.0, 1.0) * (n - 1)
        i0 = np.minimum(np.floor(x), n - 2).astype(np.intp)
        f = x - i0
        r0, g0, b0 = i0[..., 0], i0[..., 1], i0[..., 2]
        r1, g1, b1 = r0 + 1, g0 + 1, b0 + 1
        fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]
        # along red, then green, then blue
        c00 = table[b0, g0, r0] * (1.0 - fr) + table[b0, g0, r1] * fr
        c10 = table[b0, g1, r0] * (1.0 - fr) + table[b0, g1, r1] * fr
        c01 = table[b1, g0, r0] * (1.0 - fr) + table[b1, g0, r1] * fr
        c11 = table[b1, g1, r0] * (1.0 - fr) + table[b1, g1, r1] * fr
        c0 = c00 * (1.0 - fg) + c10 * fg
        c1 = c01 * (1.0 - fg) + c11 * fg
        value = c0 * (1.0 - fb) + c1 * fb
        if scale != 1.0:
            with np.errstate(over="ignore"):
                value = value * scale  # at most ±inf, never NaN, and the clamp takes either to 0 or 1
        out[start : start + APPLY_ROWS] = np.rint(np.clip(value, 0.0, 1.0) * 255.0)
    return out
