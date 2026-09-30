"""Config lines, install paths and the Python Selection model (design §A12, §M10; Req 26.2, 26.3, 26.13,
26.14).

A pure module: it reads no file and starts nothing, so the property tests reach it directly. The caller
passes each file's text. Text decoded from UTF-8 with errors="surrogateescape", and output encoded the same
way, keeps the mirror of provision.sh byte for byte.

- line_reader(), line_printer(): Line_Reader and Line_Printer, a line record and its fields (Req 26.14)
- physical_lines(), is_active(), active_lines(): the lines of a file, and which of them count
- parse_catalog(), parse_models(), parse_nodes(), parse_ollama(): each file's line records (Req 26.2)
- parse_tag(), tag_has(): the ids a wf: tag lists
- install_path(), install_path_conflicts(): where a models.list line installs to (Req 26.3)
- select(), Selection.render(): what one run of scripts/provision.sh installs, printed the way
  `provision.sh --selection <run>` prints it (§M10, Req 26.13)

The Selection model mirrors provision.sh's trim, catalog_lines, catalog_ids, catalog_all, catalog_has,
tag_has, selected and print_selection, quirks included. Where they and §A12 disagree this module follows
the bash, and the comment at that spot starts with "bash, not §A12".
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Union

from influencer.errors import Problem, ValidationError

CATALOG, MODELS, NODES, OLLAMA = "catalog.txt", "models.list", "nodes.txt", "ollama-models.txt"
CONFIG_FILES = (CATALOG, MODELS, NODES, OLLAMA)
LIST_FILES = (MODELS, NODES, OLLAMA)  # the §M10 sections, in their order
FIELD_COUNTS = {CATALOG: (4, 4), MODELS: (6, 6), NODES: (1, 3), OLLAMA: (1, 2)}  # (min, max), Req 26.2
VALUE_COUNTS = {MODELS: 5, NODES: 2, OLLAMA: 1}  # print_selection's n: the fields before the tag

FULL_RUN, GLOBAL_RUN = "all", "global"  # the runs of --selection besides a Catalog id

# Every character str.splitlines() breaks a line on. No field of a line record holds one (Req 26.14).
LINE_BREAKS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"

# trim()'s [[:space:]] under the LC_ALL=C that --selection exports: these six bytes and nothing else. A
# no-break space or any other Unicode space is kept, as the bash keeps it.
C_SPACE = " \t\n\v\f\r"

# bash's default IFS, on which the full run splits a tag's ids once its commas are spaces.
_IFS_WORDS = re.compile("[ \t\n]+")

# provision.sh's kind_to_dir: the default folder below models/ of each kind but `workflow`.
KIND_DIRS = {
    "checkpoint": "checkpoints",
    "lora": "loras",
    "vae": "vae",
    "vae_approx": "vae_approx",
    "controlnet": "controlnet",
    "upscale": "upscale_models",
    "embedding": "embeddings",
    "clip": "clip",
    "clip_vision": "clip_vision",
    "text_encoder": "text_encoders",
    "diffusion": "diffusion_models",
    "detection": "detection",
    "sams": "sams",
    "model_patch": "model_patches",
    "audio_encoder": "audio_encoders",
    "latent_upscale": "latent_upscale_models",
}
WORKFLOW_KIND = "workflow"
# The ComfyUI workflows folder relative to the Workspace: provision.sh's ${WORKFLOWS_DIR#"$WORKSPACE"/} with
# its default USER_DIR, <workspace>/comfyui-user (§A12's <user>/default/workflows).
WORKFLOWS_FOLDER = "comfyui-user/default/workflows"


# ---- the lines of a file ----


def trim(text: str) -> str:
    """provision.sh's trim(): text without leading and trailing C_SPACE."""
    return text.strip(C_SPACE)


def physical_lines(text: str) -> list[str]:
    """The lines of a file the way `while IFS= read -r line || [ -n "$line" ]` and grep read them: split on
    \\n only, so a \\r, \\v, \\f or U+2028 stays inside its line. A last line without a newline counts; a
    final newline starts no line."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def is_active(line: str) -> bool:
    """The active-line rule: neither blank nor starting with `#` after leading whitespace, the whitespace
    being trim()'s. It is print_selection's `case "$(trim "$line")" in ''|\\#*)` and catalog_lines' `grep -vE
    '^[[:space:]]*(#|$)'` alike."""
    text = trim(line)
    return text != "" and not text.startswith("#")


def active_lines(text: str) -> list[tuple[int, str]]:
    """(1-based line number, line) of each active line of a file's text, in file order."""
    return [(number, line) for number, line in enumerate(physical_lines(text), 1) if is_active(line)]


# ---- Line_Reader and Line_Printer (Req 26.14) ----


def line_reader(s: str) -> list[str]:
    """Line_Reader: the fields of one line, split on `|` once a trailing \\n and then a trailing \\r are
    dropped, as print_selection's read and ${line%$'\\r'} drop them. Fields keep their whitespace, so
    validation can report it."""
    if s.endswith("\n"):
        s = s[:-1]
    if s.endswith("\r"):
        s = s[:-1]
    return s.split("|")


def line_printer(fields: Sequence[str], *, path: str = "line record") -> str:
    """Line_Printer: the fields joined on `|`.

    Raises ValidationError, with one Problem per fault, for a record Line_Reader would not give back: no
    field, a field that contains `|` or a line break (LINE_BREAKS) or has leading or trailing whitespace
    (str.isspace, so any Unicode space), or a first field that is empty or starts with `#`.
    """
    problems = []
    if not fields:
        problems.append(Problem(path, None, None, "has no field"))
    for number, field in enumerate(fields, 1):
        name = f"field {number}"
        if number == 1 and field == "":
            problems.append(Problem(path, name, None, "is empty, which a first field must not be"))
        if number == 1 and field.startswith("#"):
            problems.append(Problem(path, name, None, "starts with '#', which makes the line a comment"))
        if "|" in field:
            problems.append(Problem(path, name, None, "contains '|'"))
        if any(char in LINE_BREAKS for char in field):
            problems.append(Problem(path, name, None, "contains a line break"))
        if field != field.strip():
            problems.append(Problem(path, name, None, "has leading or trailing whitespace"))
    if problems:
        raise ValidationError(problems)
    return "|".join(fields)


# ---- per-file line records (Req 26.2) ----


def _field(fields: Sequence[str], index: int) -> str:
    """fields[index], or "" past the end of the line."""
    return fields[index] if index < len(fields) else ""


def _values_and_tag(fields: Sequence[str], count: int) -> tuple[list[str], str]:
    """print_selection's split: the first count fields trimmed, a field past the end of the line empty, and
    the tag as the trimmed field after them. Anything after the tag is ignored, as the install loops'
    `read -r <fields> tag _` ignores it."""
    return [trim(_field(fields, index)) for index in range(count)], trim(_field(fields, count))


class _Tagged:
    """The wf: tag of a list line."""

    tag: str

    @property
    def ids(self) -> tuple[str, ...]:
        """The ids the line's tag lists (parse_tag); () when untagged."""
        return parse_tag(self.tag)


@dataclass(frozen=True)
class CatalogLine:
    """An active line of catalog.txt, `id|in_all|min_comfyui|description` (Req 26.4).

    fields are Line_Reader's, whitespace kept, for validation. id and in_all are the bash's: catalog_ids
    prints the text before the first `|` untrimmed (`cut -d'|' -f1`), and catalog_all keeps the lines whose
    second field is exactly `yes` (awk's `$2=="yes"`).

    bash, not §A12: grep, cut and awk keep a CRLF line's \\r, which Line_Reader drops. A Catalog line of one
    field ends its id in \\r, and `<id>|yes\\r` is not in_all. A line of four fields keeps the \\r in its
    description, where the Selection never looks.
    """

    number: int  # 1-based line number in catalog.txt
    fields: tuple[str, ...]
    id: str
    in_all: bool

    @property
    def min_comfyui(self) -> str:
        return _field(self.fields, 2)

    @property
    def description(self) -> str:
        return _field(self.fields, 3)


@dataclass(frozen=True)
class ModelLine(_Tagged):
    """An active line of models.list, `kind|filename|source|source_id|subdir|wf:ids` (Req 26.2).

    fields are Line_Reader's, whitespace kept, for validation. The named fields are the ones the install loop
    and print_selection use: trimmed, and empty past the end of the line.
    """

    number: int  # 1-based line number in models.list
    fields: tuple[str, ...]
    kind: str
    filename: str
    source: str
    source_id: str
    subdir: str
    tag: str

    @property
    def values(self) -> tuple[str, ...]:
        """What --selection prints of the line (§M10): the five fields before the tag."""
        return (self.kind, self.filename, self.source, self.source_id, self.subdir)


@dataclass(frozen=True)
class NodeLine(_Tagged):
    """An active line of nodes.txt, `git_url|ref|wf:ids` with ref and tag optional (Req 26.2, 26.5). A line of
    two fields is untagged: its second field is the ref, whatever it holds."""

    number: int  # 1-based line number in nodes.txt
    fields: tuple[str, ...]
    url: str
    ref: str
    tag: str

    @property
    def values(self) -> tuple[str, ...]:
        """What --selection prints of the line (§M10): the two fields before the tag."""
        return (self.url, self.ref)


@dataclass(frozen=True)
class OllamaLine(_Tagged):
    """An active line of ollama-models.txt, `model|wf:ids` with the tag optional (Req 26.2)."""

    number: int  # 1-based line number in ollama-models.txt
    fields: tuple[str, ...]
    model: str
    tag: str

    @property
    def values(self) -> tuple[str, ...]:
        """What --selection prints of the line (§M10): the field before the tag."""
        return (self.model,)


ListLine = Union[ModelLine, NodeLine, OllamaLine]


def parse_catalog(text: str) -> list[CatalogLine]:
    """The records of catalog.txt's active lines, in file order."""
    records = []
    for number, line in active_lines(text):
        raw = line.split("|")  # as grep hands the line to cut and awk, a CRLF line's \r included
        records.append(CatalogLine(number, tuple(line_reader(line)), raw[0], _field(raw, 1) == "yes"))
    return records


def parse_models(text: str) -> list[ModelLine]:
    """The records of models.list's active lines, in file order."""
    records = []
    for number, line in active_lines(text):
        fields = line_reader(line)
        values, tag = _values_and_tag(fields, VALUE_COUNTS[MODELS])
        records.append(ModelLine(number, tuple(fields), *values, tag))
    return records


def parse_nodes(text: str) -> list[NodeLine]:
    """The records of nodes.txt's active lines, in file order."""
    records = []
    for number, line in active_lines(text):
        fields = line_reader(line)
        values, tag = _values_and_tag(fields, VALUE_COUNTS[NODES])
        records.append(NodeLine(number, tuple(fields), *values, tag))
    return records


def parse_ollama(text: str) -> list[OllamaLine]:
    """The records of ollama-models.txt's active lines, in file order."""
    records = []
    for number, line in active_lines(text):
        fields = line_reader(line)
        values, tag = _values_and_tag(fields, VALUE_COUNTS[OLLAMA])
        records.append(OllamaLine(number, tuple(fields), *values, tag))
    return records


# ---- wf: tags ----


def _without_wf(tag: str) -> str:
    """${tag#wf:}: the tag without its leading `wf:`, or unchanged when it has none."""
    return tag[3:] if tag.startswith("wf:") else tag


def parse_tag(field: str) -> tuple[str, ...]:
    """The ids a wf: tag field lists: `wf:` and a comma list with spaces removed, read the way tag_has reads
    it, so for any non-empty id without a comma, `ident in parse_tag(field)` is `tag_has(trim(field), ident)`.
    An empty or all-whitespace field lists none. An empty item stays in, for validation to report.

    bash, not §A12: the `wf:` is dropped only when present (${1#wf:}), so a field without it is read as the
    comma list itself; and only spaces are removed, so a tab stays inside its id.
    """
    tag = trim(field)
    if tag == "":
        return ()
    return tuple(trim(_without_wf(tag)).replace(" ", "").split(","))


def tag_has(tag: str, ident: str) -> bool:
    """provision.sh's tag_has: whether the tag, trimmed as selected() passes it, lists ident. The list after
    `wf:` is trimmed, put between commas and stripped of every space, and must hold `,<ident>,`."""
    return f",{ident}," in ("," + trim(_without_wf(tag)) + ",").replace(" ", "")


# ---- install paths (Req 26.3) ----


def kind_folder(kind: str) -> str:
    """provision.sh's kind_to_dir: a kind's default folder below models/. An unknown kind is its own folder,
    as the bash's `*) echo "$1"` makes it."""
    return KIND_DIRS.get(kind, kind)


def install_path(line: ModelLine) -> str:
    """Where a models.list line installs to, relative to the Workspace: models/<subdir or the kind's default
    folder>/<filename>, or the ComfyUI workflows folder plus the filename for kind `workflow`, which ignores
    subdir. This is the `$dir/$filename` provision.sh's install loop logs, with its default MODELS_DIR and
    USER_DIR. The parts are the line's trimmed fields, and an empty kind and subdir leave an empty segment,
    as in the bash."""
    if line.kind == WORKFLOW_KIND:
        return f"{WORKFLOWS_FOLDER}/{line.filename}"
    return f"models/{line.subdir or kind_folder(line.kind)}/{line.filename}"


def install_path_conflicts(lines: Iterable[ModelLine]) -> list[tuple[ModelLine, ModelLine]]:
    """(first, later) for each line whose install path an earlier line already has with another source or
    source id, in file order (Req 26.3). Lines with one install path and one source may differ in tag."""
    owners: dict[str, ModelLine] = {}
    conflicts = []
    for line in lines:
        first = owners.setdefault(install_path(line), line)
        if (first.source, first.source_id) != (line.source, line.source_id):
            conflicts.append((first, line))
    return conflicts


# ---- the Selection model (§M10, Req 26.13) ----


@dataclass(frozen=True)
class Selection:
    """The lines one run of provision.sh installs: per list, the selected active lines in file order."""

    run: str  # FULL_RUN, GLOBAL_RUN or a Catalog id
    models: tuple[ModelLine, ...]
    nodes: tuple[NodeLine, ...]
    ollama: tuple[OllamaLine, ...]

    def lines(self, name: str) -> tuple[ListLine, ...]:
        """The selected lines of one of LIST_FILES."""
        return {MODELS: self.models, NODES: self.nodes, OLLAMA: self.ollama}[name]

    def render(self) -> str:
        """The §M10 printout, what `provision.sh --selection <run>` prints: the three sections in fixed order,
        each a `[<file>]` header and then one line per selected line, its trimmed fields before the tag (5, 2
        and 1 of them) joined by `|`. Every line ends in a newline, the last one included."""
        out = []
        for name in LIST_FILES:
            out.append(f"[{name}]\n")
            out.extend("|".join(line.values) + "\n" for line in self.lines(name))
        return "".join(out)


def _in_all_text(catalog: Sequence[CatalogLine]) -> str:
    """IN_ALL, `" $(catalog_all | tr '\\n' ' ') "`: each in_all id followed by a space, between spaces."""
    return " " + "".join(f"{line.id} " for line in catalog if line.in_all) + " "


def _catalog_has(catalog: Sequence[CatalogLine], run: str) -> bool:
    """catalog_has: run is non-empty, on one line, and exactly the untrimmed id of an active Catalog line."""
    return run != "" and "\n" not in run and any(line.id == run for line in catalog)


def _selected(field: str, run: str, in_all: str) -> bool:
    """provision.sh's selected(): whether a line with this tag field belongs to run."""
    tag = trim(field)
    if run == FULL_RUN:
        if tag == "":
            return True
        # bash, not §A12: the full run does not read the tag as parse_tag and tag_has do. It runs
        #   for id in $(printf '%s' "${tag#wf:}" | tr ',' ' '); do case "$IN_ALL" in *" $id "*) ...
        # so the commas become spaces and the words split on the default IFS (space, tab, newline), where
        # tag_has trims the list and removes only spaces: `wf:a b` and `wf:a,<tab>b` list b for the full run
        # but not for the b run, and `wf:<vtab>b` lists b for the b run only. An id matches when " <id> " is
        # in IN_ALL, so an in_all id holding a space matches each of its words.
        # Not mirrored: the unquoted $(...) is also glob-expanded in the working folder, so in the bash a
        # tag word holding *, ? or [ can turn into file names. Catalog ids are lowercase letters, digits and
        # hyphens (Req 26.4), so a valid tag holds none.
        words = _IFS_WORDS.split(_without_wf(tag).replace(",", " "))
        return any(word != "" and f" {word} " in in_all for word in words)
    if run == GLOBAL_RUN:
        return tag == "" or tag_has(tag, GLOBAL_RUN)
    return tag != "" and tag_has(tag, run)


def select(files: Mapping[str, str | None], run: str) -> Selection:
    """The Selection of one run of provision.sh: FULL_RUN ("all"), GLOBAL_RUN ("global") or a Catalog id.

    files maps catalog.txt and LIST_FILES to their text. A list that is absent or None selects nothing, as
    print_selection and the install phases skip a missing list. As in selection_mode, "all" and "global"
    are keywords even when the Catalog lists such an id:
    - "all", a full run: untagged lines, plus lines listing any in_all id
    - "global": untagged lines, plus lines listing global
    - an id: the lines listing that id
    Raises ValidationError where `provision.sh --selection <run>` exits 1: catalog.txt is missing, or run is
    none of these.
    """
    catalog_text = files.get(CATALOG)
    if catalog_text is None:
        raise ValidationError([Problem(CATALOG, None, None, "missing")])
    catalog = parse_catalog(catalog_text)
    if run not in (FULL_RUN, GLOBAL_RUN) and not _catalog_has(catalog, run):
        known = ", ".join(repr(line.id) for line in catalog) or "no id"
        reason = f"unknown workflow {run!r}; the Catalog has {known}"
        raise ValidationError([Problem(CATALOG, None, None, reason)])
    in_all = _in_all_text(catalog)

    def text(name: str) -> str:
        return files.get(name) or ""

    return Selection(
        run,
        tuple(line for line in parse_models(text(MODELS)) if _selected(line.tag, run, in_all)),
        tuple(line for line in parse_nodes(text(NODES)) if _selected(line.tag, run, in_all)),
        tuple(line for line in parse_ollama(text(OLLAMA)) if _selected(line.tag, run, in_all)),
    )
