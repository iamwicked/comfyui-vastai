"""Unit tests of the Licence_Gate, scripts/influencer/checks/licences.py, and the Config_Checker's licence,
licence-record and presence rules for it (task 25.14; design §M15, §C10, Decision 26; Req 2.4, 2.6, 26.17,
26.18). Example-based: the design gives the Licence_Gate no numbered property.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import lines as L  # noqa: E402
from influencer.checks import config_checker as C  # noqa: E402
from influencer.checks import licences as X  # noqa: E402
from strategies import write_config  # noqa: E402

CONFIG = REPO / "config"
LICENCES = CONFIG / "influencer" / "licences"
SAFETY = CONFIG / "influencer" / "safety"
NONCOMMERCIAL = X.load_noncommercial(LICENCES / X.NONCOMMERCIAL)
CONDITIONS = X.load_conditions(LICENCES / X.CONDITIONS)
KREA = CONDITIONS["krea-2-community-license"]
LICENCE_GROUPS = ("licence", "licence-record")


def run(tmp_path: Path, models: str = "", ollama: str = "", *, licences: dict[str, str] | None = None,
        groups=LICENCE_GROUPS) -> C.Report:  # fmt: skip
    """The groups over a config folder of the two lists and the licence files (default: the committed ones)."""
    if licences is None:
        licences = {name: (LICENCES / name).read_text(encoding="utf-8") for name in (X.NONCOMMERCIAL, X.CONDITIONS)}
    files = {"models.list": models, "ollama-models.txt": ollama}
    files.update({f"influencer/licences/{name}": text for name, text in licences.items()})
    config_dir = write_config(tmp_path / "config", files)
    return C.run_checks(C.Context(tmp_path, config_dir, SAFETY, {"PATH": ""}), groups)


def lines_of(report: C.Report, group: str) -> set[int | None]:
    return {problem.line for problem in report.by_group(group)}


def line(name: str, tag: str = "wf:influencer") -> str:
    return f"lora|{name}.safetensors|hf|owner/repo::{name}.safetensors||{tag}\n"


# ---- parsing each form ----


def test_an_hf_id_with_a_note() -> None:
    rec = X.parse_record("# licence: Apache-2.0 (Danrisi/Lenovo_Krea2)")
    assert (rec.identifier, rec.note, rec.conditions, rec.terms, rec.civitai) == ("apache-2.0", "Danrisi/Lenovo_Krea2", None, None, None)


def test_none_stated_with_terms() -> None:
    rec = X.parse_record("none stated; terms: upstream Wan-AI/Wan2.1-T2V-14B, apache-2.0")
    assert rec.identifier == "none stated"
    assert rec.terms == "upstream Wan-AI/Wan2.1-T2V-14B, apache-2.0"


def test_civitai_with_and_without_its_optional_parts() -> None:
    full = X.parse_record("Civitai permissions: commercial use Image, RentCivit; credit required; derivatives not allowed; relicensing allowed")
    assert full.identifier is None
    assert full.civitai == X.Civitai(("Image", "RentCivit"), True, False, True)
    assert full.civitai_uses == ("Image", "RentCivit") and full.credit_required
    bare = X.parse_record("Civitai permissions: commercial use none")
    assert bare.civitai == X.Civitai((), None, None, None)
    assert not bare.credit_required


def test_civitai_model_license_with_a_civitai_terms() -> None:
    rec = X.parse_record("civitai-model-license; terms: Civitai permissions: commercial use Image; credit not required")
    assert rec.identifier == "civitai-model-license"
    assert rec.terms_civitai == X.Civitai(("Image",), False)


def test_conditions_and_terms_together() -> None:
    rec = X.parse_record("other (see card); conditions: no ads; revocable; terms: https://example.com/terms, stated on the card")
    assert (rec.identifier, rec.note) == ("other", "see card")
    assert rec.conditions == "no ads; revocable"
    assert rec.terms == "https://example.com/terms, stated on the card"


@pytest.mark.parametrize("text, reason", [
    ("", "empty"),
    ("apache 2.0", "only a note"),
    ("apache-2.0; conditions: ", "'conditions:' is empty"),
    ("Civitai permissions: commercial use Photos", "unknown Civitai commercial use"),
    ("Civitai permissions: commercial use Image; relicensing allowed; credit required", "not a Civitai form"),
    ("mit; terms: x; conditions: y", "comes before"),
])  # fmt: skip
def test_a_malformed_record_raises_with_the_reason(text: str, reason: str) -> None:
    with pytest.raises(X.RecordError, match=reason):
        X.parse_record(text)


# ---- the gate ----


@pytest.mark.parametrize("record", ["qwen-research", "QWEN-RESEARCH", "cc-by-nc-4.0", "flux-1-dev-non-commercial-license"])
def test_noncommercial_licences_fail(record: str) -> None:
    result = X.gate(X.parse_record(record), NONCOMMERCIAL)
    assert not result.passed and "Noncommercial_List" in result.reason


@pytest.mark.parametrize("record", [
    "apache-2.0",
    f"krea-2-community-license; conditions: {KREA}",
    f"llama3.1 (upstream Llama 3.1); conditions: {CONDITIONS['llama3.1']}",
    "other; terms: what the card states, https://example.com",
    "none stated; terms: upstream x/y, mit",
])  # fmt: skip
def test_commercial_and_unclassifiable_licences_pass_with_their_conditions(record: str) -> None:
    rec = X.parse_record(record)
    result = X.gate(rec, NONCOMMERCIAL)
    assert result.passed, result.reason
    assert result.conditions == ((rec.conditions,) if rec.conditions else ())


@pytest.mark.parametrize("uses", ["RentCivit", "RentCivit, Rent", "none"])
def test_civitai_without_image_fails(uses: str) -> None:
    result = X.gate(X.parse_record(f"Civitai permissions: commercial use {uses}"), NONCOMMERCIAL)
    assert not result.passed and "no Image" in result.reason


@pytest.mark.parametrize("uses", ["Image", "RentCivit, Image, Rent", "Image, RentCivit, Rent, Sell, SellMerge"])
def test_civitai_with_image_passes(uses: str) -> None:
    assert X.gate(X.parse_record(f"Civitai permissions: commercial use {uses}; credit not required"), NONCOMMERCIAL).passed


def test_credit_required_adds_credit_the_creator() -> None:
    result = X.gate(X.parse_record("Civitai permissions: commercial use Image; credit required"), NONCOMMERCIAL)
    assert result.passed and result.conditions == ("credit the creator",)


def test_the_civitai_rule_applies_to_a_civitai_model_license_terms() -> None:
    rec = X.parse_record("civitai-model-license; terms: Civitai permissions: commercial use RentCivit")
    assert not X.gate(rec, NONCOMMERCIAL).passed


# ---- the committed look LoRAs that stay commented out (Req 2.6) ----


@pytest.mark.parametrize("filename", ["Krea_Amateur_V4.safetensors", "krea-smartphone-photo-slider.safetensors"])
def test_the_committed_commented_out_look_loras_fail_when_uncommented(filename: str, tmp_path: Path) -> None:
    physical = L.physical_lines((CONFIG / "models.list").read_text(encoding="utf-8"))
    [number] = [n for n, text in enumerate(physical, 1) if text.startswith(f"# lora|{filename}|")]
    physical[number - 1] = physical[number - 1][2:]
    models = "\n".join(physical) + "\n"
    report = run(tmp_path, models, (CONFIG / "ollama-models.txt").read_text(encoding="utf-8"))
    assert [(p.line, "fails the Licence_Gate" in p.reason, "no Image" in p.reason) for p in report.by_group("licence")] == [
        (number, True, True)
    ], report.text()
    assert report.by_group("licence-record") == [], report.text()


# ---- placement (Req 2.4) ----


def test_a_line_without_a_record_fails_naming_the_line(tmp_path: Path) -> None:
    report = run(tmp_path, "# a comment\n" + line("a"), "qwen3:8b|wf:global,influencer\n")
    assert [(p.path, p.line) for p in report.by_group("licence")] == [
        ("config/models.list", 2), ("config/ollama-models.txt", 1)
    ], report.text()
    assert all("no '# licence:' record" in p.reason for p in report.by_group("licence"))


def test_a_record_separated_by_a_blank_line_does_not_count(tmp_path: Path) -> None:
    report = run(tmp_path, "# licence: apache-2.0\n\n" + line("a"))
    assert lines_of(report, "licence") == {3}, report.text()


def test_two_records_in_one_block_resolve_to_the_nearer_one(tmp_path: Path) -> None:
    far_bad = run(tmp_path / "a", "# licence: cc-by-nc-4.0\n# licence: apache-2.0\n" + line("a"))
    assert far_bad.findings == (), far_bad.text()
    near_bad = run(tmp_path / "b", "# licence: apache-2.0\n# licence: cc-by-nc-4.0\n" + line("a"))
    assert [(p.line, "'cc-by-nc-4.0' (line 2)" in p.reason) for p in near_bad.by_group("licence")] == [(3, True)]


def test_a_run_of_consecutive_lines_shares_the_record_above_its_first_line(tmp_path: Path) -> None:
    models = "# licence: qwen-research\n" + line("a", "wf:qwen21") + line("b") + line("c", "wf:base,influencer")
    report = run(tmp_path, models)
    assert lines_of(report, "licence") == {3, 4}, report.text()
    assert all("'qwen-research' (line 1)" in p.reason for p in report.by_group("licence"))


def test_lines_without_an_influencer_id_need_no_record(tmp_path: Path) -> None:
    assert run(tmp_path, line("a", "wf:base"), "dolphin3:8b|wf:krea2\n").findings == ()


# ---- the licence-record rule and presence ----


@pytest.mark.parametrize("ident", ["krea-2-community-license", "minimax-h3-community-license-agreement", "llama3.1"])
def test_a_mapped_licence_whose_conditions_differ_from_the_map_fails(ident: str, tmp_path: Path) -> None:
    drifted = CONDITIONS[ident].replace(";", ",", 1)
    missing = run(tmp_path / "a", f"# licence: {ident}\n" + line("a"))
    differs = run(tmp_path / "b", f"# licence: {ident}; conditions: {drifted}\n" + line("a"))
    spaced = run(tmp_path / "c", f"# licence: {ident}; conditions: {'  '.join(CONDITIONS[ident].split(' '))}\n" + line("a"))
    assert [p.line for p in missing.by_group("licence-record")] == [1], missing.text()
    assert [(p.line, ident in p.reason) for p in differs.by_group("licence-record")] == [(1, True)], differs.text()
    assert spaced.findings == (), spaced.text()  # whitespace runs compare as one space


@pytest.mark.parametrize("ident", ["other", "civitai-model-license", "none stated"])
def test_an_unclassifiable_record_without_terms_fails(ident: str, tmp_path: Path) -> None:
    report = run(tmp_path, f"# licence: {ident}\n" + line("a"))
    assert [(p.line, "terms:" in p.reason) for p in report.by_group("licence-record")] == [(1, True)], report.text()
    assert report.by_group("licence") == []


def test_an_unparsable_record_fails_the_record_rule(tmp_path: Path) -> None:
    report = run(tmp_path, "# licence: Apache License 2.0\n" + line("a"))
    assert [(p.line, "does not parse" in p.reason) for p in report.by_group("licence-record")] == [(1, True)], report.text()


def test_an_identifier_both_listed_and_mapped_fails(tmp_path: Path) -> None:
    licences = {X.NONCOMMERCIAL: "qwen-research\nshared-id\n", X.CONDITIONS: "# c\nshared-id|some text\n"}
    report = run(tmp_path, licences=licences)
    assert [(p.path, p.line, "'shared-id'" in p.reason) for p in report.by_group("licence-record")] == [
        ("config/influencer/licences/conditions.txt", 2, True)
    ], report.text()


@pytest.mark.parametrize("case, licences, reason", [
    ("missing list", {X.CONDITIONS: "a|b\n"}, "the Noncommercial_List is missing"),
    ("only comments", {X.NONCOMMERCIAL: "# one\n\n# two\n", X.CONDITIONS: "a|b\n"}, "the Noncommercial_List lists no licence identifiers"),
    ("missing map", {X.NONCOMMERCIAL: "qwen-research\n"}, "the conditions map is missing"),
])  # fmt: skip
def test_presence_names_a_missing_or_empty_licence_file(case: str, licences: dict[str, str], reason: str, tmp_path: Path) -> None:
    report = run(tmp_path, licences=licences, groups=["presence"])
    found = [p for p in report.by_group("presence") if "licences/" in p.path]
    assert [p.reason for p in found] == [reason], report.text()


# ---- the committed Config_Lists ----


def test_the_committed_lists_pass_and_every_shared_influencer_line_has_a_record() -> None:
    ctx = C.Context(REPO, CONFIG, SAFETY, {"PATH": ""})
    assert C._presence_licences(ctx) == []
    report = C.run_checks(ctx, LICENCE_GROUPS)
    assert report.findings == (), report.text()
    shared = [(name, ln) for name, ln, _ in C.licence_lines(ctx) if not C.influencer_only(ln.tag)]
    assert shared, "the committed lists hold shared lines tagged with an Influencer_Id"
    assert all(record is not None for _, _, record in C.licence_lines(ctx))


def test_the_committed_files_hold_the_design_seed() -> None:
    assert {"qwen-research", "flux-1-dev-non-commercial-license", "flux-non-commercial-license"} <= NONCOMMERCIAL
    assert sum(ident.startswith("cc-by-nc") for ident in NONCOMMERCIAL) == 8
    assert set(CONDITIONS) == {"krea-2-community-license", "minimax-h3-community-license-agreement", "llama3.1"}
    assert not NONCOMMERCIAL & set(CONDITIONS)
