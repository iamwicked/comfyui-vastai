"""Tests of the LUT model, LUT_Reader and LUT_Printer, scripts/influencer/lut.py, and of the two repo LUTs
(task 14, design §M8; Req 14.7–14.10).

- Property 30: read_lut(print_lut(l)) == l for every valid LUT
- Property 31: a printed LUT with one injected fault is rejected with the path, the fault, its line and,
  for a wrong entry count, the expected and actual counts
- the repo LUTs, and tests/tools/make_luts.py reproducing them byte for byte
- the printer's layout, the reader's grammar (comments, blank lines, CRLF, BOM, keyword order, defaults),
  files with many faults at once, the file-level Problems of load_lut, and the model's own checks
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import example, given, settings

from helpers.harness import REPO, run_bounded

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer.errors import ValidationError  # noqa: E402
from influencer.lut import Lut, load_lut, print_lut, read_lut  # noqa: E402
from strategies_luts import FIXED_LUTS, BadEntry, LutFault, WrongCount, faults, luts  # noqa: E402

LUT_DIR = REPO / "config" / "influencer" / "luts"
MAKE_LUTS = REPO / "tests" / "tools" / "make_luts.py"
REPO_LUTS = ("example-grade-33.cube", "identity-33.cube")
ORIGIN = "# origin: generated for comfyui-vastai by tests/tools/make_luts.py; licence: CC0-1.0"
UNIT = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)  # the default domain


def lattice_point(index: int, size: int) -> tuple[int, int, int]:
    """The (i, j, k) lattice index of table entry index: red changes fastest."""
    return index % size, index // size % size, index // (size * size)


def identity_entry(index: int, size: int) -> tuple[float, float, float]:
    i, j, k = lattice_point(index, size)
    return i / (size - 1), j / (size - 1), k / (size - 1)


# ---- the properties ----


# Feature: ai-influencer-pipeline, Property 30: LUT round trip
@settings(deadline=None)  # the fixed N 65 example prints and reads 274,625 entries
@example(lut=FIXED_LUTS[33])
@example(lut=FIXED_LUTS[65])
@given(lut=luts())
def test_lut_round_trip(lut: Lut) -> None:
    """For any valid LUT (N 2–65, optional TITLE, any valid domain, finite entries), the LUT_Reader reads
    the LUT_Printer's text back as an equal LUT.

    **Validates: Requirements 14.9**
    """
    assert read_lut(print_lut(lut)) == lut


# Feature: ai-influencer-pipeline, Property 31: LUT faults are reported precisely
@settings(deadline=None)  # the fixed N 33 and 65 examples read files of 35,937 and 274,625 entries
@example(lut=FIXED_LUTS[33], fault=BadEntry(at=35_000, form=4, slot=2, token="1e999"))
@example(lut=FIXED_LUTS[65], fault=WrongCount(more=True, at=100_000, amount=2))
@given(lut=luts(), fault=faults("lut"))
def test_lut_faults_are_reported_precisely(lut: Lut, fault: LutFault) -> None:
    """For any printed valid LUT with one injected fault, the LUT_Reader rejects the file with exactly one
    Problem: the file's path, the fault, the 1-based line of the first faulty line when the fault is on a
    line, and the expected and actual counts for a wrong entry count.

    **Validates: Requirements 14.8**
    """
    injected = fault.inject(print_lut(lut))
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "grade.cube"
        path.write_bytes(injected.text.encode("utf-8"))
        with pytest.raises(ValidationError) as raised:
            load_lut(path)
    problems = raised.value.problems
    assert len(problems) == 1, f"{fault.kind}: {raised.value}"
    problem = problems[0]
    assert problem.path == os.fspath(path)
    assert (problem.field, problem.line) == (injected.field, injected.line), f"{fault.kind}: {problem}"
    assert injected.phrase in problem.reason, f"{fault.kind}: {problem}"
    if injected.counts is not None:
        expected, actual = injected.counts
        assert re.search(rf"\bexpected {expected} entries\b", problem.reason), str(problem)
        assert re.search(rf"\bfound {actual}\b", problem.reason), str(problem)


# ---- the repo LUTs (Req 14.10) ----


@pytest.mark.parametrize("name", REPO_LUTS)
def test_the_repo_luts_read_back_with_their_size_domain_and_origin(name: str) -> None:
    path = LUT_DIR / name
    assert path.read_text(encoding="utf-8").split("\n", 1)[0] == ORIGIN
    lut = load_lut(path)
    assert (lut.size, (lut.domain_min, lut.domain_max)) == (33, UNIT)


def test_the_identity_lut_is_exact() -> None:
    lut = load_lut(LUT_DIR / "identity-33.cube")
    assert all(entry == identity_entry(index, 33) for index, entry in enumerate(lut.table))


def test_the_example_grade_differs_from_the_identity_by_more_than_one_level() -> None:
    grade = load_lut(LUT_DIR / "example-grade-33.cube")
    differences = (
        abs(value - reference)
        for index, entry in enumerate(grade.table)
        for value, reference in zip(entry, identity_entry(index, 33))
    )
    assert max(differences) > 1 / 255
    black, white = grade.table[0], grade.table[-1]
    assert min(black) > 1 / 255 and black[0] > black[2] and white[0] > white[2]  # lifted and warm


def test_make_luts_reproduces_the_repo_luts_byte_for_byte(tmp_path: Path) -> None:
    result = run_bounded([sys.executable, MAKE_LUTS, "--out", tmp_path], timeout=60, cwd=tmp_path)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(REPO_LUTS)
    for name in REPO_LUTS:
        assert (tmp_path / name).read_bytes() == (LUT_DIR / name).read_bytes(), name


# ---- the LUT_Printer and the LUT_Reader's grammar (Req 14.7, 14.9) ----


def test_the_printer_writes_title_size_domain_then_entries_in_cube_order() -> None:
    table = tuple((i * 0.1, j * 0.5, float(k)) for k in range(2) for j in range(2) for i in range(2))
    lut = Lut('warm "grade"', 2, (0.0, -0.0, 0.5), (1.0, 2.0, 1e20), table)
    assert print_lut(lut) == (
        'TITLE "warm "grade""\n'
        "LUT_3D_SIZE 2\n"
        "DOMAIN_MIN 0 -0 0.5\n"
        "DOMAIN_MAX 1 2 1e+20\n"
        "0 0 0\n"
        "0.10000000000000001 0 0\n"
        "0 0.5 0\n"
        "0.10000000000000001 0.5 0\n"
        "0 0 1\n"
        "0.10000000000000001 0 1\n"
        "0 0.5 1\n"
        "0.10000000000000001 0.5 1\n"
    )
    assert print_lut(Lut(None, 2, *UNIT, table)).startswith("LUT_3D_SIZE 2\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n")


def test_the_reader_takes_comments_blank_lines_crlf_a_bom_and_the_default_domain(tmp_path: Path) -> None:
    text = (
        "# made by hand\r\n"
        "\r\n"
        "  LUT_3D_SIZE\t2  \r\n"
        "  # an indented comment\r\n"
        'TITLE "hand made"\r\n'
        "\t\r\n"
        "0 0 0\r\n"
        "# a comment among the entries\r\n"
        "1.0 0 0\r\n0 1 0\r\n1 1 0\r\n\r\n0 0 1\r\n+1 0 1e0\r\n0 1. 1\r\n"
        "1   1\t1"  # the last line has no line break
    )
    path = tmp_path / "hand.cube"
    path.write_bytes(b"\xef\xbb\xbf" + text.encode("utf-8"))
    table = tuple(tuple(float(c) for c in lattice_point(index, 2)) for index in range(8))
    assert load_lut(path) == Lut("hand made", 2, *UNIT, table)


def test_the_reader_takes_the_domain_keywords_in_any_order_and_plain_decimals() -> None:
    text = "DOMAIN_MAX 2. 1E0 +1\nLUT_3D_SIZE 2\nDOMAIN_MIN -.5 +0 1e-3\n" + "0 0 0\n" * 8
    lut = read_lut(text)
    assert (lut.title, lut.domain_min, lut.domain_max) == (None, (-0.5, 0.0, 0.001), (2.0, 1.0, 1.0))


# ---- the LUT_Reader's faults (Req 14.8) ----

ONE_OF_EACH_LINE_FAULT = """\
# hand made, with one of each fault on a line
TITLE "one"
LUT_1D_SIZE 1024
LUT_3D_SIZE 2

DOMAIN_MIN 0 0.5 0
TITLE "two"
0 0 0
0 0
# between the entries
0 0 1e999
DOMAIN_MAX 1 1 1
1 1 0
0 0 1
1 0 1
0 1 1
1 1 1
1 1 1
"""


@pytest.mark.parametrize(
    ("text", "report"),
    [
        (
            ONE_OF_EACH_LINE_FAULT,
            """\
hand.cube: line 3: LUT_1D_SIZE: a 1D LUT line: the LUT_Reader reads only 3D LUTs, sized by LUT_3D_SIZE
hand.cube: line 7: TITLE: repeated: the first TITLE line is line 2
hand.cube: line 9: entry 2: '0 0' is not 3 finite numbers
hand.cube: line 11: entry 3: '0 0 1e999' is not 3 finite numbers
hand.cube: line 12: DOMAIN_MAX: comes after the first entry, on line 8: keyword lines go before the entries
hand.cube: line 18: expected 8 entries (2³ for LUT_3D_SIZE 2), found 9""",
        ),
        (
            "DOMAIN_MAX 1 0 1\n0 0 0\n",
            """\
hand.cube: line 1: DOMAIN_MAX: the default DOMAIN_MIN is not below it in green: 0.0 >= 0.0
hand.cube: LUT_3D_SIZE: missing: a 3D LUT needs one LUT_3D_SIZE line""",
        ),
        (
            "LUT_3D_SIZE 2\nDOMAIN_MIN 0 0.5 2\nDOMAIN_MAX 1 0.5 1\n" + "0 0 0\n" * 7,
            """\
hand.cube: line 2: DOMAIN_MIN: not below DOMAIN_MAX (line 3) in green: 0.5 >= 0.5; blue: 2.0 >= 1.0
hand.cube: expected 8 entries (2³ for LUT_3D_SIZE 2), found 7""",
        ),
        (
            'TITLE unquoted\nLUT_3D_SIZE 66\nDOMAIN_MIN 0 0\nDOMAIN_MAX 1 1 inf\n' + "0 0 0\n" * 8,
            """\
hand.cube: line 1: TITLE: needs its text in double quotes, as in TITLE "Warm grade"
hand.cube: line 2: LUT_3D_SIZE: 66 is outside 2 to 65
hand.cube: line 3: DOMAIN_MIN: needs 3 finite numbers, not '0 0'
hand.cube: line 4: DOMAIN_MAX: needs 3 finite numbers, not '1 1 inf'""",
        ),
        ("LUT_3D_SIZE 2.0\n", "hand.cube: line 1: LUT_3D_SIZE: needs one whole number from 2 to 65, not '2.0'"),
        ("", "hand.cube: LUT_3D_SIZE: missing: a 3D LUT needs one LUT_3D_SIZE line"),
    ],
    ids=["line-faults", "domain-max-only", "domain-and-too-few", "keyword-values", "size-not-whole", "empty"],
)
def test_the_reader_reports_every_fault_in_line_order(text: str, report: str) -> None:
    with pytest.raises(ValidationError) as raised:
        read_lut(text, path="hand.cube")
    assert str(raised.value) == report


def test_load_lut_reports_a_missing_unreadable_or_non_utf8_file(tmp_path: Path) -> None:
    cases = {
        tmp_path / "missing.cube": "the LUT file is missing",
        tmp_path: "cannot read the LUT file: ",
    }
    for path, reason in cases.items():
        with pytest.raises(ValidationError) as raised:
            load_lut(path)
        [problem] = raised.value.problems
        assert (problem.path, problem.line) == (os.fspath(path), None)
        assert problem.reason.startswith(reason)
    latin, bom = tmp_path / "latin.cube", tmp_path / "bom.cube"
    latin.write_bytes(b'LUT_3D_SIZE 2\nTITLE "caf\xe9"\n')
    bom.write_bytes(b'\xef\xbb\xbfLUT_3D_SIZE 2\nTITLE "caf\xe9"\n')  # offsets count the BOM too
    for path, offset in ((latin, 24), (bom, 27)):
        with pytest.raises(ValidationError) as raised:
            load_lut(path)
        assert str(raised.value) == f"{path}: line 2: not UTF-8 text: byte 0xe9 at byte offset {offset}"


# ---- the model ----


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"title": "two\nlines"}, "title must be None or text without"),
        ({"size": 1, "table": ((0.0, 0.0, 0.0),)}, "size must be a whole number from 2 to 65"),
        ({"size": 66}, "size must be a whole number from 2 to 65"),
        ({"size": True}, "size must be a whole number from 2 to 65"),
        ({"domain_min": (0.0, 1.0, 0.0)}, "domain_min is not below domain_max in green"),
        ({"domain_max": (1.0, 1.0, float("inf"))}, "domain_max must be 3 finite numbers"),
        ({"table": ((0.0, 0.0, 0.0),) * 7}, "a size 2 LUT has 8 entries, not 7"),
        ({"table": ((0.0, 0.0, 0.0),) * 7 + ((0.0, float("nan"), 0.0),)}, "every entry must be 3 finite numbers"),
        ({"table": ((0.0, 0.0, 0.0),) * 7 + ((0.0, 0.0),)}, "every entry must be 3 finite numbers"),
    ],
)
def test_the_model_refuses_what_no_cube_file_can_hold(changes: dict[str, object], message: str) -> None:
    values: dict[str, object] = {
        "title": None, "size": 2, "domain_min": UNIT[0], "domain_max": UNIT[1], "table": ((0.0, 0.0, 0.0),) * 8,
    }  # fmt: skip
    values.update(changes)
    with pytest.raises(ValueError, match=re.escape(message)):
        Lut(**values)  # type: ignore[arg-type]


def test_a_large_lut_has_a_short_repr() -> None:
    shown = repr(FIXED_LUTS[65])
    assert len(shown) < 400 and "<274625 entries: " in shown
