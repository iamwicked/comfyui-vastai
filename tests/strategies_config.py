"""Config_Checker strategies (task 25.8, design Property 43): faults("config") and inject().

faults("config") draws one fault kind; a test draws a set of them with st.sets(faults("config"),
max_size=5) and applies it to a config_lists() configuration with inject(). Every fault appends its own
lines to the end of one file, so the line numbers of earlier lines never move and each fault is reported
on exactly the lines inject() names. Every word a fault writes stays clear of the Safety_Guard's term lists
(Req 26.8) and of the token formats (Req 26.9).
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from hypothesis import strategies as st

from strategies import CATALOG, MODELS, NODES, ConfigLists

# kind -> the rule group that must report it (design §C10)
FAULT_GROUPS = {
    "unknown-tag-id": "tags",
    "empty-tag-id": "tags",
    "field-count": "syntax",
    "kind": "syntax",
    "source": "syntax",
    "source-id": "syntax",
    "filename": "syntax",
    "subdir": "syntax",
    "install-path": "install-paths",
    "catalog-id": "catalog",
    "catalog-in-all": "catalog",
    "catalog-min-comfyui": "catalog",
    "catalog-duplicate": "catalog",
    "node-url": "nodes",
    "node-duplicate": "nodes",
    "nsfw-tag": "nsfw",
    "nsfw-lora-outside": "nsfw",
    "nsfw-folder-outside-set": "nsfw",
}
FAULT_KINDS = tuple(FAULT_GROUPS)
NSFW_ID = "influencer-nsfw"
NSFW_CATALOG_LINE = f"{NSFW_ID}|no||nsfw add-on"
# The Req 2.4 block an influencer-only line needs, so an NSFW fault breaks only the NSFW rule.
PROVENANCE_BLOCK = (
    "# source: https://huggingface.co/owner/fault-repo",
    "# licence: apache-2.0",
    "# verified: 2026-09-28",
)


def faults(scope: str) -> st.SearchStrategy[str]:
    """One fault kind of the scope; only "config" (Property 43) exists."""
    if scope != "config":
        raise ValueError(f"no faults for {scope!r}")
    return st.sampled_from(FAULT_KINDS)


@dataclass(frozen=True)
class Expected:
    """Where one injected fault must be reported: the file, the line, the group, and the earlier line a
    two-line fault must also name."""

    kind: str
    file: str
    line: int
    group: str
    also: int | None = None


def inject(config: ConfigLists, kinds: set[str] | frozenset[str]) -> tuple[ConfigLists, list[Expected]]:
    """config with every fault of kinds appended (in FAULT_KINDS order), and where each must be reported."""
    lines = {CATALOG: list(config.catalog), MODELS: list(config.models), NODES: list(config.nodes)}
    ident = config.ids[0]
    expected: list[Expected] = []

    def add(name: str, *texts: str) -> int:
        lines[name].extend(texts)
        return len(lines[name])  # the number of the last line added

    def first_catalog_line(target: str) -> int:
        for number, text in enumerate(lines[CATALOG], 1):
            if text.split("|")[0] == target and not text.lstrip().startswith("#") and text.strip():
                return number
        raise AssertionError(f"{target!r} is not in the Catalog")

    if kinds & {"nsfw-tag", "nsfw-lora-outside"}:
        add(CATALOG, NSFW_CATALOG_LINE)

    for k, kind in enumerate(sorted(kinds, key=FAULT_KINDS.index), 1):
        group = FAULT_GROUPS[kind]
        name = f"fault{k}"
        model = f"lora|{name}.safetensors|hf|owner/fault-repo::{name}.safetensors"
        if kind == "unknown-tag-id":
            expected.append(Expected(kind, MODELS, add(MODELS, f"{model}||wf:zz-unknown{k}"), group))
        elif kind == "empty-tag-id":
            expected.append(Expected(kind, MODELS, add(MODELS, f"{model}||wf:{ident},,{ident}"), group))
        elif kind == "field-count":
            expected.append(Expected(kind, MODELS, add(MODELS, f"{model}|"), group))
        elif kind == "kind":
            expected.append(Expected(kind, MODELS, add(MODELS, f"widget|{name}.bin|hf|owner/fault-repo::{name}.bin||"), group))
        elif kind == "source":
            expected.append(Expected(kind, MODELS, add(MODELS, f"lora|{name}.bin|ftp|{name}||"), group))
        elif kind == "source-id":
            expected.append(Expected(kind, MODELS, add(MODELS, f"lora|{name}.bin|hf|norepo{k}||"), group))
        elif kind == "filename":
            expected.append(Expected(kind, MODELS, add(MODELS, f"lora|{name}/x.bin|hf|owner/fault-repo::x.bin||"), group))
        elif kind == "subdir":
            expected.append(Expected(kind, MODELS, add(MODELS, f"lora|{name}.bin|hf|owner/fault-repo::x.bin|loras//{name}|"), group))
        elif kind == "install-path":
            first = add(MODELS, f"lora|{name}.bin|hf|owner/fault-repo::a.bin||")
            later = add(MODELS, f"lora|{name}.bin|hf|owner/fault-repo::b.bin||")
            expected.append(Expected(kind, MODELS, later, group, also=first))
        elif kind == "catalog-id":
            expected.append(Expected(kind, CATALOG, add(CATALOG, f"Fault_{k}|no||x"), group))
        elif kind == "catalog-in-all":
            expected.append(Expected(kind, CATALOG, add(CATALOG, f"{name}|maybe||x"), group))
        elif kind == "catalog-min-comfyui":
            expected.append(Expected(kind, CATALOG, add(CATALOG, f"{name}|no|1.2.3.4|x"), group))
        elif kind == "catalog-duplicate":
            first = first_catalog_line(ident)
            expected.append(Expected(kind, CATALOG, add(CATALOG, f"{ident}|no||x"), group, also=first))
        elif kind == "node-url":
            expected.append(Expected(kind, NODES, add(NODES, f"https://gitlab.com/owner/{name}"), group))
        elif kind == "node-duplicate":
            first = add(NODES, f"https://github.com/owner/{name}")
            later = add(NODES, f"https://github.com/Other/{name.upper()}.git")
            expected.append(Expected(kind, NODES, later, group, also=first))
        elif kind == "nsfw-tag":
            line = add(MODELS, f"{model}|loras/nsfw|wf:{NSFW_ID},{ident}")
            expected.append(Expected(kind, MODELS, line, group))
        elif kind == "nsfw-lora-outside":
            add(MODELS, *PROVENANCE_BLOCK)
            expected.append(Expected(kind, MODELS, add(MODELS, f"{model}|loras/looks|wf:{NSFW_ID}"), group))
        elif kind == "nsfw-folder-outside-set":
            expected.append(Expected(kind, MODELS, add(MODELS, f"{model}|loras/nsfw|wf:{ident}"), group))
        else:  # pragma: no cover - FAULT_KINDS lists every kind above
            raise AssertionError(kind)
    faulty = replace(config, catalog=tuple(lines[CATALOG]), models=tuple(lines[MODELS]), nodes=tuple(lines[NODES]))
    return faulty, expected
