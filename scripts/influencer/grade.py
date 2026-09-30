"""The grade's film grain and its validation gate (design §A9, §C8; Req 14.2–14.4, 14.6–14.8).

    grain_seed(W, P, index) -> int          the grain stream of Slide index (or Reel frame index) of Post P
    grain_field(W, P, index, h, w)          the h×w standard normal field z of that stream
    grain(pixels, strength, W, P, index)    graded pixels plus σ·z on R, G and B, σ = 16·strength
    load_grade(persona, root) -> Grading    the Persona_File's LUT and grain strength, or a ValidationError

The seed is SHA-256 of "influencer-grain/1|W|P|index", the fields joined the way ids.seed_for joins its own,
and the whole digest seeds numpy.random.Generator(PCG64(seed)). No process, clock or environment state
enters it, so a re-export draws the same field (Req 14.4), while two Slides of a Post, or two frames of a
Reel, draw different ones (Req 14.3).

NumPy and the standard library; load_grade reads the LUT file and nothing here writes one.
"""
from __future__ import annotations

import hashlib
import math
import os
import sys
from dataclasses import dataclass

import numpy as np

from influencer import lut as lut_module
from influencer.errors import Problem, ValidationError
from influencer.ids import _post, _week, _whole
from influencer.persona import Persona, resolve_path

GRAIN_PREFIX = "influencer-grain/1"
GRAIN_SIGMA = 16.0  # σ at strength 1.0, in 8-bit units
INDEX_MAX = sys.maxsize  # a Slide number (1-20) or a Reel frame number (1 to F)
GRAIN_FIELD = "grade.grain"
LUT_FIELD = "grade.lut"


def grain_seed(week: str, post: str, index: int) -> int:
    """The grain seed of Slide index of Post post in week, or of frame index of a Reel (§A9). Invalid ids
    raise ValueError."""
    text = "|".join([GRAIN_PREFIX, _week(week), _post(post), str(_whole("index", index, 1, INDEX_MAX))])
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest(), "big")


def grain_field(week: str, post: str, index: int, height: int, width: int) -> np.ndarray:
    """z: the height×width float64 standard normal field of grain_seed(week, post, index)."""
    generator = np.random.Generator(np.random.PCG64(grain_seed(week, post, index)))
    return generator.standard_normal((height, width))


def _strength(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
        raise ValueError(f"the grain strength must be a number from 0.0 to 1.0, not {value!r}")
    return float(value)


def grain(pixels: np.ndarray, strength: float, week: str, post: str, index: int) -> np.ndarray:
    """graded, an H×W×3 uint8 array from lut.apply, with luma grain (§A9, Req 14.2): one field z of
    grain_field(week, post, index, H, W) added equally to R, G and B with σ = 16·strength, as
    clip(round(graded + σ·z), 0, 255) in a new uint8 array. At strength 0.0 no field is drawn and a copy of
    graded is returned as it is. graded is never written to."""
    strength = _strength(strength)
    graded = np.asarray(pixels)
    if graded.dtype != np.uint8 or graded.ndim != 3 or graded.shape[2] != 3:
        raise ValueError(f"grain needs an H×W×3 uint8 RGB array, not {graded.dtype} of shape {graded.shape}")
    if strength == 0.0:
        return graded.copy()
    height, width = graded.shape[:2]
    noise = (GRAIN_SIGMA * strength) * grain_field(week, post, index, height, width)
    return np.clip(np.rint(graded + noise[..., None]), 0, 255).astype(np.uint8)


# ---- the validation gate ----


@dataclass(frozen=True)
class Grading:
    """What load_grade accepted: the LUT, the file it was read from, and the grain strength 0.0 to 1.0."""

    lut: lut_module.Lut
    lut_path: str
    strength: float


def _grain_problem(persona: Persona) -> Problem | None:
    value = persona.grade.grain
    where = persona.source
    if value is None:
        return Problem(where, GRAIN_FIELD, None, "is missing: the Export_Tool needs a grain strength from 0.0 to 1.0")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return Problem(where, GRAIN_FIELD, None, f"{value!r} is not a number: use 0.0 to 1.0")
    if not (math.isfinite(value) and 0.0 <= value <= 1.0):
        return Problem(where, GRAIN_FIELD, None, f"{value!r} is out of range: use 0.0 to 1.0")
    return None


def load_grade(persona: Persona, root: str | os.PathLike[str]) -> Grading:
    """The Persona_File's grade (Req 14.6): its LUT read by the LUT_Reader from resolve_path(root,
    grade.lut), and grade.grain checked to be a number from 0.0 to 1.0.

    Raises one ValidationError holding every fault: the grain strength's, then each Problem the LUT_Reader
    names, unchanged, so the file path, the 1-based line and the expected and actual entry counts reach the
    user (Req 14.8). The caller runs this before it creates any folder or opens any output, so a rejected
    grade writes nothing to the Output_Root; at the command line the ValidationError is exit status 1."""
    problems: list[Problem] = []
    grain_problem = _grain_problem(persona)
    if grain_problem is not None:
        problems.append(grain_problem)
    loaded: lut_module.Lut | None = None
    shown = persona.grade.lut
    try:
        path = resolve_path(root, persona.grade.lut)
    except ValueError as error:
        problems.append(Problem(persona.source, LUT_FIELD, None, str(error)))
    else:
        shown = os.fspath(path)
        try:
            loaded = lut_module.load_lut(path)
        except ValidationError as error:
            problems.extend(error.problems)
    if problems or loaded is None:
        raise ValidationError(problems)
    return Grading(loaded, shown, float(persona.grade.grain))  # type: ignore[arg-type]
