"""The Safety_Guard and its committed term lists (design §A5, §M14; Req 4.4, 26.8, 26.17, 27.5).

One pure matcher for every text the pipeline checks, so each caller rejects exactly the same things: the
Persona_Reader runs check_tree over a parsed Persona_File (Req 4.4), plan validation over each LLM response
(Req 6.5), the Training_Kit over dataset captions (Req 18.6, 18.10), the grid stage over Test_Grid prompts
(Req 20.7), and the Config_Checker over every Config_Lists line and node pack name, with both lists
(Req 26.8).

- Normalisation: unicodedata.normalize("NFKC", text).casefold(). Words are maximal runs of [^\\W_], that
  is letters or digits; every other character separates words.
- Terms: a term matches when its own words appear as consecutive words of the text. "face swap" therefore
  matches "Face_Swap" and "face-swap" but not "faceswap", and "kid" does not match "kidding".
- Ages: a number of one to three digits written with an age word ("19 year old", "22yo", "aged 19") is
  rejected when it is below 25.

Each TermList is compiled once, into a dict from its terms' first words to the terms that start with
them, so a check is one pass over the text's words with one dict lookup per word; no regex is built from
a list. Both age patterns run in time linear in the length of the text.

Standard library only. Only the loaders read files; nothing here writes one or opens a connection.
"""
from __future__ import annotations

import functools
import os
import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

from influencer.errors import Problem, ValidationError

SAFETY_DIR = Path(__file__).resolve().parents[2] / "config" / "influencer" / "safety"
MINOR_TERMS = "minor-terms.txt"  # the Safety_Guard's minor-coded words, phrases and spelled-out ages
RESOURCE_TERMS = "resource-terms.txt"  # swap, deepfake and age-slider resources: the Config_Checker only

AGE_LIMIT = 25  # a numeric age below this is rejected (Req 27.5)

_WORD = re.compile(r"[^\W_]+")

# The two age patterns of design §A5, run on normalised text. The first is the design's, verbatim: digits,
# then an age word ("19 year old", "19-years-old", "19 yrs", "19yo", "19 y/o", "19 y.o.").
_AGE_AFTER = re.compile(
    r"(?<![\d.])(\d{1,3})[\s\-_]*(years?[\s\-_]*old|yrs?[\s\-_]*old|yrs?|y/?o|y\.o\.)(?![a-z0-9])"
)
# The second: "age" or "aged", an optional ":" or "=", then the digits ("aged 19", "age: 19", "age=19").
# The design writes the middle as \s*[:=]?\s*; (?:\s*[:=])?\s* matches the same strings, without the two
# adjacent \s* that backtrack quadratically over a long run of whitespace that no digit follows.
_AGE_BEFORE = re.compile(r"(?<![a-z0-9])aged?(?:\s*[:=])?\s*(\d{1,3})(?![\d])")
_AGE_PATTERNS = (_AGE_AFTER, _AGE_BEFORE)


def normalize(text: str) -> str:
    """NFKC, then casefold (design §A5)."""
    return unicodedata.normalize("NFKC", text).casefold()


def words(text: str) -> tuple[str, ...]:
    """The words of text after normalisation: its maximal runs of letters and digits, in order."""
    return tuple(_WORD.findall(normalize(text)))


class TermList:
    """The terms of one list in order, compiled for matching.

    A term whose words repeat an earlier term's (the same words after normalisation, as "Teen" after
    "teen" or "face-swap" after "face swap") matches exactly the same texts, so only its first spelling is
    kept. A term without a letter or digit could never match and is a ValueError.
    """

    __slots__ = ("path", "terms", "_starts")

    def __init__(self, terms: Iterable[str], path: str = "<terms>") -> None:
        kept: list[str] = []
        starts: dict[str, list[tuple[tuple[str, ...], str]]] = {}
        for term in terms:
            term_words = words(term)
            if not term_words:
                raise ValueError(f"{path}: the term {term!r} has no letter or digit, so it can never match")
            entries = starts.setdefault(term_words[0], [])
            if all(known != term_words for known, _ in entries):
                entries.append((term_words, term))
                kept.append(term)
        self.path = path
        self.terms = tuple(kept)
        self._starts = {first: tuple(entries) for first, entries in starts.items()}

    def __repr__(self) -> str:
        return f"TermList({self.path!r}, {len(self.terms)} terms)"

    def matches(self, text_words: tuple[str, ...]) -> Iterator[tuple[int, str]]:
        """(word index, term) for every place where a term's words are consecutive words of text_words,
        in text order, and at one place in list order."""
        starts = self._starts
        for index, word in enumerate(text_words):
            for term_words, term in starts.get(word, ()):
                if text_words[index : index + len(term_words)] == term_words:
                    yield index, term


class SafetyGuard:
    """Term lists and, unless ages is False, the numeric age patterns (design §A5)."""

    __slots__ = ("term_lists", "ages")

    def __init__(self, term_lists: Iterable[TermList], *, ages: bool = True) -> None:
        self.term_lists = tuple(term_lists)
        self.ages = ages

    def __repr__(self) -> str:
        return f"SafetyGuard({list(self.term_lists)!r}, ages={self.ages})"

    def check_text(self, text: str) -> list[str]:
        """What the guard rejects in text, in the order it appears there and each once: a term as its list
        writes it, an age under 25 as the normalised text that matched ("19 year old", "aged 19"). An empty
        list means the text passes."""
        normal = normalize(text)
        spans = [(match.start(), match.group()) for match in _WORD.finditer(normal)]
        text_words = tuple(word for _, word in spans)
        hits: list[tuple[int, str]] = []
        for term_list in self.term_lists:
            hits += ((spans[index][0], term) for index, term in term_list.matches(text_words))
        if self.ages:
            for pattern in _AGE_PATTERNS:
                hits += (
                    (match.start(), match.group())
                    for match in pattern.finditer(normal)
                    if int(match.group(1)) < AGE_LIMIT
                )
        hits.sort(key=lambda hit: hit[0])  # stable: at one place, list order and then the age patterns
        return list(dict.fromkeys(found for _, found in hits))

    def check_tree(self, value: object, path: str = "") -> list[tuple[str, str]]:
        """(field path, matched term or age text) for every string in value, mapping keys included, in
        document order; see strings() for the paths. An empty list means every string passes."""
        return [(where, found) for where, text in strings(value, path) for found in self.check_text(text)]


def strings(value: object, path: str = "") -> Iterator[tuple[str, str]]:
    """(field path, string) for every string of a parsed structure, in document order: each mapping key
    before its value, list items in order.

    path names value itself ("" for a whole document). A mapping key and its value both get the key's
    field path, and a list item adds its item number, counted from 1: {"looks": [{"lora": "x"}]} gives
    ("looks", "looks"), ("looks[1].lora", "lora") and ("looks[1].lora", "x"). Numbers, booleans, None and
    dates hold no text. A container that holds itself, as a recursive YAML alias can, is not walked again
    below itself.
    """
    stack: list[tuple[object, str, frozenset[int]]] = [(value, path, frozenset())]
    while stack:
        node, where, above = stack.pop()
        if isinstance(node, str):
            yield where, node
            continue
        if isinstance(node, Mapping):
            children: list[tuple[object, str]] = []
            for key, item in node.items():
                field = f"{where}.{key}" if where else str(key)
                children += ((key, field), (item, field))
        elif isinstance(node, (list, tuple, set, frozenset)):
            items = sorted(node, key=repr) if isinstance(node, (set, frozenset)) else node
            children = [(item, f"{where}[{number}]") for number, item in enumerate(items, 1)]
        else:
            continue
        if id(node) in above:
            continue
        inside = above | {id(node)}
        stack.extend((child, child_path, inside) for child, child_path in reversed(children))


def load_term_list(path: str | os.PathLike[str]) -> TermList:
    """One committed term list (design §M14): UTF-8, one term per line, and "#" starts a comment.

    Blank lines and comments are dropped and the terms keep file order; a repeated term keeps its first
    spelling (see TermList). A missing or unreadable file, a file that is not UTF-8, a term without a letter
    or digit, and a list with no terms raise one ValidationError whose Problems name the file (Req 26.17).
    """
    shown = os.fspath(path)
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        raise ValidationError([Problem(shown, None, None, "the term list is missing")]) from None
    except OSError as error:
        reason = f"cannot read the term list: {error.strerror or error}"
        raise ValidationError([Problem(shown, None, None, reason)]) from None
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        line = data.count(b"\n", 0, error.start) + 1
        raise ValidationError([Problem(shown, None, line, "the term list is not UTF-8 text")]) from None
    terms: list[str] = []
    problems: list[Problem] = []
    for number, line in enumerate(text.split("\n"), 1):
        term = line.split("#", 1)[0].strip()
        if not term:
            continue
        if words(term):
            terms.append(term)
        else:
            reason = f"the term {term!r} has no letter or digit, so it can never match"
            problems.append(Problem(shown, None, number, reason))
    if not terms:
        problems.append(Problem(shown, None, None, "the term list lists no terms"))
    if problems:
        raise ValidationError(problems)
    return TermList(terms, shown)


def load_guard(
    folder: str | os.PathLike[str] = SAFETY_DIR, *, resources: bool = False, ages: bool = True
) -> SafetyGuard:
    """The guard of the term lists in folder: minor-terms.txt, and also resource-terms.txt when resources
    is True (the Config_Checker's pair, Req 26.8). The Problems of both lists are raised together."""
    names = (MINOR_TERMS, RESOURCE_TERMS) if resources else (MINOR_TERMS,)
    term_lists: list[TermList] = []
    problems: list[Problem] = []
    for name in names:
        try:
            term_lists.append(load_term_list(Path(folder) / name))
        except ValidationError as error:
            problems += error.problems
    if problems:
        raise ValidationError(problems)
    return SafetyGuard(term_lists, ages=ages)


@functools.lru_cache(maxsize=None)
def default_guard() -> SafetyGuard:
    """The Safety_Guard of every text check: the committed minor-terms.txt and the age patterns, read and
    compiled once per process. A missing or empty list raises its ValidationError on every call."""
    return load_guard()


def check_text(text: str, guard: SafetyGuard | None = None) -> list[str]:
    """guard.check_text(text), with default_guard() when guard is None."""
    return (default_guard() if guard is None else guard).check_text(text)


def check_tree(value: object, path: str = "", guard: SafetyGuard | None = None) -> list[tuple[str, str]]:
    """(field path, matched term or age text) for every string in value, mapping keys included:
    guard.check_tree(value, path), with default_guard() when guard is None."""
    return (default_guard() if guard is None else guard).check_tree(value, path)
