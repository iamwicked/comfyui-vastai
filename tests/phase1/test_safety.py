"""Tests of the Safety_Guard, scripts/influencer/safety.py, and its committed term lists (task 7, design
§A5, §M14; Req 4.4, 26.8, 26.17, 27.5).

- Property 38: a term of either committed list is rejected as consecutive whole words in any letter case
  with any separators, and accepted when it appears only inside longer words
- Property 39: a number written with an age word is rejected exactly when it is below 25
- the design's matching examples, the term-list loader, and the field paths check_tree reports
- the committed lists pass every string of the Persona_Example in design.md, and long text is checked in
  linear time
"""
from __future__ import annotations

import re
import sys
import time
import unicodedata

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import safety  # noqa: E402
from influencer.errors import ValidationError  # noqa: E402

DESIGN = REPO / ".kiro" / "specs" / "ai-influencer-pipeline" / "design.md"

# Both committed lists and the age patterns: the Config_Checker's guard, and Property 38's "either list".
BOTH = safety.load_guard(resources=True)
TERMS = tuple(term for term_list in BOTH.term_lists for term in term_list.terms)


# ---- words as design §A5 defines them, computed here without safety.py ----


def design_words(text: str) -> tuple[str, ...]:
    """The maximal runs of [^\\W_] in the NFKC-casefolded text."""
    return tuple(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", text).casefold()))


TERM_WORDS = {term: design_words(term) for term in TERMS}


def terms_in(text: str) -> set[str]:
    """The terms whose words are consecutive words of text, found the slow and obvious way."""
    text_words = design_words(text)
    return {
        term
        for term, words in TERM_WORDS.items()
        if any(text_words[i : i + len(words)] == words for i in range(len(text_words) - len(words) + 1))
    }


# ---- a term-free alphabet ----

_TERM_LETTERS = {char for words in TERM_WORDS.values() for word in words for char in word}
# Letters that no term holds, even after NFKC and casefolding, so text made of them never completes a term
# word. They include no digit and no "y", so they never write an age either.
SAFE_LETTERS = "".join(
    char for char in "qzQZøØλΛжЖ漢" if not set(unicodedata.normalize("NFKC", char).casefold()) & _TERM_LETTERS
)
assert len(SAFE_LETTERS) >= 4, f"only {SAFE_LETTERS!r} are in no term; add candidate letters"
# Characters that separate words: punctuation, spaces, a line break, an emoji, and marks that NFKC rewrites.
MARKS = " \t\n,;:!?()'\"—…\u00a0，。\U0001f600"
SEPARATORS = " _-./"  # between a term's words (Property 38)


def flip_case(draw: st.DrawFn, text: str) -> str:
    """text with each character upper- or lower-cased at random."""
    flips = draw(st.lists(st.booleans(), min_size=len(text), max_size=len(text)))
    return "".join(char.upper() if flip else char.lower() for char, flip in zip(text, flips))


_separator = st.text(SEPARATORS, min_size=1, max_size=3)
_glue = st.text(SAFE_LETTERS, min_size=1, max_size=3)
_filler = st.lists(
    st.one_of(st.text(SAFE_LETTERS, min_size=1, max_size=6), st.text(MARKS, min_size=1, max_size=3)), max_size=4
).map("".join)


@st.composite
def phrases(draw: st.DrawFn, words: tuple[str, ...]) -> str:
    """words in random letter case, joined by runs of separators."""
    phrase = flip_case(draw, words[0])
    for word in words[1:]:
        phrase += draw(_separator) + flip_case(draw, word)
    return phrase


@st.composite
def surrounded(draw: st.DrawFn, core: str) -> str:
    """core inside term-free text, parted from it by separators."""
    before, after = draw(_filler), draw(_filler)
    return (before + draw(_separator) if before else "") + core + (draw(_separator) + after if after else "")


# ---- the properties ----


# Feature: ai-influencer-pipeline, Property 38: Safety_Guard matches whole words only
@given(term=st.sampled_from(TERMS), data=st.data())
def test_safety_guard_matches_whole_words_only(term: str, data: st.DataObject) -> None:
    """A term's words as consecutive whole words, in any case and with any separators, are rejected; the
    same words with letters glued on are not, and nothing is found that the design's definition does not
    find.

    **Validates: Requirements 4.4, 26.8, 27.5**
    """
    phrase = data.draw(phrases(TERM_WORDS[term]), label="phrase")
    positive = data.draw(surrounded(phrase), label="positive")
    found = BOTH.check_text(positive)
    assert term in found
    assert set(found) == terms_in(positive)

    glue = data.draw(st.sampled_from(("before", "after", "both")), label="glue")
    glued = (data.draw(_glue) if glue != "after" else "") + phrase + (data.draw(_glue) if glue != "before" else "")
    negative = data.draw(surrounded(glued), label="negative")
    found = BOTH.check_text(negative)
    assert term not in found
    # Accepted when nothing else matches. A shorter term left whole inside the glued one ("one year old"
    # in "qtwenty-one year old") is still found.
    assert set(found) == terms_in(negative)


# The age forms of Property 39, as the §A5 patterns read them: {n} is the number, {s} a run that the first
# pattern allows between its parts ([\s\-_]*), and {w} whitespace (\s*).
AGE_FORMS = (
    "{n}{s}year{s}old", "{n}{s}years{s}old", "{n}{s}yr{s}old", "{n}{s}yrs{s}old", "{n}{s}yr", "{n}{s}yrs",
    "{n}{s}yo", "{n}{s}y/o", "{n}{s}y.o.",
    "age{w}{n}", "aged{w}{n}", "age{w}:{w}{n}", "aged{w}:{w}{n}", "age{w}={w}{n}", "aged{w}={w}{n}",
)  # fmt: skip
_age_spacing = st.sampled_from(("", " ", "-", "_", "  ", " - ", "_-", "\t", "\n"))
_whitespace = st.sampled_from(("", " ", "  ", "\t", "\n", " \t "))


@st.composite
def age_texts(draw: st.DrawFn, n: int, form: str) -> str:
    """form with n written in, random spacing and letter case, inside term-free text. Next to the form is a
    character the patterns' lookarounds allow: before it never a letter, a digit or ".", after it never a
    letter or digit."""
    text = ""
    for piece in re.split(r"(\{[nsw]\})", form):
        if piece == "{n}":
            text += str(n)
        elif piece == "{s}":
            text += draw(_age_spacing)
        elif piece == "{w}":
            text += draw(_whitespace)
        else:
            text += flip_case(draw, piece)
    before, after = draw(_filler), draw(_filler)
    if before:
        text = before + draw(st.sampled_from(" \t\n,;:(\"'")) + text
    if after:
        text += draw(st.sampled_from(" \t\n,;.!?)\"'")) + after
    return text


# Feature: ai-influencer-pipeline, Property 39: Numeric ages are rejected exactly below 25
@given(n=st.one_of(st.integers(0, 999), st.integers(20, 30)), form=st.sampled_from(AGE_FORMS), data=st.data())
def test_numeric_ages_are_rejected_exactly_below_25(n: int, form: str, data: st.DataObject) -> None:
    """The default Safety_Guard rejects a number written in any age form exactly when it is below 25, and
    then reports that age.

    **Validates: Requirements 27.5**
    """
    text = data.draw(age_texts(n, form), label="text")
    found = safety.check_text(text)
    assert bool(found) == (n < 25)
    if found:
        [age] = found
        assert re.search(rf"(?<!\d){n}(?!\d)", age)


# ---- the design's examples ----


def test_the_design_examples_of_whole_word_matching() -> None:
    guard = safety.SafetyGuard([safety.TermList(["face swap", "kid"])], ages=False)
    assert guard.check_text("a Face_Swap workflow") == ["face swap"]
    assert guard.check_text("face-swap") == ["face swap"]
    assert guard.check_text("ＦＡＣＥ\u3000ＳＷＡＰ") == ["face swap"]  # NFKC: fullwidth letters and space
    assert guard.check_text("faceswap") == []
    assert guard.check_text("just kidding") == []
    assert guard.check_text("a KID's room") == ["kid"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("24 years old", ["24 years old"]),
        ("25 years old", []),
        ("she is 22yo", ["22yo"]),
        ("a 19-year-old", ["19-year-old"]),
        ("Aged: 19", ["aged: 19"]),
        ("a 29-year-old woman", []),
        ("1999 years old", []),
        ("page 12, image 3", []),
    ],
)
def test_the_design_examples_of_age_matching(text: str, expected: list[str]) -> None:
    assert safety.check_text(text) == expected


# ---- the term-list loader (Req 26.17) ----


def test_the_loader_drops_comments_and_blank_lines_and_keeps_file_order(tmp_path) -> None:
    path = tmp_path / "terms.txt"
    path.write_text(
        "# a header comment\n\nteen\n   \n  # an indented comment\nface swap  # a trailing comment\n"
        "Teen\nface-swap\nkid\n",
        encoding="utf-8",
    )
    loaded = safety.load_term_list(path)
    assert loaded.terms == ("teen", "face swap", "kid")  # "Teen" and "face-swap" repeat earlier terms
    assert loaded.path == str(path)


def test_a_missing_list_is_a_problem_naming_the_file(tmp_path) -> None:
    with pytest.raises(ValidationError) as caught:
        safety.load_guard(tmp_path, resources=True)
    assert [(problem.path, problem.line, problem.reason) for problem in caught.value.problems] == [
        (str(tmp_path / "minor-terms.txt"), None, "the term list is missing"),
        (str(tmp_path / "resource-terms.txt"), None, "the term list is missing"),
    ]


@pytest.mark.parametrize(
    ("content", "line", "reason"),
    [
        ("# only\n\n   # comments\n", None, "the term list lists no terms"),
        ("", None, "the term list lists no terms"),
        ("kid\n--- # no word\n", 2, "the term '---' has no letter or digit, so it can never match"),
    ],
    ids=["only-comments", "empty", "wordless-term"],
)
def test_a_list_without_usable_terms_is_a_problem_naming_the_file(tmp_path, content, line, reason) -> None:
    path = tmp_path / "minor-terms.txt"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValidationError) as caught:
        safety.load_term_list(path)
    assert [(problem.path, problem.line, problem.reason) for problem in caught.value.problems] == [
        (str(path), line, reason)
    ]


# ---- check_tree ----


def test_check_tree_names_the_field_of_every_hit() -> None:
    persona = {
        "name": "Mara Lind",
        "age": 29,
        "sharp_decode": False,
        "looks": {"teen look": [{"lora": "looks/a.safetensors", "strength": 0.6}]},  # a mapping key
        "themes": ["city walks", "School-Uniform days"],  # a list item
        "a": {"b": [{"c": {"d": ["fine", {"e": "just a kid, 19yo"}]}}]},  # a deeply nested value
        "grade": None,
    }
    assert safety.check_tree(persona) == [
        ("looks.teen look", "teen"),
        ("themes[2]", "school uniform"),
        ("a.b[1].c.d[2].e", "kid"),
        ("a.b[1].c.d[2].e", "19yo"),
    ]
    assert safety.check_tree(persona["themes"], "themes") == [("themes[2]", "school uniform")]
    assert safety.check_tree({"name": "Mara", "age": 29, "tags": [1, 2.5, None, True]}) == []


def test_check_tree_walks_a_recursive_alias_once() -> None:
    assert safety.check_tree(yaml.safe_load("a: &loop [*loop, a teen]\n")) == [("a[2]", "teen")]


# ---- the Persona_Example (design §M1) ----


def _persona_example() -> object:
    """The YAML block that follows design.md's "### M1" heading, parsed; a skip when there is none."""
    if not DESIGN.is_file():
        pytest.skip(f"{DESIGN.relative_to(REPO)} is not in this checkout")
    lines = DESIGN.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"### M1\b", line)), None)
    if start is not None:
        for i in range(start + 1, len(lines)):
            if lines[i].startswith("#"):  # the next heading, before any YAML block
                break
            if lines[i].strip() == "```yaml":
                end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == "```"), None)
                if end is not None:
                    return yaml.safe_load("\n".join(lines[i + 1 : end]))
                break
    pytest.skip("design.md has no Persona_Example YAML block under its ### M1 heading")


def test_the_committed_lists_pass_the_persona_example() -> None:
    example = _persona_example()
    assert isinstance(example, dict) and "identity" in example  # the block really is the Persona_Example
    assert BOTH.check_tree(example) == []


# ---- speed ----


@pytest.mark.time_limit(60)
def test_long_text_is_checked_in_linear_time() -> None:
    """Long adversarial texts, among them the whitespace run after "age" over which the design's
    \\s*[:=]?\\s* backtracks quadratically (minutes at this length)."""
    texts = [
        "age" + " " * 100_000 + "x",
        "1" + " " * 100_000 + "x",
        "5 year" + "-" * 100_000 + "x",
        "twenty " * 50_000,  # the first word of twelve terms
        "kid " * 50_000,
    ]
    started = time.perf_counter()
    assert [BOTH.check_text(text) for text in texts] == [[], [], [], [], ["kid"]]
    assert time.perf_counter() - started < 5
