"""Tests of the Prompt_Assembler, scripts/influencer/assembler.py (task 13; design §A4, Decision 16;
Req 6.6, 6.7, 11.3, 11.4).

- Property 12: an identity prompt starts with the Trigger_Word, holds the Identity_Paragraph exactly once
  and orders its parts T, P, location and outfit, scene
- Property 23: every filled Edit_Template prompt starts with the template's fixed instruction, then the
  scene text, over every edit-kind template in config/influencer/templates/ and a minimal fixture
  (tests/fixtures/edit-fixture.json), so the real edit.json is covered once it lands (task 18)
- the design's examples, a Persona that breaks Decision 16, and the edit templates' instruction text
"""
from __future__ import annotations

import dataclasses
import functools
import sys

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helpers.harness import REPO
from strategies import texts

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

import strategies_prompts as SP  # noqa: E402
import strategies_templates as ST  # noqa: E402
from influencer import assembler as A  # noqa: E402
from influencer import persona as P  # noqa: E402
from influencer import templates as T  # noqa: E402

SCENE = "{{scene}}"
PERSONA = P.Persona(
    slug="mara-lind", name="Mara Lind", age=31, voice="warm and dry", identity="A woman with freckles",
    trigger_word="maralind", hero_image="refs/hero.png", grade=P.Grade("grade/mara.cube"),
)  # fmt: skip


def occurrences(text: str, piece: str) -> int:
    """How often piece occurs in text, overlapping occurrences included ("aa" is twice in "aaa")."""
    return sum(1 for start in range(len(text) - len(piece) + 1) if text.startswith(piece, start))


def remove_all(piece: str, text: str) -> str:
    """text without piece, removed again until none is left: the design's strip_all, written out here."""
    while piece in text:
        text = text.replace(piece, "")
    return text


# ---- Property 12 ----


# Feature: ai-influencer-pipeline, Property 12: Identity prompts contain the Identity_Paragraph exactly once, in order
@given(case=SP.identity_cases())
def test_identity_prompts_contain_the_identity_paragraph_exactly_once_in_order(
    case: tuple[P.Persona, str | None, str | None, str],
) -> None:
    persona, location, outfit, scene = case
    trigger_word, paragraph = persona.trigger_word, persona.identity
    prompt = A.identity_prompt(persona, location, outfit, scene)
    assert prompt.startswith(trigger_word + "\n\n" + paragraph)
    assert occurrences(prompt, paragraph) == 1
    # the order: T, P, then the location and outfit part, then the scene part, each without P
    place = remove_all(paragraph, " ".join(part for part in (location, outfit) if part)).strip()
    action = remove_all(paragraph, scene).strip()
    assert prompt == "\n\n".join([trigger_word, paragraph] + [part for part in (place, action) if part])


# ---- Property 23 ----

EDIT_CASES = [pytest.param(None, id="fixture"), *(pytest.param(name, id=name) for name in SP.edit_template_names())]


@functools.lru_cache(maxsize=None)
def edit_template(name: str | None) -> T.Template:
    """The committed edit-kind template name, or the fixture for None."""
    return SP.edit_fixture() if name is None else T.load_template(name)


def scene_inputs(template: T.Template) -> list[tuple[str, str, str, str]]:
    """(node id, input name, text before {{scene}}, text after it) of every input whose template value
    holds {{scene}}: the Edit prompt."""
    found = []
    for node_id, node in template.prompt.items():
        for input_name, value in node["inputs"].items():
            if isinstance(value, str) and SCENE in value:
                before, _, after = value.partition(SCENE)
                found.append((node_id, input_name, before, after))
    return found


# Feature: ai-influencer-pipeline, Property 23: Edit instruction invariant
@pytest.mark.parametrize("name", EDIT_CASES)
@given(scenes=st.lists(texts(), min_size=2, max_size=4), data=st.data())
def test_edit_instruction_invariant(name: str | None, scenes: list[str], data: st.DataObject) -> None:
    template = edit_template(name)
    [(node_id, input_name, instruction, _)] = scene_inputs(template)
    assert instruction.strip() and not ST.PLACEHOLDER.search(instruction)  # fixed text, filled unchanged
    for scene in scenes:  # one Job each, with its own values for every other placeholder
        values = dict(data.draw(ST.values_for(template)))
        values["scene"] = A.edit_prompt_value(scene)
        prompt = T.fill(template, values)[node_id]["inputs"][input_name]
        assert prompt.startswith(instruction + scene)


# ---- identity prompts: the design's examples ----


def test_the_four_parts_come_in_order_joined_by_blank_lines() -> None:
    """Req 6.6; a regenerated carousel's Slides each get the Post's location and outfit (Req 11.4)."""
    for scene in ("She reads by the window.", "She pours coffee.", "She laughs at the camera."):
        assert A.identity_prompt(PERSONA, "a corner café", "a linen set", scene) == (
            f"maralind\n\nA woman with freckles\n\na corner café a linen set\n\n{scene}"
        )


@pytest.mark.parametrize(
    ("location", "outfit", "scene", "rest"),
    [
        (None, None, "", ""),
        ("", None, "  \n", ""),
        (None, "a linen set", "", "\n\na linen set"),
        ("a corner café", "", " She reads. ", "\n\na corner café\n\nShe reads."),
        (None, None, "A woman with freckles", ""),  # the scene is only P
        ("A woman with freckles", "A woman with freckles", "\nShe reads.\n", "\n\nShe reads."),
    ],
    ids=["nothing", "blank", "outfit-only", "stripped", "scene-is-p", "descriptions-are-p"],
)
def test_empty_parts_are_left_out(location: str | None, outfit: str | None, scene: str, rest: str) -> None:
    assert A.identity_prompt(PERSONA, location, outfit, scene) == "maralind\n\nA woman with freckles" + rest


def test_the_paragraph_is_removed_to_a_fixpoint_and_across_the_join() -> None:
    paragraph = PERSONA.identity
    nested = "A woman with" + paragraph + " freckles on the beach"  # one removal leaves P again
    assert A.strip_all(paragraph, nested) == " on the beach"
    prompt = A.identity_prompt(PERSONA, "a café. A woman", "with freckles in linen", nested)
    assert prompt == "maralind\n\nA woman with freckles\n\na café.  in linen\n\non the beach"
    with pytest.raises(ValueError):
        A.strip_all("", "any text")  # "" is in every text, so the loop would never end


@pytest.mark.parametrize(
    ("identity", "reason"),
    [("", "is empty"), (" A woman", "whitespace"), ("A woman\n", "whitespace"), ("A woman\n\nwith freckles", "blank line"),
     ("mara", "inside the Trigger_Word")],
    ids=["empty", "leading", "trailing", "blank-line", "in-trigger-word"],
)  # fmt: skip
def test_a_persona_that_breaks_decision_16_is_refused(identity: str, reason: str) -> None:
    persona = dataclasses.replace(PERSONA, identity=identity)
    with pytest.raises(ValueError, match=reason):
        A.identity_prompt(persona, "a corner café", "a linen set", "She reads.")


# ---- the Edit prompt ----


def test_the_edit_value_is_the_scene_as_it_stands_and_is_never_expanded() -> None:
    template = SP.edit_fixture()
    for scene in ("", "  She turns to the window.\n", "{{scene}} and {{reference_image}}", "\\1 $prev"):
        assert A.edit_prompt_value(scene) == scene
        values = {"scene": A.edit_prompt_value(scene), "reference_image": "influencer/0a1b.png"}
        assert T.fill(template, values)["8"]["inputs"]["prompt"] == SP.EDIT_INSTRUCTION + " " + scene


@pytest.mark.parametrize("name", EDIT_CASES)
def test_the_edit_prompt_is_the_positive_encoders_prompt_with_the_design_instruction(name: str | None) -> None:
    """§C7: node 8's prompt is "<EDIT_INSTRUCTION> {{scene}}", {{scene}} at the end (§A4); the encoder's own
    "Picture 1:" label is added inside the node, so the input starts with the instruction."""
    template = edit_template(name)
    [(node_id, input_name, before, after)] = scene_inputs(template)
    assert (template.prompt[node_id]["class_type"], input_name) == ("TextEncodeQwenImageEditPlus", "prompt")
    assert (before, after) == (SP.EDIT_INSTRUCTION + " ", "")
    assert template.kind == "edit" and template.identity_required is False
