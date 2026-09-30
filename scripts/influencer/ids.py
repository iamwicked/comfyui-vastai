"""Job identity: seeds, Job_Keys, upload names, Job ids and every output path (design §A1, §A2, §M5;
Req 5.10, 10.1, 10.2, 10.11, 11.5, 21.8).

    seed_for(W, P, s, c) -> int           the seed of Candidate c of Slide s of Post P in week W (§A1)
    reel_seed(W, P) -> int                a Reel's seed, seed_for(W, P, 1, 1)
    refine_seed(version, base) -> int     a refine Job's seed, hashed the same way as seed_for
    canonical(prompt) -> bytes            the prompt's canonical JSON, UTF-8 (§A2)
    job_key(prompt, inputs) -> str        the Job_Key of a filled prompt and the files it loads (§A2)
    upload_name(data, ext) -> str         influencer/<sha256 of data><ext>
    safe_name(name) -> str                a user file name as one path component
    render_id / finish_id / reel_id / refine_id / grid_id        the §M5 Job ids
    render_prefix / finish_prefix / reel_prefix / refine_prefix / grid_prefix
                                          the §M5 output_prefix values, relative to ComfyUI's output folder
    post_folder, export_path, qc_sheet_path, publish_sheet_path  paths relative to the Output_Root

Pure: nothing here reads a clock, the process or the environment, or touches a file, so the same values
give the same result in every process and on every machine.

Every path component is a validated id (Week_Id, Post id, persona slug, Dataset version), a number of at
least two digits (s01, p07, s080), a fixed word, or a safe name, so no component is empty, `.`, `..` or
holds a `/`, and every path stays inside the Output_Root. The Output_Root is `<ComfyUI output>/influencer/
<slug>/`, so an output_prefix is `influencer/<slug>/` followed by the Output_Root-relative path. Posts with
platform `other` get the `other/` segment (`<W>/other/<post>/`); no Post has the id `other`, so an
Instagram Post's folder `<W>/<post>/` is never inside `<W>/other/`. Invalid input raises ValueError.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from fractions import Fraction
from typing import Any

from influencer.calendar_io import (
    CANDIDATES_MAX,
    CANDIDATES_MIN,
    PLATFORMS,
    POST_ID_RE,
    RESERVED_ID,
    week_problem,
)
from influencer.env import SLUG_RE

# ---- constants ----

SEED_PREFIX = "influencer-seed/1"
SEED_STEP = 0x9E3779B1  # odd, so invertible modulo 2**32: Candidates of one Slide never share a seed
SEED_MODULUS = 2**32
JOB_KEY_PREFIX = b"influencer-job/1\n"
UPLOAD_FOLDER = "influencer"  # the ComfyUI input subfolder of uploads
OUTPUT_FOLDER = "influencer"  # the Output_Root is <ComfyUI output>/influencer/<slug>/
OTHER_FOLDER = RESERVED_ID  # "other": the folder of platform `other` Posts
EXT_RE = re.compile(r"\.[a-z0-9]{1,10}")
EXPORT_EXTS = (".jpg", ".mp4")
VERSION_RE = re.compile(r"[a-z0-9-]{1,48}")  # a Dataset version as a path component
NUMBER_MAX = 999  # s<NN>, p<NN> and export numbers: two digits, or three from 100
SAFE_KEEP = 64  # characters kept of the lowered, mapped name before its hash suffix
SAFE_HASH = 6  # hex digits of the original name's SHA-256
_UNSAFE = re.compile(r"[^a-z0-9-]")

# ---- validation ----


def _week(week: object) -> str:
    problem = week_problem(week)
    if problem is not None:
        raise ValueError(f"week: {problem}")
    return week  # type: ignore[return-value]


def _post(post: object) -> str:
    if not isinstance(post, str) or not POST_ID_RE.fullmatch(post) or post == RESERVED_ID:
        raise ValueError(f"post: {post!r} is not a Post id (1 to 48 of a-z, 0-9 and -, not 'other')")
    return post


def _slug(slug: object) -> str:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ValueError(f"slug: {slug!r} is not a persona slug (1 to 32 of a-z, 0-9 and -)")
    return slug


def _platform(platform: object) -> str:
    if platform not in PLATFORMS:
        raise ValueError(f"platform: {platform!r} is not a platform: use instagram or other")
    return platform  # type: ignore[return-value]


def _version(version: object) -> str:
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError(f"version: {version!r} is not a Dataset version (1 to 48 of a-z, 0-9 and -)")
    return version


def _whole(field: str, value: object, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{field}: {value!r} is not a whole number from {low} to {high}")
    return value


def _number(field: str, value: object) -> str:
    """value, 1 to 999, as two digits or three (s01, s100)."""
    return f"{_whole(field, value, 1, NUMBER_MAX):02d}"


def _candidate(value: object) -> int:
    return _whole("candidate", value, CANDIDATES_MIN, CANDIDATES_MAX)


def _encode(text: str) -> bytes:
    return text.encode("utf-8", "surrogatepass")  # every str, lone surrogates included, one to one


def strength_hundredths(strength: object) -> int:
    """strength × 100 as a whole number 0-999, for s<SSS>: 0.8 gives 80. A strength that is not a
    multiple of 0.01, is negative or reaches 10 raises ValueError."""
    if isinstance(strength, bool) or not isinstance(strength, (int, float, Fraction)):
        raise ValueError(f"strength: {strength!r} is not a number")
    try:
        exact = Fraction(str(strength)) if isinstance(strength, float) else Fraction(strength)
    except (ValueError, OverflowError):
        raise ValueError(f"strength: {strength!r} is not a finite number") from None
    hundredths = exact * 100
    if hundredths.denominator != 1 or not 0 <= hundredths <= NUMBER_MAX:
        raise ValueError(f"strength: {strength!r} is not a multiple of 0.01 from 0 to 9.99")
    return int(hundredths)


# ---- seeds (§A1) ----


def _base(*fields: object) -> int:
    text = "|".join([SEED_PREFIX, *(str(field) for field in fields)])
    return int.from_bytes(hashlib.sha256(_encode(text)).digest()[:4], "big")


def seed_for(week: str, post: str, slide: int, candidate: int) -> int:
    """The seed of Candidate candidate of Slide slide of Post post in week, 0 to 2**32 - 1 (Req 10.1).
    Candidates 1-4 of one Slide have pairwise different seeds (Req 11.5)."""
    base = _base(_week(week), _post(post), _whole("slide", slide, 1, NUMBER_MAX))
    return (base + (_candidate(candidate) - 1) * SEED_STEP) % SEED_MODULUS


def reel_seed(week: str, post: str) -> int:
    """A Reel's seed: seed_for(week, post, 1, 1)."""
    return seed_for(week, post, 1, 1)


def refine_seed(version: str, base: str) -> int:
    """A refine Job's seed: (Dataset version, image base name) hashed the same way as seed_for."""
    if not isinstance(base, str) or base == "":
        raise ValueError(f"base: {base!r} is not a file name")
    return _base(_version(version), base)


# ---- Job_Keys and uploads (§A2) ----


def canonical(prompt: Any) -> bytes:
    """The canonical JSON of prompt, UTF-8: sorted keys, no spaces, non-ASCII kept."""
    return json.dumps(prompt, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def job_key(prompt: Any, inputs: Mapping[str, bytes] = {}) -> str:  # noqa: B006 - never mutated
    """The Job_Key of a filled prompt and the input files it loads, given as {upload name: bytes}: the
    SHA-256 of a version line, the canonical prompt and the sorted name=digest lines (Req 10.2)."""
    digests = {}
    for name, data in inputs.items():
        if not isinstance(name, str) or any(c in name for c in "=\n"):
            raise ValueError(f"inputs: {name!r} is not an upload name")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise ValueError(f"inputs: the content of {name!r} is not bytes")
        digests[name] = hashlib.sha256(data).hexdigest()
    lines = "\n".join(f"{name}={digest}" for name, digest in sorted(digests.items()))
    return hashlib.sha256(JOB_KEY_PREFIX + canonical(prompt) + b"\n" + _encode(lines)).hexdigest()


def upload_name(data: bytes, ext: str = "") -> str:
    """influencer/<sha256 of data><ext>: the same bytes always upload to the same name. ext is "" or a dot
    and 1-10 of a-z and 0-9, taken in lower case (".PNG" gives ".png")."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ValueError(f"data: {type(data).__name__} is not bytes")
    if not isinstance(ext, str) or (ext != "" and not EXT_RE.fullmatch(ext.lower())):
        raise ValueError(f"ext: {ext!r} is not a file extension such as .png")
    return f"{UPLOAD_FOLDER}/{hashlib.sha256(data).hexdigest()}{ext.lower()}"


# ---- safe names ----


def safe_name(name: str) -> str:
    """A user file name (Dataset image base, checkpoint stem) as one path component: lower-cased, every
    character outside [a-z0-9-] made `-`, cut to 64 characters, then `-` and the first 6 hex digits of the
    original name's SHA-256, so two different names give different safe names."""
    if not isinstance(name, str) or name == "":
        raise ValueError(f"name: {name!r} is not a file name")
    mapped = _UNSAFE.sub("-", name.lower())[:SAFE_KEEP]
    return f"{mapped}-{hashlib.sha256(_encode(name)).hexdigest()[:SAFE_HASH]}"


# ---- Job ids (§M5) ----


def render_id(post: str, slide: int, candidate: int) -> str:
    return f"render/{_post(post)}/s{_number('slide', slide)}/c{_candidate(candidate)}"


def finish_id(post: str, slide: int) -> str:
    return f"finish/{_post(post)}/s{_number('slide', slide)}"


def reel_id(post: str) -> str:
    return f"reel/{_post(post)}"


def refine_id(base: str) -> str:
    """refine/<safe base>. Like every id component, the image base name is a safe name, so an id is a safe
    path too; the Manifest record keeps the original base name, which the copy into the refined Dataset
    uses (Req 18.7)."""
    return f"refine/{safe_name(base)}"


def grid_id(checkpoint_stem: str, strength: object, prompt: int) -> str:
    """grid/<safe stem>/s<SSS>/p<NN>: the checkpoint stem as a safe name and the strength in its s<SSS>
    form, as in the output prefix; the Manifest record keeps the checkpoint and strength themselves."""
    return f"grid/{safe_name(checkpoint_stem)}/s{strength_hundredths(strength):03d}/p{_number('prompt', prompt)}"


# ---- paths relative to the Output_Root ----


def post_folder(week: str, post: str, platform: str) -> str:
    """<W>/<post> for an Instagram Post, <W>/other/<post> for platform other (Req 10.11, 21.8)."""
    middle = f"{OTHER_FOLDER}/" if _platform(platform) == "other" else ""
    return f"{_week(week)}/{middle}{_post(post)}"


def export_path(week: str, post: str, platform: str, number: int, ext: str) -> str:
    """<post folder>/<post>_NN.jpg or .mp4, NN the upload position from 01."""
    if ext not in EXPORT_EXTS:
        raise ValueError(f"ext: {ext!r} is not an export extension: use .jpg or .mp4")
    return f"{post_folder(week, post, platform)}/{post}_{_number('number', number)}{ext}"


def qc_sheet_path(week: str, post: str, platform: str) -> str:
    return f"{post_folder(week, post, platform)}/{post}_qc.jpg"


def publish_sheet_path(week: str, post: str, platform: str) -> str:
    return f"{post_folder(week, post, platform)}/{post}_publish.md"


# ---- output prefixes, relative to ComfyUI's output folder ----


def _under_root(slug: str, relative: str) -> str:
    return f"{OUTPUT_FOLDER}/{_slug(slug)}/{relative}"


def render_prefix(slug: str, week: str, post: str, platform: str, slide: int, candidate: int) -> str:
    folder = post_folder(week, post, platform)
    name = f"{post}_s{_number('slide', slide)}_c{_candidate(candidate)}"
    return _under_root(slug, f"{folder}/render/{name}")


def finish_prefix(slug: str, week: str, post: str, platform: str, slide: int) -> str:
    folder = post_folder(week, post, platform)
    return _under_root(slug, f"{folder}/finish/{post}_s{_number('slide', slide)}")


def reel_prefix(slug: str, week: str, post: str, platform: str) -> str:
    return _under_root(slug, f"{post_folder(week, post, platform)}/reel/{post}")


def refine_prefix(slug: str, version: str, base: str) -> str:
    return _under_root(slug, f"refine/{_version(version)}/{safe_name(base)}")


def grid_prefix(slug: str, checkpoint_stem: str, strength: object, prompt: int) -> str:
    stem = safe_name(checkpoint_stem)
    return _under_root(slug, f"grid/{stem}/s{strength_hundredths(strength):03d}/p{_number('prompt', prompt)}")
