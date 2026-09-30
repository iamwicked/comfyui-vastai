"""The Licence_Gate: `# licence:` records, the Noncommercial_List and the conditions map (design §M15,
Decision 26; Req 2.4, 24.3, 26.17, 26.18).

One module for both checkers: the Config_Checker gates the committed records offline (task 25.13), and the
Source_Checker (task 43) gates the licence a source declares now with the same parser and gate. Apart from
the two loaders' file reads, everything here is pure.

    parse_record(text) -> Record          the §M15 grammar; a malformed record raises RecordError
    gate(record, noncommercial) -> GateResult
    load_noncommercial(path) -> frozenset[str]     lower-cased identifiers; read_noncommercial() keeps lines
    load_conditions(path) -> dict[str, str]        licence id -> text; read_conditions() keeps lines

Record grammar (§M15):

    record     = "# licence: " primary [ "; conditions: " text ] [ "; terms: " text ]
    primary    = licence-id [ " (" note ")" ] | "none stated" [ " (" note ")" ] | civitai
    civitai    = "Civitai permissions: commercial use " uses [ "; credit " ("required" / "not required") ]
                 [ "; derivatives " ("allowed" / "not allowed") ] [ "; relicensing " ("allowed" / "not allowed") ]
    uses       = "none" / use *( ", " use )      ; Image, RentCivit, Rent, Sell, SellMerge
    licence-id = 1*( ALPHA / DIGIT / "." / "-" / "_" / "+" )    ; compared in lower case
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from influencer.errors import Problem, ValidationError

LICENCES_FOLDER = ("influencer", "licences")  # below the config folder
NONCOMMERCIAL = "noncommercial.txt"
CONDITIONS = "conditions.txt"

RECORD_PREFIX = "licence:"
CONDITIONS_SEP = "; conditions: "
TERMS_SEP = "; terms: "
NONE_STATED = "none stated"
CIVITAI_PREFIX = "Civitai permissions:"
CIVITAI_USES = ("Image", "RentCivit", "Rent", "Sell", "SellMerge")  # Civitai's allowCommercialUse values
IMAGE_USE = "Image"
CREDIT_CONDITION = "credit the creator"
# Identifiers the gate cannot classify: they pass, and their record must name the terms in `terms:`.
UNCLASSIFIABLE = ("other", "civitai-model-license", NONE_STATED)
CIVITAI_MODEL_LICENSE = "civitai-model-license"

_LICENCE_ID = re.compile(r"[A-Za-z0-9._+-]+")
_PRIMARY = re.compile(r"([A-Za-z0-9._+-]+)(?: \((.*)\))?", re.S)
_NONE_STATED = re.compile(r"none stated(?: \((.*)\))?", re.S)
_CIVITAI = re.compile(
    r"Civitai permissions: commercial use (?P<uses>[^;]+?)"
    r"(?:; credit (?P<credit>required|not required))?"
    r"(?:; derivatives (?P<derivatives>allowed|not allowed))?"
    r"(?:; relicensing (?P<relicensing>allowed|not allowed))?",
    re.S,
)
_RECORD_LINE = re.compile(r"#+\s*licence:\s*(.*)", re.S)


class RecordError(ValueError):
    """A `# licence:` record that does not follow the §M15 grammar; str() is the reason."""


@dataclass(frozen=True)
class Civitai:
    """One Civitai form: the commercial uses and the optional credit, derivative and relicensing parts."""

    uses: tuple[str, ...]  # () for "none"
    credit_required: bool | None = None
    derivatives: bool | None = None
    relicensing: bool | None = None

    @property
    def allows_image(self) -> bool:
        return IMAGE_USE in self.uses


@dataclass(frozen=True)
class Record:
    """A parsed `# licence:` record."""

    primary: str  # the primary as written: licence id with note, "none stated ...", or a Civitai form
    identifier: str | None  # lower-cased licence id or "none stated"; None for a Civitai primary
    note: str | None = None
    civitai: Civitai | None = None  # the primary's Civitai form
    conditions: str | None = None
    terms: str | None = None
    terms_civitai: Civitai | None = None  # a `terms:` text in the Civitai form

    @property
    def civitai_forms(self) -> tuple[Civitai, ...]:
        return tuple(form for form in (self.civitai, self.terms_civitai) if form is not None)

    @property
    def civitai_uses(self) -> tuple[str, ...] | None:
        """The uses of every Civitai form in the record, None when it has none."""
        forms = self.civitai_forms
        return None if not forms else tuple(use for form in forms for use in form.uses)

    @property
    def credit_required(self) -> bool:
        return any(form.credit_required for form in self.civitai_forms)


@dataclass(frozen=True)
class GateResult:
    """failed with the reason, or passed with the conditions to report."""

    passed: bool
    reason: str | None = None
    conditions: tuple[str, ...] = ()


# ---- records ----


def parse_civitai(text: str) -> Civitai:
    """A Civitai form; RecordError when it does not follow the grammar."""
    match = _CIVITAI.fullmatch(text)
    if match is None:
        raise RecordError(
            f"{text!r} is not a Civitai form: 'Civitai permissions: commercial use <uses>' then optional "
            "'; credit required|not required', '; derivatives allowed|not allowed', "
            "'; relicensing allowed|not allowed', in that order"
        )
    raw = match["uses"]
    if raw == "none":
        uses: tuple[str, ...] = ()
    else:
        uses = tuple(raw.split(", "))
        unknown = [use for use in uses if use not in CIVITAI_USES]
        if unknown:
            raise RecordError(
                f"unknown Civitai commercial use {unknown[0]!r}; the uses are 'none' or a ', ' list of "
                + ", ".join(CIVITAI_USES)
            )
        if len(set(uses)) != len(uses):
            raise RecordError(f"a Civitai commercial use is listed twice in {raw!r}")

    def flag(value: str | None, yes: str) -> bool | None:
        return None if value is None else value == yes

    return Civitai(
        uses,
        flag(match["credit"], "required"),
        flag(match["derivatives"], "allowed"),
        flag(match["relicensing"], "allowed"),
    )


def parse_record(text: str) -> Record:
    """A `# licence:` record, given as its value (the text after `licence:`) or as the whole comment line.

    `; terms: ` is split off first, then `; conditions: `, and the rest is the primary, whose first token,
    lower-cased, is the identifier. A `terms:` text starting `Civitai permissions:` is a second Civitai form.
    Raises RecordError with the reason for a malformed record."""
    value = text.strip()
    line = _RECORD_LINE.fullmatch(value)
    if line is not None:
        value = line[1].strip()
    if value == "":
        raise RecordError("the record is empty; write 'none stated' when the source states no licence")
    terms = conditions = None
    if value.endswith(TERMS_SEP.rstrip()):
        raise RecordError("'terms:' is empty")
    if TERMS_SEP in value:
        value, terms = value.split(TERMS_SEP, 1)
        terms = terms.strip()
        if terms == "":
            raise RecordError("'terms:' is empty")
        if CONDITIONS_SEP in terms or TERMS_SEP in terms:
            raise RecordError("'conditions:' comes before 'terms:', and each appears once")
    value = value.rstrip()
    if value.endswith(CONDITIONS_SEP.rstrip()):
        raise RecordError("'conditions:' is empty")
    if CONDITIONS_SEP in value:
        value, conditions = value.split(CONDITIONS_SEP, 1)
        conditions = conditions.strip()
        if conditions == "":
            raise RecordError("'conditions:' is empty")
        if CONDITIONS_SEP in conditions:
            raise RecordError("'conditions:' appears twice")
    primary = value.strip()
    if primary == "":
        raise RecordError("the record has no licence before 'conditions:' or 'terms:'")
    terms_civitai = parse_civitai(terms) if terms is not None and terms.startswith(CIVITAI_PREFIX) else None
    if primary.startswith(CIVITAI_PREFIX):
        return Record(primary, None, None, parse_civitai(primary), conditions, terms, terms_civitai)
    stated = _NONE_STATED.fullmatch(primary)
    if stated is not None:
        return Record(primary, NONE_STATED, stated[1], None, conditions, terms, terms_civitai)
    match = _PRIMARY.fullmatch(primary)
    if match is None:
        first = primary.split(" ", 1)[0]
        if not _LICENCE_ID.fullmatch(first):
            raise RecordError(
                f"{first!r} is not a licence identifier (letters, digits, '.', '-', '_' and '+'), "
                "'none stated' or a Civitai form"
            )
        raise RecordError(f"after the identifier {first!r} only a note in ' (...)' may follow, not {primary[len(first):]!r}")
    return Record(primary, match[1].lower(), match[2], None, conditions, terms, terms_civitai)


# ---- the gate ----


def gate(record: Record, noncommercial: frozenset[str] | set[str]) -> GateResult:
    """Fails a record whose identifier is on the Noncommercial_List or whose Civitai form, the primary or the
    `terms:`, lists no Image use. Otherwise passes it with its `conditions:` text, plus "credit the creator"
    for a Civitai form with `credit required`. Unclassifiable records (other, civitai-model-license, none
    stated) pass, while the Civitai rule still applies to their `terms:`."""
    if record.identifier is not None and record.identifier in noncommercial:
        return GateResult(False, f"{record.identifier!r} is on the Noncommercial_List")
    for form in record.civitai_forms:
        if not form.allows_image:
            listed = ", ".join(form.uses) or "none"
            return GateResult(False, f"the Civitai permissions list no Image commercial use (commercial use {listed})")
    conditions = ((record.conditions,) if record.conditions else ()) + ((CREDIT_CONDITION,) if record.credit_required else ())
    return GateResult(True, None, conditions)


def same_text(a: str, b: str) -> bool:
    """Two condition texts compared with every whitespace run as one space."""
    return " ".join(a.split()) == " ".join(b.split())


# ---- the two lists ----


def licences_dir(config_dir: str | os.PathLike[str]) -> Path:
    return Path(config_dir).joinpath(*LICENCES_FOLDER)


def _read_text(path: str | os.PathLike[str], what: str) -> str:
    shown = os.fspath(path)
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        raise ValidationError([Problem(shown, None, None, f"the {what} is missing")]) from None
    except OSError as error:
        raise ValidationError([Problem(shown, None, None, f"cannot read the {what}: {error.strerror or error}")]) from None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        line = data.count(b"\n", 0, error.start) + 1
        raise ValidationError([Problem(shown, None, line, f"the {what} is not UTF-8 text")]) from None


def read_noncommercial(path: str | os.PathLike[str]) -> dict[str, int]:
    """The Noncommercial_List: lower-cased identifier -> line number. One identifier per line, and `#`
    starts a comment. A missing file, a line that is not one identifier, and a list with no identifier
    raise one ValidationError whose Problems name the file (Req 26.17)."""
    shown = os.fspath(path)
    text = _read_text(path, "Noncommercial_List")
    found: dict[str, int] = {}
    problems: list[Problem] = []
    for number, line in enumerate(text.split("\n"), 1):
        entry = line.split("#", 1)[0].strip()
        if entry == "":
            continue
        if not _LICENCE_ID.fullmatch(entry):
            problems.append(Problem(shown, None, number, f"{entry!r} is not one licence identifier"))
            continue
        found.setdefault(entry.lower(), number)
    if not found:
        problems.append(Problem(shown, None, None, "the Noncommercial_List lists no licence identifiers"))
    if problems:
        raise ValidationError(problems)
    return found


def load_noncommercial(path: str | os.PathLike[str]) -> frozenset[str]:
    return frozenset(read_noncommercial(path))


def read_conditions(path: str | os.PathLike[str]) -> dict[str, tuple[int, str]]:
    """The conditions map: lower-cased licence id -> (line number, text). One `licence-id|text` line per
    licence; a line whose first non-blank character is `#` is a comment (the texts are prose, so a `#`
    inside one stays text). A missing file, a malformed line and a repeated id raise one ValidationError."""
    shown = os.fspath(path)
    text = _read_text(path, "conditions map")
    found: dict[str, tuple[int, str]] = {}
    problems: list[Problem] = []
    for number, line in enumerate(text.split("\n"), 1):
        entry = line.strip()
        if entry == "" or entry.startswith("#"):
            continue
        parts = entry.split("|")
        if len(parts) != 2:
            problems.append(Problem(shown, None, number, "a line is 'licence-id|text', with no '|' inside the text"))
            continue
        ident, condition = parts[0].strip(), parts[1].strip()
        if not _LICENCE_ID.fullmatch(ident):
            problems.append(Problem(shown, None, number, f"{ident!r} is not a licence identifier"))
        elif condition == "":
            problems.append(Problem(shown, None, number, f"{ident!r} has no conditions text"))
        elif ident.lower() in found:
            problems.append(Problem(shown, None, number, f"{ident!r} is already mapped on line {found[ident.lower()][0]}"))
        else:
            found[ident.lower()] = (number, condition)
    if problems:
        raise ValidationError(problems)
    return found


def load_conditions(path: str | os.PathLike[str]) -> dict[str, str]:
    return {ident: condition for ident, (_, condition) in read_conditions(path).items()}
