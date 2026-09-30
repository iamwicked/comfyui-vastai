"""The Prompt_Assembler: identity prompts and the Edit_Template's scene value (design §A4, Decision 16;
Req 6.6, 6.7, 11.3, 11.4).

    identity_prompt(persona, location, outfit, scene) -> str   the prompt of an identity-required template
    edit_prompt_value(scene) -> str                             the value of the Edit_Template's {{scene}}
    strip_all(paragraph, text) -> str                           text without paragraph, to a fixpoint

Identity prompt, for the Render_Templates flagged identity_required (still, still-sharp, finish): the
Trigger_Word T, the Identity_Paragraph P, the Persona_File's descriptions of the Post's location and outfit,
and the Slide's scene text as the Prompt_Sheet records it, in that order and joined by blank lines (Req 6.6):

    LO = strip_all(P, " ".join(the location and outfit descriptions that are given)).strip()
    S  = strip_all(P, scene).strip()
    prompt = "\\n\\n".join([T, P] + ([LO] if LO else []) + ([S] if S else []))

P occurs in the prompt verbatim exactly once, whatever the scene text (Req 6.7, Property 12). The
Persona_Reader guarantees that P holds no blank line, has no leading or trailing whitespace and is not a
substring of T (Decision 16). So no occurrence can sit inside T, and none inside LO or S, which are
stripped to a fixpoint. None can cross a "\\n\\n" separator either: P would have to hold both newlines, or
end or start with one. identity_prompt raises ValueError for a Persona that breaks one of these rules
instead of returning a prompt that would break Req 6.7.

A carousel Post with mode regenerate renders every Slide with the Still_Template. The render stage then
builds every Slide's prompt with identity_prompt and the Post's location and outfit, so no Slide waits for
the Pick of Slide 1 (Req 11.4).

Edit prompt: the fixed instruction lives in the Edit_Template, as the positive encoder's prompt
"<EDIT_INSTRUCTION> {{scene}}" (§C7). edit_prompt_value gives the Slide's scene text unchanged, and the
Template_Filler never expands a supplied value again. So every Edit_Template Job's prompt starts with the
identical instruction, followed by the scene text (Req 11.3, Property 23). The encoder writes its
"Picture 1:" label for the reference image inside the node, so the prompt input itself starts with the
instruction.

The Reel, Ref_Reel and dialogue prompts of §A4 (reel_prompt) land with the reels stage in Phase 3.

Pure: no file, process or network access.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotations only, so importing this module does not import PyYAML through persona.py
    from influencer.persona import Persona

PART_SEPARATOR = "\n\n"  # a blank line between the parts of an identity prompt


def strip_all(paragraph: str, text: str) -> str:
    """text with every occurrence of paragraph removed, again and again until none is left: removing one
    can join the text around it into a new one. Each pass shortens text, so the loop ends. paragraph must
    not be empty."""
    if not paragraph:
        raise ValueError("strip_all needs a non-empty paragraph to remove")
    while paragraph in text:
        text = text.replace(paragraph, "")
    return text


def _check_paragraph(paragraph: str, trigger_word: str) -> None:
    """The Decision 16 rules that make P occur exactly once; the Persona_Reader enforces all of them."""
    if not paragraph:
        reason = "is empty"
    elif paragraph != paragraph.strip():
        reason = "starts or ends with whitespace"
    elif PART_SEPARATOR in paragraph:
        reason = "holds a blank line"
    elif paragraph in trigger_word:
        reason = f"occurs inside the Trigger_Word {trigger_word!r}"
    else:
        return
    raise ValueError(f"the Identity_Paragraph {reason}, so it cannot appear in a prompt exactly once (Decision 16)")


def identity_prompt(persona: Persona, location: str | None, outfit: str | None, scene: str) -> str:
    """The prompt of an identity-required Render_Template (see the module docstring).

    location and outfit are the Persona_File's descriptions of the Post's location and outfit, None or ""
    when the Post names none; scene is the Slide's scene text as the Prompt_Sheet records it, possibly
    empty. The result starts with the Trigger_Word and holds the Identity_Paragraph exactly once (Req 6.6,
    6.7). Raises ValueError when the Persona breaks a Decision 16 rule."""
    trigger_word, paragraph = persona.trigger_word, persona.identity
    _check_paragraph(paragraph, trigger_word)
    place = strip_all(paragraph, " ".join(part for part in (location, outfit) if part)).strip()
    action = strip_all(paragraph, scene).strip()
    return PART_SEPARATOR.join([trigger_word, paragraph] + [part for part in (place, action) if part])


def edit_prompt_value(scene: str) -> str:
    """The value of the Edit_Template's {{scene}} placeholder: the Slide's scene text as it stands. The
    template's "<EDIT_INSTRUCTION> {{scene}}" puts the fixed instruction in front of it (Req 11.3)."""
    return scene
