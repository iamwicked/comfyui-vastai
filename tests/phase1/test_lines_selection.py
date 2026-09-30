"""Tests of scripts/influencer/lines.py: config lines, install paths and the Python Selection model (task 6,
design §A12, §M10; Req 26.2, 26.3, 26.13, 26.14).

- Property 41: Line_Printer and Line_Reader round-trip every valid line record and reject the other shapes
- Property 42: on generated Config_Lists the Python Selection and `/bin/bash scripts/provision.sh --selection`
  agree byte for byte on every run; one bash process per configuration runs all of its runs
- unit tests: the kind folders against provision.sh's kind_to_dir, subdir, workflow lines, a trailing `|`,
  wf: with spaces, shared install paths, the Selection rules, and the bash's CRLF, whitespace and tag-field
  quirks checked against the bash itself
- the repo's config/: every Catalog id, global and all, byte for byte against provision.sh
"""
from __future__ import annotations

import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers.harness import REPO, run_bounded
from strategies import (
    FIELD_COUNTS as STRATEGY_FIELD_COUNTS,
    KIND_DIRS as STRATEGY_KIND_DIRS,
    LINE_BREAKS as STRATEGY_LINE_BREAKS,
    ConfigLists,
    config_lists,
    field_text,
    is_active as strategy_is_active,
    temp_config_dir,
)

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import lines  # noqa: E402
from influencer.errors import ValidationError  # noqa: E402
from influencer.lines import CATALOG, MODELS, NODES, OLLAMA  # noqa: E402

BASH = "/bin/bash"  # on the Mac, bash 3.2, the oldest bash the Provisioner runs under
PROVISION = "scripts/provision.sh"  # repo-relative, as in its usage lines
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# Runs `bash scripts/provision.sh --selection <run>` once per run, all in one process. After each run's output
# comes a NUL byte, which no Selection holds (a bash string cannot), then the run's exit status and "\n".
RUN_SELECTIONS = r"""
script=$1; shift
for run in "$@"; do
  /bin/bash "$script" --selection "$run"
  printf '\0%s\n' "$?"
done
"""


Results = list[tuple[int, bytes]]  # (exit status, stdout) of each run


def bash_selections(config_dir: Path, sandbox: Path, runs: Sequence[str]) -> tuple[Results, bytes]:
    """(exit status, stdout) of `/bin/bash scripts/provision.sh --selection <run>` for each run, from the repo
    root, and the runs' stderr together. The environment is PATH, CONFIG_DIR, and HOME, TMPDIR, WORKSPACE and
    COMFYUI_DIR inside sandbox; nothing else is inherited."""
    home, tmpdir, workspace = sandbox / "home", sandbox / "tmp", sandbox / "workspace"
    for folder in (home, tmpdir, workspace):
        folder.mkdir(exist_ok=True)
    env = {
        "PATH": SYSTEM_PATH,
        "HOME": str(home),
        "TMPDIR": str(tmpdir),
        "WORKSPACE": str(workspace),
        "COMFYUI_DIR": str(workspace / "ComfyUI"),
        "CONFIG_DIR": str(config_dir),
    }
    argv = [BASH, "-c", RUN_SELECTIONS, "run-selections", PROVISION, *runs]
    result = run_bounded(argv, timeout=60 + 5 * len(runs), cwd=REPO, env=env)
    assert result.returncode == 0, result.stderr.decode("utf-8", "backslashreplace")
    chunks = result.stdout.split(b"\0")
    assert len(chunks) == len(runs) + 1, result.stdout
    results, output = [], chunks[0]
    for chunk in chunks[1:]:
        status, _, rest = chunk.partition(b"\n")
        results.append((int(status), output))
        output = rest
    assert output == b"", result.stdout
    return results, result.stderr


def python_selection(files: Mapping[str, str | None], run: str) -> bytes:
    return lines.select(files, run).render().encode("utf-8", "surrogateescape")


def read_config(folder: Path) -> dict[str, str | None]:
    """The four files' text as the bash reads their bytes, or None for a missing file."""
    paths = {name: folder / name for name in lines.CONFIG_FILES}
    return {
        name: path.read_bytes().decode("utf-8", "surrogateescape") if path.is_file() else None
        for name, path in paths.items()
    }


def first_difference(run: str, bash: bytes, python: bytes) -> str:
    bash_lines, python_lines = bash.split(b"\n"), python.split(b"\n")
    for number, (ours, theirs) in enumerate(zip(python_lines, bash_lines), 1):
        if ours != theirs:
            return f"run {run!r}, line {number}: bash printed {theirs!r}, Python {ours!r}"
    return f"run {run!r}: bash printed {len(bash_lines)} lines, Python {len(python_lines)}"


def assert_selections_agree(files: Mapping[str, str | None], runs: Sequence[str], results: Results) -> None:
    for run, (status, output) in zip(runs, results):
        assert status == 0, f"provision.sh --selection {run!r} exited {status}"
        python = python_selection(files, run)
        assert output == python, first_difference(run, output, python)


# ---- Property 41 ----

_WHITESPACE = tuple(char for char in map(chr, range(0x3001)) if char.isspace())  # U+3000 is the last one
_REJECTED = {  # a rejected shape -> the start of the reason its Problem gives
    "no field": "has no field",
    "empty first field": "is empty",
    "first field starts with #": "starts with '#'",
    "field holds |": "contains '|'",
    "field holds a line break": "contains a line break",
    "leading or trailing whitespace": "has leading or trailing whitespace",
}


@st.composite
def line_records(draw: st.DrawFn, name: str) -> list[str]:
    """A valid line record of the file name: its field count, a first field that is non-empty and does not
    start with #, and no field holding |, a line break, or leading or trailing whitespace."""
    low, high = STRATEGY_FIELD_COUNTS[name]
    return [draw(field_text(first=True)), *draw(st.lists(field_text(), min_size=low - 1, max_size=high - 1))]


@st.composite
def rejected_records(draw: st.DrawFn, record: list[str]) -> tuple[str, list[str]]:
    """(shape, record) with one fault of a shape the printer rejects put into a valid record."""
    shape = draw(st.sampled_from(sorted(_REJECTED)))
    fields = list(record)
    if shape == "no field":
        return shape, []
    if shape == "empty first field":
        fields[0] = ""
    elif shape == "first field starts with #":
        fields[0] = "#" + fields[0]
    else:
        index = draw(st.integers(0, len(fields) - 1))
        field = fields[index]
        if shape == "leading or trailing whitespace":
            space = draw(st.sampled_from(_WHITESPACE))
            fields[index] = draw(st.sampled_from((space + field, field + space)))
        else:
            char = "|" if shape == "field holds |" else draw(st.sampled_from(lines.LINE_BREAKS))
            at = draw(st.integers(0, len(field)))
            fields[index] = field[:at] + char + field[at:]
    return shape, fields


# Feature: ai-influencer-pipeline, Property 41: Config line round trip
@given(name=st.sampled_from(lines.CONFIG_FILES), data=st.data())
def test_config_line_round_trip(name: str, data: st.DataObject) -> None:
    record = data.draw(line_records(name), label="record")
    line = lines.line_printer(record)
    for ending in ("", "\n", "\r\n"):
        assert lines.line_reader(line + ending) == record
    assert lines.active_lines(line + "\n") == [(1, line)]  # one physical line, and an active one

    shape, rejected = data.draw(rejected_records(record), label="rejected")
    with pytest.raises(ValidationError) as caught:
        lines.line_printer(rejected)
    assert any(problem.reason.startswith(_REJECTED[shape]) for problem in caught.value.problems), caught.value


# ---- Property 42 ----


# Feature: ai-influencer-pipeline, Property 42: Python and bash Selections agree
@settings(max_examples=100, deadline=None)
@given(config=config_lists())
def test_python_and_bash_selections_agree(config: ConfigLists) -> None:
    runs = [*config.ids, lines.GLOBAL_RUN, lines.FULL_RUN]
    with temp_config_dir(config) as config_dir:
        results, stderr = bash_selections(config_dir, config_dir.parent, runs)
    assert stderr == b""
    assert_selections_agree(config.files(), runs, results)


# ---- unit tests: the tables, Line_Reader and Line_Printer, the active-line rule ----


def test_the_tables_match_provision_sh_and_the_strategies(tmp_path: Path) -> None:
    source = (REPO / PROVISION).read_text(encoding="utf-8")
    function = re.search(r"^kind_to_dir\(\) \{\n.*?^\}\n", source, re.M | re.S)
    assert function, "provision.sh has no kind_to_dir()"
    arms = dict(re.findall(r"^ +([a-z_]+)\) +echo ([a-z_]+) ;;$", function.group(0), re.M))
    assert arms == lines.KIND_DIRS == STRATEGY_KIND_DIRS
    # the bash function itself, run on every kind, `workflow` and an unknown kind
    kinds = [*lines.KIND_DIRS, lines.WORKFLOW_KIND, "mystery"]
    script = function.group(0) + 'for kind in "$@"; do printf "%s\\n" "$(kind_to_dir "$kind")"; done'
    argv = [BASH, "-c", script, "kind-to-dir", *kinds]
    result = run_bounded(argv, timeout=30, cwd=tmp_path, env={"PATH": SYSTEM_PATH})
    assert result.stdout.decode().splitlines() == [lines.kind_folder(kind) for kind in kinds]
    assert lines.FIELD_COUNTS == STRATEGY_FIELD_COUNTS
    assert lines.LINE_BREAKS == STRATEGY_LINE_BREAKS


def test_line_reader_keeps_whitespace_and_drops_the_line_ending() -> None:
    assert lines.line_reader(" a | b \t\r\n") == [" a ", " b \t"]
    assert lines.line_reader("a|b\r") == ["a", "b"]
    assert lines.line_reader("a|b\r\r\n") == ["a", "b\r"]  # one \r, as ${line%$'\r'} drops one
    assert lines.line_reader("") == [""]


def test_line_printer_reports_every_fault() -> None:
    with pytest.raises(ValidationError) as caught:
        lines.line_printer(["#x", " y", "a|b", "c\u2028"], path="models.list")
    assert [str(problem) for problem in caught.value.problems] == [
        "models.list: field 1: starts with '#', which makes the line a comment",
        "models.list: field 2: has leading or trailing whitespace",
        "models.list: field 3: contains '|'",
        "models.list: field 4: contains a line break",
        "models.list: field 4: has leading or trailing whitespace",  # U+2028 is whitespace as well
    ]
    with pytest.raises(ValidationError, match="line record: field 1: is empty"):
        lines.line_printer([""])
    assert lines.line_printer(["a b", "", "c\td"]) == "a b||c\td"  # inner spaces, empty later fields: fine


def test_physical_and_active_lines() -> None:
    text = "a\r\n\n  \t\r\n  # note\n\tb|c\n\v\f\nlast"
    assert lines.physical_lines(text) == ["a\r", "", "  \t\r", "  # note", "\tb|c", "\v\f", "last"]
    assert lines.active_lines(text) == [(1, "a\r"), (5, "\tb|c"), (7, "last")]
    assert lines.physical_lines("") == [] and lines.physical_lines("x\n") == ["x"]
    assert lines.physical_lines("a\u2028b\x85c\n") == ["a\u2028b\x85c"]  # only \n ends a line
    assert lines.is_active("\xa0") and lines.is_active("\xa0# x")  # a no-break space is no C-locale space


# ---- unit tests: install paths (Req 26.3) ----


def test_default_folder_per_kind() -> None:
    text = "".join(f"{kind}|x.safetensors|hf|o/r||wf:a\n" for kind in lines.KIND_DIRS)
    paths = [lines.install_path(line) for line in lines.parse_models(text)]
    assert paths == [f"models/{folder}/x.safetensors" for folder in lines.KIND_DIRS.values()]


def test_empty_nested_and_trimmed_subdir() -> None:
    text = (
        "lora|a.safetensors|hf|o/r||wf:a\n"
        "lora|b.safetensors|hf|o/r|loras/looks|wf:a\n"
        "lora| c.safetensors |hf|o/r| vosr2/VOSR2 |wf:a\n"
        "mystery|d.bin|url|https://h.example/d.bin||\n"  # an unknown kind is its own folder, as in kind_to_dir
    )
    assert [lines.install_path(line) for line in lines.parse_models(text)] == [
        "models/loras/a.safetensors",
        "models/loras/looks/b.safetensors",
        "models/vosr2/VOSR2/c.safetensors",
        "models/mystery/d.bin",
    ]


def test_workflow_lines_install_to_the_workflows_folder() -> None:
    text = (
        "workflow|flow.json|url|https://h.example/flow.json||wf:a\n"
        " workflow |pack.zip|hf|o/r::pack.zip|ignored|\n"  # trimmed kind; a subdir that does not count
    )
    assert [lines.install_path(line) for line in lines.parse_models(text)] == [
        "comfyui-user/default/workflows/flow.json",
        "comfyui-user/default/workflows/pack.zip",  # subdir does not count for a workflow
    ]


def test_a_trailing_pipe_adds_an_empty_field() -> None:
    assert lines.line_reader("llama3|") == ["llama3", ""]
    [ollama] = lines.parse_ollama("llama3:8b|\n")
    assert ollama.fields == ("llama3:8b", "") and ollama.tag == "" and ollama.ids == ()
    [node] = lines.parse_nodes("https://github.com/o/r|\n")
    assert node.values == ("https://github.com/o/r", "") and node.tag == ""
    [model] = lines.parse_models("vae|v.pt|hf|o/r||wf:a|\n")  # a seventh field; the sixth is still the tag
    assert len(model.fields) == 7 and model.tag == "wf:a" and model.ids == ("a",)


def test_wf_tags_with_spaces() -> None:
    assert lines.parse_tag("wf:a, b") == ("a", "b")
    assert lines.parse_tag("  wf: a ,b  ") == ("a", "b")
    assert lines.parse_tag("wf:a b") == ("ab",)  # spaces are removed, not split on
    assert lines.parse_tag("wf:a,,b") == ("a", "", "b")  # an empty item stays, for validation to report
    assert lines.parse_tag("") == () and lines.parse_tag(" \t") == ()
    assert lines.parse_tag("a,b") == ("a", "b")  # bash, not §A12: the wf: is optional
    [model] = lines.parse_models("vae|v.pt|hf|o/r|| wf:a , b \n")
    assert model.tag == "wf:a , b" and model.ids == ("a", "b")
    assert lines.tag_has(model.tag, "b") and not lines.tag_has(model.tag, "a ,b")


def test_two_active_lines_sharing_an_install_path() -> None:
    text = (
        "lora|x.safetensors|hf|o/r::x.safetensors||wf:a\n"  # line 1: models/loras/x.safetensors
        "checkpoint|x.safetensors|hf|o/r::x.safetensors|loras|wf:b\n"  # the same path and source: allowed
        "# lora|x.safetensors|url|https://h.example/x.safetensors||wf:c\n"  # inactive, so it does not count
        "lora|x.safetensors|civitai|123||wf:c\n"  # line 4: the same path, another source
        "lora|x.safetensors|hf|o/other::x.safetensors|loras|\n"  # line 5: the same path, another source id
        "lora|x.safetensors|hf|o/r::x.safetensors|loras/sub|\n"  # another path
    )
    records = lines.parse_models(text)
    assert {lines.install_path(record) for record in records[:4]} == {"models/loras/x.safetensors"}
    conflicts = lines.install_path_conflicts(records)
    assert [(first.number, later.number) for first, later in conflicts] == [(1, 4), (1, 5)]


# ---- unit tests: the Selection model ----

SMALL = {
    CATALOG: "# id|in_all|min_comfyui|description\na|yes||A\nb|no||B, opt-in\nc|yes|0.3|C\n",
    MODELS: (
        "vae|shared.pt|hf|o/r||\n"
        "lora|a.safetensors|hf|o/r||wf:a\n"
        "lora|b.safetensors|hf|o/r|loras/b|wf:b\n"
        "clip|g.pt|hf|o/r||wf:global,b\n"
    ),
    NODES: "https://github.com/o/n|abc1234|wf:c\n",
    OLLAMA: None,  # a missing list prints its header only
}


def test_the_three_kinds_of_run() -> None:
    assert lines.select(SMALL, "all").render() == (
        "[models.list]\nvae|shared.pt|hf|o/r|\nlora|a.safetensors|hf|o/r|\n"
        "[nodes.txt]\nhttps://github.com/o/n|abc1234\n[ollama-models.txt]\n"
    )
    assert lines.select(SMALL, "global").render() == (
        "[models.list]\nvae|shared.pt|hf|o/r|\nclip|g.pt|hf|o/r|\n[nodes.txt]\n[ollama-models.txt]\n"
    )
    assert lines.select(SMALL, "b").render() == (
        "[models.list]\nlora|b.safetensors|hf|o/r|loras/b\nclip|g.pt|hf|o/r|\n"
        "[nodes.txt]\n[ollama-models.txt]\n"
    )
    assert [line.number for line in lines.select(SMALL, "b").models] == [3, 4]


def test_an_unknown_run_or_a_missing_catalog_is_rejected() -> None:
    for run in ("d", "", " a", "A"):
        with pytest.raises(ValidationError, match="unknown workflow"):
            lines.select(SMALL, run)
    with pytest.raises(ValidationError, match="catalog.txt: missing"):
        lines.select({MODELS: SMALL[MODELS]}, "all")


# The bash's quirks: CRLF, whitespace in and around fields, where the tag field is, a tag without wf:, and
# the full run splitting a tag on IFS where tag_has removes only spaces.
QUIRKS: dict[str, str] = {
    CATALOG: (
        "# CRLF, an untrimmed id, a short in_all line and no final newline\n"
        "amber|yes||Amber\r\n"
        "  birch|yes||Birch, its id untrimmed\n"
        "cedar|no||Cedar, opt-in\n"
        "dune|yes\r\n"  # awk's $2 is "yes\r": not in_all
        "ember\n"
        "gorse\r\n"  # one field: cut's id is "gorse\r"
        "fern|yes|1.2|Fern"
    ),
    MODELS: (
        "checkpoint|a.safetensors|hf|o/r||wf:amber\r\n"
        " lora | b.safetensors | hf | o/r | loras/x | wf: birch , cedar \n"
        "vae|c.pt|url|https://h.example/c.pt||wf:dune,\tfern\n"  # full run: fern; fern run: \tfern
        "clip|d.pt|hf|o/r|||wf:amber\n"  # the sixth field is the tag, and it is empty
        "clip|e.pt|hf|o/r||wf:fern|wf:amber\n"  # a seventh field is ignored
        "diffusion|f.pt|hf|o/r\n"  # four fields: subdir and tag empty
        "upscale|g.pt|hf|o/r||fern\n"  # no wf:
        "text_encoder|h.pt|hf|o/r||wf:amber cedar\n"  # full run: amber and cedar; tag_has: ambercedar
        "sams|i.pt|hf|o/r||wf:\vamber\n"  # full run: \vamber; tag_has trims it to amber
        "controlnet|j.pt|hf|o/r||\xa0wf:amber\n"  # a no-break space is kept, so the wf: stays
        "embedding|k.pt|hf|o/r||wf:\n"  # lists nothing
        "detection|l.pt|hf|o/r||wf:global\r\r\n"  # the read drops one \r, trim the other
        "  # an indented comment\n"
        "\t\r\n"
        "\xa0\n"  # active: the kind is a no-break space
        "model_patch|m.pt|hf|o/r||wf:amber,global\r"
    ),
    NODES: (
        "https://github.com/o/one\r\n"
        "https://github.com/o/two|wf:amber\n"  # two fields: the ref is wf:amber, and the line is untagged
        "https://github.com/o/three| abc1234 |wf:birch\n"
        "https://github.com/o/four||wf:global,amber|x\n"
    ),
    OLLAMA: "llama3:8b\nqwen|wf:cedar\r\n mistral | wf:global \ngemma|wf:amber|extra",
}
QUIRK_RUNS = ("amber", "  birch", "cedar", "dune", "ember", "gorse\r", "fern", "global", "all")
QUIRK_UNKNOWN = ("birch", "gorse", "", "nope")  # provision.sh exits 1 for each


def test_the_bash_quirks_agree_with_provision_sh(tmp_path: Path) -> None:
    catalog = lines.parse_catalog(QUIRKS[CATALOG])
    assert [line.id for line in catalog] == list(QUIRK_RUNS[:-2])
    assert [line.id for line in catalog if line.in_all] == ["amber", "  birch", "fern"]
    config = tmp_path / "config"
    config.mkdir()
    for name, text in QUIRKS.items():
        (config / name).write_bytes(text.encode("utf-8"))
    results, _ = bash_selections(config, tmp_path, [*QUIRK_RUNS, *QUIRK_UNKNOWN])
    assert_selections_agree(QUIRKS, QUIRK_RUNS, results[: len(QUIRK_RUNS)])
    for run, (status, output) in zip(QUIRK_UNKNOWN, results[len(QUIRK_RUNS) :]):
        assert (status, output) == (1, b""), run
        with pytest.raises(ValidationError, match="unknown workflow"):
            lines.select(QUIRKS, run)
    # a few of the outcomes above, spelled out
    full = lines.select(QUIRKS, "all").render()
    assert "\nvae|c.pt|" in full and "\nvae|c.pt|" not in lines.select(QUIRKS, "fern").render()
    assert "\ntext_encoder|h.pt|" in full and "\nsams|i.pt|" not in full
    assert "\nsams|i.pt|" in lines.select(QUIRKS, "amber").render()
    assert "\n\xa0||||\n" in full and "controlnet" not in full + lines.select(QUIRKS, "amber").render()


# ---- the repo's config/ ----


def test_the_repo_config_selections_match_provision_sh(tmp_path: Path) -> None:
    config = REPO / "config"
    files = read_config(config)
    assert files[CATALOG] is not None
    ids = [line.split("|")[0] for line in files[CATALOG].split("\n") if strategy_is_active(line)]
    assert [line.id for line in lines.parse_catalog(files[CATALOG])] == ids
    runs = list(dict.fromkeys([*ids, lines.GLOBAL_RUN, lines.FULL_RUN]))
    results, stderr = bash_selections(config, tmp_path, runs)
    assert stderr == b""
    assert_selections_agree(files, runs, results)
