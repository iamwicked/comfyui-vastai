"""The Persona_File: the Persona model, the Persona_Reader and the Persona_Printer (design §M1 and
Decision 16; Req 4, 8.10, 26.12).

    read_persona(path, root, *, check_files=True) -> Persona   one ValidationError holding every Problem
    print_persona(persona) -> str                               YAML that read_persona reads back equal
    persona_warnings(persona) -> list[Problem]                  a location count other than 3 or 4 (Req 4.9)
    resolve_path(root, relative) -> Path                        the file a Persona_File path names (Req 4.10)

Pure apart from reading the Persona_File and the optional file-existence check: with check_files=False the
Reader touches no other file. The Instagram_Allowlist checks (Req 21.6) need models.list, so they run when
a stage starts (allowlist.py), not here.

The rules of §M1, at any depth:
- A key the format does not define is an unknown field. `looks` takes `stills` and `video` only, so
  `looks.edit` is one: there is no edit look stack. A value of another type than the format's is rejected
  as such, and YAML null is no field's value: an optional field is left out instead.
- Required: slug, name, age, voice, identity, trigger_word, hero_image and grade.lut. The text ones must
  not be blank (empty or only whitespace).
- slug: 1-32 of [a-z0-9-]. trigger_word: 4-32 of [a-z0-9_]. age: a YAML int, not a bool, 25 to 99.
  version: optional, and 1 when given.
- identity (Decision 16): no blank line, no leading or trailing whitespace, and not a substring of the
  Trigger_Word, so the Prompt_Assembler (task 13) can remove every other occurrence of it.
- hero_image, grade.lut and each reference_pack item: relative, with no `..` segment, and with check_files
  an existing regular file at resolve_path(root, path) (a folder does not count). Location shots and LoRA
  paths (below models/loras) follow the same relative, no-`..` rule without the existence check.
  reference_pack lists 0 to 5 images.
- locations: {name, description[, shots]}; wardrobe: {name, description}. Names and descriptions are
  non-blank text, and names are unique within their list. Shots are Location_Shots, so outfits have none.
- character_loras.stills and .video: {lora, strength}, strength in (0, 2.0], default 1.0.
  looks.<stills|video>.<instagram|other>: at most 8 entries, each {lora, strength} with strength in
  [-2.0, 2.0] (default 1.0), or a plain string, which means strength 1.0.
- grade.grain: a number 0.0 to 1.0, optional here (the Export_Tool requires it). sharp_decode: a bool,
  default false. captions.hashtag_limit: an int >= 0, default 5, values above 5 counting as 5
  (Captions.max_hashtags). captions.disclosure: a non-blank single line without `#`. language (default
  English) and ollama.instagram and .other (defaults qwen3:8b and dolphin3:8b): non-blank single lines.
  niche, themes, signature_items, captions.rules and captions.flagged_terms are free text.
- Every string, mapping keys included, passes the Safety_Guard (Req 4.4).

Every Problem names the file, the field as safety.strings() writes it ("locations[2].name", item numbers
counted from 1) and the reason; a YAML syntax error names its line instead (yamlio.load_yaml).
"""
from __future__ import annotations

import dataclasses
import datetime
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from influencer.env import PERSONA_FILE, SLUG_RE
from influencer.errors import Problem, ValidationError
from influencer.safety import check_tree
from influencer.yamlio import dump_yaml, load_yaml

FORMAT_VERSION = 1
TRIGGER_WORD_RE = re.compile(r"[a-z0-9_]{4,32}")
AGE_MIN, AGE_MAX = 25, 99  # Req 4.3
MAX_REFERENCES = 5  # Req 4.8
MAX_LOOKS = 8  # per look list (Req 8.10)
CHARACTER_STRENGTH_MAX = 2.0  # a Character_LoRA strength is in (0, 2.0] (Req 8.10)
LOOK_STRENGTH_LIMIT = 2.0  # a look LoRA strength is in [-2.0, 2.0] (Req 8.10)
DEFAULT_STRENGTH = 1.0
HASHTAG_CAP = 5  # a hashtag_limit above this counts as this (Req 6.8)
DEFAULT_HASHTAG_LIMIT = 5
DEFAULT_LANGUAGE = "English"  # the dialogue language tag (Decision 18)
DEFAULT_OLLAMA_INSTAGRAM = "qwen3:8b"
DEFAULT_OLLAMA_OTHER = "dolphin3:8b"
RECOMMENDED_LOCATIONS = (3, 4)  # any other count warns (Req 4.9)

# The keys of each mapping of the format, in §M1 order.
PERSONA_FIELDS = (
    "version", "slug", "name", "age", "niche", "language", "voice", "themes", "identity", "trigger_word",
    "hero_image", "reference_pack", "grade", "sharp_decode", "locations", "wardrobe", "signature_items",
    "character_loras", "looks", "captions", "ollama",
)  # fmt: skip
GRADE_FIELDS = ("lut", "grain")
LOCATION_FIELDS = ("name", "description", "shots")
OUTFIT_FIELDS = ("name", "description")
CHARACTER_LORA_FIELDS = ("stills", "video")
LORA_FIELDS = ("lora", "strength")
LOOK_MEDIA = ("stills", "video")
LOOK_PLATFORMS = ("instagram", "other")
CAPTION_FIELDS = ("rules", "disclosure", "hashtag_limit", "flagged_terms")
OLLAMA_FIELDS = ("instagram", "other")

INFLUENCER_ROOT = "the Influencer_Root"
LORA_ROOT = "models/loras"


# ---- the model ----


@dataclass(frozen=True)
class LoraRef:
    """A LoRA file, as a path below models/loras, and its strength."""

    lora: str
    strength: float = DEFAULT_STRENGTH


@dataclass(frozen=True)
class CharacterLoras:
    """The persona's own trained LoRAs: Krea 2 stills and MiniMax-H3 video; None before training."""

    stills: LoraRef | None = None
    video: LoraRef | None = None


@dataclass(frozen=True)
class LookStack:
    """The look LoRAs of one medium for each platform, in chain order."""

    instagram: tuple[LoraRef, ...] = ()
    other: tuple[LoraRef, ...] = ()


@dataclass(frozen=True)
class Looks:
    stills: LookStack = dataclasses.field(default_factory=LookStack)
    video: LookStack = dataclasses.field(default_factory=LookStack)


@dataclass(frozen=True)
class Grade:
    lut: str  # the 3D LUT, relative to the Influencer_Root
    grain: float | None = None  # None when the Persona_File gives none


@dataclass(frozen=True)
class Location:
    name: str
    description: str
    shots: tuple[str, ...] = ()  # Location_Shots, relative to the Influencer_Root


@dataclass(frozen=True)
class Outfit:
    name: str
    description: str


@dataclass(frozen=True)
class Captions:
    rules: str | None = None
    disclosure: str | None = None  # the disclosure line, None when the Persona_File gives none
    hashtag_limit: int = DEFAULT_HASHTAG_LIMIT  # as written
    flagged_terms: tuple[str, ...] = ()

    @property
    def max_hashtags(self) -> int:
        """The number of hashtags a caption may hold: hashtag_limit, counting values above 5 as 5."""
        return min(self.hashtag_limit, HASHTAG_CAP)


@dataclass(frozen=True)
class Ollama:
    """The Ollama model for each platform's plans and captions."""

    instagram: str = DEFAULT_OLLAMA_INSTAGRAM
    other: str = DEFAULT_OLLAMA_OTHER


@dataclass(frozen=True)
class Persona:
    """A valid Persona_File's field values, defaults filled in. Paths stay as written, relative to the
    Influencer_Root (resolve_path gives the file). `source`, the file it was read from, names the file in
    persona_warnings() and takes no part in equality."""

    slug: str
    name: str
    age: int
    voice: str
    identity: str  # the Identity_Paragraph
    trigger_word: str
    hero_image: str
    grade: Grade
    version: int = FORMAT_VERSION
    niche: str | None = None
    language: str = DEFAULT_LANGUAGE
    themes: tuple[str, ...] = ()
    reference_pack: tuple[str, ...] = ()
    sharp_decode: bool = False
    locations: tuple[Location, ...] = ()
    wardrobe: tuple[Outfit, ...] = ()
    signature_items: tuple[str, ...] = ()
    character_loras: CharacterLoras = dataclasses.field(default_factory=CharacterLoras)
    looks: Looks = dataclasses.field(default_factory=Looks)
    captions: Captions = dataclasses.field(default_factory=Captions)
    ollama: Ollama = dataclasses.field(default_factory=Ollama)
    source: str = dataclasses.field(default=PERSONA_FILE, compare=False, repr=False)


# ---- paths ----


def _escapes(relative: str) -> str | None:
    """Why relative is no path inside a root, or None when it is one."""
    if PurePosixPath(relative).is_absolute():
        return "is an absolute path"
    if ".." in relative.split("/"):
        return "holds a '..' segment"
    return None


def resolve_path(root: str | os.PathLike[str], relative: str) -> Path:
    """The file a Persona_File path names: the Influencer_Root joined with the path, whatever the working
    folder (Req 4.10). An absolute path or a `..` segment, which would lead outside root, is a ValueError."""
    reason = _escapes(relative)
    if reason is not None:
        raise ValueError(f"{relative!r} {reason}, so it is not a path inside {os.fspath(root)}")
    return Path(root) / relative


# ---- the Reader ----

_ABSENT = object()
Check = Callable[[Any, str], Any]  # (value, field path) -> the accepted value, or None after a Problem


def _join(parent: str, key: object) -> str:
    """A field path, written as safety.strings() writes it."""
    return f"{parent}.{key}" if parent else str(key)


def _short(value: object, limit: int = 60) -> str:
    shown = repr(value)
    return shown if len(shown) <= limit else shown[: limit - 3] + "..."


def _describe(value: object) -> str:
    """value's YAML type, with the value when it is a short scalar: for "must be X, not <this>"."""
    if value is None:
        return "an empty value (null)"
    if isinstance(value, bool):
        return f"the boolean {'true' if value else 'false'}"
    if isinstance(value, int):
        return f"the whole number {_short(value)}"
    if isinstance(value, float):
        return f"the number {_short(value)}"
    if isinstance(value, str):
        return f"the text {_short(value)}"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a mapping"
    if isinstance(value, datetime.date):  # datetimes included
        return f"the date {value.isoformat()}"
    return f"a value of type {type(value).__name__}"


def _listing(keys: tuple[str, ...]) -> str:
    return keys[0] if len(keys) == 1 else ", ".join(keys[:-1]) + " and " + keys[-1]


class _Reader:
    """Checks the parsed data of one Persona_File and collects every Problem. Each check returns the value
    it accepted, or None after recording why not; the Persona is built only when no Problem was found."""

    def __init__(self, source: str, root: Path | None, check_files: bool) -> None:
        self.source = source  # the Persona_File, as every Problem names it
        self.root = root
        self.check_files = check_files
        self.problems: list[Problem] = []

    def fail(self, where: str | None, reason: str) -> None:
        self.problems.append(Problem(self.source, where or None, None, reason))

    def get(self, data: object, key: str, parent: str, check: Check, *, required: bool = False,
            default: object = None) -> Any:  # fmt: skip
        """check(data[key]) when data is a mapping holding key. default when data is no mapping (the parent
        is left out, or its own Problem is already recorded) or lacks key, which is a Problem when required.
        A null value is a Problem: no field takes null."""
        where = _join(parent, key)
        if not isinstance(data, dict):
            return default
        value = data.get(key, _ABSENT)
        if value is _ABSENT:
            if required:
                self.fail(where, "is missing")
            return default
        if value is None:
            self.fail(where, "has no value" if required else "has no value: give one, or leave the field out")
            return None
        return check(value, where)

    # ---- containers ----

    def mapping(self, value: object, where: str, keys: tuple[str, ...]) -> dict[Any, Any] | None:
        """value when it is a mapping; each key outside keys is reported as an unknown field."""
        if not isinstance(value, dict):
            self.fail(where, f"must be a mapping of {_listing(keys)}, not {_describe(value)}")
            return None
        for key in value:
            if key not in keys:
                owner = f"{where}, which takes {_listing(keys)}" if where else "the Persona_File format"
                self.fail(_join(where, key), f"is not a field of {owner}")
        return value

    def mapping_of(self, keys: tuple[str, ...]) -> Check:
        return lambda value, where: self.mapping(value, where, keys)

    def items(self, value: object, where: str, check: Check, what: str = "a list") -> tuple[Any, ...] | None:
        """check(item) for each item of a list, the item number counted from 1."""
        if not isinstance(value, list):
            self.fail(where, f"must be {what}, not {_describe(value)}")
            return None
        return tuple(check(item, f"{where}[{number}]") for number, item in enumerate(value, 1))

    def texts(self, value: object, where: str) -> tuple[str, ...] | None:
        return self.items(value, where, self.free_text, "a list of text")

    # ---- scalars ----

    def text(self, value: object, where: str, *, blank: bool = True, single_line: bool = False) -> str | None:
        if not isinstance(value, str):
            self.fail(where, f"must be text, not {_describe(value)}")
            return None
        if not blank and not value.strip():
            self.fail(where, "is blank")
            return None
        if single_line and value.splitlines() != [value]:
            self.fail(where, "must be a single line")
            return None
        return value

    def free_text(self, value: object, where: str) -> str | None:
        return self.text(value, where)

    def required_text(self, value: object, where: str) -> str | None:
        return self.text(value, where, blank=False)

    def line(self, value: object, where: str) -> str | None:
        return self.text(value, where, blank=False, single_line=True)

    def pattern(self, value: object, where: str, regex: re.Pattern[str], what: str, rule: str) -> str | None:
        text = self.text(value, where, blank=False)
        if text is not None and not regex.fullmatch(text):
            self.fail(where, f"{_short(text)} is not a valid {what}: use {rule}")
            return None
        return text

    def slug(self, value: object, where: str) -> str | None:
        return self.pattern(value, where, SLUG_RE, "slug", "1 to 32 of the characters a-z, 0-9 and -")

    def trigger_word(self, value: object, where: str) -> str | None:
        return self.pattern(value, where, TRIGGER_WORD_RE, "Trigger_Word", "4 to 32 of the characters a-z, 0-9 and _")

    def integer(self, value: object, where: str, what: str) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int):
            self.fail(where, f"must be {what}, not {_describe(value)}")
            return None
        return value

    def version(self, value: object, where: str) -> int | None:
        number = self.integer(value, where, f"the whole number {FORMAT_VERSION}")
        if number is not None and number != FORMAT_VERSION:
            self.fail(where, f"is {number}; this Persona_Reader reads version {FORMAT_VERSION}")
            return None
        return number

    def age(self, value: object, where: str) -> int | None:
        age = self.integer(value, where, "a YAML integer")
        if age is not None and not AGE_MIN <= age <= AGE_MAX:
            self.fail(where, f"{age} is out of range: the age must be {AGE_MIN} to {AGE_MAX}")
            return None
        return age

    def hashtag_limit(self, value: object, where: str) -> int | None:
        limit = self.integer(value, where, "a whole number")
        if limit is not None and limit < 0:
            self.fail(where, f"{limit} is below 0")
            return None
        return limit

    def boolean(self, value: object, where: str) -> bool | None:
        if not isinstance(value, bool):
            self.fail(where, f"must be true or false, not {_describe(value)}")
            return None
        return value

    def number(self, value: object, where: str) -> int | float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.fail(where, f"must be a number, not {_describe(value)}")
            return None
        return value

    def grain(self, value: object, where: str) -> float | None:
        grain = self.number(value, where)
        if grain is None:
            return None
        if not 0.0 <= grain <= 1.0:  # NaN fails too
            self.fail(where, f"{_short(grain)} is out of range: use 0.0 to 1.0")
            return None
        return float(grain)

    def strength(self, value: object, where: str, lora: str | None, *, character: bool) -> float | None:
        strength = self.number(value, where)
        if strength is None:
            return None
        if character:
            fits, rule = 0.0 < strength <= CHARACTER_STRENGTH_MAX, f"above 0 and at most {CHARACTER_STRENGTH_MAX}"
        else:
            fits, rule = -LOOK_STRENGTH_LIMIT <= strength <= LOOK_STRENGTH_LIMIT, (
                f"{-LOOK_STRENGTH_LIMIT} to {LOOK_STRENGTH_LIMIT}"
            )
        if not fits:  # compared before float(), so a huge int cannot overflow
            of = f" of {lora!r}" if lora is not None else ""
            self.fail(where, f"the strength {_short(strength)}{of} is out of range: use {rule}")
            return None
        return float(strength)

    # ---- Persona_File text rules ----

    def identity(self, value: object, where: str) -> str | None:
        text = self.text(value, where, blank=False)
        if text is None:
            return None
        valid = True
        if text[0].isspace():
            self.fail(where, "starts with whitespace; the Identity_Paragraph must not")
            valid = False
        if text[-1].isspace():
            self.fail(where, "ends with whitespace; the Identity_Paragraph must not")
            valid = False
        if any(not line.strip() for line in text.splitlines()):
            self.fail(where, "holds a blank line; the Identity_Paragraph must be a single paragraph")
            valid = False
        return text if valid else None

    def disclosure(self, value: object, where: str) -> str | None:
        text = self.line(value, where)
        if text is not None and "#" in text:
            self.fail(where, f"{_short(text)} holds '#'; the disclosure line takes no hashtag")
            return None
        return text

    # ---- paths ----

    def path(self, value: object, where: str, *, base: str = INFLUENCER_ROOT, must_exist: bool = False) -> str | None:
        text = self.text(value, where, blank=False)
        if text is None:
            return None
        reason = _escapes(text)
        if reason is not None:
            self.fail(where, f"{text!r} {reason}: give a path inside {base}")
            return None
        if must_exist and self.check_files:
            self.check_file(text, where)
        return text

    def file(self, value: object, where: str) -> str | None:
        return self.path(value, where, must_exist=True)

    def lora_path(self, value: object, where: str) -> str | None:
        return self.path(value, where, base=LORA_ROOT)

    def check_file(self, text: str, where: str) -> None:
        """A Problem naming the field and the path unless root/text is an existing regular file."""
        assert self.root is not None  # read_persona refuses check_files without a root
        resolved = resolve_path(self.root, text)
        try:
            mode = os.stat(resolved).st_mode
        except (FileNotFoundError, NotADirectoryError):
            self.fail(where, f"{text!r} does not exist: there is no file at {resolved}")
            return
        except (OSError, ValueError) as error:  # a NUL byte, a name too long, no permission
            detail = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
            self.fail(where, f"{text!r} cannot be checked at {resolved}: {detail}")
            return
        if stat.S_ISDIR(mode):
            self.fail(where, f"{text!r} is a folder, not a file: {resolved}")
        elif not stat.S_ISREG(mode):
            self.fail(where, f"{text!r} is not a regular file: {resolved}")

    # ---- list fields ----

    def references(self, value: object, where: str) -> tuple[str, ...] | None:
        if isinstance(value, list) and len(value) > MAX_REFERENCES:
            self.fail(where, f"lists {len(value)} images; the Reference_Pack holds at most {MAX_REFERENCES}")
        return self.items(value, where, self.file, "a list of image paths")

    def named(self, value: object, where: str, keys: tuple[str, ...]) -> tuple[Location | Outfit | None, ...] | None:
        """Locations (keys with shots) or outfits; a name already used in the list is a Problem naming the
        repeated name (Req 4.13)."""
        if not isinstance(value, list):
            self.fail(where, f"must be a list of mappings with {_listing(keys)}, not {_describe(value)}")
            return None
        first: dict[str, int] = {}
        found: list[Location | Outfit | None] = []
        for number, item in enumerate(value, 1):
            at = f"{where}[{number}]"
            data = self.mapping(item, at, keys)
            if data is None:
                found.append(None)
                continue
            name = self.get(data, "name", at, self.required_text, required=True)
            description = self.get(data, "description", at, self.required_text, required=True)
            if name is not None:
                if name in first:
                    self.fail(_join(at, "name"), f"repeats the name {name!r} of {where}[{first[name]}]")
                else:
                    first[name] = number
            if "shots" in keys:
                shots = self.get(data, "shots", at, self.shots, default=())
                found.append(Location(name, description, shots))
            else:
                found.append(Outfit(name, description))
        return tuple(found)

    def shots(self, value: object, where: str) -> tuple[str, ...] | None:
        return self.items(value, where, self.path, "a list of image paths")

    def character_lora(self, value: object, where: str) -> LoraRef | None:
        data = self.mapping(value, where, LORA_FIELDS)
        if data is None:
            return None
        lora = self.get(data, "lora", where, self.lora_path, required=True)
        check = lambda strength, at: self.strength(strength, at, lora, character=True)  # noqa: E731
        return LoraRef(lora, self.get(data, "strength", where, check, default=DEFAULT_STRENGTH))

    def look(self, value: object, where: str) -> LoraRef | None:
        if isinstance(value, str):  # a plain string: strength 1.0
            return LoraRef(self.lora_path(value, where), DEFAULT_STRENGTH)
        if not isinstance(value, dict):
            self.fail(where, f"must be a LoRA path or a mapping of lora and strength, not {_describe(value)}")
            return None
        data = self.mapping(value, where, LORA_FIELDS)
        lora = self.get(data, "lora", where, self.lora_path, required=True)
        check = lambda strength, at: self.strength(strength, at, lora, character=False)  # noqa: E731
        return LoraRef(lora, self.get(data, "strength", where, check, default=DEFAULT_STRENGTH))

    def looks_list(self, value: object, where: str) -> tuple[LoraRef | None, ...] | None:
        if isinstance(value, list) and len(value) > MAX_LOOKS:
            self.fail(where, f"lists {len(value)} LoRAs; a look list holds at most {MAX_LOOKS}")
        return self.items(value, where, self.look, "a list of look LoRAs")

    # ---- the document ----

    def read(self, data: object) -> Persona | None:
        if not isinstance(data, dict):
            found = "the file is empty" if data is None else f"the file holds {_describe(data)}"
            self.fail(None, f"{found}; a Persona_File is a mapping of the fields of design §M1")
            return None
        doc = self.mapping(data, "", PERSONA_FIELDS)
        get = self.get
        version = get(doc, "version", "", self.version, default=FORMAT_VERSION)
        slug = get(doc, "slug", "", self.slug, required=True)
        name = get(doc, "name", "", self.required_text, required=True)
        age = get(doc, "age", "", self.age, required=True)
        niche = get(doc, "niche", "", self.free_text)
        language = get(doc, "language", "", self.line, default=DEFAULT_LANGUAGE)
        voice = get(doc, "voice", "", self.required_text, required=True)
        themes = get(doc, "themes", "", self.texts, default=())
        identity = get(doc, "identity", "", self.identity, required=True)
        trigger_word = get(doc, "trigger_word", "", self.trigger_word, required=True)
        if identity is not None and trigger_word is not None and identity in trigger_word:
            self.fail("identity", f"occurs inside the Trigger_Word {trigger_word!r}; the Identity_Paragraph must not")
        hero_image = get(doc, "hero_image", "", self.file, required=True)
        reference_pack = get(doc, "reference_pack", "", self.references, default=())

        grade = get(doc, "grade", "", self.mapping_of(GRADE_FIELDS))
        if "grade" not in doc:
            self.fail("grade.lut", "is missing")
        lut = get(grade, "lut", "grade", self.file, required=True)
        grain = get(grade, "grain", "grade", self.grain)

        sharp_decode = get(doc, "sharp_decode", "", self.boolean, default=False)
        locations = get(doc, "locations", "", lambda v, w: self.named(v, w, LOCATION_FIELDS), default=())
        wardrobe = get(doc, "wardrobe", "", lambda v, w: self.named(v, w, OUTFIT_FIELDS), default=())
        signature_items = get(doc, "signature_items", "", self.texts, default=())

        loras = get(doc, "character_loras", "", self.mapping_of(CHARACTER_LORA_FIELDS))
        character_loras = CharacterLoras(
            stills=get(loras, "stills", "character_loras", self.character_lora),
            video=get(loras, "video", "character_loras", self.character_lora),
        )

        looks_data = get(doc, "looks", "", self.mapping_of(LOOK_MEDIA))
        stacks: dict[str, LookStack] = {}
        for medium in LOOK_MEDIA:
            where = _join("looks", medium)
            medium_data = get(looks_data, medium, "looks", self.mapping_of(LOOK_PLATFORMS))
            platforms = {p: get(medium_data, p, where, self.looks_list, default=()) for p in LOOK_PLATFORMS}
            stacks[medium] = LookStack(**platforms)

        captions_data = get(doc, "captions", "", self.mapping_of(CAPTION_FIELDS))
        captions = Captions(
            rules=get(captions_data, "rules", "captions", self.free_text),
            disclosure=get(captions_data, "disclosure", "captions", self.disclosure),
            hashtag_limit=get(captions_data, "hashtag_limit", "captions", self.hashtag_limit,
                              default=DEFAULT_HASHTAG_LIMIT),
            flagged_terms=get(captions_data, "flagged_terms", "captions", self.texts, default=()),
        )  # fmt: skip

        ollama_data = get(doc, "ollama", "", self.mapping_of(OLLAMA_FIELDS))
        ollama = Ollama(
            instagram=get(ollama_data, "instagram", "ollama", self.line, default=DEFAULT_OLLAMA_INSTAGRAM),
            other=get(ollama_data, "other", "ollama", self.line, default=DEFAULT_OLLAMA_OTHER),
        )

        for where, found in check_tree(doc):  # every string and key at any depth (Req 4.4)
            self.fail(where, f"holds {found!r}, which the Safety_Guard rejects")

        if self.problems:
            return None
        return Persona(
            slug=slug, name=name, age=age, voice=voice, identity=identity, trigger_word=trigger_word,
            hero_image=hero_image, grade=Grade(lut, grain), version=version, niche=niche, language=language,
            themes=themes, reference_pack=reference_pack, sharp_decode=sharp_decode, locations=locations,
            wardrobe=wardrobe, signature_items=signature_items, character_loras=character_loras,
            looks=Looks(**stacks), captions=captions, ollama=ollama, source=self.source,
        )  # fmt: skip


def read_persona(path: str | os.PathLike[str], root: str | os.PathLike[str] | None, *,
                 check_files: bool = True) -> Persona:  # fmt: skip
    """The Persona_File at path, checked against every rule of the module docstring. Its file paths are
    resolved against root, the Influencer_Root, and must exist unless check_files is False (root may then
    be None). Raises one ValidationError holding every Problem found (Req 4.2)."""
    if check_files and root is None:
        raise ValueError("read_persona needs the Influencer_Root to check that the named files exist")
    shown = os.fspath(path)
    data = load_yaml(path)  # a syntax error: one Problem with its line
    reader = _Reader(shown, None if root is None else Path(root), check_files)
    persona = reader.read(data)
    if persona is None:
        raise ValidationError(reader.problems)
    return persona


# ---- the Printer ----


def _lora_data(ref: LoraRef) -> dict[str, object]:
    return {"lora": ref.lora, "strength": ref.strength}


def persona_data(persona: Persona) -> dict[str, object]:
    """The plain data print_persona prints: the §M1 fields in §M1 order, defaults written out, a field
    without a value (None) left out, look entries as {lora, strength} and empty shots left out."""
    data: dict[str, object] = {
        "version": persona.version,
        "slug": persona.slug,
        "name": persona.name,
        "age": persona.age,
    }
    if persona.niche is not None:
        data["niche"] = persona.niche
    data |= {
        "language": persona.language,
        "voice": persona.voice,
        "themes": list(persona.themes),
        "identity": persona.identity,
        "trigger_word": persona.trigger_word,
        "hero_image": persona.hero_image,
        "reference_pack": list(persona.reference_pack),
    }
    grade: dict[str, object] = {"lut": persona.grade.lut}
    if persona.grade.grain is not None:
        grade["grain"] = persona.grade.grain
    data["grade"] = grade
    data["sharp_decode"] = persona.sharp_decode
    locations = []
    for location in persona.locations:
        item: dict[str, object] = {"name": location.name, "description": location.description}
        if location.shots:
            item["shots"] = list(location.shots)
        locations.append(item)
    data["locations"] = locations
    data["wardrobe"] = [{"name": outfit.name, "description": outfit.description} for outfit in persona.wardrobe]
    data["signature_items"] = list(persona.signature_items)
    loras = {
        medium: _lora_data(ref)
        for medium, ref in (("stills", persona.character_loras.stills), ("video", persona.character_loras.video))
        if ref is not None
    }
    if loras:
        data["character_loras"] = loras
    data["looks"] = {
        medium: {platform: [_lora_data(ref) for ref in getattr(stack, platform)] for platform in LOOK_PLATFORMS}
        for medium, stack in (("stills", persona.looks.stills), ("video", persona.looks.video))
    }
    captions: dict[str, object] = {}
    if persona.captions.rules is not None:
        captions["rules"] = persona.captions.rules
    if persona.captions.disclosure is not None:
        captions["disclosure"] = persona.captions.disclosure
    captions["hashtag_limit"] = persona.captions.hashtag_limit
    captions["flagged_terms"] = list(persona.captions.flagged_terms)
    data["captions"] = captions
    data["ollama"] = {"instagram": persona.ollama.instagram, "other": persona.ollama.other}
    return data


def print_persona(persona: Persona) -> str:
    """persona as Persona_File YAML, through yamlio.dump_yaml: multi-line text in literal block style and
    YAML-ambiguous text quoted, so read_persona reads back an equal Persona (Req 4.11)."""
    return dump_yaml(persona_data(persona))


# ---- warnings ----


def persona_warnings(persona: Persona) -> list[Problem]:
    """What the Persona_Reader warns about without rejecting: a location count other than 3 or 4, stated
    (Req 4.9). Each warning names persona.source and the field."""
    count = len(persona.locations)
    low, high = RECOMMENDED_LOCATIONS
    if low <= count <= high:
        return []
    noun = "location" if count == 1 else "locations"
    return [Problem(persona.source, "locations", None, f"defines {count} {noun}; {low} or {high} are recommended")]
