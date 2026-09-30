"""Hypothesis strategies for the LUT properties (task 14.5; design Testing Strategy, Properties 30 and 31).

The shared generators stay in tests/strategies.py; this module holds the LUT ones and takes texts() from it.

- luts(): valid Lut models (§M8): N from 2 to 9 for speed, an optional TITLE from texts(), any valid domain
  and finite entries from st.floats(allow_nan=False, allow_infinity=False). FIXED_LUTS holds the fixed
  N 33 and 65 LUTs that the property tests add as explicit examples, so N reaches 65.
- faults("lut"): the Property 31 catalogue as LutFault values. A LutFault injects its one fault into a
  LUT_Printer text and returns an Injected: the faulty text and what the LUT_Reader must report for it.

A TITLE is one line of a `.cube` file, so a generated title is texts() without \\n; every other character is
kept, \\r, U+2028 and `"` included.
"""
from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from hypothesis import strategies as st

from strategies import texts

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer.lut import DEFAULT_DOMAIN_MAX, DEFAULT_DOMAIN_MIN, Lut, Rgb  # noqa: E402

FINITE = st.floats(allow_nan=False, allow_infinity=False)

# ---- luts() ----

SIZES = st.integers(2, 9)  # N³ ≤ 729 entries keeps an example fast; FIXED_LUTS covers N 33 and 65
POOL_SIZE = 12  # at most this many drawn floats fill a table, plus PLACED entries drawn one by one
PLACED = 8


def one_line(text: str) -> str:
    return text.replace("\n", "")


@st.composite
def domains(draw: st.DrawFn) -> tuple[Rgb, Rgb]:
    """(domain_min, domain_max): the default domain, or any finite pair per channel with min below max."""
    if draw(st.booleans()):
        return DEFAULT_DOMAIN_MIN, DEFAULT_DOMAIN_MAX
    low, high = [], []
    for _ in range(3):
        a = draw(FINITE)
        b = draw(FINITE.filter(lambda value, a=a: value != a))  # also keeps -0.0 and 0.0 apart
        low.append(min(a, b))
        high.append(max(a, b))
    return (low[0], low[1], low[2]), (high[0], high[1], high[2])


@st.composite
def luts(draw: st.DrawFn, sizes: st.SearchStrategy[int] = SIZES) -> Lut:
    """A valid Lut. Drawing all N³·3 entries one by one fails Hypothesis' large_base_example health check
    from N = 7 on, so the table is filled from a pool of up to POOL_SIZE drawn floats by a random.Random with
    a drawn seed, and then up to PLACED entries drawn one by one go to drawn places. Any finite float can
    therefore land in any channel of any entry, and the example stays small and replayable."""
    size = draw(sizes)
    title = draw(st.none() | texts().map(one_line))
    domain_min, domain_max = draw(domains())
    count = size**3
    pool = draw(st.lists(FINITE, min_size=1, max_size=POOL_SIZE))
    rng = random.Random(draw(st.integers(0, 2**32 - 1)))
    table = [(rng.choice(pool), rng.choice(pool), rng.choice(pool)) for _ in range(count)]
    entry = st.tuples(FINITE, FINITE, FINITE)
    for index, value in draw(st.lists(st.tuples(st.integers(0, count - 1), entry), max_size=PLACED)):
        table[index] = value
    return Lut(title, size, domain_min, domain_max, tuple(table))


# Floats whose printing is easy to get wrong: signed zero, the subnormal and normal extremes, the largest
# floats, neighbours of 1, and values with no short decimal form.
SPECIAL_FLOATS = (
    0.0, -0.0, 1.0, -1.0, 0.5, 0.1, 1 / 3, 2 / 3, 1e-300, 5e-324, 2.2250738585072014e-308,
    1.7976931348623157e308, -1.7976931348623157e308, 1e16, 123456.789, -2.5e-8, 0.9999999999999999,
    1.0000000000000002, math.pi,
)  # fmt: skip


def fixed_lut(size: int, title: str | None, domain_min: Rgb, domain_max: Rgb) -> Lut:
    """A deterministic size-N Lut whose entries mix SPECIAL_FLOATS, a different mix in each channel."""
    p, n = SPECIAL_FLOATS, len(SPECIAL_FLOATS)
    table = tuple((p[t % n], p[(t // n + 2 * t) % n], p[(t // (n * n) + 3 * t + 7) % n]) for t in range(size**3))
    return Lut(title, size, domain_min, domain_max, table)


FIXED_LUTS = {
    33: fixed_lut(33, 'grade "33" \u2014 暖色\r', (-0.5, 0.0, 1e-300), (1.5, 1e308, 2.0)),
    65: fixed_lut(65, None, DEFAULT_DOMAIN_MIN, DEFAULT_DOMAIN_MAX),
}

# ---- faults("lut") ----


@dataclass(frozen=True)
class Injected:
    """A LUT_Printer text with one injected fault, and what the LUT_Reader must report for it."""

    text: str
    field: str | None  # the Problem's field: a keyword, "entry <n>", or None for a wrong entry count
    line: int | None  # the 1-based line of the first faulty line; None for a fault on no line
    phrase: str  # a phrase of the Problem's reason that names the fault
    counts: tuple[int, int] | None = None  # (expected, actual) for a wrong entry count


class _Printed:
    """The lines of a LUT_Printer text: TITLE when set, LUT_3D_SIZE, DOMAIN_MIN and DOMAIN_MAX, then the
    entries."""

    def __init__(self, text: str) -> None:
        lines = text.split("\n")
        assert lines[-1] == "", "a LUT_Printer text ends with \\n"
        self.lines = lines[:-1]
        self.header = 4 if self.lines[0].startswith('TITLE "') else 3
        self.size = int(self.lines[self.index("LUT_3D_SIZE")].split(" ")[1])
        assert len(self.lines) == self.header + self.size**3

    def index(self, keyword: str) -> int:
        return next(i for i in range(self.header) if self.lines[i].split(" ", 1)[0] == keyword)

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _values(line: str) -> list[float]:
    return [float(value) for value in line.split(" ")[1:]]


def _domain_line(keyword: str, values: list[float]) -> str:
    return keyword + " " + " ".join(format(value, ".17g") for value in values)


class LutFault:
    """One fault of the Property 31 catalogue. Positions are reduced modulo the places the fault can go, so
    one value fits a LUT of any size."""

    kind: ClassVar[str]

    def inject(self, printed: str) -> Injected:
        raise NotImplementedError


@dataclass(frozen=True)
class OneDimSize(LutFault):
    """A `LUT_1D_SIZE` line, anywhere in the file."""

    kind = "a LUT_1D_SIZE line"
    at: int
    size: int

    def inject(self, printed: str) -> Injected:
        p = _Printed(printed)
        index = self.at % (len(p.lines) + 1)
        p.lines.insert(index, f"LUT_1D_SIZE {self.size}")
        return Injected(p.text(), "LUT_1D_SIZE", index + 1, "1D LUT")


@dataclass(frozen=True)
class MissingSize(LutFault):
    """No `LUT_3D_SIZE` line."""

    kind = "a missing LUT_3D_SIZE"

    def inject(self, printed: str) -> Injected:
        p = _Printed(printed)
        del p.lines[p.index("LUT_3D_SIZE")]
        return Injected(p.text(), "LUT_3D_SIZE", None, "missing")


@dataclass(frozen=True)
class RepeatedKeyword(LutFault):
    """A copy of a keyword line put anywhere among the keyword lines: of LUT_3D_SIZE when size_only, else of
    any keyword line the LUT has. Whichever of the two comes second is the faulty line."""

    kind = "a repeated keyword"
    size_only: bool
    which: int
    at: int

    def inject(self, printed: str) -> Injected:
        p = _Printed(printed)
        sources = [p.index("LUT_3D_SIZE")] if self.size_only else list(range(p.header))
        source = sources[self.which % len(sources)]
        copy = p.lines[source]
        index = self.at % (p.header + 1)
        p.lines.insert(index, copy)
        second = source + 1 if index <= source else index
        return Injected(p.text(), copy.split(" ", 1)[0], second + 1, "repeated")


@dataclass(frozen=True)
class SizeOutOfRange(LutFault):
    """`LUT_3D_SIZE` giving a whole number outside 2 to 65, or no whole number at all."""

    kind = "an N outside 2 to 65"
    value: str

    def inject(self, printed: str) -> Injected:
        p = _Printed(printed)
        index = p.index("LUT_3D_SIZE")
        p.lines[index] = f"LUT_3D_SIZE {self.value}"
        return Injected(p.text(), "LUT_3D_SIZE", index + 1, "2 to 65")


@dataclass(frozen=True)
class DomainOrder(LutFault):
    """A DOMAIN_MIN value not below the matching DOMAIN_MAX value in the channels of the mask (bit c for
    channel c). Mode 0 gives DOMAIN_MIN the DOMAIN_MAX value there; mode 1 swaps the two lines' values;
    mode 2 drops DOMAIN_MAX and gives DOMAIN_MIN the default maximum 1.0 there; mode 3 drops DOMAIN_MIN and
    gives DOMAIN_MAX the default minimum 0.0 there. The fault is on the DOMAIN_MIN line, or on the DOMAIN_MAX
    line when no DOMAIN_MIN is left."""

    kind = "a DOMAIN_MIN not below DOMAIN_MAX"
    mode: int
    channels: int

    def inject(self, printed: str) -> Injected:
        p = _Printed(printed)
        i_min, i_max = p.index("DOMAIN_MIN"), p.index("DOMAIN_MAX")
        assert i_min < i_max, "the LUT_Printer writes DOMAIN_MIN before DOMAIN_MAX"
        low, high = _values(p.lines[i_min]), _values(p.lines[i_max])
        chosen = [c for c in range(3) if self.channels >> c & 1]
        assert chosen, "the mask names at least one channel"
        if self.mode == 0:
            p.lines[i_min] = _domain_line("DOMAIN_MIN", [high[c] if c in chosen else low[c] for c in range(3)])
        elif self.mode == 1:
            p.lines[i_min], p.lines[i_max] = _domain_line("DOMAIN_MIN", high), _domain_line("DOMAIN_MAX", low)
        elif self.mode == 2:
            p.lines[i_min] = _domain_line("DOMAIN_MIN", [1.0 if c in chosen else low[c] for c in range(3)])
            del p.lines[i_max]
        else:
            p.lines[i_max] = _domain_line("DOMAIN_MAX", [0.0 if c in chosen else high[c] for c in range(3)])
            del p.lines[i_min]
            return Injected(p.text(), "DOMAIN_MAX", i_max, "not below")  # moved up one line
        return Injected(p.text(), "DOMAIN_MIN", i_min + 1, "not below")


# Tokens that are not a finite number and cannot make a line blank, a comment or a keyword line.
BAD_TOKENS = (
    "nan", "NaN", "inf", "-inf", "+inf", "Infinity", "1e999", "-1e400", "1e309", "x", "1_0", "0x1p-2",
    "1,5", "\u0661", "\u00bd", "\u221e", "--1", "1e", ".", "e5", "1.2.3", "0.5f", "(0)",
)  # fmt: skip


@dataclass(frozen=True)
class BadEntry(LutFault):
    """One entry line replaced by one that is not exactly 3 finite numbers. Form 0 keeps 2 of its numbers,
    1 adds a fourth, 2 keeps one, 3 joins them with commas, and 4 puts token in place of number slot."""

    kind = "a malformed or non-finite entry line"
    at: int
    form: int
    slot: int
    token: str

    def inject(self, printed: str) -> Injected:
        p = _Printed(printed)
        entry = self.at % p.size**3
        index = p.header + entry
        numbers = p.lines[index].split(" ")
        forms = (
            numbers[:2],
            numbers + numbers[:1],
            numbers[:1],
            [",".join(numbers)],
            [self.token if slot == self.slot % 3 else number for slot, number in enumerate(numbers)],
        )
        p.lines[index] = " ".join(forms[self.form % len(forms)])
        return Injected(p.text(), f"entry {entry + 1}", index + 1, "is not 3 finite numbers")


@dataclass(frozen=True)
class WrongCount(LutFault):
    """Too many entries (1 to 3 copies of the first entry line put anywhere among the entries) or too few
    (1 to N³ consecutive entry lines removed). Too many is a fault on the line of the first entry beyond N³;
    too few is on no line."""

    kind = "too many or too few entries"
    more: bool
    at: int
    amount: int

    def inject(self, printed: str) -> Injected:
        p = _Printed(printed)
        expected = p.size**3
        if self.more:
            added = 1 + (self.amount - 1) % 3
            index = p.header + self.at % (expected + 1)
            p.lines[index:index] = [p.lines[p.header]] * added
            actual, line = expected + added, p.header + expected + 1  # every line after the header is an entry
        else:
            removed = 1 + (self.amount - 1) % expected
            start = p.header + self.at % (expected - removed + 1)
            del p.lines[start : start + removed]
            actual, line = expected - removed, None
        return Injected(p.text(), None, line, f"expected {expected} entries", (expected, actual))


# LUT_3D_SIZE values that are no N from 2 to 65: whole numbers outside the range, and text that is no
# whole number.
BAD_SIZES = st.one_of(
    st.integers(max_value=1).map(str),
    st.integers(min_value=66).map(str),
    st.sampled_from(("", "33.0", "3e1", "x", "33 33", "0x21", "3_3", "\u0663\u0663", "+", "- 3")),
)


def faults(kind: str) -> st.SearchStrategy[LutFault]:
    """The single-fault mutators of kind. Only "lut" lives here: the Property 31 catalogue, a LUT_1D_SIZE
    line, a missing or repeated LUT_3D_SIZE, an N outside 2 to 65, a repeated keyword, a DOMAIN_MIN not below
    DOMAIN_MAX, a malformed or non-finite entry line, and too many or too few entries."""
    if kind != "lut":
        raise ValueError(f"faults({kind!r}): tests/strategies_luts.py has only faults('lut')")
    anywhere = st.integers(0, 10**9)
    amounts = st.integers(1, 10**6)
    # st.one_of draws its first branch most often, so the fault with the most variants comes first and the
    # one without any comes last.
    tokens = st.sampled_from(BAD_TOKENS)
    return st.one_of(
        st.builds(BadEntry, at=anywhere, form=st.integers(0, 4), slot=st.integers(0, 2), token=tokens),
        st.builds(WrongCount, more=st.just(True), at=anywhere, amount=amounts),
        st.builds(WrongCount, more=st.just(False), at=anywhere, amount=amounts),
        st.builds(DomainOrder, mode=st.integers(0, 3), channels=st.integers(1, 7)),
        st.builds(RepeatedKeyword, size_only=st.just(True), which=anywhere, at=anywhere),
        st.builds(RepeatedKeyword, size_only=st.just(False), which=anywhere, at=anywhere),
        st.builds(SizeOutOfRange, value=BAD_SIZES),
        st.builds(OneDimSize, at=anywhere, size=st.integers(2, 65536)),
        st.just(MissingSize()),
    )
