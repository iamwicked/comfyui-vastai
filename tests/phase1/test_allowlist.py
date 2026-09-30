"""Tests of the Instagram_Allowlist, scripts/influencer/allowlist.py (task 15.3, 15.8; design §A6; Req 21.3,
21.6, 21.7).

- Property 40: over generated models.list lines and Personas, the allowlist is exactly the names the
  requirements define, and a Post's look list or a Persona Instagram look stack is refused exactly when
  an entry is outside it, each Problem naming that entry
- unit tests: which lines and Character_LoRAs count, subdirs with empty or `..` segments, near misses and
  the case hint, platform other, the reel source, check_persona and read_allowlist
"""
from __future__ import annotations

import dataclasses
import sys

import pytest
from hypothesis import given, settings

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import allowlist as A  # noqa: E402
from influencer import persona as P  # noqa: E402
from influencer.errors import EnvironmentFailure, ValidationError  # noqa: E402
from strategies_network import AllowlistCase, allowlist_cases, entry_name, near_misses  # noqa: E402

MODELS = """\
# the Lenovo look, for stills and video
lora|a.safetensors|hf|amber/maple::a.safetensors||wf:influencer
lora|b.safetensors|hf|amber/maple::b.safetensors|loras/looks|wf:krea2, influencer-video
lora|c.safetensors|hf|amber/maple::c.safetensors|characters|wf:influencer
checkpoint|d.safetensors|hf|amber/maple::d.safetensors|loras/x|wf:influencer
lora|e.safetensors|hf|amber/maple::e.safetensors|loras/nsfw|wf:influencer-nsfw
# lora|f.safetensors|hf|amber/maple::f.safetensors||wf:influencer
lora|g.safetensors|hf|amber/maple::g.safetensors||wf:influencerx
lora|h.safetensors|hf|amber/maple::h.safetensors|loras//deep/./er|wf:influencer
lora|i.safetensors|hf|amber/maple::i.safetensors|loras/../checkpoints|wf:influencer
workflow|j.safetensors|url|https://raw.example/j||wf:influencer
"""
CONFIG = frozenset({"a.safetensors", "looks/b.safetensors", "x/d.safetensors", "deep/er/h.safetensors"})


def persona(stills: str | None = "characters/me.safetensors", video: str | None = None, *,
            instagram: tuple[str, ...] = (), video_instagram: tuple[str, ...] = (),
            other: tuple[str, ...] = ()) -> P.Persona:  # fmt: skip
    refs = lambda names: tuple(P.LoraRef(name) for name in names)  # noqa: E731
    return P.Persona(
        slug="mara-lind", name="Mara Lind", age=29, voice="Warm.", identity="Mara, 29.", trigger_word="mara_lind",
        hero_image="refs/hero.png", grade=P.Grade("grade/mara.cube"),
        character_loras=P.CharacterLoras(
            P.LoraRef(stills) if stills is not None else None, P.LoraRef(video) if video is not None else None
        ),
        looks=P.Looks(
            stills=P.LookStack(instagram=refs(instagram), other=refs(other)),
            video=P.LookStack(instagram=refs(video_instagram), other=refs(other)),
        ),
    )  # fmt: skip


# ---- the property ----


def _refs(entries: tuple[str | P.LoraRef, ...]) -> tuple[P.LoraRef, ...]:
    return tuple(entry if isinstance(entry, P.LoraRef) else P.LoraRef(entry) for entry in entries)


# Feature: ai-influencer-pipeline, Property 40: Instagram_Allowlist is exact
@settings(deadline=None)
@given(case=allowlist_cases())
def test_instagram_allowlist_is_exact(case: AllowlistCase) -> None:
    """Validates: Requirements 21.3, 21.6, 21.7"""
    allowlist = A.build(case.models_text, case.persona)
    assert allowlist.names == case.expected
    outside = [entry_name(entry) not in case.expected for entry in case.entries]

    # an Instagram Post's own look list: one Problem per entry outside, naming the Post and the LoRA
    problems = A.post_problems("p-7", case.entries, allowlist, platform=case.platform, path="calendar.yaml",
                               looks_field="posts[7].looks")  # fmt: skip
    bad = [number for number, out in enumerate(outside, 1) if out]
    assert [problem.field for problem in problems] == [f"posts[7].looks[{number}]" for number in bad]
    for problem, number in zip(problems, bad):
        assert problem.path == "calendar.yaml" and problem.reason.startswith("Post 'p-7': look LoRA ")
        assert f"{entry_name(case.entries[number - 1])!r} is outside the Instagram_Allowlist" in problem.reason

    # the same entries as the Persona's Instagram look stacks, with its Character_LoRAs checked too
    stills, video = _refs(case.entries[: case.split]), _refs(case.entries[case.split :])
    looks = P.Looks(
        stills=dataclasses.replace(case.persona.looks.stills, instagram=stills),
        video=dataclasses.replace(case.persona.looks.video, instagram=video),
    )
    subject = dataclasses.replace(case.persona, looks=looks)
    expected = [
        (f"character_loras.{medium}", ref.lora)
        for medium, ref in (("stills", subject.character_loras.stills), ("video", subject.character_loras.video))
        if ref is not None and ref.lora not in case.expected
    ]
    for medium, stack in (("stills", stills), ("video", video)):
        expected += [(f"looks.{medium}.instagram[{n}]", ref.lora) for n, ref in enumerate(stack, 1) if ref.lora not in case.expected]
    problems = A.persona_problems(subject, allowlist)
    assert [problem.field for problem in problems] == [field for field, _ in expected]
    for problem, (_, lora) in zip(problems, expected):
        assert problem.path == subject.source and f"{lora!r} is outside the Instagram_Allowlist" in problem.reason
    if expected:
        with pytest.raises(ValidationError) as caught:
            A.check_persona(subject, allowlist)
        assert caught.value.problems == problems
    else:
        A.check_persona(subject, allowlist)


# ---- the allowlist ----


def test_the_lines_and_character_loras_that_count() -> None:
    allowlist = A.build(MODELS, persona("characters/me.safetensors", "NSFW/voice.safetensors"))
    # a: lora, default folder; b: below loras/; d: any kind below loras/; h: empty and . segments dropped.
    # Not: c (installs below models/characters), e (influencer-nsfw), f (commented out), g (another id),
    # i (the .. leads to models/checkpoints), j (a workflow)
    assert allowlist.config == CONFIG
    assert allowlist.characters == {"characters/me.safetensors"}
    assert allowlist.inside("looks/b.safetensors") and not allowlist.inside("e.safetensors")


@pytest.mark.parametrize(
    ("path", "joins"),
    [
        ("characters/me.safetensors", True),
        ("me.safetensors", True),
        ("nsfw", True),  # a file named nsfw, not the folder
        ("nsfwish/me.safetensors", True),
        ("nsfw/me.safetensors", False),
        ("NSFW/me.safetensors", False),
        ("./nsfw/me.safetensors", False),
        ("nsfw//me.safetensors", False),
        ("/workspace/models/loras/me.safetensors", False),
        ("characters/../me.safetensors", False),
        ("", False),
    ],
)
def test_a_character_lora_joins_when_relative_without_dotdot_and_outside_nsfw(path: str, joins: bool) -> None:
    allowlist = A.build("", persona(path))
    assert allowlist.inside(path) is joins
    problems = A.persona_problems(persona(path), allowlist)
    assert [problem.field for problem in problems] == ([] if joins else ["character_loras.stills"])


def test_near_misses_are_outside_and_a_case_miss_gets_a_hint() -> None:
    allowlist = A.build(MODELS, persona(None))
    misses = near_misses("looks/b.safetensors")
    assert not any(allowlist.inside(name) for name in misses)
    problems = A.post_problems("p1", misses, allowlist)
    assert [problem.field for problem in problems] == [f"looks[{n}]" for n in range(1, len(misses) + 1)]
    assert problems[0].reason == (
        "Post 'p1': look LoRA 'LOOKS/B.SAFETENSORS' is outside the Instagram_Allowlist: no active models.list line "
        "tagged influencer or influencer-video installs it below models/loras/, and it is not a Character_LoRA of "
        "the Persona_File (the allowlist has 'looks/b.safetensors'; names match exactly, letter case included)"
    )
    assert problems[3].reason.endswith("'../looks/b.safetensors' is outside the Instagram_Allowlist: it holds a '..' segment")
    assert problems[5].reason.endswith("it is under models/loras/nsfw/, where the NSFW add-on installs")


# ---- Posts ----


def test_a_post_fails_on_any_strength_and_other_platforms_are_never_checked() -> None:
    allowlist = A.build(MODELS, persona())
    entries = [P.LoraRef("nsfw/e.safetensors", 0.05), P.LoraRef("a.safetensors", -2.0), "characters/me.safetensors"]
    for platform in (None, "instagram"):
        problems = A.post_problems("p2", entries, allowlist, platform=platform)
        assert [problem.field for problem in problems] == ["looks[1]"]
    assert A.post_problems("p2", entries, allowlist, platform="other") == []


def test_a_reel_source_from_a_post_for_other_platforms_fails_the_post() -> None:
    allowlist = A.build(MODELS, persona())
    source = A.ReelSource("p0", 2, "other")
    [problem] = A.post_problems("r1", [], allowlist, reel_source=source, path="week.yaml", source_field="posts[4].source")
    assert (problem.path, problem.field) == ("week.yaml", "posts[4].source")
    assert problem.reason.startswith("Post 'r1': its reel source is Slide 2 of Post 'p0', whose platform is other")
    for platform in (None, "instagram"):
        assert A.post_problems("r1", [], allowlist, reel_source=A.ReelSource("p0", 2, platform)) == []
    assert A.post_problems("r1", [], allowlist, platform="other", reel_source=source) == []


# ---- the Persona_File check ----


def test_check_persona_names_every_field_and_lora_and_ignores_the_other_stacks() -> None:
    subject = persona("characters/me.safetensors", "nsfw/v.safetensors",
                      instagram=("a.safetensors", "Looks/b.safetensors"), video_instagram=("characters/me.safetensors",
                      "nsfw/e.safetensors"), other=("nsfw/e.safetensors",))  # fmt: skip
    allowlist = A.build(MODELS, subject)
    with pytest.raises(ValidationError) as caught:
        A.check_persona(subject, allowlist)
    assert [(problem.path, problem.field) for problem in caught.value.problems] == [
        ("persona.yaml", "character_loras.video"),
        ("persona.yaml", "looks.stills.instagram[2]"),
        ("persona.yaml", "looks.video.instagram[2]"),
    ]
    assert "Character_LoRA 'nsfw/v.safetensors' is outside" in str(caught.value)
    A.check_persona(persona(instagram=("looks/b.safetensors",), other=("nsfw/e.safetensors",)), allowlist)


def test_read_allowlist_reads_models_list_from_the_config_folder(tmp_path) -> None:
    (tmp_path / "models.list").write_bytes(MODELS.encode("utf-8") + b"lora|caf\xc3\xa9.safetensors|hf|o/r::x||wf:influencer\n")
    allowlist = A.read_allowlist(tmp_path, persona())
    assert allowlist.config == CONFIG | {"café.safetensors"}
    with pytest.raises(EnvironmentFailure, match=r"models\.list: cannot be read .*the Instagram_Allowlist needs it"):
        A.read_allowlist(tmp_path / "missing", persona())
