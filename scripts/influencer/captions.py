"""Caption rules: the disclosure line, Instagram's caption limits and flagged terms (design §A10 items 1-3,
Decision 17; Req 6.8, 6.9, 6.10).

    add_disclosure(caption, disclosure) -> str        the caption ending in the disclosure line, once
    full_text(caption, hashtags, disclosure) -> str   the full caption text Instagram's limits apply to
    utf16_len(text) -> int                            its length in UTF-16 code units
    hashtag_count(text) -> int                        its whitespace-split words that start with "#"
    instagram_problems(text, hashtag_limit) -> list   why a full caption text breaks the limits
    flagged_terms(caption, hashtags, terms) -> tuple  the Persona_File's flagged terms that a caption holds
    check_caption(caption, hashtags, settings, *, instagram) -> CaptionCheck   all of these for one attempt

- Lines are the parts between "\\n" characters. add_disclosure drops every line equal to the disclosure line
  d after stripping both, then the trailing blank lines, then appends d. The result has exactly one line
  equal to d and it is the last line, and applying it again changes nothing (Decision 17, Property 13).
- The full caption text is the caption with its disclosure line (when the Persona_File defines one), then
  "\\n\\n" and the hashtags joined by spaces when there are any. An Instagram attempt passes exactly when
  that text is at most 2,200 UTF-16 code units long (an emoji such as U+1F600 counts as 2) and holds at most
  min(hashtag_limit, 5) words starting with "#" (Req 6.8, Property 14).
- Flagged terms use the Safety_Guard's word matcher (safety.py): a term matches when its words are
  consecutive whole words of the text, in any letter case. The caption, with its disclosure line, is one
  text and each hashtag, its leading "#" stripped, is another. A term without a letter or digit can never
  match and is skipped. Hits go to the entry's review list and never fail the attempt or change the
  caption (Req 6.10).

Pure: no file, process or network access.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from influencer.persona import HASHTAG_CAP, Captions
from influencer.safety import SafetyGuard, TermList, words

INSTAGRAM_MAX_UTF16 = 2200  # the longest full caption text Instagram takes, in UTF-16 code units (Req 6.8)
HASHTAG_SEPARATOR = "\n\n"  # between the caption and its hashtags


def add_disclosure(caption: str, disclosure: str) -> str:
    """caption ending in the disclosure line, on its own line and exactly once (Req 6.9, Decision 17).

    Every line that equals the disclosure after stripping both is dropped, then every trailing blank line,
    and the disclosure is appended as the last line. A caption that already ends that way comes back
    unchanged. disclosure is one line, as the Persona_Reader guarantees."""
    wanted = disclosure.strip()
    lines = [line for line in caption.split("\n") if line.strip() != wanted]
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append(disclosure)
    return "\n".join(lines)


def full_text(caption: str, hashtags: Iterable[str], disclosure: str | None = None) -> str:
    """The full caption text of Req 6.8: the caption with its disclosure line (none when disclosure is
    None), then "\\n\\n" and the hashtags joined by single spaces when there are any."""
    text = caption if disclosure is None else add_disclosure(caption, disclosure)
    tags = list(hashtags)
    return text + (HASHTAG_SEPARATOR + " ".join(tags) if tags else "")


def utf16_len(text: str) -> int:
    """The length of text in UTF-16 code units: 2 for a character above U+FFFF, 1 for any other. A lone
    surrogate, which json.loads can produce from a broken escape, counts 1 instead of raising."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def hashtag_count(text: str) -> int:
    """The words of text, split at whitespace, that start with "#"."""
    return sum(1 for word in text.split() if word.startswith("#"))


def instagram_problems(text: str, hashtag_limit: int) -> list[str]:
    """Why text, a full caption text, fails an Instagram attempt: it is too long, holds too many hashtags,
    or both, in that order. Empty when it passes. hashtag_limit is the Persona_File's value as written; a
    value above 5 counts as 5 (Req 6.8)."""
    problems = []
    length = utf16_len(text)
    if length > INSTAGRAM_MAX_UTF16:
        problems.append(f"the full caption text is {length:,} UTF-16 code units long; Instagram takes at most "
                        f"{INSTAGRAM_MAX_UTF16:,}")  # fmt: skip
    allowed = min(hashtag_limit, HASHTAG_CAP)
    count = hashtag_count(text)
    if count > allowed:
        words_found = f"{count} word{'' if count == 1 else 's'}"
        problems.append(f"the full caption text holds {words_found} starting with '#'; the Persona_File's hashtag "
                        f"limit allows {allowed}")  # fmt: skip
    return problems


def flagged_terms(caption: str, hashtags: Iterable[str], terms: Iterable[str]) -> tuple[str, ...]:
    """The terms that caption or one of hashtags holds as whole words in any letter case, each once, as
    the Persona_File writes it: the caption's in text order, then each hashtag's in list order. Of two terms
    with the same words only the first can be reported (safety.TermList)."""
    matchable = [term for term in terms if words(term)]
    if not matchable:
        return ()
    guard = SafetyGuard([TermList(matchable, "captions.flagged_terms")], ages=False)
    found = guard.check_text(caption)
    for tag in hashtags:
        found += guard.check_text(tag.lstrip("#"))
    return tuple(dict.fromkeys(found))


@dataclass(frozen=True)
class CaptionCheck:
    """One attempt's caption, checked against the Persona_File's caption settings."""

    caption: str  # as the Prompt_Sheet stores it: ending in the disclosure line when the Persona_File has one
    problems: tuple[str, ...]  # why the attempt fails Instagram's limits; always empty for platform other
    review: tuple[str, ...]  # the flagged terms found, for the entry's review list; they never fail it


def check_caption(caption: str, hashtags: Sequence[str], settings: Captions, *, instagram: bool) -> CaptionCheck:
    """The caption and hashtags of one plan attempt under settings, the Persona_File's captions block.

    The disclosure line is added (Req 6.9); for an Instagram Post the full caption text is checked against
    the length and hashtag limits (Req 6.8); and the flagged terms are looked for in the caption, disclosure
    line included, and in the hashtags (Req 6.10). The caption comes back as it will be stored, whatever the
    terms found."""
    text = caption if settings.disclosure is None else add_disclosure(caption, settings.disclosure)
    problems = instagram_problems(full_text(text, hashtags), settings.hashtag_limit) if instagram else []
    return CaptionCheck(text, tuple(problems), flagged_terms(text, hashtags, settings.flagged_terms))
