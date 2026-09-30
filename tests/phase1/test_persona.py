"""Tests of the Persona_File, scripts/influencer/persona.py, and the Persona_Example (task 8; design §M1,
Decision 16; Req 4, 8.10, 26.12).

- Property 1: print_persona, then read_persona, gives back an equal Persona
- Property 2: a file with 1-6 independent faults is rejected, with a Problem naming each faulted field
- the committed Persona_Example: the §M1 block, every field given a value, no problem and no warning
  without the file check, and only file-existence problems with it
- the file checks in a temp Influencer_Root, the location-count warning, a YAML syntax error, looks.edit,
  the defaults, the printer's styles, and the reasons that Req 4.4, 4.8, 4.13 and 8.10 ask for
"""
from __future__ import annotations

import dataclasses
import re
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

from influencer import persona as P  # noqa: E402
from influencer.errors import Problem, ValidationError  # noqa: E402
from influencer.yamlio import dump_yaml  # noqa: E402
from strategies_persona import Fault, faults, independent, personas  # noqa: E402

EXAMPLE = REPO / "config" / "influencer" / "examples" / "persona.yaml"
DESIGN = REPO / ".kiro" / "specs" / "ai-influencer-pipeline" / "design.md"


def write(folder: Path, content: object, name: str = "persona.yaml") -> Path:
    """content (YAML text, or data printed with dump_yaml) as folder/name."""
    path = Path(folder) / name
    path.write_bytes((content if isinstance(content, str) else dump_yaml(content)).encode("utf-8"))
    return path


def example_data() -> dict:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def problems_of(path: Path, root: object, *, check_files: bool = False) -> list[Problem]:
    with pytest.raises(ValidationError) as caught:
        P.read_persona(path, root, check_files=check_files)
    return caught.value.problems


# ---- the properties ----


# Feature: ai-influencer-pipeline, Property 1: Persona_File round trip
@settings(deadline=None)
@given(persona=personas())
def test_persona_file_round_trip(persona: P.Persona) -> None:
    """**Validates: Requirements 4.11**"""
    with tempfile.TemporaryDirectory(prefix="persona-") as folder:
        path = write(Path(folder), P.print_persona(persona))
        assert P.read_persona(path, folder, check_files=False) == persona


# Feature: ai-influencer-pipeline, Property 2: Persona_Reader reports every failure
@settings(deadline=None)
@given(persona=personas(), fault_set=st.sets(faults("persona"), min_size=1, max_size=6).filter(independent))
def test_persona_reader_reports_every_failure(persona: P.Persona, fault_set: frozenset[Fault]) -> None:
    """**Validates: Requirements 4.2, 4.3, 4.4, 4.5, 4.6, 4.8, 4.12, 4.13, 8.10**"""
    document = P.persona_data(persona)
    applied = sorted(fault_set)
    faulted = [fault.apply(document) for fault in applied]
    with tempfile.TemporaryDirectory(prefix="persona-") as folder:
        path = write(Path(folder), document)
        problems = problems_of(path, folder)
    assert {problem.path for problem in problems} == {str(path)}
    named = {problem.field for problem in problems}
    unnamed = [(fault, field) for fault, field in zip(applied, faulted) if field not in named]
    assert not unnamed, f"no Problem names {unnamed}; the Problems were:\n" + "\n".join(map(str, problems))


# ---- the Persona_Example (Req 4.1, 26.12) ----


def _design_example() -> object:
    """The YAML block under design.md's "### M1" heading; a skip when the design is not checked out."""
    if not DESIGN.is_file():
        pytest.skip(f"{DESIGN.relative_to(REPO)} is not in this checkout")
    match = re.search(r"^### M1\b.*?^```yaml\n(.*?)^```", DESIGN.read_text(encoding="utf-8"), re.S | re.M)
    if match is None:
        pytest.skip("design.md has no YAML block under its ### M1 heading")
    return yaml.safe_load(match.group(1))


def test_the_example_is_the_design_example() -> None:
    assert example_data() == _design_example()


def test_the_example_gives_every_field_a_value() -> None:
    data = example_data()

    def empty(mapping: dict, keys: tuple[str, ...], where: str) -> list[str]:
        return [where + key for key in keys if mapping.get(key) in (None, "", [], {})]

    found = empty(data, P.PERSONA_FIELDS, "") + empty(data["grade"], P.GRADE_FIELDS, "grade.")
    for list_name, keys in (("locations", P.LOCATION_FIELDS), ("wardrobe", P.OUTFIT_FIELDS)):
        for number, item in enumerate(data[list_name], 1):
            found += empty(item, keys, f"{list_name}[{number}].")
    found += empty(data["character_loras"], P.CHARACTER_LORA_FIELDS, "character_loras.")
    for medium in P.CHARACTER_LORA_FIELDS:
        found += empty(data["character_loras"][medium], P.LORA_FIELDS, f"character_loras.{medium}.")
    found += empty(data["looks"], P.LOOK_MEDIA, "looks.")
    for medium in P.LOOK_MEDIA:
        found += empty(data["looks"][medium], P.LOOK_PLATFORMS, f"looks.{medium}.")
        for platform in P.LOOK_PLATFORMS:
            for number, entry in enumerate(data["looks"][medium][platform], 1):
                found += empty(entry, P.LORA_FIELDS, f"looks.{medium}.{platform}[{number}].")
    found += empty(data["captions"], P.CAPTION_FIELDS, "captions.") + empty(data["ollama"], P.OLLAMA_FIELDS, "ollama.")
    assert found == []


def test_the_example_reads_with_no_problem_and_no_warning() -> None:
    persona = P.read_persona(EXAMPLE, EXAMPLE.parent, check_files=False)
    assert P.persona_warnings(persona) == []
    assert (persona.age, persona.slug, persona.trigger_word, len(persona.locations)) == (29, "mara-lind", "maralind", 3)
    assert persona.character_loras.video is not None
    assert persona.captions.disclosure == "AI-generated character — not a real person."


def test_the_example_reports_only_its_missing_files() -> None:
    data = example_data()
    files = {"hero_image": data["hero_image"], "grade.lut": data["grade"]["lut"]}
    files |= {f"reference_pack[{number}]": path for number, path in enumerate(data["reference_pack"], 1)}
    problems = problems_of(EXAMPLE, EXAMPLE.parent, check_files=True)
    assert sorted(problem.field for problem in problems) == sorted(files)
    for problem in problems:
        assert problem.path == str(EXAMPLE)
        assert problem.reason.startswith(f"{files[problem.field]!r} does not exist: there is no file at ")


# ---- the file checks (Req 4.7, 4.10) ----


def test_the_file_checks_name_the_field_and_the_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "mara-lind"
    (root / "refs").mkdir(parents=True)
    (root / "refs" / "hero.png").write_bytes(b"png")
    (root / "grade" / "mara.cube").mkdir(parents=True)  # a folder where the LUT should be
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")
    data = example_data()
    data["reference_pack"] = ["refs/ref-01.png", "../outside.png", str(outside)]
    path = write(root, data)
    # The working folder holds refs/hero.png and refs/ref-01.png; neither counts, only the root does.
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "refs").mkdir(parents=True)
    (elsewhere / "refs" / "ref-01.png").write_bytes(b"png")
    monkeypatch.chdir(elsewhere)

    reasons = {problem.field: problem.reason for problem in problems_of(path, root, check_files=True)}
    assert sorted(reasons) == ["grade.lut", "reference_pack[1]", "reference_pack[2]", "reference_pack[3]"]
    assert reasons["reference_pack[1]"] == (
        f"'refs/ref-01.png' does not exist: there is no file at {root / 'refs' / 'ref-01.png'}"
    )
    assert reasons["grade.lut"] == f"'grade/mara.cube' is a folder, not a file: {root / 'grade' / 'mara.cube'}"
    assert reasons["reference_pack[2]"].startswith("'../outside.png' holds a '..' segment")
    assert reasons["reference_pack[3]"].startswith(f"{str(outside)!r} is an absolute path")

    # with every file in place, the file check passes and changes nothing
    (root / "grade" / "mara.cube").rmdir()
    (root / "grade" / "mara.cube").write_text("LUT_3D_SIZE 2\n", encoding="utf-8")
    (root / "refs" / "ref-01.png").write_bytes(b"png")
    data["reference_pack"] = ["refs/ref-01.png"]
    path = write(root, data)
    assert P.read_persona(path, root, check_files=True) == P.read_persona(path, None, check_files=False)


def test_resolve_path_joins_the_root_and_refuses_escapes(tmp_path: Path) -> None:
    assert P.resolve_path(tmp_path, "refs/hero.png") == tmp_path / "refs" / "hero.png"
    for escape in ("/etc/hosts", "../hero.png", "refs/../../hero.png"):
        with pytest.raises(ValueError, match="not a path inside"):
            P.resolve_path(tmp_path, escape)
    with pytest.raises(ValueError, match="needs the Influencer_Root"):
        P.read_persona(EXAMPLE, None, check_files=True)


# ---- warnings, syntax errors, unknown fields (Req 4.2, 4.9, 4.12) ----


@pytest.mark.parametrize("count", [0, 2, 3, 4, 5])
def test_a_location_count_outside_3_to_4_warns_with_the_count(tmp_path: Path, count: int) -> None:
    data = example_data()
    data["locations"] = [
        {"name": f"place-{n}", "description": f"a quiet place, number {n}"} for n in range(1, count + 1)
    ]
    path = write(tmp_path, data)
    warnings = P.persona_warnings(P.read_persona(path, tmp_path, check_files=False))  # a warning never rejects
    if count in (3, 4):
        assert warnings == []
    else:
        assert [(warning.path, warning.field, warning.line) for warning in warnings] == [(str(path), "locations", None)]
        assert re.search(rf"\bdefines {count} locations?;", warnings[0].reason), warnings[0].reason


def test_a_yaml_syntax_error_reports_its_line(tmp_path: Path) -> None:
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    number = next(n for n, line in enumerate(lines, 1) if line.startswith("trigger_word:"))
    lines[number - 1] = "trigger_word: mara: lind"
    path = write(tmp_path, "\n".join(lines) + "\n")
    [problem] = problems_of(path, tmp_path)
    assert (problem.path, problem.field, problem.line) == (str(path), None, number)
    assert problem.reason.startswith("YAML syntax error")
    [problem] = problems_of(write(tmp_path, "", "empty.yaml"), tmp_path)
    assert (problem.field, problem.line) == (None, None) and problem.reason.startswith("the file is empty")


def test_a_looks_edit_stack_is_an_unknown_field(tmp_path: Path) -> None:
    data = example_data()
    data["looks"]["edit"] = {"instagram": [{"lora": "looks/qwen-edit.safetensors", "strength": 0.5}], "other": []}
    [problem] = problems_of(write(tmp_path, data), tmp_path)
    assert (problem.field, problem.reason) == ("looks.edit", "is not a field of looks, which takes stills and video")


# ---- defaults, the printer and the reasons (Req 4.4, 4.8, 4.11, 4.13, 8.10) ----

MINIMAL = {
    "slug": "mara-lind", "name": "Mara Lind", "age": 25, "voice": "Warm.", "identity": "A woman with freckles.",
    "trigger_word": "maralind", "hero_image": "refs/hero.png", "grade": {"lut": "grade/mara.cube"},
}  # fmt: skip


def test_fields_left_out_take_their_defaults(tmp_path: Path) -> None:
    persona = P.read_persona(write(tmp_path, MINIMAL), tmp_path, check_files=False)
    assert persona == P.Persona(**{**MINIMAL, "grade": P.Grade("grade/mara.cube")})
    assert (persona.version, persona.language, persona.sharp_decode, persona.grade.grain) == (1, "English", False, None)
    assert (persona.captions.hashtag_limit, persona.ollama) == (5, P.Ollama("qwen3:8b", "dolphin3:8b"))
    assert persona.character_loras == P.CharacterLoras() and persona.looks == P.Looks()

    data = {
        **MINIMAL,
        "character_loras": {"stills": {"lora": "characters/mara.safetensors"}},
        "looks": {"video": {"other": ["MINIMAX/relim.safetensors", {"lora": "MINIMAX/amateur.safetensors"}]}},
        "captions": {"hashtag_limit": 9},
    }
    persona = P.read_persona(write(tmp_path, data), tmp_path, check_files=False)
    assert persona.character_loras == P.CharacterLoras(stills=P.LoraRef("characters/mara.safetensors", 1.0))
    assert persona.looks.video.other == (
        P.LoraRef("MINIMAX/relim.safetensors", 1.0),  # a plain string means strength 1.0
        P.LoraRef("MINIMAX/amateur.safetensors", 1.0),
    )
    assert persona.looks.video.instagram == () and persona.looks.stills == P.LookStack()
    assert (persona.captions.hashtag_limit, persona.captions.max_hashtags) == (9, 5)  # above 5 counts as 5


def test_print_persona_keeps_block_text_and_ambiguous_strings(tmp_path: Path) -> None:
    example = P.read_persona(EXAMPLE, None, check_files=False)
    persona = dataclasses.replace(
        example,
        voice="Warm, dry.\nShort sentences.\n",
        identity="A 29-year-old woman\nwith light freckles",
        niche="no",
        name="2026",
        themes=("#x", "a: b", "on", "\U0001f600"),
    )
    text = P.print_persona(persona)
    assert "\nvoice: |\n  Warm, dry.\n  Short sentences.\n" in text
    assert "\nidentity: |-\n  A 29-year-old woman\n  with light freckles\n" in text
    data = yaml.safe_load(text)
    assert (data["niche"], data["name"], data["themes"]) == ("no", "2026", ["#x", "a: b", "on", "\U0001f600"])
    assert P.read_persona(write(tmp_path, text), tmp_path, check_files=False) == persona


def test_the_reasons_name_what_the_requirements_ask_for(tmp_path: Path) -> None:
    data = example_data()
    data["themes"].append("a teen look")  # Req 4.4: the field and the matched term
    data["looks"]["school uniform"] = []  # a term in a key
    data["reference_pack"] = [f"refs/ref-{n}.png" for n in range(1, 8)]  # Req 4.8: the number listed
    data["wardrobe"].append({"name": "linen-set", "description": "the same set again"})  # Req 4.13
    data["character_loras"]["stills"]["strength"] = 2.5  # Req 8.10: the LoRA and the value
    data["looks"]["video"]["other"] = ["MINIMAX/relim.safetensors"] * 9  # Req 8.10: the list and the count
    reasons: dict[str | None, list[str]] = {}
    for problem in problems_of(write(tmp_path, data), tmp_path):
        reasons.setdefault(problem.field, []).append(problem.reason)
    assert reasons.pop("themes[4]") == ["holds 'teen', which the Safety_Guard rejects"]
    assert reasons.pop("looks.school uniform") == [
        "is not a field of looks, which takes stills and video",
        "holds 'school uniform', which the Safety_Guard rejects",
    ]
    assert reasons.pop("reference_pack") == ["lists 7 images; the Reference_Pack holds at most 5"]
    assert reasons.pop("wardrobe[4].name") == ["repeats the name 'linen-set' of wardrobe[1]"]
    assert reasons.pop("character_loras.stills.strength") == [
        "the strength 2.5 of 'characters/mara-lind/mara_v1_000002500.safetensors' is out of range: "
        "use above 0 and at most 2.0"
    ]
    assert reasons.pop("looks.video.other") == ["lists 9 LoRAs; a look list holds at most 8"]
    assert reasons == {}
