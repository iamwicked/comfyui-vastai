"""Tests of the Prompt_Sheet, scripts/influencer/prompt_sheet.py (task 10, design §M3; Req 6.12, 11.7,
11.10).

- Property 11: a printed Prompt_Sheet reads back with the same entries and fields, every text value
  character for character
- the design's example reads; YAML-ambiguous and multi-line text reads back; left-out keys read as their
  defaults; every structural Problem is reported at once, with its field
- Picks of 0, 5 and "2" are Problems naming the Post, the Slide, the value and the valid numbers, and the
  other Slides keep their Picks; a null Pick follows the Candidate count
- an entry with another Slide count waits for planning; an entry whose Post left the calendar is kept
"""
from __future__ import annotations

import dataclasses
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import given

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer.errors import ValidationError  # noqa: E402
from influencer.prompt_sheet import (  # noqa: E402
    CalendarPost,
    Entry,
    Planned,
    PromptSheet,
    Slide,
    pick_candidate,
    print_prompt_sheet,
    read_prompt_sheet,
)
from strategies_state import prompt_sheets  # noqa: E402


def typed(value: object) -> object:
    """value with the type of every part kept, so that True and 1, or 1 and 1.0, never compare equal."""
    if dataclasses.is_dataclass(value):
        fields = dataclasses.fields(value)
        return type(value).__name__, tuple(typed(getattr(value, field.name)) for field in fields)
    if isinstance(value, (list, tuple)):
        return type(value).__name__, tuple(typed(item) for item in value)
    return type(value).__name__, value


def write(folder: Path, text: str, name: str = "prompts.yaml") -> Path:
    path = folder / name
    path.write_bytes(text.encode("utf-8"))
    return path


# ---- Property 11 ----


# Feature: ai-influencer-pipeline, Property 11: Prompt_Sheet round trip
# **Validates: Requirements 6.12**
@given(sheet=prompt_sheets())
def test_prompt_sheet_round_trip(sheet):
    with tempfile.TemporaryDirectory() as folder:
        back = read_prompt_sheet(write(Path(folder), print_prompt_sheet(sheet)))
    assert typed(back) == typed(sheet)


# ---- Reader and Printer ----

DESIGN_EXAMPLE = """\
format: influencer-prompt-sheet/1
week: 2026-W40
entries:
  - post: tue-street
    approved: false                 # only the user sets true (Req 6.16, 6.17)
    needs_manual_prompt: false      # set after the 4th failed attempt (Req 6.14)
    review: []                      # flagged terms found in caption or hashtags (Req 6.10)
    planned: {model: "qwen3:8b", at: "2026-09-28T09:12:31Z", attempts: 1}
    slides:
      - {scene: "she stops at the bakery window, holding a paper cup", alt: "A woman in a cream sweater looks into a bakery window.", pick: null}
      - {scene: "…", alt: "…", pick: 2}
    caption: |-
      Tuesdays are for the long way round.
      AI-generated character — not a real person.
    hashtags: ["#slowliving", "#filmphotography"]
    motion: null                    # reel Posts only
    raw_response: null              # last raw response that passed the Safety_Guard, kept only with needs_manual_prompt
"""

DESIGN_SHEET = PromptSheet(
    "2026-W40",
    [
        Entry(
            "tue-street",
            planned=Planned("qwen3:8b", "2026-09-28T09:12:31Z", 1),
            slides=[
                Slide(
                    "she stops at the bakery window, holding a paper cup",
                    "A woman in a cream sweater looks into a bakery window.",
                ),
                Slide("…", "…", 2),
            ],
            caption="Tuesdays are for the long way round.\nAI-generated character — not a real person.",
            hashtags=["#slowliving", "#filmphotography"],
        )
    ],
)


def test_the_design_example_reads_and_prints_back(tmp_path):
    sheet = read_prompt_sheet(write(tmp_path, DESIGN_EXAMPLE))
    assert typed(sheet) == typed(DESIGN_SHEET)
    assert typed(read_prompt_sheet(write(tmp_path, print_prompt_sheet(sheet), "again.yaml"))) == typed(sheet)


TRICKY = (
    "no", "yes", "on", "2026", "1e3", "0o17", "~", "null", "", "#x", "a: b", "a #b", " lead", "trail ",
    "line 1\nline 2", "tab\there", "cr\r\nlf", 'say "hi"', "back\\slash", "\U0001f600",
    "\U0001f469\U0001f3fd\u200d\U0001f4bb",
)


def test_yaml_ambiguous_and_multi_line_text_reads_back_character_for_character(tmp_path):
    caption = "Tuesdays are for the long way round. \U0001f600\nAI-generated character — not a real person."
    entry = Entry(
        "tue-street",
        review=list(TRICKY),
        slides=[Slide(text, text) for text in TRICKY],
        caption=caption,
        hashtags=list(TRICKY),
        motion="~",
        raw_response="no",
    )
    sheet = PromptSheet("2026-W40", [entry])
    text = print_prompt_sheet(sheet)
    assert "  caption: |-\n    Tuesdays are for the long way round. \U0001f600\n" in text
    for scalar in ('"no"', '"2026"', '"~"', '"#x"', '"a: b"', '""'):
        assert f"  - scene: {scalar}\n" in text
    assert typed(read_prompt_sheet(write(tmp_path, text))) == typed(sheet)


def test_left_out_keys_read_as_their_defaults(tmp_path):
    path = write(
        tmp_path,
        "format: influencer-prompt-sheet/1\nweek: 2026-W40\nentries:\n"
        "  - post: tue-street\n    slides:\n      - {scene: s, alt: a}\n",
    )
    assert read_prompt_sheet(path) == PromptSheet("2026-W40", [Entry("tue-street", slides=[Slide("s", "a")])])


def test_every_problem_is_reported_with_its_field(tmp_path):
    path = write(
        tmp_path,
        """\
format: influencer-prompt-sheet/2
week: 2026-W40
entries:
  - post: tue-street
    aproved: true
    approved: "yes"
    slides:
      - {scene: 3, pick: 1}
    hashtags: "#one"
  - post: tue-street
    planned: {model: qwen3:8b, at: now, attempts: 0}
""",
    )
    with pytest.raises(ValidationError) as caught:
        read_prompt_sheet(path)
    problems = caught.value.problems
    assert {problem.path for problem in problems} == {str(path)}
    assert [problem.field for problem in problems] == [
        "format",
        "entries[1].aproved",
        "entries[1].approved",
        "entries[1].slides[1].scene",
        "entries[1].slides[1].alt",
        "entries[1].hashtags",
        "entries[2].post",
        "entries[2].planned.attempts",
    ]
    reasons = {problem.field: problem.reason for problem in problems}
    assert reasons["format"] == 'is "influencer-prompt-sheet/2"; this version reads influencer-prompt-sheet/1'
    assert reasons["entries[1].approved"] == "Post 'tue-street': must be true or false, not \"yes\""
    assert reasons["entries[2].post"].startswith("Post 'tue-street' has a second entry; entries[1] is its first")


def test_the_printer_refuses_a_sheet_the_reader_would_reject():
    with pytest.raises(ValueError, match=r"entries\[1\]\.post: must be a Post id"):
        print_prompt_sheet(PromptSheet("2026-W40", [Entry("")]))


# ---- the sheet against the calendar ----


@pytest.mark.parametrize(
    ("pick", "candidates", "expected"),
    [(None, 1, 1), (None, 2, None), (None, 4, None), (1, 1, 1), (2, 4, 2), (4, 4, 4)],
)
def test_a_pick_names_the_candidate_and_null_follows_the_candidate_count(pick, candidates, expected):
    assert pick_candidate(pick, candidates) == expected


@pytest.mark.parametrize("pick", [0, 5, "2", 2.0, True, [2]])
def test_a_pick_that_is_not_a_candidate_number_is_refused(pick):
    with pytest.raises(ValueError, match="is not a Candidate number; the valid Candidate numbers are 1, 2, 3, 4$"):
        pick_candidate(pick, 4)


def test_picks_of_0_5_and_text_are_problems_and_hold_only_their_slides(tmp_path):
    slides = [Slide("a", "a", 0), Slide("b", "b", 5), Slide("c", "c", "2"), Slide("d", "d", 3), Slide("e", "e")]
    path = write(tmp_path, print_prompt_sheet(PromptSheet("2026-W40", [Entry("tue-street", slides=slides)])))
    sheet = read_prompt_sheet(path)  # the Reader keeps each value as recorded
    assert [slide.pick for slide in sheet.entries[0].slides] == [0, 5, "2", 3, None]
    check = sheet.check([CalendarPost("tue-street", slides=5, candidates=4)], path=str(path))
    assert check.picks == {("tue-street", 4): 3, ("tue-street", 5): None}  # the other Slides go on
    assert [(problem.path, problem.field, problem.reason) for problem in check.problems] == [
        (
            str(path),
            f"entries[1].slides[{number}].pick",
            f"Post 'tue-street', Slide {number}: pick {shown} is not a Candidate number; "
            "the valid Candidate numbers are 1, 2, 3, 4",
        )
        for number, shown in ((1, "0"), (2, "5"), (3, '"2"'))
    ]
    assert check.planning == {}
    assert check.left_calendar == ()


def test_another_slide_count_waits_for_planning_and_an_entry_whose_post_left_is_kept(tmp_path):
    old = Entry("old-post", approved=True, slides=[Slide("b", "b", 1)], caption="kept")
    sheet = PromptSheet(
        "2026-W40",
        [Entry("tue-street", slides=[Slide("a", "a")]), old, Entry("wed-cafe", slides=[Slide("c", "c")])],
    )
    path = write(tmp_path, print_prompt_sheet(sheet))
    calendar = [CalendarPost("tue-street", 3, 1), CalendarPost("wed-cafe", 1, 1), CalendarPost("thu-new", 1, 1)]
    check = read_prompt_sheet(path).check(calendar, path=str(path))
    assert check.planning == {
        "tue-street": "its Prompt_Sheet entry has 1 Slide and the calendar gives it 3 Slides; "
        "re-plan it (plan --week 2026-W40 --replan tue-street)",
        "thu-new": "has no Prompt_Sheet entry yet; plan it (plan --week 2026-W40)",
    }
    assert check.picks == {("wed-cafe", 1): 1}
    assert check.problems == ()
    assert check.left_calendar == ("old-post",)
    # Kept, not deleted: the entry prints and reads back unchanged.
    again = read_prompt_sheet(write(tmp_path, print_prompt_sheet(read_prompt_sheet(path)), "again.yaml"))
    assert [entry.post for entry in again.entries] == ["tue-street", "old-post", "wed-cafe"]
    assert again.entry("old-post") == old
