"""Tests of the shared generators in tests/strategies.py (task 3.2).

config_lists() is checked against an independent reading of Req 26.1-26.8, so a property test that takes a
generated configuration as valid really gets a valid one.
"""
from __future__ import annotations

import re

from hypothesis import Phase, given, settings
from hypothesis import find as _find

import strategies as S

# find() without its shrink phase: these tests need an example of a shape, not the smallest one, and
# shrinking a config_lists() example takes seconds each time under the derandomized `check` profile.
_REACH = settings(max_examples=2000, phases=(Phase.explicit, Phase.reuse, Phase.generate))


def find(strategy, condition):  # noqa: ANN001, ANN201 - the signature of hypothesis.find
    return _find(strategy, condition, settings=_REACH)


# ---- texts() and field_text() ----


@given(S.texts())
def test_texts_hold_no_surrogates(text):
    text.encode("utf-8")  # a lone surrogate would raise UnicodeEncodeError


@given(S.texts(single_line=True, min_size=1, max_size=12))
def test_single_line_texts_hold_no_line_break(text):
    assert 1 <= len(text) <= 12
    assert not any(c in S.LINE_BREAKS for c in text)
    assert len(text.splitlines()) == 1


def test_texts_include_the_designs_samples():
    for sample in ('"', "no", "2026", "#x", "a: b", "\n", "\U0001f600"):
        assert sample in S.TEXT_SAMPLES
    assert "\n" in find(S.texts(), lambda t: "\n" in t and len(t) > 1)
    assert "a: b" in find(S.texts(single_line=True), lambda t: "a: b" in t)


@given(S.field_text(), S.field_text(first=True))
def test_field_text_is_a_valid_field(field, first):
    for text in (field, first):
        assert "|" not in text
        assert not any(c in S.LINE_BREAKS for c in text)
        assert text == text.strip()
    assert first and not first.startswith("#")


# ---- an independent reading of Req 26.1-26.8 ----

ID = re.compile(r"[a-z0-9-]+\Z")
VERSION = re.compile(r"[0-9]+(\.[0-9]+){1,2}\Z")
REPO_PART = r"[A-Za-z0-9._-]+"
HF = re.compile(rf"{REPO_PART}/{REPO_PART}(::(?P<path>.+))?\Z")
CIVITAI = re.compile(r"[0-9]+(::.+)?\Z")
URL = re.compile(r"https://[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*(/\S*)?\Z")
NODE_URL = re.compile(rf"https://github\.com/{REPO_PART}/(?P<repo>{REPO_PART})\Z")
HEX_TOKEN = re.compile(r"(?<![A-Za-z0-9])([0-9a-fA-F]{32}|[0-9a-fA-F]{64})(?![A-Za-z0-9])")
HF_TOKEN = re.compile(r"hf_[A-Za-z0-9]{30,}")
FLAGGED_LOOKING = {  # words a minor-coded, age-slider or swap term list would hold
    "age", "baby", "body", "boy", "child", "face", "girl", "head", "kid", "little", "loli", "minor", "nsfw",
    "petite", "school", "slider", "swap", "teen", "tiny", "underage", "young",
}  # fmt: skip


def _is_active(line: str) -> bool:
    stripped = line.lstrip(" \t")
    return bool(stripped) and not stripped.startswith("#")


def _tag_problems(tag: str, ids: set[str], where: str) -> list[str]:
    if tag == "":
        return []
    if not tag.startswith("wf:"):
        return [f"{where}: tag field {tag!r}"]
    listed = tag[3:].replace(" ", "").split(",")
    return [f"{where}: tag id {ident!r} is not a Catalog id" for ident in listed if ident not in ids]


def _relative_path_ok(path: str) -> bool:
    return not path.startswith("/") and all(part not in ("", ".", "..") for part in path.split("/"))


def config_problems(config: S.ConfigLists) -> list[str]:
    problems: list[str] = []
    files = config.files()
    for name, text in files.items():
        if text and not text.endswith("\n"):
            problems.append(f"{name}: no final newline")
        if "\r" in text:
            problems.append(f"{name}: carriage return")
    active = {name: [(n, line) for n, line in enumerate(config.lines(name), 1) if _is_active(line)] for name in files}
    for name, lines in active.items():
        for n, line in lines:
            fields = line.split("|")
            low, high = S.FIELD_COUNTS[name]
            if not low <= len(fields) <= high:
                problems.append(f"{name}:{n}: {len(fields)} fields")
            for position, field in enumerate(fields):
                if field != field.strip():
                    problems.append(f"{name}:{n}: field {position + 1} has surrounding whitespace")
                is_tag = (name, position) in {(S.MODELS, 5), (S.NODES, 2), (S.OLLAMA, 1)}
                if field.startswith("wf:") and not is_tag:
                    problems.append(f"{name}:{n}: field {position + 1} starts with wf:")
    # Catalog (Req 26.4)
    ids = [line.split("|")[0] for _, line in active[S.CATALOG]]
    if not ids:
        problems.append("catalog.txt: no id")
    if len(set(ids)) != len(ids):
        problems.append(f"catalog.txt: duplicate ids {ids}")
    for n, line in active[S.CATALOG]:
        ident, in_all, version, _ = (line.split("|") + ["", "", "", ""])[:4]
        if not ID.match(ident) or ident.startswith("-"):
            problems.append(f"catalog.txt:{n}: id {ident!r}")
        if ident in {"all", "global"} | S.RESERVED_IDS:
            problems.append(f"catalog.txt:{n}: excluded id {ident!r}")
        if in_all not in ("yes", "no"):
            problems.append(f"catalog.txt:{n}: in_all {in_all!r}")
        if version and not VERSION.match(version):
            problems.append(f"catalog.txt:{n}: min_comfyui {version!r}")
    known = set(ids)
    # models.list (Req 26.2, 26.3, 26.7)
    owners: dict[str, tuple[str, str]] = {}
    for n, line in active[S.MODELS]:
        fields = line.split("|")
        if len(fields) != 6:
            continue
        kind, filename, source, source_id, subdir, tag = fields
        where = f"models.list:{n}"
        if kind not in S.KINDS:
            problems.append(f"{where}: kind {kind!r}")
            continue
        if source not in S.GENERATED_SOURCES:
            problems.append(f"{where}: source {source!r}")
        if not filename or "/" in filename:
            problems.append(f"{where}: filename {filename!r}")
        if subdir and not _relative_path_ok(subdir):
            problems.append(f"{where}: subdir {subdir!r}")
        if source == "hf":
            match = HF.match(source_id)
            if not match or (match["path"] is not None and not _relative_path_ok(match["path"])):
                problems.append(f"{where}: hf source id {source_id!r}")
        elif source == "civitai" and not CIVITAI.match(source_id):
            problems.append(f"{where}: civitai source id {source_id!r}")
        elif source == "url" and not URL.match(source_id):
            problems.append(f"{where}: url source id {source_id!r}")
        problems += _tag_problems(tag, known, where)
        path = f"<workflows>/{filename}" if kind == "workflow" else f"models/{subdir or S.KIND_DIRS[kind]}/{filename}"
        if owners.setdefault(path, (source, source_id)) != (source, source_id):
            problems.append(f"{where}: {path} has a second source")
        if path.startswith("models/loras/nsfw/"):
            problems.append(f"{where}: {path} is in the NSFW folder")
    # nodes.txt (Req 26.5)
    repos: set[str] = set()
    for n, line in active[S.NODES]:
        fields = line.split("|")
        match = NODE_URL.match(fields[0])
        if not match:
            problems.append(f"nodes.txt:{n}: url {fields[0]!r}")
            continue
        repo = match["repo"].lower()
        repo = repo[: -len(".git")] if repo.endswith(".git") else repo
        if repo in repos:
            problems.append(f"nodes.txt:{n}: repo {repo!r} twice")
        repos.add(repo)
        if len(fields) == 3:
            problems += _tag_problems(fields[2], known, f"nodes.txt:{n}")
    # ollama-models.txt
    for n, line in active[S.OLLAMA]:
        fields = line.split("|")
        if not fields[0]:
            problems.append(f"ollama-models.txt:{n}: empty model")
        if len(fields) == 2:
            problems += _tag_problems(fields[1], known, f"ollama-models.txt:{n}")
    # every word from the fixed vocabulary (Req 26.8), and nothing shaped like a token (Req 26.9)
    for name in files:
        for n, line in enumerate(config.lines(name), 1):
            for word in re.findall(r"[^\W_]+", line):
                low = word.lower()
                hex_ref = len(low) in (7, 40) and set(low) <= set("0123456789abcdef")
                if low not in S.VOCABULARY and not low.isdigit() and not hex_ref:
                    problems.append(f"{name}:{n}: word {word!r} is not from the vocabulary")
            if HEX_TOKEN.search(line) or HF_TOKEN.search(line):
                problems.append(f"{name}:{n}: token-shaped string")
    return problems


@given(S.config_lists())
def test_config_lists_are_valid_under_req_26_1_to_26_8(config):
    assert config_problems(config) == []


def test_the_validator_catches_broken_configurations():
    catalog = ("amber|yes||moss",)
    good = S.ConfigLists(
        catalog=catalog,
        models=("lora|cedar.safetensors|hf|Opal/pine::cedar.safetensors||wf:amber",),
        nodes=("https://github.com/opal/Pine",),
        ollama=("sage:8b|wf:amber",),
    )
    assert config_problems(good) == []
    broken = {
        "duplicate ids": S.ConfigLists(catalog=("amber|yes||moss", "amber|no||sage")),
        "excluded id 'global'": S.ConfigLists(catalog=("global|yes||moss",)),
        "in_all 'maybe'": S.ConfigLists(catalog=("amber|maybe||moss",)),
        "3 fields": S.ConfigLists(catalog=("amber|yes|moss",)),
        "is not a Catalog id": S.ConfigLists(catalog=catalog, models=("lora|cedar.pt|hf|opal/pine||wf:moss",)),
        "starts with wf:": S.ConfigLists(catalog=catalog, models=("lora|wf:cedar.pt|hf|opal/pine||",)),
        "NSFW folder": S.ConfigLists(catalog=catalog, models=("lora|cedar.pt|hf|opal/pine|loras/nsfw|",)),
        "second source": S.ConfigLists(catalog=catalog, models=("lora|cedar.pt|hf|opal/pine||", "lora|cedar.pt|civitai|12||")),
        "twice": S.ConfigLists(catalog=catalog, nodes=("https://github.com/opal/Pine", "https://github.com/sage/pine.git")),
        "not from the vocabulary": S.ConfigLists(catalog=("amber|yes||face swap",)),
    }
    for reason, config in broken.items():
        problems = config_problems(config)
        assert any(reason in problem for problem in problems), (reason, problems)


def test_the_vocabulary_is_neutral():
    assert not FLAGGED_LOOKING & S.VOCABULARY
    assert not S.EXCLUDED_IDS & set(S.WORDS)
    # no word spelt only with hex letters, so words joined together can never form a hex token (Req 26.9)
    assert all(not (word.isalpha() and set(word) <= set("abcdef")) for word in S.VOCABULARY)


def test_config_lists_reach_the_interesting_shapes():
    def shared_path(config: S.ConfigLists) -> bool:
        keys = [fields[:5] for _, fields in config.active(S.MODELS)]
        return len(keys) != len(set(keys))

    def prefix_ids(config: S.ConfigLists) -> bool:
        return any(a != b and b.startswith(a) for a in config.ids for b in config.ids)

    def spaced_name(config: S.ConfigLists) -> bool:
        return any(" " in fields[1] for _, fields in config.active(S.MODELS))

    def commented_record(config: S.ConfigLists) -> bool:
        return any(line.lstrip().startswith("#") and "|" in line for line in config.models)

    for shape in (shared_path, prefix_ids, spaced_name, commented_record):
        find(S.config_lists(), shape)


# ---- writing a configuration ----


@given(S.config_lists())
def test_temp_config_dir_writes_the_files_byte_for_byte(config):
    with S.temp_config_dir(config) as folder:
        assert sorted(p.name for p in folder.iterdir()) == sorted(S.CONFIG_FILES)
        for name, text in config.files().items():
            assert (folder / name).read_bytes() == text.encode("utf-8")
    assert not folder.exists()


def test_write_config_takes_a_mapping_and_refuses_paths_outside(tmp_path):
    target = S.write_config(tmp_path / "config", {"catalog.txt": "amber|yes||x\n", "patches/a.json.patch.json": b"{}"})
    assert (target / "catalog.txt").read_text(encoding="utf-8") == "amber|yes||x\n"
    assert (target / "patches" / "a.json.patch.json").read_bytes() == b"{}"
    for name in ("../escape.txt", "/abs.txt"):
        try:
            S.write_config(tmp_path / "config", {name: "x"})
        except ValueError:
            continue
        raise AssertionError(f"{name} was written")
    assert not (tmp_path / "escape.txt").exists()


def test_config_lists_accessors():
    config = S.ConfigLists(
        catalog=("# c", "amber|yes||x", "  ", "moss|no|0.3|y"),
        nodes=("https://github.com/o/r|", "  # https://github.com/o/s"),
    )
    assert config.ids == ("amber", "moss")
    assert config.in_all == ("amber",)
    assert config.active(S.NODES) == [(1, ("https://github.com/o/r", ""))]
    assert config.text(S.CATALOG) == "# c\namber|yes||x\n  \nmoss|no|0.3|y\n"
    assert config.text(S.OLLAMA) == ""
    assert S.install_path("lora", "x.safetensors", "") == "models/loras/x.safetensors"
    assert S.install_path("lora", "x.safetensors", "loras/looks") == "models/loras/looks/x.safetensors"
    assert S.install_path("workflow", "w.json", "") == "<workflows>/w.json"
