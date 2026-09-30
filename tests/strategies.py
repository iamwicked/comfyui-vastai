"""Shared Hypothesis strategies for the property tests (task 3.2, design Correctness Properties).

- texts(): all Unicode except surrogates, mixed with the deliberate samples the design lists
- field_text(): one field of a valid Config_Lists line record (Req 26.14)
- config_lists(): a random valid Catalog plus models.list, nodes.txt and ollama-models.txt, with wf: tags
- write_config() and temp_config_dir(): put a generated configuration into a CONFIG_DIR

Later tasks add personas(), calendars(), faults(), templates(), values_for(), luts() and the rest here.
"""
from __future__ import annotations

import re
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from hypothesis import strategies as st

# ---- texts() and field_text() ----

# Every character str.splitlines() breaks a line on.
LINE_BREAKS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"

# The design's deliberate samples: YAML-ambiguous words and numbers, a comment start, a mapping lookalike,
# emoji (one code point, and a skin-tone ZWJ sequence), a line break and a double quote.
TEXT_SAMPLES = ("no", "2026", "#x", "a: b", "\U0001f600", "\U0001f469\U0001f3fd\u200d\U0001f4bb", "\n", '"')

_ANY_CHAR = st.characters(exclude_categories=("Cs",))
_LINE_CHAR = st.characters(exclude_categories=("Cs",), exclude_characters=LINE_BREAKS)


def texts(
    *, min_size: int = 0, max_size: int | None = None, single_line: bool = False
) -> st.SearchStrategy[str]:
    """Any text over all of Unicode except surrogates, often mixed with TEXT_SAMPLES.

    single_line=True leaves out every line break (and the "\\n" sample). Sizes count code points; the mixed
    branch meets the size bounds by construction (see _mixed_texts), so no draw is ever filtered away.
    """
    char = _LINE_CHAR if single_line else _ANY_CHAR
    samples = tuple(s for s in TEXT_SAMPLES if not (single_line and any(c in LINE_BREAKS for c in s)))
    mixed = _mixed_texts(char, samples, min_size, max_size)
    return st.one_of(st.text(char, min_size=min_size, max_size=max_size), mixed)


@st.composite
def _mixed_texts(
    draw: st.DrawFn, char: st.SearchStrategy[str], samples: tuple[str, ...], min_size: int, max_size: int | None
) -> str:
    """1-6 pieces joined, each a sample or 1-8 characters, as before; but every piece is drawn to fit the
    room max_size leaves (a sample only if it fits, a character run no longer than the room), and a text
    shorter than min_size is topped up with characters. Filtering joined pieces to the bounds instead threw
    away most draws for a small max_size (texts(min_size=1, max_size=6) inside a 16-entry LoRA list)."""
    text = ""
    for _ in range(draw(st.integers(1, 6))):
        room = 8 if max_size is None else min(8, max_size - len(text))
        if room <= 0:
            break
        fitting = tuple(sample for sample in samples if len(sample) <= room)
        run = st.text(char, min_size=1, max_size=room)
        text += draw(st.one_of(st.sampled_from(fitting), run) if fitting else run)
    if len(text) < min_size:
        text += draw(st.text(char, min_size=min_size - len(text), max_size=min_size - len(text)))
    return text


def _clean_field(text: str) -> str:
    return "".join(c for c in text if c != "|" and c not in LINE_BREAKS).strip()


def field_text(*, first: bool = False, max_size: int = 40) -> st.SearchStrategy[str]:
    """One field of a valid line record (Req 26.14): texts() without `|`, without any line break, and
    without leading or trailing whitespace. first=True also makes it non-empty and not start with `#`,
    as the first field of a record must."""
    strategy = texts(max_size=max_size).map(_clean_field)
    if first:
        strategy = strategy.filter(lambda s: s != "" and not s.startswith("#"))
    return strategy


# ---- the Config_Lists vocabulary ----

# Every word a generated configuration can hold comes from these lists, so no Safety_Guard term (Req 26.8)
# can appear in a line, a comment, an id or a file name.
WORDS = (
    "alder", "amber", "aspen", "basil", "birch", "brook", "cedar", "clover", "coral", "dune", "ember",
    "fern", "fjord", "flint", "grove", "harbor", "hazel", "heron", "iris", "ivory", "juniper", "kelp",
    "lark", "lumen", "maple", "meadow", "moss", "nectar", "opal", "orchid", "pebble", "pine", "quartz",
    "quill", "raven", "river", "sage", "slate", "sorrel", "spruce", "thistle", "tulip", "umber", "violet",
    "willow", "wren", "yarrow", "zephyr",
)  # fmt: skip
PROSE = WORDS + ("café", "über", "—", "2026", "v2", "x3")  # comments and Catalog descriptions
MODEL_EXTENSIONS = ("safetensors", "gguf", "pt", "pth", "bin", "ckpt", "onnx")
WORKFLOW_EXTENSIONS = ("json", "zip")
OLLAMA_TAGS = ("latest", "8b", "14b", "q4")

# ---- the Config_Lists formats (Req 26.2–26.5) ----

CATALOG, MODELS, NODES, OLLAMA = "catalog.txt", "models.list", "nodes.txt", "ollama-models.txt"
CONFIG_FILES = (CATALOG, MODELS, NODES, OLLAMA)
FIELD_COUNTS = {CATALOG: (4, 4), MODELS: (6, 6), NODES: (1, 3), OLLAMA: (1, 2)}  # (min, max), Req 26.2

KINDS = (
    "workflow", "checkpoint", "lora", "vae", "vae_approx", "clip", "clip_vision", "text_encoder",
    "diffusion", "upscale", "embedding", "controlnet", "detection", "sams", "model_patch",
    "audio_encoder", "latent_upscale",
)  # fmt: skip
SOURCES = ("hf", "civitai", "url", "bundled")
# bundled needs a file inside config/ that git tracks, which a temp CONFIG_DIR is not.
GENERATED_SOURCES = ("hf", "civitai", "url")

# provision.sh kind_to_dir: the default folder below models/ of each kind but `workflow`.
KIND_DIRS = {
    "checkpoint": "checkpoints", "lora": "loras", "vae": "vae", "vae_approx": "vae_approx",
    "clip": "clip", "clip_vision": "clip_vision", "text_encoder": "text_encoders",
    "diffusion": "diffusion_models", "upscale": "upscale_models", "embedding": "embeddings",
    "controlnet": "controlnet", "detection": "detection", "sams": "sams", "model_patch": "model_patches",
    "audio_encoder": "audio_encoders", "latent_upscale": "latent_upscale_models",
}  # fmt: skip

# Ids a generated Catalog never holds: `all` and `global` are keywords of --selection, and each
# Influencer_Id brings rules of its own (provenance and `# licence:` records, Req 26.18; NSFW placement,
# Req 26.7). A test that needs one adds it together with the records it requires.
RESERVED_IDS = frozenset({"influencer", "influencer-video", "influencer-nsfw", "character-design"})
EXCLUDED_IDS = RESERVED_IDS | {"all", "global"}


def _words_of(text: str) -> list[str]:
    """The words of text as the Safety_Guard splits them: runs of letters and digits."""
    return re.findall(r"[^\W_]+", text)


# Every word (lowercased) that config_lists() can put in a file, digit runs and hex refs aside.
VOCABULARY = frozenset(
    word.lower()
    for text in (
        *PROSE, *MODEL_EXTENSIONS, *WORKFLOW_EXTENSIONS, *OLLAMA_TAGS, *KINDS, *KIND_DIRS.values(),
        *GENERATED_SOURCES, "https github com git example wf yes no",
    )
    for word in _words_of(text)
)  # fmt: skip


def install_path(kind: str, filename: str, subdir: str) -> str:
    """A models.list line's install path (Req 26.3): models/<subdir or the kind's folder>/<filename>, or the
    ComfyUI workflows folder plus the filename for kind `workflow`."""
    if kind == "workflow":
        return f"<workflows>/{filename}"
    return f"models/{subdir or KIND_DIRS[kind]}/{filename}"


def is_active(line: str) -> bool:
    """Neither blank nor a comment after leading whitespace (Req 26.1)."""
    stripped = line.lstrip(" \t\v\f\r")
    return bool(stripped) and not stripped.startswith("#")


@dataclass(frozen=True)
class ConfigLists:
    """One configuration: the physical lines (without "\\n") of the Catalog and the three lists."""

    catalog: tuple[str, ...]
    models: tuple[str, ...] = ()
    nodes: tuple[str, ...] = ()
    ollama: tuple[str, ...] = ()

    def lines(self, name: str) -> tuple[str, ...]:
        return {CATALOG: self.catalog, MODELS: self.models, NODES: self.nodes, OLLAMA: self.ollama}[name]

    def text(self, name: str) -> str:
        """The file's content: every line followed by "\\n"."""
        return "".join(f"{line}\n" for line in self.lines(name))

    def files(self) -> dict[str, str]:
        return {name: self.text(name) for name in CONFIG_FILES}

    def active(self, name: str) -> list[tuple[int, tuple[str, ...]]]:
        """(1-based line number, fields) of every active line of the file."""
        return [(n, tuple(line.split("|"))) for n, line in enumerate(self.lines(name), 1) if is_active(line)]

    @property
    def ids(self) -> tuple[str, ...]:
        """The Catalog ids in file order."""
        return tuple(fields[0] for _, fields in self.active(CATALOG))

    @property
    def in_all(self) -> tuple[str, ...]:
        """The Catalog ids with in_all=yes, in file order (the full run's ids)."""
        return tuple(fields[0] for _, fields in self.active(CATALOG) if fields[1] == "yes")


# ---- config_lists() building blocks ----

_DIGITS = st.from_regex(r"[0-9]{1,3}", fullmatch=True)
_WORD = st.sampled_from(WORDS)
_PART = st.one_of(_WORD, _DIGITS)
_HEX = "0123456789abcdef"


@st.composite
def _catalog_id(draw: st.DrawFn) -> str:
    """Lowercase letters, digits and hyphens, never starting with `-` (Req 26.4)."""
    parts = draw(st.lists(_PART, min_size=1, max_size=3))
    ident = parts[0]
    for part in parts[1:]:
        ident += draw(st.sampled_from(("-", "-", "--"))) + part
    if draw(st.integers(0, 9)) == 0:
        ident += "-"
    return ident


@st.composite
def _catalog_ids(draw: st.DrawFn, max_ids: int) -> list[str]:
    ids = draw(st.lists(_catalog_id().filter(lambda i: i not in EXCLUDED_IDS), min_size=1, max_size=max_ids,
                        unique=True))  # fmt: skip
    if len(ids) < max_ids and draw(st.booleans()):
        # An id that extends another one, so an exact tag match is really tested ("amber" vs "amber-2").
        # Always joined by hyphens: "amber" + "moss" would make "ambermoss", a word outside VOCABULARY.
        derived = draw(st.sampled_from(ids)) + draw(st.sampled_from(("-", "--"))) + draw(_PART)
        if derived not in ids and derived not in EXCLUDED_IDS:
            ids.append(derived)
    return list(draw(st.permutations(ids)))


@st.composite
def _tag(draw: st.DrawFn, ids: list[str]) -> str:
    """Empty, or `wf:` and 1-3 distinct Catalog ids; spaces after a comma are allowed (Req 26.1)."""
    if draw(st.integers(0, 3)) == 0:
        return ""
    listed = draw(st.lists(st.sampled_from(ids), min_size=1, max_size=min(3, len(ids)), unique=True))
    tag = "wf:" + listed[0]
    for ident in listed[1:]:
        tag += draw(st.sampled_from((",", ",", ",", ", "))) + ident
    return tag


_PROSE_TOKEN = st.builds(lambda word, mark: word + mark, st.sampled_from(PROSE),
                         st.sampled_from(("", "", "", ",", ":", "+")))  # fmt: skip
_PROSE = st.lists(_PROSE_TOKEN, max_size=6).map(" ".join)

_SEPARATORS = (" ", "_", "-", ".", "+")
# Rarer, but valid in a file name: they catch an unquoted expansion or a `read` without -r.
_SPECIALS = ("#", "(", ")", "[", "]", "&", ",", "'", "*", "?", "$", "\\", "~", "=", "%", "@", "!")


@st.composite
def _stem(draw: st.DrawFn) -> str:
    parts = draw(st.lists(_PART, min_size=1, max_size=3))
    stem = parts[0]
    for part in parts[1:]:
        pool = _SPECIALS if draw(st.integers(0, 9)) == 0 else _SEPARATORS
        stem += draw(st.sampled_from(pool)) + part
    return stem


@st.composite
def _slug(draw: st.DrawFn, max_parts: int = 2) -> str:
    """Words and digits joined by `-` or `_`: path segments, hf repo parts, URL parts."""
    parts = draw(st.lists(_PART, min_size=1, max_size=max_parts))
    slug = parts[0]
    for part in parts[1:]:
        slug += draw(st.sampled_from(("-", "_"))) + part
    return slug


@st.composite
def _cased(draw: st.DrawFn, strategy: st.SearchStrategy[str]) -> str:
    text = draw(strategy)
    return draw(st.sampled_from((text, text, text.capitalize(), text.upper())))


def _join_path(*segments: str) -> str:
    return "/".join(segments)


@st.composite
def _source_id(draw: st.DrawFn, source: str, filename: str) -> str:
    if source == "hf":  # <owner>/<name>, optionally ::<relative path without ..> (Req 26.2)
        repo = draw(_cased(_slug())) + "/" + draw(_cased(_slug()))
        if draw(st.integers(0, 3)) == 0:
            return repo
        folders = draw(st.lists(_slug(), max_size=2))
        leaf = draw(_slug()) + "." + filename.rsplit(".", 1)[-1]
        return repo + "::" + _join_path(*folders, leaf)
    if source == "civitai":  # digits, optionally ::<member name>
        version = draw(st.from_regex(r"[1-9][0-9]{0,6}", fullmatch=True))
        if draw(st.integers(0, 3)) == 0:
            return version + "::" + draw(_slug()) + "." + filename.rsplit(".", 1)[-1]
        return version
    host = draw(_slug(1)) + "." + (draw(_slug(1)) + "." if draw(st.booleans()) else "") + "example"
    folders = draw(st.lists(_slug(), max_size=2))
    return f"https://{host}/" + _join_path(*folders, draw(_slug()) + "." + filename.rsplit(".", 1)[-1])


@st.composite
def _subdir(draw: st.DrawFn, kind: str) -> str:
    """Empty, or a relative path without empty, `.` or `..` segments, never below loras/nsfw (Req 26.7)."""
    if kind == "workflow" or draw(st.booleans()):
        return ""
    segments = draw(st.lists(_cased(_slug()), min_size=1, max_size=2))
    if draw(st.integers(0, 2)) == 0:
        segments.insert(0, KIND_DIRS[kind])  # the real lists' style: loras/looks, vosr2/VOSR2
    return _join_path(*segments)


@st.composite
def _models_lines(draw: st.DrawFn, ids: list[str], max_lines: int) -> list[str]:
    records: list[tuple[str, ...]] = []
    owners: dict[str, tuple[str, str]] = {}  # install path -> (source, source id), Req 26.3
    for index in range(draw(st.integers(0, max_lines))):
        if records and draw(st.integers(0, 4)) == 0:
            # one more line for an install path already listed: same source, its own tag (Req 26.3 allows it)
            kind, filename, source, source_id, subdir, _ = draw(st.sampled_from(records))
            records.append((kind, filename, source, source_id, subdir, draw(_tag(ids))))
            continue
        kind = draw(st.sampled_from(KINDS))
        extensions = WORKFLOW_EXTENSIONS if kind == "workflow" else MODEL_EXTENSIONS
        stem, extension = draw(_stem()), draw(st.sampled_from(extensions))
        subdir = draw(_subdir(kind))
        filename = f"{stem}.{extension}"
        attempt = 0
        while install_path(kind, filename, subdir) in owners:
            attempt += 1
            filename = f"{stem}-{index}-{attempt}.{extension}"
        source = draw(st.sampled_from(GENERATED_SOURCES))
        source_id = draw(_source_id(source, filename))
        owners[install_path(kind, filename, subdir)] = (source, source_id)
        records.append((kind, filename, source, source_id, subdir, draw(_tag(ids))))
    return ["|".join(record) for record in records]


@st.composite
def _nodes_lines(draw: st.DrawFn, ids: list[str], max_lines: int) -> list[str]:
    lines, repos = [], set()
    for index in range(draw(st.integers(0, max_lines))):
        owner, repo = draw(_cased(_slug())), draw(_cased(_slug()))
        attempt = 0
        while repo.lower() in repos:  # <repo> is unique ignoring case and .git (Req 26.5)
            attempt += 1
            repo = f"{repo}-{index}{attempt}"
        repos.add(repo.lower())
        url = f"https://github.com/{owner}/{repo}" + (".git" if draw(st.integers(0, 3)) == 0 else "")
        fields = [url]
        count = draw(st.integers(1, 3))
        if count >= 2:
            fields.append(draw(st.one_of(
                st.just(""), st.just(""), st.text(_HEX, min_size=7, max_size=7),
                st.text(_HEX, min_size=40, max_size=40), _WORD,
            )))  # fmt: skip
        if count == 3:
            fields.append(draw(_tag(ids)))
        lines.append("|".join(fields))
    return lines


@st.composite
def _ollama_lines(draw: st.DrawFn, ids: list[str], max_lines: int) -> list[str]:
    lines = []
    for _ in range(draw(st.integers(0, max_lines))):
        model = draw(_slug())
        if draw(st.booleans()):
            model += ":" + draw(st.sampled_from(OLLAMA_TAGS))
        lines.append(model if draw(st.booleans()) else model + "|" + draw(_tag(ids)))
    return lines


@st.composite
def _inactive_line(draw: st.DrawFn, active: list[str]) -> str:
    """A blank line, a comment, or a commented-out record (possibly naming an id the Catalog lacks)."""
    indent = draw(st.sampled_from(("", "", " ", "  ", "\t")))
    choice = draw(st.integers(0, 3))
    if choice == 0:
        return draw(st.sampled_from(("", "", " ", "\t", "  \t")))
    if choice == 1 or not active:
        prose = draw(_PROSE)
        return indent + "#" + (" " + prose if prose else "")
    record = draw(st.sampled_from(active))
    if draw(st.booleans()):
        record = record.rsplit("|", 1)[0] + "|wf:" + draw(_catalog_id())
    return indent + draw(st.sampled_from(("#", "# "))) + record


@st.composite
def _with_inactive(draw: st.DrawFn, active: list[str]) -> tuple[str, ...]:
    lines = list(active)
    for _ in range(draw(st.integers(0, 3))):
        lines.insert(draw(st.integers(0, len(lines))), draw(_inactive_line(active)))
    return tuple(lines)


@st.composite
def config_lists(draw: st.DrawFn, *, max_ids: int = 6, max_lines: int = 8) -> ConfigLists:
    """A random configuration that is valid under Req 26.1-26.8.

    - Field counts are 4 (Catalog), 6 (models.list), 1-3 (nodes.txt) and 1-2 (ollama-models.txt).
    - Kinds, sources and source-id shapes are valid; sources are hf, civitai and url, because `bundled`
      needs a file in config/ that git tracks.
    - Catalog ids are 1-max_ids unique ids of lowercase letters, digits and hyphens, never starting with
      `-`, with in_all yes or no and an empty or dotted min_comfyui. They are never `all` or `global`, and
      never one of RESERVED_IDS (influencer, influencer-video, influencer-nsfw, character-design): lines
      tagged with those need provenance and `# licence:` records (Req 26.18) and NSFW placement (Req 26.7).
    - Every wf: tag lists 1-3 Catalog ids; an untagged line has an empty or missing tag field.
    - Every install path has one source and source id; some paths are listed twice with other tags.
    - nodes.txt repos are unique ignoring case and `.git`; no install path is below models/loras/nsfw/.
    - All free text, ids and names are drawn from VOCABULARY (WORDS and a few fixed tokens), so no
      Safety_Guard term (Req 26.8) and no token format (Req 26.9) can appear.
    - Each file has up to 3 blank, comment or commented-out lines, and ends with a newline.
    - No field has a `|`, a line break, or leading or trailing whitespace (Req 26.14).
    """
    ids = draw(_catalog_ids(max_ids))
    catalog = [
        "|".join((
            ident,
            draw(st.sampled_from(("yes", "no"))),
            draw(st.one_of(st.just(""), st.from_regex(r"[0-9]{1,2}\.[0-9]{1,2}(\.[0-9]{1,2})?", fullmatch=True))),
            draw(_PROSE),
        ))  # fmt: skip
        for ident in ids
    ]
    models = draw(_models_lines(ids, max_lines))
    nodes = draw(_nodes_lines(ids, max_lines))
    ollama = draw(_ollama_lines(ids, max_lines))
    return ConfigLists(
        catalog=draw(_with_inactive(catalog)),
        models=draw(_with_inactive(models)),
        nodes=draw(_with_inactive(nodes)),
        ollama=draw(_with_inactive(ollama)),
    )


# ---- writing a configuration ----


def write_config(config_dir: Path, config: ConfigLists | Mapping[str, str | bytes]) -> Path:
    """Writes the four files of a ConfigLists (or any name -> content mapping, names relative to
    config_dir) byte for byte into config_dir, creating it, and returns config_dir."""
    config_dir = Path(config_dir)
    files = config.files() if isinstance(config, ConfigLists) else config
    config_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"not a file name inside CONFIG_DIR: {name!r}")
        target = config_dir.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    return config_dir


@contextmanager
def temp_config_dir(config: ConfigLists | Mapping[str, str | bytes]) -> Iterator[Path]:
    """A fresh CONFIG_DIR holding config, removed afterwards. For Hypothesis tests, which cannot take a
    function-scoped fixture such as tmp_path once per example."""
    with tempfile.TemporaryDirectory(prefix="config-lists-") as folder:
        yield write_config(Path(folder) / "config", config)
