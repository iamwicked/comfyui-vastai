"""Reading and printing YAML (design §A11, Data Models; Req 4.11, 5.13, 6.12).

load_yaml reads with PyYAML's SafeLoader and turns every way the file can fail to load into a
ValidationError, a syntax error included, with its line number. dump_yaml prints with

    yaml.dump(obj, Dumper=SafeDumper, allow_unicode=True, sort_keys=False, width=10**9,
              default_flow_style=False)

through a SafeDumper subclass with one string representer:
- literal block style (`|`, `|-`) for multi-line strings holding none of \\r, \\x85, \\u2028, \\u2029 or a
  tab, no trailing space on any line, and no leading or trailing blank line;
- double quotes for every other string that PyYAML would not print as a safe plain scalar: values YAML
  reads as another type (`no`, `on`, `y`, `2026`, `1e3`, `0o17`, `~`, dates, the empty string) and values
  starting with `#`, containing `: ` or ` #`, or with leading or trailing spaces. Besides PyYAML's own
  YAML 1.1 rules, the YAML 1.2 number forms and the 1.1 booleans y/n are quoted too, so other YAML tools
  read the file the same way. PyYAML escapes non-printable characters inside double quotes;
- plain style for the rest.
So dump_yaml's output reads back equal: yaml.load(dump_yaml(obj), SafeLoader) == obj for nested dicts,
lists and strings. It never writes anchors and aliases; a shared object is printed at each place.
The global SafeDumper is left untouched.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

from influencer.errors import Problem, ValidationError

_STR_TAG = "tag:yaml.org,2002:str"
# Line breaks YAML folds or keeps differently in a block scalar, and the tab, which PyYAML cannot print
# there; a multi-line string holding one of them is double-quoted instead.
_NOT_IN_LITERAL = frozenset("\r\x85\u2028\u2029\t")
# YAML 1.2 core-schema numbers; PyYAML (YAML 1.1) reads 1e3 and 0o17 as strings, other tools as numbers.
_YAML12_NUMBER = re.compile(
    r"[-+]?(?:\.[0-9]+|[0-9]+(?:\.[0-9]*)?)(?:[eE][-+]?[0-9]+)?|0o[0-7]+|0x[0-9a-fA-F]+"
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)"
)
_YAML11_SHORT_BOOLS = frozenset({"y", "Y", "n", "N"})  # YAML 1.1 booleans that PyYAML reads as strings
# PyYAML counts these as line breaks in its marks: \r\n, then any single break character.
_LINE_BREAK = re.compile(r"\r\n|[\r\n\x85\u2028\u2029]")


class _Dumper(yaml.SafeDumper):
    """SafeDumper with the §A11 string representer and no aliases."""

    def ignore_aliases(self, data: object) -> bool:
        return True


def _literal_ok(value: str, analysis: yaml.emitter.ScalarAnalysis) -> bool:
    return (
        "\n" in value
        and analysis.allow_block  # PyYAML's own verdict: no trailing spaces, no special characters
        and _NOT_IN_LITERAL.isdisjoint(value)
        and not value.startswith("\n")  # a leading blank line
        and not value.endswith("\n\n")  # a trailing blank line
    )


def _represent_str(dumper: _Dumper, value: str) -> yaml.ScalarNode:
    analysis = dumper.analyze_scalar(value)
    if _literal_ok(value, analysis):
        return dumper.represent_scalar(_STR_TAG, value, style="|")
    ambiguous = (
        dumper.resolve(yaml.ScalarNode, value, (True, False)) != _STR_TAG  # no, on, 2026, ~, dates, ""
        or _YAML12_NUMBER.fullmatch(value) is not None
        or value in _YAML11_SHORT_BOOLS
    )
    if ambiguous or not analysis.allow_block_plain:  # "#x", "a: b", "a #b", " x", "x ", line breaks
        return dumper.represent_scalar(_STR_TAG, value, style='"')
    return dumper.represent_scalar(_STR_TAG, value)


_Dumper.add_representer(str, _represent_str)


def dump_yaml(obj: object) -> str:
    """obj as YAML text (see the module docstring for the string styles)."""
    return yaml.dump(
        obj,
        Dumper=_Dumper,
        allow_unicode=True,
        sort_keys=False,
        width=10**9,
        default_flow_style=False,
    )


def _line_of(text: str, index: int) -> int:
    """The 1-based line of text[index], counting line breaks as PyYAML's marks do."""
    return len(_LINE_BREAK.findall(text, 0, index)) + 1


def load_yaml(path: str | os.PathLike[str]) -> object:
    """The file's single YAML document, read with SafeLoader (None for an empty file).

    A file that cannot be read, is not UTF-8, holds a character YAML does not allow, or has a syntax
    error raises a ValidationError with one Problem naming the path and, when known, the line."""
    name = os.fspath(path)
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        reason = f"cannot read the file: {error.strerror or error}"
        raise ValidationError([Problem(name, None, None, reason)]) from None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        line = data.count(b"\n", 0, error.start) + 1
        reason = f"not UTF-8 text: byte 0x{data[error.start]:02x} at byte offset {error.start}"
        raise ValidationError([Problem(name, None, line, reason)]) from None
    try:
        return yaml.load(text, Loader=yaml.SafeLoader)
    except yaml.MarkedYAMLError as error:
        mark = error.problem_mark or error.context_mark
        where = f" (column {mark.column + 1})" if mark is not None else ""
        what = "; ".join(part for part in (error.context, error.problem) if part)
        reason = f"YAML syntax error{where}: {what or 'the file does not parse'}"
        raise ValidationError([Problem(name, None, mark.line + 1 if mark else None, reason)]) from None
    except yaml.reader.ReaderError as error:  # a control character, for example
        character = error.character
        shown = f"U+{character:04X}" if isinstance(character, int) else repr(character)
        reason = f"character {shown} is not allowed in YAML"
        raise ValidationError([Problem(name, None, _line_of(text, error.position), reason)]) from None
    except yaml.YAMLError as error:
        raise ValidationError([Problem(name, None, None, f"YAML error: {error}")]) from None
