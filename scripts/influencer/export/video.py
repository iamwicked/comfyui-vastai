"""The Reel side of the Export_Tool: ffprobe and ffmpeg (design §C8 Reels; Req 13.10, 13.11, 13.13, 14.1-14.3).

    discover() -> Tools | Unavailable            find ffmpeg and ffprobe once, and pick the passthrough flag
    ffmpeg_available() -> bool                   both tools found
    probe(src) -> Probe | Unavailable            frame rate, frame count F, size and whether there is audio
    grab_frame(src, number) -> bytes | Unavailable   frame `number` (1 to F) as PNG bytes
    export_reel(src, dest, grading, week, post_id) -> Probe | Unavailable   decode, grade, encode (Phase 3)

The command builders (probe_command, frame_command, decode_command, encode_command) write the §C8 calls
down in one place:
- ffprobe -v error -count_frames -show_entries stream=codec_type,width,height,r_frame_rate,nb_read_frames
  -of json SRC
- the rawvideo decode, rgb24, with `-fps_mode passthrough`, or `-vsync passthrough` on ffmpeg older than
  5.1 (detected once, from `ffmpeg -version`)
- the libx264 encode: -preset slow -crf 16 -profile:v high -pix_fmt yuv420p -frames:v F, `-c:a aac -b:a
  192k -t F/R` when the source has audio, -map_metadata -1, -map_chapters -1, the bitexact flags and
  +faststart
- the single-frame grab, `-vf select=eq(n\\,k)`, which the QC_Sheet of task 24.1 uses

Phase 1 boundary: Phase 1 calls only ffmpeg_available() and grab_frame(). probe's decode and encode
companions, export_reel, are first reached by the reels export of Phase 3, and no Phase 1 test encodes a
Reel. Every entry point returns an Unavailable ("ffmpeg not available: ...") instead of raising when
ffmpeg or ffprobe is missing, so a Mac run without ffmpeg reports reel Posts as waiting for ffmpeg. A tool
that is present but fails raises VideoError with its stderr.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

FFMPEG, FFPROBE = "ffmpeg", "ffprobe"
NOT_AVAILABLE = "ffmpeg not available"
FPS_MODE_SINCE = (5, 1)  # ffmpeg 5.1 has -fps_mode; older builds take -vsync
PASSTHROUGH = ("-fps_mode", "passthrough")
PASSTHROUGH_OLD = ("-vsync", "passthrough")
VERTICAL_SIZE = (1080, 1920)  # a Reel's export size (Req 13.2)
VERSION_TIMEOUT_S = 30
PROBE_SHOW = "stream=codec_type,width,height,r_frame_rate,nb_read_frames"
_VERSION_RE = re.compile(r"ffmpeg version n?(\d+)\.(\d+)")


class VideoError(RuntimeError):
    """ffmpeg or ffprobe ran and failed, or printed something this module cannot read."""


@dataclass(frozen=True)
class Unavailable:
    """ffmpeg or ffprobe is missing: falsy, and its text is the report."""

    reason: str

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True)
class Tools:
    """The two tools, the ffmpeg version (None when `ffmpeg -version` names none, as git builds do) and
    the passthrough flag pair that version takes."""

    ffmpeg: str
    ffprobe: str
    version: tuple[int, int] | None
    passthrough: tuple[str, str]


@dataclass(frozen=True)
class Probe:
    """What ffprobe reports of a source: the video's frame rate (a rational), frame count F, width and
    height, and whether an audio stream exists."""

    rate: Fraction
    frames: int
    width: int
    height: int
    audio: bool

    @property
    def duration_s(self) -> Fraction:
        return Fraction(self.frames) / self.rate


# ---- discovery, once per process ----

_found: Tools | Unavailable | None = None


def parse_version(text: str) -> tuple[int, int] | None:
    """(major, minor) from the first line of `ffmpeg -version`; None for a build without one."""
    match = _VERSION_RE.search(text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def passthrough_flags(version: tuple[int, int] | None) -> tuple[str, str]:
    """-fps_mode passthrough, or -vsync passthrough on ffmpeg older than 5.1. A version-less build is a
    recent one."""
    return PASSTHROUGH_OLD if version is not None and version < FPS_MODE_SINCE else PASSTHROUGH


def discover(*, which: Callable[[str], str | None] = shutil.which, run: Callable[..., Any] = subprocess.run,
             refresh: bool = False) -> Tools | Unavailable:  # fmt: skip
    """The tools on PATH, found once (refresh=True looks again); an Unavailable naming each missing one."""
    global _found
    if _found is not None and not refresh:
        return _found
    paths = {name: which(name) for name in (FFMPEG, FFPROBE)}
    missing = [name for name, path in paths.items() if path is None]
    if missing:
        _found = Unavailable(f"{NOT_AVAILABLE}: {' and '.join(missing)} not found on PATH")
        return _found
    try:
        shown = run([paths[FFMPEG], "-version"], capture_output=True, text=True, timeout=VERSION_TIMEOUT_S)
        version = parse_version(shown.stdout or "")
    except (OSError, subprocess.SubprocessError) as error:
        _found = Unavailable(f"{NOT_AVAILABLE}: {paths[FFMPEG]} -version failed ({error})")
        return _found
    _found = Tools(str(paths[FFMPEG]), str(paths[FFPROBE]), version, passthrough_flags(version))
    return _found


def reset() -> None:
    """Forgets the discovery, so the next call looks at PATH again (tests change PATH)."""
    global _found
    _found = None


def ffmpeg_available() -> bool:
    return isinstance(discover(), Tools)


# ---- the commands (§C8) ----


def probe_command(tools: Tools, src: str | os.PathLike[str]) -> list[str]:
    return [tools.ffprobe, "-v", "error", "-count_frames", "-show_entries", PROBE_SHOW, "-of", "json", os.fspath(src)]


def frame_command(tools: Tools, src: str | os.PathLike[str], number: int) -> list[str]:
    """Frame `number` (1-based) as one PNG on stdout; select counts n from 0."""
    return [tools.ffmpeg, "-v", "error", "-i", os.fspath(src), "-vf", f"select=eq(n\\,{number - 1})",
            *tools.passthrough, "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"]  # fmt: skip


def decode_command(tools: Tools, src: str | os.PathLike[str]) -> list[str]:
    return [tools.ffmpeg, "-v", "error", "-i", os.fspath(src), "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24",
            *tools.passthrough, "-"]  # fmt: skip


def encode_command(tools: Tools, probe: Probe, src: str | os.PathLike[str], dest: str | os.PathLike[str]) -> list[str]:
    width, height = VERTICAL_SIZE
    command = [tools.ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
               "-framerate", str(probe.rate), "-i", "-"]  # fmt: skip
    if probe.audio:
        command += ["-i", os.fspath(src), "-map", "1:a:0"]
    command += ["-map", "0:v:0", "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-profile:v", "high",
                "-pix_fmt", "yuv420p", "-frames:v", str(probe.frames)]  # fmt: skip
    if probe.audio:
        command += ["-c:a", "aac", "-b:a", "192k", "-t", f"{float(probe.duration_s):.6f}"]
    command += ["-map_metadata", "-1", "-map_chapters", "-1", "-fflags", "+bitexact", "-flags:v", "+bitexact",
                "-movflags", "+faststart", "-f", "mp4", os.fspath(dest)]  # fmt: skip
    return command


# ---- running them ----


def _run(command: Sequence[str]) -> bytes | Unavailable:
    try:
        done = subprocess.run(list(command), capture_output=True, check=False)
    except FileNotFoundError as error:  # the tool vanished after discovery
        return Unavailable(f"{NOT_AVAILABLE}: {error}")
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip() or f"exit status {done.returncode}"
        raise VideoError(f"{command[0]} failed: {detail}")
    return done.stdout


def parse_probe(text: str) -> Probe:
    """The Probe of ffprobe's JSON; VideoError when it has no video stream or no readable rate or count."""
    try:
        streams = json.loads(text).get("streams", [])
        video = next(stream for stream in streams if stream.get("codec_type") == "video")
        rate = Fraction(str(video["r_frame_rate"]))
        frames, width, height = int(video["nb_read_frames"]), int(video["width"]), int(video["height"])
    except (ValueError, KeyError, TypeError, StopIteration, ZeroDivisionError, AttributeError) as error:
        raise VideoError(f"ffprobe gave no readable video stream: {error!r}") from None
    if rate <= 0 or frames <= 0 or width <= 0 or height <= 0:
        raise VideoError(f"ffprobe reports rate {rate}, {frames} frames at {width}x{height}")
    audio = any(stream.get("codec_type") == "audio" for stream in streams)
    return Probe(rate, frames, width, height, audio)


def probe(src: str | os.PathLike[str]) -> Probe | Unavailable:
    tools = discover()
    if not isinstance(tools, Tools):
        return tools
    shown = _run(probe_command(tools, src))
    if isinstance(shown, Unavailable):
        return shown
    return parse_probe(shown.decode("utf-8", "replace"))


def grab_frame(src: str | os.PathLike[str], number: int) -> bytes | Unavailable:
    """Frame `number` (1 to F) of src as PNG bytes (the QC_Sheet's Reel row, §C8)."""
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError(f"a frame number counts from 1, not {number!r}")
    tools = discover()
    if not isinstance(tools, Tools):
        return tools
    data = _run(frame_command(tools, src, number))
    if isinstance(data, bytes) and not data:
        raise VideoError(f"{src} has no frame {number}")
    return data


def grade_frame(frame: Any, grading: Any, week: str, post_id: str, index: int) -> Any:
    """One decoded H×W×3 frame through the image path: the crop box, Lanczos to the Vertical_Size, the LUT
    and grain seeded by the frame index (1-based)."""
    import numpy as np
    from PIL import Image

    from influencer import grade, lut
    from influencer.geometry import crop_box

    height, width = frame.shape[:2]
    x, y, cw, ch = crop_box(width, height, *VERTICAL_SIZE)
    resized = Image.fromarray(frame).crop((x, y, x + cw, y + ch)).resize(VERTICAL_SIZE, Image.LANCZOS)
    graded = lut.apply(grading.lut, np.array(resized, dtype=np.uint8))
    return grade.grain(graded, grading.strength, week, post_id, index)


def export_reel(src: str | os.PathLike[str], dest: str | os.PathLike[str], grading: Any, week: str,
                post_id: str) -> Probe | Unavailable:  # fmt: skip
    """The Phase 3 Reel export: frames stream from the decoder through grade_frame into the encoder, a few
    frames in memory at a time; dest is written by ffmpeg. Not reached in Phase 1."""
    import numpy as np

    tools = discover()
    if not isinstance(tools, Tools):
        return tools
    found = probe(src)
    if isinstance(found, Unavailable):
        return found
    frame_bytes = found.width * found.height * 3
    with tempfile.TemporaryFile() as decode_err, tempfile.TemporaryFile() as encode_err:
        try:
            decoder = subprocess.Popen(decode_command(tools, src), stdout=subprocess.PIPE, stderr=decode_err)
            encoder = subprocess.Popen(encode_command(tools, found, src, dest), stdin=subprocess.PIPE,
                                       stderr=encode_err)  # fmt: skip
        except FileNotFoundError as error:
            return Unavailable(f"{NOT_AVAILABLE}: {error}")
        assert decoder.stdout is not None and encoder.stdin is not None
        count = 0
        try:
            while count < found.frames:
                raw = decoder.stdout.read(frame_bytes)
                if len(raw) < frame_bytes:
                    break
                count += 1
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(found.height, found.width, 3)
                encoder.stdin.write(grade_frame(frame, grading, week, post_id, count).tobytes())
        finally:
            encoder.stdin.close()
            decoder.stdout.close()
            decoded, encoded = decoder.wait(), encoder.wait()
        for status, log, name in ((decoded, decode_err, "decode"), (encoded, encode_err, "encode")):
            if status != 0:
                log.seek(0)
                raise VideoError(f"the ffmpeg {name} of {src} failed: {log.read().decode('utf-8', 'replace').strip()}")
    if count != found.frames:
        raise VideoError(f"{src}: decoded {count} frames, ffprobe counted {found.frames}")
    if not Path(dest).is_file():
        raise VideoError(f"the ffmpeg encode wrote no {dest}")
    return found
