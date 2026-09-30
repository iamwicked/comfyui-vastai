"""Tests of the caption rules, scripts/influencer/captions.py (task 13; design §A10 items 1-3, Decision 17;
Req 6.8, 6.9, 6.10).

- Property 13: add_disclosure leaves exactly one line equal to the disclosure line, the last one, and a
  second application changes nothing
- Property 14: an Instagram attempt passes exactly when the full caption text is at most 2,200 UTF-16 code
  units long and holds at most min(limit, 5) words starting with "#"
- the caption branches: a caption that is only the disclosure line, an empty caption, trailing blank lines,
  hashtags without a caption, hashtag limits 0 and 12, the length boundary, platform other, and flagged
  terms in a hashtag and in the disclosure line
"""
from __future__ import annotations

import dataclasses
import sys

import pytest
from hypothesis import given

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

import strategies_prompts as SP  # noqa: E402
from influencer import captions as C  # noqa: E402
from influencer.persona import Captions  # noqa: E402

DISCLOSURE = "AI-generated image"
MAX_UNITS = 2200  # Req 6.8
CAP = 5  # a hashtag limit above 5 counts as 5 (Req 6.8)


def utf16_units(text: str) -> int:
    """UTF-16 code units, counted by code point: 2 above U+FFFF, else 1."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def check(caption: str, hashtags: list[str], **settings: object) -> C.CaptionCheck:
    return C.check_caption(caption, hashtags, Captions(**settings), instagram=True)


# ---- Property 13 ----


# Feature: ai-influencer-pipeline, Property 13: Disclosure line idempotence
@given(case=SP.disclosure_cases())
def test_disclosure_line_idempotence(case: tuple[str, str]) -> None:
    caption, disclosure = case
    once = C.add_disclosure(caption, disclosure)
    lines = once.split("\n")
    assert [line for line in lines if line == disclosure] == [disclosure]
    assert lines[-1] == disclosure
    assert C.add_disclosure(once, disclosure) == once


# ---- Property 14 ----


# Feature: ai-influencer-pipeline, Property 14: Instagram caption limits
@given(case=SP.instagram_cases())
def test_instagram_caption_limits(case: tuple[str, str | None, list[str], int]) -> None:
    caption, disclosure, hashtags, limit = case
    # the full caption text of Req 6.8: the caption with its disclosure line, a blank line, the hashtags
    with_line = caption if disclosure is None else C.add_disclosure(caption, disclosure)
    full = with_line + ("\n\n" + " ".join(hashtags) if hashtags else "")
    assert C.full_text(caption, hashtags, disclosure) == full
    units = utf16_units(full)
    tags = sum(1 for word in full.split() if word.startswith("#"))
    assert (C.utf16_len(full), C.hashtag_count(full)) == (units, tags)
    result = check(caption, hashtags, disclosure=disclosure, hashtag_limit=limit)
    too_long, too_many = units > MAX_UNITS, tags > min(limit, CAP)
    assert (result.problems == ()) is (not too_long and not too_many)
    assert len(result.problems) == too_long + too_many
    assert result.review == ()


# ---- the disclosure line ----


@pytest.mark.parametrize(
    ("caption", "expected"),
    [
        (DISCLOSURE, DISCLOSURE),  # the only line is the disclosure line
        (f"  {DISCLOSURE}\t\r", DISCLOSURE),  # the same after stripping
        ("", DISCLOSURE),  # an empty caption
        ("\n \n", DISCLOSURE),
        ("Morning light.\n\n \t\n", f"Morning light.\n{DISCLOSURE}"),  # trailing blank lines
        (f"Line one\n{DISCLOSURE}\n\nLine two\n\n", f"Line one\n\nLine two\n{DISCLOSURE}"),
        (f"Line one\n{DISCLOSURE}", f"Line one\n{DISCLOSURE}"),  # already ends with it
        (f"ai-generated image\n{DISCLOSURE} too", f"ai-generated image\n{DISCLOSURE} too\n{DISCLOSURE}"),
    ],
    ids=["only-line", "padded", "empty", "blank", "trailing-blanks", "inside", "unchanged", "not-equal"],
)
def test_the_disclosure_line_ends_the_caption_once(caption: str, expected: str) -> None:
    assert C.add_disclosure(caption, DISCLOSURE) == expected
    assert check(caption, [], disclosure=DISCLOSURE).caption == expected


def test_without_a_disclosure_line_the_caption_stays_as_it_is() -> None:
    assert C.full_text("Morning light.\n\n", []) == "Morning light.\n\n"
    assert check("Morning light.\n\n", []).caption == "Morning light.\n\n"


# ---- the full caption text and Instagram's limits ----


def test_hashtags_without_a_caption() -> None:
    assert C.full_text("", ["#coffee", "#morning"], DISCLOSURE) == f"{DISCLOSURE}\n\n#coffee #morning"
    assert C.full_text("", ["#coffee"]) == "\n\n#coffee"  # the blank line counts: 9 units
    assert C.utf16_len(C.full_text("", ["#coffee"])) == 9
    assert check("", ["#coffee", "#morning"]).problems == ()


def test_utf16_length_and_hashtag_count() -> None:
    assert [C.utf16_len(t) for t in ("", "a", "é", "e\u0301", "\U0001f600", "\U0001f469\U0001f3fd\u200d\U0001f4bb")] == [
        0, 1, 1, 2, 2, 7,
    ]  # fmt: skip
    assert C.hashtag_count("#a #b c#d # x\n#e\t#f\u3000#g") == 6  # "#" alone counts, "c#d" does not


@pytest.mark.parametrize(
    ("limit", "allowed"), [(0, 0), (3, 3), (5, 5), (12, 5)], ids=["zero", "three", "five", "twelve"]
)
def test_the_hashtag_limit_counts_every_hash_word_and_stops_at_5(limit: int, allowed: int) -> None:
    for count in range(8):
        tags = [f"#tag{n}" for n in range(count)]
        problems = check("Morning light.", tags, hashtag_limit=limit).problems
        assert (problems == ()) is (count <= allowed), (limit, count)
        if problems:
            [problem] = problems
            assert problem.startswith(f"the full caption text holds {count} word"), problem
            assert problem.endswith(f"starting with '#'; the Persona_File's hashtag limit allows {allowed}"), problem
    # a word starting with "#" inside the caption counts too
    assert len(check("Morning #light", [], hashtag_limit=limit).problems) == (allowed == 0)


def test_the_length_limit_is_2200_utf16_code_units() -> None:
    assert check("\U0001f600" * 1100, []).problems == ()  # 1,100 emoji are 2,200 units
    [problem] = check("\U0001f600" * 1100 + "a", []).problems
    assert problem == "the full caption text is 2,201 UTF-16 code units long; Instagram takes at most 2,200"
    fitted = "a" * (MAX_UNITS - len(DISCLOSURE) - 1)  # with "\n" and the disclosure line: 2,200
    assert check(fitted, [], disclosure=DISCLOSURE).problems == ()
    assert len(check(fitted + "a", [], disclosure=DISCLOSURE).problems) == 1
    both = check("a" * MAX_UNITS, ["#a"], hashtag_limit=0).problems  # "\n\n#a" makes 2,204
    assert [problem.split(";")[0] for problem in both] == [
        "the full caption text is 2,204 UTF-16 code units long",
        "the full caption text holds 1 word starting with '#'",
    ]


def test_platform_other_has_no_instagram_limits() -> None:
    result = C.check_caption("a" * 3000, ["#a"] * 9, Captions(disclosure=DISCLOSURE, hashtag_limit=0), instagram=False)
    assert result.problems == ()
    assert result.caption == "a" * 3000 + "\n" + DISCLOSURE


# ---- flagged terms ----


def test_flagged_terms_in_a_hashtag_and_in_the_disclosure_line_fill_review_only() -> None:
    settings = Captions(disclosure="Made with AI, no miracle cure", flagged_terms=("detox", "Miracle Cure", "!!!", "glow"))
    caption = "Sunday reset, #detoxing slowly."  # "detoxing" is another word, so "detox" is not in it
    result = C.check_caption(caption, ["#DETOX", "#sunday"], settings, instagram=True)
    assert result.review == ("Miracle Cure", "detox")  # the caption's first, then the hashtags'
    assert result.caption == C.add_disclosure(caption, settings.disclosure)  # unchanged by the hits
    assert result.problems == ()  # a hit never fails the attempt
    unflagged = C.check_caption(caption, ["#DETOX", "#sunday"], dataclasses.replace(settings, flagged_terms=()), instagram=True)
    assert (unflagged.caption, unflagged.problems, unflagged.review) == (result.caption, result.problems, ())


def test_flagged_terms_match_whole_words_in_any_case() -> None:
    terms = ("anti-aging", "weight loss", "cure")
    assert C.flagged_terms("Anti_Aging serum", ["#WeightLoss", "#weight_loss", "#cured"], terms) == (
        "anti-aging", "weight loss",
    )  # fmt: skip
    assert C.flagged_terms("No claims here.", ["#cure"], terms) == ("cure",)
    assert C.flagged_terms("A cure! A CURE!", [], terms) == ("cure",)  # each term once
    assert C.flagged_terms("anything", ["#x"], ("", "!!!", "\U0001f600")) == ()  # no letter or digit: skipped
