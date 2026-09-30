"""The Instagram_Allowlist (design §A6; Req 21.3, 21.6, 21.7).

    build(models_text, persona) -> Allowlist            from models.list's text and the Persona
    read_allowlist(config_dir, persona) -> Allowlist    the same, reading <config_dir>/models.list
    Allowlist.inside(name) -> bool                      an exact, case-sensitive match
    persona_problems(persona, allowlist) -> [Problem]   the Persona_File check at stage start (Req 21.6)
    check_persona(persona, allowlist)                   the same, raised as one ValidationError
    post_problems(post_id, looks, allowlist, ...)       one Instagram Post's look list and reel source (Req 21.3)

The allowlist holds names read as paths relative to models/loras/ (Req 21.7):
- the path below models/loras/ of each file that an active models.list line tagged influencer or
  influencer-video installs there: a `lora` line without a subdir, or a line of any kind whose subdir is
  loras/ or below it (lines.install_path, with empty and `.` segments dropped as the file system drops
  them; a path with a `..` segment is not below models/loras/)
- each Character_LoRA path of the Persona that is relative, holds no `..` segment and is not under nsfw/.
  "Under nsfw/" compares the first segment in any letter case, because on a case-insensitive disk (the
  Mac's) NSFW/ is the same folder; a Character_LoRA there is refused rather than trusted.
inside(name) is an exact match, letter case included: with only looks/x listed, Looks/x, looks//x,
/looks/x, ./looks/x, looks/../looks/x and nsfw/looks/x are all outside.

When the plan, render, finish and reels stages start, they call check_persona before any request: one
Character_LoRA, or one entry of looks.stills.instagram or looks.video.instagram, outside the allowlist
rejects the Persona_File with a Problem naming the field and the LoRA (exit status 1). There is no edit
look stack, and the `other` stacks are never checked. Then each Instagram Post (any Post whose platform is
not `other`, a Post without a platform included) goes through post_problems: a look LoRA outside the
allowlist, at any strength, or a reel source Slide of a Post with platform `other` fails the Post. The
stage submits no Job for it, reports its Problems, goes on with the other Posts and exits 3 at the end.

Pure: build() reads nothing, read_allowlist() only models.list.
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Union

from influencer import lines
from influencer.errors import EnvironmentFailure, Problem, ValidationError
from influencer.persona import LoraRef, Persona

INSTAGRAM_IDS = ("influencer", "influencer-video")  # the ids whose LoRAs Instagram Posts may use
LORA_FOLDER = ("models", "loras")
NSFW_FOLDER = "nsfw"  # models/loras/nsfw/, where the influencer-nsfw add-on installs (Req 21.1)
OTHER_PLATFORM = "other"
CHARACTER_FIELDS = ("stills", "video")  # character_loras.<medium>
LOOK_MEDIA = ("stills", "video")  # looks.<medium>.instagram

LookEntry = Union[str, LoraRef]  # a look list entry: a LoRA name, or a LoRA and its strength


@dataclass(frozen=True)
class Allowlist:
    """The names Instagram Posts may use, as paths relative to models/loras/."""

    config: frozenset[str]  # installed by active models.list lines tagged influencer or influencer-video
    characters: frozenset[str]  # the Persona's Character_LoRAs that qualify

    @property
    def names(self) -> frozenset[str]:
        return self.config | self.characters

    def inside(self, name: str) -> bool:
        """Whether name is one of the allowlist's names exactly, letter case included."""
        return name in self.config or name in self.characters


def below_loras(path: str) -> str | None:
    """The part of an install path below models/loras/, or None when the file does not land there. Empty
    and `.` segments are dropped, as the file system drops them; a `..` segment makes the answer None."""
    segments = [segment for segment in path.split("/") if segment not in ("", ".")]
    if ".." in segments or len(segments) <= len(LORA_FOLDER) or tuple(segments[: len(LORA_FOLDER)]) != LORA_FOLDER:
        return None
    return "/".join(segments[len(LORA_FOLDER) :])


def config_names(models_text: str) -> frozenset[str]:
    """The names below models/loras/ that active models.list lines tagged influencer or influencer-video
    install."""
    names = set()
    for line in lines.parse_models(models_text):
        if any(ident in line.ids for ident in INSTAGRAM_IDS):
            name = below_loras(lines.install_path(line))
            if name is not None:
                names.add(name)
    return frozenset(names)


def path_fault(name: str) -> str | None:
    """Why name, read as a path below models/loras/, can never be a Character_LoRA of the allowlist: an
    absolute path, a `..` segment, or a place under nsfw/ (first segment in any letter case). None when
    it can be one."""
    if not name:
        return "is empty"
    if PurePosixPath(name).is_absolute():
        return "is an absolute path"
    segments = name.split("/")
    if ".." in segments:
        return "holds a '..' segment"
    real = [segment for segment in segments if segment not in ("", ".")]
    if len(real) > 1 and real[0].casefold() == NSFW_FOLDER:
        return "is under models/loras/nsfw/, where the NSFW add-on installs"
    return None


def character_names(persona: Persona) -> frozenset[str]:
    """The Persona's Character_LoRA paths that join the allowlist."""
    refs = (getattr(persona.character_loras, medium) for medium in CHARACTER_FIELDS)
    return frozenset(ref.lora for ref in refs if ref is not None and path_fault(ref.lora) is None)


def build(models_text: str, persona: Persona) -> Allowlist:
    """The allowlist from models.list's text and the Persona."""
    return Allowlist(config_names(models_text), character_names(persona))


def read_allowlist(config_dir: str | os.PathLike[str], persona: Persona) -> Allowlist:
    """build() with <config_dir>/models.list, read as provision.sh reads it (UTF-8, bytes kept). A file that
    cannot be read is an EnvironmentFailure naming it."""
    path = Path(config_dir) / lines.MODELS
    try:
        data = path.read_bytes()
    except OSError as error:
        detail = error.strerror or str(error)
        raise EnvironmentFailure(f"{path}: cannot be read ({detail}); the Instagram_Allowlist needs it") from None
    return build(data.decode("utf-8", "surrogateescape"), persona)


def _outside(name: str, allowlist: Allowlist, what: str) -> str:
    """The reason for a name outside the allowlist."""
    fault = path_fault(name)
    if fault is not None:
        why = f"it {fault}"
    else:
        why = (
            "no active models.list line tagged influencer or influencer-video installs it below models/loras/, "
            "and it is not a Character_LoRA of the Persona_File"
        )
    near = sorted(other for other in allowlist.names if other.casefold() == name.casefold())
    hint = f" (the allowlist has {near[0]!r}; names match exactly, letter case included)" if near else ""
    return f"{what} {name!r} is outside the Instagram_Allowlist: {why}{hint}"


def persona_problems(persona: Persona, allowlist: Allowlist) -> list[Problem]:
    """One Problem per Character_LoRA and per entry of an Instagram look stack (looks.stills.instagram,
    looks.video.instagram) outside the allowlist, naming persona.source, the field and the LoRA."""
    problems = []
    for medium in CHARACTER_FIELDS:
        ref = getattr(persona.character_loras, medium)
        if ref is not None and not allowlist.inside(ref.lora):
            reason = _outside(ref.lora, allowlist, "Character_LoRA")
            problems.append(Problem(persona.source, f"character_loras.{medium}", None, reason))
    for medium in LOOK_MEDIA:
        for number, ref in enumerate(getattr(persona.looks, medium).instagram, 1):
            if not allowlist.inside(ref.lora):
                reason = _outside(ref.lora, allowlist, "look LoRA")
                problems.append(Problem(persona.source, f"looks.{medium}.instagram[{number}]", None, reason))
    return problems


def check_persona(persona: Persona, allowlist: Allowlist) -> None:
    """persona_problems() raised as one ValidationError: the stage stops before any request (Req 21.6)."""
    problems = persona_problems(persona, allowlist)
    if problems:
        raise ValidationError(problems)


@dataclass(frozen=True)
class ReelSource:
    """The Slide a Reel starts from."""

    post: str  # the source Post's id
    slide: int  # its Slide number, from 1
    platform: str | None  # the source Post's platform; None: none given, which is an Instagram Post


def post_problems(post_id: str, looks: Iterable[LookEntry], allowlist: Allowlist, *, platform: str | None = None,
                  reel_source: ReelSource | None = None, path: str = "the Content_Calendar",
                  looks_field: str = "looks", source_field: str = "source") -> list[Problem]:  # fmt: skip
    """The Problems that fail one Post (Req 21.3): for an Instagram Post (platform not `other`, None
    included), one per look list entry outside the allowlist, whatever its strength, and one for a reel
    source Slide of a Post with platform `other`. Each names the Post id and the LoRA or the Slide. A Post
    with platform `other` has none. path is the Content_Calendar's file, looks_field and source_field the
    fields of this Post's look list and reel source there (entries are numbered from 1)."""
    if platform == OTHER_PLATFORM:
        return []
    problems = []
    for number, entry in enumerate(looks, 1):
        name = entry.lora if isinstance(entry, LoraRef) else entry
        if not allowlist.inside(name):
            reason = f"Post {post_id!r}: {_outside(name, allowlist, 'look LoRA')}"
            problems.append(Problem(path, f"{looks_field}[{number}]", None, reason))
    if reel_source is not None and reel_source.platform == OTHER_PLATFORM:
        reason = (
            f"Post {post_id!r}: its reel source is Slide {reel_source.slide} of Post {reel_source.post!r}, whose "
            "platform is other; an Instagram Post never uses a Slide made for other platforms"
        )
        problems.append(Problem(path, source_field, None, reason))
    return problems
