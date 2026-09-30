"""Tests of Job identity, scripts/influencer/ids.py (task 11; design §A1, §A2, §M5; Req 5.10, 10.1, 10.2,
10.11, 11.5, 20.4, 21.8).

- Property 24: seeds are in range, and a separate Python process gives the same values
- Property 25: the Candidates of one Slide have pairwise different seeds
- Property 26: the Job_Key follows every prompt value and every input byte
- Property 9: every Job id, output prefix, export, QC_Sheet and Publish_Sheet path stays inside the
  Output_Root, in the Post's own folder
- Property 10: the paths of platform `other` Posts are inside <W>/other/, an Instagram Post's outside it
- the naming edge cases: safe names, the s080 strength form, refine and grid ids, three-digit Slide
  numbers, upload names and the validation of every component
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import calendar_io as C  # noqa: E402
from influencer import ids as I  # noqa: E402
from influencer import templates as T  # noqa: E402
from strategies_calendar import calendars, weeks  # noqa: E402
from strategies_ids import post_ids  # noqa: E402
from strategies_persona import personas  # noqa: E402
from strategies_templates import templates, values_for  # noqa: E402

# A folder that need not exist: Path.resolve() is not strict, and resolves the system's own links alike
# on both sides of every comparison.
COMFY_OUTPUT = Path(tempfile.gettempdir()) / "ids-test" / "ComfyUI" / "output"
SEED_RANGE = range(0, 2**32)

# ---- Property 24 ----

_BATCH: list[tuple[str, str, int, int, int]] = []  # (W, P, s, c, seed) of every example of one run

_CHILD = """
import json, sys
from influencer.ids import seed_for
with open(sys.argv[1], encoding="utf-8") as handle:
    batch = json.load(handle)
print(json.dumps([seed_for(w, p, s, c) for w, p, s, c in batch]))
"""


def _then_in_a_subprocess(test):
    """Run the Hypothesis test, then send every example it tried, as one batch in a temp file, to one
    separate Python process with its own hash seed and working folder, and compare its seeds."""

    def run() -> None:
        _BATCH.clear()
        test()
        assert _BATCH, "the property ran no example"
        environ = {**os.environ, "PYTHONPATH": str(SCRIPTS), "PYTHONHASHSEED": "12345"}
        batch = [[w, p, s, c] for w, p, s, c, _ in _BATCH]
        with tempfile.TemporaryDirectory() as folder:
            batch_file = Path(folder) / "batch.json"
            batch_file.write_text(json.dumps(batch), encoding="utf-8")
            done = subprocess.run(
                [sys.executable, "-c", _CHILD, str(batch_file)], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, env=environ, cwd=folder, timeout=30, check=False,
            )  # fmt: skip
        assert done.returncode == 0, done.stderr
        theirs = json.loads(done.stdout)
        mismatches = [(e[:4], e[4], t) for e, t in zip(_BATCH, theirs) if e[4] != t]
        assert len(theirs) == len(_BATCH) and not mismatches, mismatches[:5]

    run.__name__ = test.__name__
    run.__doc__ = test.__doc__
    return run


# Feature: ai-influencer-pipeline, Property 24: Seeds are deterministic and in range
@_then_in_a_subprocess
@settings(max_examples=200, deadline=None)
@given(weeks(), post_ids(), st.integers(1, 20), st.integers(1, 4))
def test_seeds_are_deterministic_and_in_range(week, post, slide, candidate):
    seed = I.seed_for(week, post, slide, candidate)
    assert type(seed) is int and seed in SEED_RANGE
    assert I.seed_for(week, post, slide, candidate) == seed
    _BATCH.append((week, post, slide, candidate, seed))


# ---- Property 25 ----


# Feature: ai-influencer-pipeline, Property 25: Candidate seeds differ within a Slide
@given(weeks(), post_ids(), st.integers(1, 20))
def test_candidate_seeds_differ_within_a_slide(week, post, slide):
    seeds = [I.seed_for(week, post, slide, candidate) for candidate in range(1, 5)]
    assert len(set(seeds)) == 4, seeds


# ---- Property 26 ----


def _leaves(value: Any, path: tuple = ()) -> list[tuple]:
    """The paths of every non-container value inside a filled prompt."""
    if isinstance(value, dict):
        return [leaf for key, item in value.items() for leaf in _leaves(item, (*path, key))]
    if isinstance(value, list):
        return [leaf for index, item in enumerate(value) for leaf in _leaves(item, (*path, index))]
    return [path]


def _changed(value: Any) -> Any:
    """A value that prints differently in canonical JSON."""
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        return value + "x"
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return -value if value != 0 else 1.0
    return 0  # None


def _set(prompt: Any, path: tuple, value: Any) -> None:
    for step in path[:-1]:
        prompt = prompt[step]
    prompt[path[-1]] = value


def _reordered(value: Any) -> Any:
    """value with every mapping's keys in reverse order: equal, but inserted differently."""
    if isinstance(value, dict):
        return {key: _reordered(value[key]) for key in reversed(list(value))}
    if isinstance(value, list):
        return [_reordered(item) for item in value]
    return value


def _inputs(files: list[tuple[bytes, str]]) -> dict[str, bytes]:
    return {I.upload_name(data, ext): data for data, ext in files}


FILLED = templates().flatmap(lambda t: st.tuples(st.just(t), values_for(t)))
INPUT_FILES = st.lists(st.tuples(st.binary(min_size=1, max_size=64), st.sampled_from((".png", ".jpg", ".wav", ""))),
                       max_size=4)  # fmt: skip


# Feature: ai-influencer-pipeline, Property 26: Job_Key tracks prompt values and input bytes
@settings(deadline=None)
@given(FILLED, INPUT_FILES, st.data())
def test_job_key_tracks_prompt_values_and_input_bytes(filled, files, data):
    template, values = filled
    prompt = T.fill(template, values)
    key = I.job_key(prompt, _inputs(files))
    assert len(key) == 64 and int(key, 16) >= 0
    # equal inputs, built again and inserted in another order, give the same key
    again = dict(reversed(list(_inputs(files).items())))
    assert I.job_key(_reordered(T.fill(template, values)), again) == key

    choices = ["prompt"] + (["input"] if files else [])
    if data.draw(st.sampled_from(choices), label="mutate") == "prompt":
        leaf = data.draw(st.sampled_from(_leaves(prompt)), label="leaf")
        mutated = copy.deepcopy(prompt)
        _set(mutated, leaf, _changed(copy.deepcopy(mutated_value(prompt, leaf))))
        assert I.job_key(mutated, _inputs(files)) != key
    else:
        index = data.draw(st.integers(0, len(files) - 1), label="file")
        old, ext = files[index]
        at = data.draw(st.integers(0, len(old) - 1), label="byte")
        flip = data.draw(st.integers(1, 255), label="xor")
        new = old[:at] + bytes([old[at] ^ flip]) + old[at + 1 :]
        assert I.upload_name(new, ext) != I.upload_name(old, ext)
        changed = [*files[:index], (new, ext), *files[index + 1 :]]
        assert I.job_key(prompt, _inputs(changed)) != key
        # the key hashes the bytes themselves too: new bytes under the old name also change it
        same_names = {name: (new if name == I.upload_name(old, ext) else content)
                      for name, content in _inputs(files).items()}  # fmt: skip
        assert I.job_key(prompt, same_names) != key


def mutated_value(prompt: Any, path: tuple) -> Any:
    for step in path:
        prompt = prompt[step]
    return prompt


# ---- the paths of a calendar (Properties 9 and 10) ----


def _post_paths(slug: str, week: str, post: C.Post) -> dict[str, list[str]]:
    """Every path built for post: its Job ids, its output prefixes (ComfyUI-output relative) and its
    exports, QC_Sheet and Publish_Sheet (Output_Root relative)."""
    ident, platform = post.id, post.platform
    jobs: list[str] = []
    prefixes: list[str] = []
    if post.type == "reel":
        jobs.append(I.reel_id(ident))
        prefixes.append(I.reel_prefix(slug, week, ident, platform))
        exports = [I.export_path(week, ident, platform, 1, ".mp4")]
    else:
        for slide in range(1, post.slides + 1):
            for candidate in range(1, post.candidates + 1):
                jobs.append(I.render_id(ident, slide, candidate))
                prefixes.append(I.render_prefix(slug, week, ident, platform, slide, candidate))
            jobs.append(I.finish_id(ident, slide))
            prefixes.append(I.finish_prefix(slug, week, ident, platform, slide))
        exports = [I.export_path(week, ident, platform, n, ".jpg") for n in range(1, post.slides + 1)]
    sheets = [I.qc_sheet_path(week, ident, platform), I.publish_sheet_path(week, ident, platform)]
    return {"jobs": jobs, "prefixes": prefixes, "files": exports + sheets}


def _inside(path: Path, folder: Path) -> bool:
    return path != folder and path.is_relative_to(folder)


PERSONA_CALENDARS = personas().flatmap(lambda persona: st.tuples(st.just(persona), calendars(persona)))


# Feature: ai-influencer-pipeline, Property 9: Every output path stays inside the Output_Root
@settings(deadline=None)
@given(PERSONA_CALENDARS)
def test_every_output_path_stays_inside_the_output_root(persona_calendar):
    persona, calendar = persona_calendar
    slug, week = persona.slug, calendar.week
    output_root = COMFY_OUTPUT / "influencer" / slug
    root = output_root.resolve()
    for post in calendar.posts:
        own = root / week / ("other" if post.platform == "other" else "") / post.id  # "" adds no segment
        paths = _post_paths(slug, week, post)
        for job in paths["jobs"]:
            assert _inside((output_root / job).resolve(), root), job
        for prefix in paths["prefixes"]:
            assert prefix.startswith(f"influencer/{slug}/"), prefix
            resolved = (COMFY_OUTPUT / prefix).resolve()
            assert _inside(resolved, root) and _inside(resolved, own), prefix
        for relative in paths["files"]:
            resolved = (output_root / relative).resolve()
            assert _inside(resolved, root) and resolved.parent == own, relative


# Feature: ai-influencer-pipeline, Property 10: Platform `other` files are confined to `other/`
@settings(deadline=None)
@given(PERSONA_CALENDARS)
def test_platform_other_files_are_confined_to_other(persona_calendar):
    persona, calendar = persona_calendar
    slug, week = persona.slug, calendar.week
    output_root = COMFY_OUTPUT / "influencer" / slug
    other = (output_root / week / "other").resolve()
    for post in calendar.posts:
        paths = _post_paths(slug, week, post)
        resolved = [(COMFY_OUTPUT / prefix).resolve() for prefix in paths["prefixes"]]
        resolved += [(output_root / relative).resolve() for relative in paths["files"]]
        for path in resolved:
            assert _inside(path, other) == (post.platform == "other"), (post.id, post.platform, path)


# ---- the naming edge cases (task 11.9) ----


def _hex6(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:6]


@pytest.mark.parametrize(
    ("name", "mapped"),
    [
        ("Img 01", "img-01"),
        ("café au lait", "caf--au-lait"),
        ("..", "--"),
        (".hidden", "-hidden"),
        ("my_lora.v2 ÉTÉ", "my-lora-v2--t-"),
        ("a/b", "a-b"),
    ],
)
def test_safe_names_map_every_unsafe_character(name, mapped):
    safe = I.safe_name(name)
    assert safe == f"{mapped}-{_hex6(name)}"
    assert safe not in (".", "..") and "/" not in safe


def test_names_that_differ_only_in_case_get_different_safe_names():
    assert I.safe_name("Img01").split("-")[0] == I.safe_name("img01").split("-")[0] == "img01"
    assert I.safe_name("Img01") != I.safe_name("img01")
    assert I.refine_prefix("ava", "v1", "Img01") != I.refine_prefix("ava", "v1", "img01")
    assert I.grid_prefix("ava", "Ava-V2", 1.0, 1) != I.grid_prefix("ava", "ava-v2", 1.0, 1)


def test_a_long_name_is_cut_before_its_hash():
    safe = I.safe_name("x" * 300)
    assert safe == "x" * I.SAFE_KEEP + "-" + _hex6("x" * 300)


def test_dataset_base_and_checkpoint_stem_paths():
    assert I.refine_id("Img 01") == f"refine/img-01-{_hex6('Img 01')}"
    assert I.refine_id(".hidden") == f"refine/-hidden-{_hex6('.hidden')}"
    assert I.refine_id("..") == f"refine/---{_hex6('..')}"  # the base of "...png"
    assert I.refine_prefix("ava", "v1", "Img 01") == f"influencer/ava/refine/v1/img-01-{_hex6('Img 01')}"
    assert I.refine_prefix("ava", "v1", "..") == f"influencer/ava/refine/v1/---{_hex6('..')}"
    assert I.grid_id("Ava Café", 0.8, 3) == f"grid/ava-caf--{_hex6('Ava Café')}/s080/p03"
    assert I.grid_id("..", 0.8, 3) == f"grid/---{_hex6('..')}/s080/p03"  # the stem of "...safetensors"
    assert I.grid_prefix("ava", "Ava Café", 0.8, 3) == f"influencer/ava/grid/ava-caf--{_hex6('Ava Café')}/s080/p03"


@pytest.mark.parametrize(("strength", "form"), [(0.8, "s080"), (1.0, "s100"), (0.1, "s010"), (1.25, "s125"), (2, "s200")])
def test_the_strength_form(strength, form):
    assert I.grid_prefix("ava", "c", strength, 1).split("/")[-2] == form
    assert I.grid_id("c", strength, 1).split("/")[2] == form


@pytest.mark.parametrize("strength", [0.805, -0.5, 10.0, float("nan"), float("inf"), True, "0.8"])
def test_a_strength_without_an_s_form_is_refused(strength):
    with pytest.raises(ValueError):
        I.grid_prefix("ava", "c", strength, 1)


def test_the_stage_ids_and_prefixes():
    assert I.render_id("a--b", 1, 2) == "render/a--b/s01/c2"
    assert I.render_id("0", 100, 4) == "render/0/s100/c4"  # a three-digit Slide number
    assert I.finish_id("0", 100) == "finish/0/s100"
    assert I.reel_id("mon-reel") == "reel/mon-reel"
    assert I.render_prefix("ava", "2026-W40", "mon", "instagram", 3, 1) == "influencer/ava/2026-W40/mon/render/mon_s03_c1"
    assert I.render_prefix("ava", "2026-W40", "mon", "other", 3, 1) == "influencer/ava/2026-W40/other/mon/render/mon_s03_c1"
    assert I.finish_prefix("ava", "2026-W40", "mon", "instagram", 12) == "influencer/ava/2026-W40/mon/finish/mon_s12"
    assert I.reel_prefix("ava", "2026-W40", "r", "other") == "influencer/ava/2026-W40/other/r/reel/r"
    assert I.export_path("2026-W40", "mon", "instagram", 2, ".jpg") == "2026-W40/mon/mon_02.jpg"
    assert I.qc_sheet_path("2026-W40", "mon", "other") == "2026-W40/other/mon/mon_qc.jpg"
    assert I.publish_sheet_path("2026-W40", "mon", "instagram") == "2026-W40/mon/mon_publish.md"


def test_seeds_follow_the_design_formula():
    base = int.from_bytes(hashlib.sha256(b"influencer-seed/1|2026-W40|mon|3").digest()[:4], "big")
    assert I.seed_for("2026-W40", "mon", 3, 1) == base
    assert I.seed_for("2026-W40", "mon", 3, 4) == (base + 3 * 0x9E3779B1) % 2**32
    assert I.reel_seed("2026-W40", "r") == I.seed_for("2026-W40", "r", 1, 1)
    refine = int.from_bytes(hashlib.sha256("influencer-seed/1|v1|Img 01".encode()).digest()[:4], "big")
    assert I.refine_seed("v1", "Img 01") == refine


@pytest.mark.parametrize(("ext", "suffix"), [(".png", ".png"), (".jpg", ".jpg"), (".wav", ".wav"), ("", ""),
                                             (".PNG", ".png")])  # fmt: skip
def test_upload_names(ext, suffix):
    digest = hashlib.sha256(b"\x00pick").hexdigest()
    assert I.upload_name(b"\x00pick", ext) == f"influencer/{digest}{suffix}"
    assert I.upload_name(b"\x00pick!", ext) != I.upload_name(b"\x00pick", ext)


@pytest.mark.parametrize("ext", ["png", ".", ".p/g", "./..", ".a b", ".abcdefghijk"])
def test_an_extension_that_is_no_suffix_is_refused(ext):
    with pytest.raises(ValueError):
        I.upload_name(b"x", ext)


def test_the_job_key_formula():
    prompt = {"b": 1, "a": "é"}
    inputs = {"influencer/b.png": b"2", "influencer/a.png": b"1"}
    digests = [f"{name}={hashlib.sha256(data).hexdigest()}" for name, data in sorted(inputs.items())]
    expected = hashlib.sha256(b'influencer-job/1\n{"a":"\xc3\xa9","b":1}\n' + "\n".join(digests).encode())
    assert I.job_key(prompt, inputs) == expected.hexdigest()
    assert I.job_key(prompt) == hashlib.sha256(b'influencer-job/1\n{"a":"\xc3\xa9","b":1}\n').hexdigest()


@pytest.mark.parametrize(
    "call",
    [
        lambda: I.render_id("other", 1, 1),  # the reserved id
        lambda: I.render_id("../x", 1, 1),
        lambda: I.render_id("Mon", 1, 1),
        lambda: I.render_id("mon", 0, 1),
        lambda: I.render_id("mon", 1000, 1),
        lambda: I.render_id("mon", 1, 5),
        lambda: I.render_id("mon", True, 1),
        lambda: I.post_folder("2025-W53", "mon", "instagram"),
        lambda: I.post_folder("2026-W40", "mon", "tiktok"),
        lambda: I.render_prefix("../ava", "2026-W40", "mon", "instagram", 1, 1),
        lambda: I.export_path("2026-W40", "mon", "instagram", 1, ".png"),
        lambda: I.refine_id(""),
        lambda: I.refine_prefix("ava", "../v1", "img"),
        lambda: I.refine_seed("v1", ""),
        lambda: I.grid_id("", 1.0, 1),
        lambda: I.safe_name(""),
        lambda: I.seed_for("2026-W40", "mon", 1, 0),
    ],
)
def test_an_invalid_component_is_refused(call):
    with pytest.raises(ValueError):
        call()
