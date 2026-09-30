"""Tests of the Content_Calendar, scripts/influencer/calendar_io.py, its commands and the example calendars
(task 9; design §M2; Req 5, 8.10, 11.6, 16.10-16.12, 26.12).

- Property 3: Week_Ids through the Reader, and any text that is not YYYY-Www
- Properties 4 and 5: print_calendar_yaml and print_calendar_csv, then read_calendar, give back the calendar
- Property 6: YAML and CSV files holding the same data, faults included, read the same, with a BOM and
  blank rows in the CSV
- Property 7: one fault in one Post rejects exactly that Post and leaves every other one as it was
- Property 8: skeleton calendars are accepted and name only the persona's locations and outfits
- the committed example calendars, the CSV file-level rejections, two calendar files for one week, the
  YAML-only rules, and the new-calendar, calendar check and persona check commands
"""
from __future__ import annotations

import datetime
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import __main__ as cli  # noqa: E402
from influencer import calendar_io as C  # noqa: E402
from influencer import persona as P  # noqa: E402
from influencer.errors import Exit, ValidationError  # noqa: E402
from influencer.yamlio import dump_yaml  # noqa: E402
from strategies import texts  # noqa: E402
from strategies_calendar import PostFault, calendars, csv_text, faults, weeks, write_needed_files  # noqa: E402
from strategies_persona import personas  # noqa: E402

EXAMPLES = REPO / "config" / "influencer" / "examples"
DESIGN = REPO / ".kiro" / "specs" / "ai-influencer-pipeline" / "design.md"
EXAMPLE_PERSONA = P.read_persona(EXAMPLES / "persona.yaml", None, check_files=False)
EXTERNAL_REEL = {"id": "fri-dance", "type": "reel", "date": "2026-10-02", "mode": "external", "duration": 10}

PERSONA_CALENDARS = personas().flatmap(lambda persona: st.tuples(st.just(persona), calendars(persona)))


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def read(root: Path, name: str, text: str, persona: P.Persona = EXAMPLE_PERSONA) -> C.CalendarResult:
    return C.read_calendar(write(root / name, text), persona, root)


def names(problems: list, label: str, field: str) -> bool:
    """Some Problem names the Post's field, or an item or key inside it."""
    prefix = f"{label}.{field}"
    return any(p.field == prefix or p.field.startswith((prefix + "[", prefix + ".")) for p in problems)


# ---- the properties ----


# Feature: ai-influencer-pipeline, Property 3: Week_Id validation
@settings(deadline=None)
@given(year=st.integers(1900, 2200), week=st.integers(0, 60), text=texts())
def test_week_id_validation(year: int, week: int, text: str) -> None:
    """**Validates: Requirements 5.3**"""
    try:  # an oracle independent of date(Y, 12, 28): the ISO calendar itself
        valid = datetime.date.fromisocalendar(year, week, 1) is not None
    except ValueError:
        valid = False
    cases = [(f"{year:04d}-W{week:02d}", valid)]
    if not re.fullmatch(r"\d{4}-W\d{2}", text, re.ASCII):
        cases.append((text, False))
    with tempfile.TemporaryDirectory(prefix="calendar-") as folder:
        for ident, accepted in cases:
            result = read(Path(folder), "calendar.yaml", dump_yaml({"week": ident, "posts": [EXTERNAL_REEL]}))
            assert (result.calendar is not None) == accepted, (ident, result.problems)
            assert accepted or [p.field for p in result.problems] == ["week"], result.problems
            assert (C.week_problem(ident) is None) == accepted


# Feature: ai-influencer-pipeline, Property 4: Content_Calendar YAML round trip
@settings(deadline=None)
@given(pair=PERSONA_CALENDARS)
def test_content_calendar_yaml_round_trip(pair: tuple[P.Persona, C.Calendar]) -> None:
    """**Validates: Requirements 5.13**"""
    persona, calendar = pair
    with tempfile.TemporaryDirectory(prefix="calendar-") as folder:
        write_needed_files(Path(folder), persona, calendar)
        result = read(Path(folder), "calendar.yaml", C.print_calendar_yaml(calendar), persona)
    assert (result.problems, result.rejected) == ([], {})
    assert result.calendar == calendar


# Feature: ai-influencer-pipeline, Property 5: Content_Calendar CSV round trip
@settings(deadline=None)
@given(pair=PERSONA_CALENDARS)
def test_content_calendar_csv_round_trip(pair: tuple[P.Persona, C.Calendar]) -> None:
    """**Validates: Requirements 5.18**"""
    persona, calendar = pair
    with tempfile.TemporaryDirectory(prefix="calendar-") as folder:
        write_needed_files(Path(folder), persona, calendar)
        result = read(Path(folder), "calendar.csv", C.print_calendar_csv(calendar), persona)
    assert (result.problems, result.rejected) == ([], {})
    assert result.calendar == calendar


# Feature: ai-influencer-pipeline, Property 6: CSV and YAML calendars are read identically
@settings(deadline=None)
@given(pair=PERSONA_CALENDARS, data=st.data(), bom=st.booleans(), blank_rows=st.lists(st.integers(), max_size=4))
def test_csv_and_yaml_calendars_are_read_identically(pair: tuple[P.Persona, C.Calendar], data: st.DataObject,
                                                     bom: bool, blank_rows: list[int]) -> None:  # fmt: skip
    """**Validates: Requirements 5.16, 5.14**"""
    persona, calendar = pair
    document = C.calendar_data(calendar)
    posts = document["posts"]
    for fault in data.draw(st.lists(faults("post"), max_size=3), label="faults"):
        index = data.draw(st.sampled_from(range(len(posts))), label="index")
        if fault.applies(posts, index, persona):
            fault.apply(posts, index, persona)
    with tempfile.TemporaryDirectory(prefix="calendar-") as folder:
        root = Path(folder)
        write_needed_files(root, persona, calendar)
        from_yaml = read(root, "calendar.yaml", dump_yaml(document), persona)
        from_csv = read(root, "calendar.csv", csv_text(document, bom=bom, blank_rows=blank_rows), persona)
    assert (from_csv.calendar is None) == (from_yaml.calendar is None), (from_yaml.problems, from_csv.problems)
    if from_yaml.calendar is not None:
        assert from_csv.calendar == from_yaml.calendar
        assert set(from_csv.rejected) == set(from_yaml.rejected)


# Feature: ai-influencer-pipeline, Property 7: A rejected Post never affects the others
@settings(deadline=None)
@given(pair=PERSONA_CALENDARS, data=st.data(), as_csv=st.booleans())
def test_a_rejected_post_never_affects_the_others(pair: tuple[P.Persona, C.Calendar], data: st.DataObject,
                                                  as_csv: bool) -> None:  # fmt: skip
    """**Validates: Requirements 5.9, 5.2, 5.4, 5.5, 5.6, 5.7, 5.8, 5.20, 8.10, 11.6, 16.10, 16.11, 16.12**"""
    persona, calendar = pair
    document = C.calendar_data(calendar)
    posts = document["posts"]
    index = data.draw(st.sampled_from(range(len(posts))), label="index")
    fault: PostFault = data.draw(faults("post").filter(lambda f: f.applies(posts, index, persona)), label="fault")
    field = fault.apply(posts, index, persona)
    label = f"#{index + 1}" if field == "id" else calendar.posts[index].id
    with tempfile.TemporaryDirectory(prefix="calendar-") as folder:
        root = Path(folder)
        write_needed_files(root, persona, calendar)
        if as_csv:
            result = read(root, "calendar.csv", csv_text(document), persona)
        else:
            result = read(root, "calendar.yaml", dump_yaml(document), persona)
    assert result.problems == []
    assert list(result.rejected) == [label]
    assert names(result.rejected[label], label, field), result.rejected[label]
    assert result.calendar is not None
    assert result.calendar.posts == tuple(post for number, post in enumerate(calendar.posts) if number != index)


# Feature: ai-influencer-pipeline, Property 8: Skeleton calendars are valid
@settings(deadline=None)
@given(persona=personas(), week=weeks(), as_csv=st.booleans())
def test_skeleton_calendars_are_valid(persona: P.Persona, week: str, as_csv: bool) -> None:
    """**Validates: Requirements 5.11, 5.17**"""
    skeleton = C.skeleton_calendar(week, persona)
    assert skeleton.week == week and len(skeleton.posts) >= 1
    for post in skeleton.posts:
        assert post.location is None or post.location in [location.name for location in persona.locations]
        assert post.outfit is None or post.outfit in [outfit.name for outfit in persona.wardrobe]
    with tempfile.TemporaryDirectory(prefix="calendar-") as folder:
        root = Path(folder)
        assert C.calendar_files(root, week) == []
        name, text = ("calendar.csv", C.print_calendar_csv(skeleton)) if as_csv else (
            "calendar.yaml", C.print_calendar_yaml(skeleton))  # fmt: skip
        result = C.read_calendar(write(C.week_folder(root, week) / name, text), persona, root)
    assert (result.problems, result.rejected, result.warnings) == ([], {}, [])
    assert result.calendar == skeleton


# ---- the example calendars (Req 5.16, 26.12) ----


def _design_example() -> object:
    """The YAML block under design.md's "### M2" heading; a skip when the design is not checked out."""
    if not DESIGN.is_file():
        pytest.skip(f"{DESIGN.relative_to(REPO)} is not in this checkout")
    match = re.search(r"^### M2\b.*?^```yaml\n(.*?)^```", DESIGN.read_text(encoding="utf-8"), re.S | re.M)
    if match is None:
        pytest.skip("design.md has no YAML block under its ### M2 heading")
    return yaml.safe_load(match.group(1))


def test_the_example_yaml_is_the_design_example() -> None:
    assert yaml.safe_load((EXAMPLES / "calendar.yaml").read_text(encoding="utf-8")) == _design_example()


def test_the_example_calendars_read_equal_against_the_persona_example() -> None:
    results = [C.read_calendar(EXAMPLES / name, EXAMPLE_PERSONA, None, check_files=False)
               for name in ("calendar.yaml", "calendar.csv")]  # fmt: skip
    for result in results:
        assert (result.problems, result.rejected, result.warnings) == ([], {}, [])
    from_yaml, from_csv = (result.calendar for result in results)
    assert from_yaml == from_csv
    assert from_yaml.week == "2026-W40"
    assert [(p.id, p.type, p.mode) for p in from_yaml.posts] == [
        ("mon-cafe", "single", None), ("tue-street", "carousel", None), ("wed-story", "story", None),
        ("thu-reel", "reel", None), ("fri-dance", "reel", "external"),
    ]  # fmt: skip
    assert (from_yaml.posts[1].slides, from_yaml.posts[1].aspect, from_yaml.posts[3].source_slide) == (5, "4:5", 2)


# ---- the CSV format (Req 5.14, 5.15) ----

EXAMPLE_CSV = (EXAMPLES / "calendar.csv").read_text(encoding="utf-8")
HEADER, *ROWS = EXAMPLE_CSV.splitlines()


def _csv(header: str = HEADER, rows: list[str] = ROWS) -> str:
    return "\r\n".join([header, *rows]) + "\r\n"


def test_a_bom_and_all_empty_rows_are_accepted(tmp_path: Path) -> None:
    blank = "," * HEADER.count(",")
    text = "\ufeff" + _csv(rows=[blank, ROWS[0], "", blank, *ROWS[1:], blank])
    result = read(tmp_path, "calendar.csv", text)
    assert (result.problems, result.rejected) == ([], {})
    expected = C.read_calendar(EXAMPLES / "calendar.yaml", EXAMPLE_PERSONA, None, check_files=False).calendar
    assert result.calendar == expected


@pytest.mark.parametrize(
    ("header", "field", "reason"),
    [
        (HEADER.replace(",outfit,", ",outfits,"), "outfit", "is a required column"),
        (HEADER.replace(",outfit,", ",outfits,"), "outfits", "is not a column of the CSV format"),
        (HEADER + ",mood", "mood", "is not a column of the CSV format"),
        (HEADER.replace(",platform,", ",idea,"), "idea", "is named 2 times in the header row (columns 7 and 8)"),
    ],
    ids=["missing", "renamed", "unknown", "repeated"],
)
def test_a_bad_header_rejects_the_file_naming_the_column(tmp_path: Path, header: str, field: str, reason: str) -> None:
    result = read(tmp_path, "calendar.csv", _csv(header))
    assert result.calendar is None
    assert any(p.field == field and p.reason.startswith(reason) for p in result.problems), result.problems


def test_mixed_week_ids_are_reported_by_spreadsheet_row(tmp_path: Path) -> None:
    rows = list(ROWS)
    rows[2] = rows[2].replace("2026-W40", "2026-W41", 1)
    rows[4] = rows[4].replace("2026-W40", "", 1)
    result = read(tmp_path, "calendar.csv", _csv(rows=["", *rows]))  # a blank row 2: the Posts are rows 3-7
    assert result.calendar is None
    reasons = [p.reason for p in result.problems if p.field == "week"]
    assert len(reasons) == 2
    assert reasons[0].startswith("row 5 has the Week_Id '2026-W41', but the first Post row (row 3) has '2026-W40'")
    assert reasons[1].startswith("row 7 has no Week_Id, but the first Post row (row 3) has '2026-W40'")


def test_csv_post_problems_carry_the_spreadsheet_row(tmp_path: Path) -> None:
    rows = list(ROWS)
    rows[1] = rows[1].replace(",5,", ",25,")  # tue-street: 25 Slides
    result = read(tmp_path, "calendar.csv", _csv(rows=rows))
    assert list(result.rejected) == ["tue-street"]
    [problem] = result.rejected["tue-street"]
    assert (problem.field, problem.line) == ("tue-street.slides", 3)
    assert "25" in problem.reason and "2 to 20" in problem.reason


# ---- the YAML-only rules (the two list encodings, §M2) ----


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"dialogue": ["one line", "two\nlines"], "voice": "voice/clip.wav"}, "fri-dance.dialogue[2]"),
        ({"looks": ["looks/a;b.safetensors"]}, "fri-dance.looks[1]"),
        ({"looks": [{"lora": "looks/a@2.safetensors", "strength": 0.5}]}, "fri-dance.looks[1].lora"),
        ({"colour": "ochre"}, "fri-dance.colour"),
    ],
    ids=["dialogue-line-break", "lora-semicolon", "lora-at", "unknown-key"],
)
def test_yaml_values_that_csv_cannot_hold_are_rejected(tmp_path: Path, change: dict, field: str) -> None:
    reel = {**EXTERNAL_REEL, **change}
    if "dialogue" in change:
        del reel["mode"]
        reel["source_post"] = "mon-cafe"
        write(tmp_path / "voice" / "clip.wav", "x")
    still = {"id": "mon-cafe", "type": "single", "date": "2026-09-28", "location": "corner-cafe", "outfit": "linen-set"}
    result = read(tmp_path, "calendar.yaml", dump_yaml({"week": "2026-W40", "posts": [still, reel]}))
    assert list(result.rejected) == ["fri-dance"]
    assert [p.field for p in result.rejected["fri-dance"]] == [field]
    assert [p.id for p in result.calendar.posts] == ["mon-cafe"]


def test_a_date_outside_the_week_warns_without_rejecting(tmp_path: Path) -> None:
    reel = {**EXTERNAL_REEL, "date": "2026-10-05"}
    result = read(tmp_path, "calendar.yaml", dump_yaml({"week": "2026-W40", "posts": [reel]}))
    assert (result.problems, result.rejected) == ([], {})
    assert [(w.field, w.reason) for w in result.warnings] == [
        ("fri-dance.date", "2026-10-05 is outside 2026-W40, the calendar's week")
    ]


# ---- one calendar file per week (Req 5.19) ----


def _tree(folder: Path) -> dict[str, bytes]:
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def test_two_calendar_files_for_one_week_are_both_named_and_nothing_is_written(tmp_path: Path) -> None:
    folder = C.week_folder(tmp_path, "2026-W40")
    shutil.copy(EXAMPLES / "calendar.yaml", write(folder / "calendar.yaml", ""))
    shutil.copy(EXAMPLES / "calendar.csv", write(folder / "calendar.csv", ""))
    before = _tree(tmp_path)
    with pytest.raises(ValidationError) as caught:
        C.find_calendar(tmp_path, "2026-W40")
    assert [p.path for p in caught.value.problems] == [str(folder / "calendar.yaml"), str(folder / "calendar.csv")]
    assert all("is one of 2 calendar files for 2026-W40" in p.reason for p in caught.value.problems)
    assert _tree(tmp_path) == before
    with pytest.raises(ValidationError, match="holds no calendar.yaml, calendar.yml or calendar.csv"):
        C.find_calendar(tmp_path, "2026-W41")


# ---- the commands (Req 4.14, 5.11, 5.12, 5.17) ----


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An Influencer home holding the Persona_Example as mara-lind, with the files it names."""
    root = tmp_path / "home" / "mara-lind"
    shutil.copy(EXAMPLES / "persona.yaml", write(root / "persona.yaml", ""))
    for relative in (EXAMPLE_PERSONA.hero_image, *EXAMPLE_PERSONA.reference_pack, EXAMPLE_PERSONA.grade.lut):
        write(root / relative, "x")
    return tmp_path / "home"


@pytest.mark.parametrize("as_csv", [False, True], ids=["yaml", "csv"])
def test_new_calendar_writes_a_skeleton_that_calendar_check_accepts(home: Path, as_csv: bool, capsys) -> None:
    argv = ["--home", str(home), "new-calendar", "--week", "2026-W40"] + (["--csv"] if as_csv else [])
    assert cli.main(argv) == Exit.OK
    target = home / "mara-lind" / "weeks" / "2026-W40" / ("calendar.csv" if as_csv else "calendar.yaml")
    assert capsys.readouterr().out == f"wrote {target}\n"
    result = C.read_calendar(target, EXAMPLE_PERSONA, home / "mara-lind")
    assert result.calendar == C.skeleton_calendar("2026-W40", EXAMPLE_PERSONA)
    assert result.calendar.posts[0].date == "2026-09-28"  # the Monday
    assert cli.main(["--home", str(home), "calendar", "check", "--week", "2026-W40"]) == Exit.OK
    assert capsys.readouterr() == (f"{target}: 1 of 1 Posts accepted\n", "")


def test_new_calendar_refuses_an_invalid_week_and_an_existing_file(home: Path, capsys) -> None:
    before = _tree(home)
    assert cli.main(["--home", str(home), "new-calendar", "--week", "2025-W53"]) == Exit.USAGE
    assert capsys.readouterr().err == "command line: --week: '2025-W53' names week 53, but 2025 has the ISO weeks 01 to 52\n"
    assert _tree(home) == before
    existing = write(home / "mara-lind" / "weeks" / "2026-W40" / "calendar.yml", "week: 2026-W40\n")
    before = _tree(home)
    assert cli.main(["--home", str(home), "new-calendar", "--week", "2026-W40", "--csv"]) == Exit.USAGE
    assert capsys.readouterr().err == f"{existing}: already exists, so new-calendar writes no calendar for 2026-W40\n"
    assert _tree(home) == before


def test_calendar_check_reports_a_rejected_post_and_exits_1(home: Path, capsys) -> None:
    text = (EXAMPLES / "calendar.yaml").read_text(encoding="utf-8").replace("slides: 5", "slides: 25")
    path = write(home / "mara-lind" / "weeks" / "2026-W40" / "calendar.yaml", text)
    assert cli.main(["--home", str(home), "calendar", "check", "--week", "2026-W40"]) == Exit.USAGE
    out, err = capsys.readouterr()
    assert out == f"{path}: 4 of 5 Posts accepted\n"
    assert err.startswith(f"{path}: tue-street.slides: 25 is out of range")


def test_persona_check_passes_the_example_and_rejects_a_missing_file(home: Path, capsys) -> None:
    path = home / "mara-lind" / "persona.yaml"
    assert cli.main(["--home", str(home), "persona", "check"]) == Exit.OK
    assert capsys.readouterr() == (f"{path}: the Persona_File is valid\n", "")
    (home / "mara-lind" / EXAMPLE_PERSONA.hero_image).unlink()
    assert cli.main(["--home", str(home), "persona", "check"]) == Exit.USAGE
    out, err = capsys.readouterr()
    assert out == "" and err.startswith(f"{path}: hero_image: 'refs/hero.png' does not exist"), err
