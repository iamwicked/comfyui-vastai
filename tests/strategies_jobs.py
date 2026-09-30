"""Hypothesis strategies and the in-memory file access for the Job builders (task 20; design §A13,
Property 28).

- job_persona(): a valid Persona with locations, outfits and a stills Character_LoRA, so calendars(persona)
  draws still Posts and the render and finish builders can run.
- approval_sheets(calendar): (sheet, states) where each Post's entry is drawn independently as missing,
  unapproved (approved: false) or approved (approved: true); sheet is sometimes None, no Prompt_Sheet at
  all, and then every state is "missing".
- MemoryFiles: the Files a builder takes, over a dict of paths to bytes and image sizes, so the builders
  run without touching the disk.

A module of its own so tests/strategies.py stays untouched.
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

from hypothesis import strategies as st

from helpers.harness import REPO

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import persona as P  # noqa: E402
from influencer.calendar_io import Calendar  # noqa: E402
from influencer.jobs import Files  # noqa: E402
from influencer.prompt_sheet import Entry, PromptSheet, Slide  # noqa: E402

MISSING, UNAPPROVED, APPROVED = STATES = ("missing", "unapproved", "approved")
OUTPUT_DIR = Path("/comfy/output")

BASE_PERSONA = P.Persona(
    slug="mara-lind",
    name="Mara Lind",
    age=29,
    voice="warm, dry humour",
    identity="Mara is a 29-year-old woman with shoulder-length auburn hair and green eyes.",
    trigger_word="m4r4_lind",
    hero_image="refs/hero.png",
    grade=P.Grade(lut="grade/film.cube"),
    locations=(P.Location("cafe", "a small corner cafe with tall windows"),
               P.Location("street", "a cobbled street in the old town")),  # fmt: skip
    wardrobe=(P.Outfit("cream", "a cream knit sweater and dark jeans"), P.Outfit("coat", "a long camel coat")),
    character_loras=P.CharacterLoras(stills=P.LoraRef("characters/mara-lind/mara-v1.safetensors", 0.9)),
    looks=P.Looks(stills=P.LookStack(instagram=(P.LoraRef("looks/film-grain.safetensors", 0.5),))),
)

_SCENES = st.sampled_from(("she stops at the bakery window", "", "a slow walk home", "coffee, then rain"))


@st.composite
def job_persona(draw: st.DrawFn) -> P.Persona:
    """BASE_PERSONA with sharp decoding on or off and the Instagram and other stills looks drawn."""
    look = st.builds(P.LoraRef, lora=st.sampled_from(("looks/a.safetensors", "looks/b c.safetensors")),
                     strength=st.floats(-2.0, 2.0))  # fmt: skip
    looks = st.lists(look, max_size=3).map(tuple)
    stack = P.LookStack(instagram=draw(looks), other=draw(looks))
    return dataclasses.replace(BASE_PERSONA, sharp_decode=draw(st.booleans()), looks=P.Looks(stills=stack))


@st.composite
def approval_sheets(draw: st.DrawFn, calendar: Calendar) -> tuple[PromptSheet | None, dict[str, str]]:
    """A Prompt_Sheet for calendar with each Post's entry missing, unapproved or approved, drawn
    independently, and the state of every Post; None (no sheet) with every Post missing."""
    if draw(st.integers(0, 9)) == 0:
        return None, {post.id: MISSING for post in calendar.posts}
    states = {post.id: draw(st.sampled_from(STATES)) for post in calendar.posts}
    entries = []
    for post in calendar.posts:
        if states[post.id] == MISSING:
            continue
        slides = tuple(Slide(draw(_SCENES), "alt text", None) for _ in range(post.slides))
        entries.append(Entry(post.id, approved=states[post.id] == APPROVED, slides=slides))
    order = draw(st.permutations(entries)) if entries else []
    return PromptSheet(calendar.week, tuple(order)), states


class MemoryFiles:
    """Files over memory: add(path, data, size) puts a file (an image of size (w, h)) in place."""

    def __init__(self, output_dir: Path = OUTPUT_DIR) -> None:
        self.output_dir = Path(output_dir)
        self.data: dict[Path, bytes] = {}
        self.sizes: dict[Path, tuple[int, int]] = {}
        self.reads: list[Path] = []

    def add(self, path: Path | str, data: bytes, size: tuple[int, int] | None = None) -> Path:
        path = Path(path)
        if not path.is_absolute():
            path = self.output_dir / path
        self.data[path] = data
        if size is not None:
            self.sizes[path] = size
        return path

    def _read(self, path: Path) -> bytes:
        self.reads.append(path)
        return self.data[path]

    @property
    def files(self) -> Files:
        return Files(
            output_dir=self.output_dir,
            size=lambda path: len(self.data[path]) if path in self.data else None,
            read=self._read,
            image_size=lambda path: self.sizes[path],
        )
