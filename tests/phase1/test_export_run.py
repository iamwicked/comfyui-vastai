"""Tests of the export stage, scripts/influencer/export/run.py and export/video.py (task 23; design §C8, §M9;
Req 13.11-13.15).
- the opening steps: the lock (exit 4), an invalid LUT or grain strength (exit 1, no folder created)
- sources: a Slide without a finished image waits and names the Slide; an image finished from an earlier
  Pick is never exported
- staging: an unreadable source and a read-only Output_Root fail with the path and leave the earlier
  exports and no temp folder; a Slide fewer drops the stale _05.jpg; stale temp folders of dead and live PIDs
- --external: every bad value stops the run before a write; registration writes the absolute path into
  external.yaml, a later run reads it back, and a deleted registered video makes the Post waiting
- every test runs with no ffmpeg on PATH, so each reel Post waits and no test encodes a video; the §C8
  ffmpeg command lines are checked as data
"""
from __future__ import annotations

import dataclasses
import io
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import pytest
import yaml
from PIL import Image

from helpers.harness import REPO, TempEnv

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import __main__ as cli  # noqa: E402
from influencer import jobs as J  # noqa: E402
from influencer.calendar_io import Calendar, Post, print_calendar_yaml  # noqa: E402
from influencer.engine import disk_files  # noqa: E402
from influencer.errors import Exit  # noqa: E402
from influencer.export import run as R  # noqa: E402
from influencer.export import video as V  # noqa: E402
from influencer.locks import ProcessLock  # noqa: E402
from influencer.manifest import DONE, FAILED, JobRecord, Manifest, Output, read_manifest, write_manifest  # noqa: E402
from influencer.persona import Grade, print_persona  # noqa: E402
from influencer.prompt_sheet import Entry, PromptSheet, Slide, print_prompt_sheet  # noqa: E402
from influencer.templates import load_template  # noqa: E402
from strategies_jobs import BASE_PERSONA  # noqa: E402

WEEK = "2026-W40"
PERSONA = dataclasses.replace(BASE_PERSONA, slug="test-persona", grade=Grade("grade/film.cube", 0.0))
IDENTITY_2 = "LUT_3D_SIZE 2\n" + "".join(f"{r} {g} {b}\n" for b in (0, 1) for g in (0, 1) for r in (0, 1))
PICK_SIZE = (720, 960)  # finish factor 3: inside finish.json's 2-4
FINISHED_SIZE = (12, 16)  # tiny, so each export is fast; smaller than the target, so flagged "upscaled"
TEMPLATES = {name: load_template(name) for name in J.template_names(J.FINISH, PERSONA)}


def single(ident: str = "mon-cafe", **fields: object) -> Post:
    return Post(ident, "single", "2026-09-28", location="cafe", outfit="cream", **fields)


def carousel(ident: str = "tue-street", slides: int = 2, **fields: object) -> Post:
    return Post(ident, "carousel", "2026-09-29", location="street", outfit="coat", slides=slides, mode="regenerate",
                **fields)  # fmt: skip


def external_reel(ident: str = "fri-dance") -> Post:
    return Post(ident, "reel", "2026-10-02", mode="external", duration=Fraction(8))


def sourced_reel(ident: str = "sat-reel", source: str = "mon-cafe") -> Post:
    return Post(ident, "reel", "2026-10-03", location="cafe", outfit="cream", duration=Fraction(6), source_post=source)


def png(path: Path, size: tuple[int, int], colour: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path, "PNG")
    return path


@dataclass
class Result:
    code: int
    out: str
    err: str


class World:
    """A temp Influencer_Root with the persona, an identity LUT, a week's calendar and an approved
    Prompt_Sheet with Pick 1 on every Slide; finish() puts done render and finish records in place."""

    def __init__(self, env: TempEnv) -> None:
        self.env = env
        self.root = env.influencer_root
        self.folder = self.root / "weeks" / WEEK
        self.output_dir = env.comfyui_dir / "output"
        self.output_root = env.output_root
        self.calendar: Calendar | None = None
        self.sheet: PromptSheet | None = None

    def write(self, *posts: Post, persona=PERSONA, lut: str = IDENTITY_2) -> None:
        png(self.root / "refs" / "hero.png", (4, 4), (1, 2, 3))
        (self.root / "grade").mkdir(exist_ok=True)
        (self.root / "grade" / "film.cube").write_text(lut)
        (self.root / "persona.yaml").write_text(print_persona(persona))
        self.folder.mkdir(parents=True, exist_ok=True)
        self.calendar = Calendar(WEEK, posts)
        (self.folder / "calendar.yaml").write_text(print_calendar_yaml(self.calendar))
        entries = [Entry(post.id, approved=True, slides=tuple(Slide(f"scene {n}", "alt", 1)
                                                               for n in range(1, post.slides + 1)))
                   for post in posts if post.type != "reel"]  # fmt: skip
        self.sheet = PromptSheet(WEEK, tuple(entries))
        (self.folder / "prompts.yaml").write_text(print_prompt_sheet(self.sheet))

    @property
    def manifest_path(self) -> Path:
        return self.folder / "manifest.json"

    def manifest(self) -> Manifest:
        return read_manifest(self.manifest_path) if self.manifest_path.exists() else Manifest("week", WEEK)

    def build(self, stage: str, manifest: Manifest) -> J.Build:
        builder = J.build_render if stage == J.RENDER else J.build_finish
        return builder(WEEK, self.calendar, PERSONA, self.sheet, manifest, TEMPLATES, disk_files(self.output_dir))

    def _done(self, job: J.Job, path: Path) -> JobRecord:
        relative = path.relative_to(self.output_dir).as_posix()
        return JobRecord(job.id, job.stage, job.template_name, DONE, post=job.post, slide=job.slide,
                         candidate=job.candidate, job_key=job.key(Path.read_bytes), seed=job.seed,
                         outputs=(Output(relative, path.stat().st_size),))  # fmt: skip

    def finish(self, post: str, slides: tuple[int, ...] | None = None) -> None:
        """Done render records for the Picks and done finish records, with their files, for these Slides."""
        manifest = self.manifest()
        records = list(manifest.jobs)
        for job in self.build(J.RENDER, manifest).jobs:
            if job.post == post and job.candidate == 1 and (slides is None or job.slide in slides):
                pick = png(self.output_dir / f"{job.output_prefix}_00001_.png", PICK_SIZE, (job.slide * 20, 90, 60))
                records.append(self._done(job, pick))
        manifest = Manifest("week", WEEK, tuple(records))
        for job in self.build(J.FINISH, manifest).jobs:
            if job.post == post and job.waiting_for is None and (slides is None or job.slide in slides):
                done = png(self.output_dir / f"{job.output_prefix}_00001_.png", FINISHED_SIZE, (job.slide * 30, 40, 200))
                records.append(self._done(job, done))
        write_manifest(self.manifest_path, Manifest("week", WEEK, tuple(records)))

    def post_folder(self, post: str, platform: str = "instagram") -> Path:
        middle = "other/" if platform == "other" else ""
        return self.output_root / WEEK / f"{middle}{post}"

    def run(self, external: list[tuple[str, str]] = ()) -> Result:
        out, err = io.StringIO(), io.StringIO()
        code = R.run_export(WEEK, external=external, out=out, err=err)
        return Result(code, out.getvalue(), err.getvalue())


@pytest.fixture
def world(temp_env: TempEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The World, with PATH an empty folder: no ffmpeg or ffprobe, whatever the host has."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    V.reset()
    yield World(temp_env)
    V.reset()


def snapshot(folder: Path) -> dict[str, bytes]:
    return {entry.name: entry.read_bytes() for entry in sorted(folder.iterdir()) if entry.is_file()}


def temps(folder: Path) -> list[str]:
    return [entry.name for entry in folder.iterdir() if entry.name.startswith(R.TEMP_PREFIX)]


needs_permissions = pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")


# ---- a full export (Req 13.12) ----


def test_every_slide_exports_under_its_folder_with_the_counts_and_the_reminder(world: World) -> None:
    world.write(carousel(), single("wed-note", platform="other"))
    world.finish("tue-street")
    world.finish("wed-note")
    result = world.run()
    assert result.code == Exit.OK, result.out + result.err
    folder = world.post_folder("tue-street")
    assert sorted(snapshot(folder)) == ["tue-street_01.jpg", "tue-street_02.jpg", "tue-street_publish.md",
                                        "tue-street_qc.jpg"]  # fmt: skip
    with Image.open(folder / "tue-street_02.jpg") as image:
        assert (image.format, image.size) == ("JPEG", (1080, 1440))
    assert (world.post_folder("wed-note", "other") / "wed-note_01.jpg").is_file()  # <W>/other/<post-id>/
    assert temps(folder) == []
    assert "export 2026-W40: 2 exported, 0 failed, 0 waiting" in result.out
    assert f"Output_Root: {world.output_root}" in result.out
    assert result.out.rstrip().endswith(R.REMINDER)
    assert "upscaled at export: tue-street_01" in result.out


# ---- sources (Req 13.14, 12.5) ----


def test_a_slide_without_a_finished_image_waits_and_the_earlier_exports_stay(world: World) -> None:
    world.write(carousel(slides=3))
    world.finish("tue-street", slides=(1, 2))
    folder = world.post_folder("tue-street")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tue-street_01.jpg").write_bytes(b"old one")
    (folder / "tue-street_02.jpg").write_bytes(b"old two")
    before = snapshot(folder)
    result = world.run()
    assert result.code == Exit.OK, result.out + result.err
    assert "waiting: Post 'tue-street': missing Slide 3" in result.out
    assert snapshot(folder) == before
    assert "0 exported, 0 failed, 1 waiting" in result.out


def test_an_image_finished_from_an_earlier_pick_is_not_exported(world: World) -> None:
    world.write(single())
    world.finish("mon-cafe")
    pick = next(record for record in world.manifest().jobs if record.stage == J.RENDER)
    png(world.output_dir / pick.outputs[0].path, PICK_SIZE, (250, 1, 1))  # the Pick's bytes change
    old_key = next(record.job_key for record in world.manifest().jobs if record.stage == J.FINISH)
    result = world.run()
    assert result.code == Exit.OK
    assert next(record.job_key for record in world.manifest().jobs if record.stage == J.FINISH) == old_key
    assert "waiting: Post 'mon-cafe': missing Slide 1" in result.out
    assert "no finished image from its current Pick" in result.out
    assert snapshot(world.post_folder("mon-cafe")) == {}  # only the render/ and finish/ folders


def test_a_failed_job_of_a_post_that_is_not_exported_makes_it_failed(world: World) -> None:
    world.write(single())
    [job] = [job for job in world.build(J.RENDER, world.manifest()).jobs]
    record = JobRecord(job.id, job.stage, job.template_name, FAILED, post=job.post, slide=1, candidate=1,
                       job_key="a" * 64, seed=job.seed, error="CUDA out of memory")  # fmt: skip
    write_manifest(world.manifest_path, Manifest("week", WEEK, (record,)))
    result = world.run()
    assert result.code == Exit.FAILED
    assert "failed: Post 'mon-cafe': the Job render/mon-cafe/s01/c1 failed: CUDA out of memory" in result.out
    assert "0 exported, 1 failed, 0 waiting" in result.out


# ---- staging and the atomic replace (Req 13.15, 13.12) ----


@needs_permissions
def test_an_unreadable_source_fails_with_its_path_and_keeps_the_earlier_exports(world: World) -> None:
    world.write(carousel())
    world.finish("tue-street")
    folder = world.post_folder("tue-street")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "tue-street_01.jpg").write_bytes(b"old one")
    before = snapshot(folder)
    finished = [world.output_dir / r.outputs[0].path for r in world.manifest().jobs if r.stage == J.FINISH]
    source = next(path for path in finished if "_s02" in path.name)
    source.chmod(0)
    try:
        result = world.run()
    finally:
        source.chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert result.code == Exit.FAILED, result.out + result.err
    assert f"failed: Post 'tue-street': {source}: Permission denied" in result.out
    assert snapshot(folder) == before and temps(folder) == []


@needs_permissions
def test_a_read_only_output_root_fails_with_the_path_and_leaves_no_temp_folder(world: World) -> None:
    world.write(single())
    world.finish("mon-cafe")
    folder = world.post_folder("mon-cafe")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "mon-cafe_01.jpg").write_bytes(b"old")
    before = snapshot(folder)
    folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        result = world.run()
    finally:
        folder.chmod(stat.S_IRWXU)
    assert result.code == Exit.FAILED
    assert f"failed: Post 'mon-cafe': {folder / f'{R.TEMP_PREFIX}{os.getpid()}'}: Permission denied" in result.out
    assert snapshot(folder) == before and temps(folder) == []


def test_a_slide_fewer_drops_the_stale_file_and_stale_temp_folders_are_handled(world: World) -> None:
    world.write(carousel(slides=4))
    world.finish("tue-street")
    folder = world.post_folder("tue-street")
    folder.mkdir(parents=True, exist_ok=True)
    for number in range(1, 6):
        (folder / f"tue-street_{number:02d}.jpg").write_bytes(b"old")
    (folder / "tue-street_qc.jpg").write_bytes(b"not an NN file")
    dead = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    dead_temp, live_temp = folder / f"{R.TEMP_PREFIX}{int(dead.stdout)}", folder / f"{R.TEMP_PREFIX}{os.getppid()}"
    for temp in (dead_temp, live_temp):
        temp.mkdir()
        (temp / "tue-street_01.jpg").write_bytes(b"partial")
    result = world.run()
    assert result.code == Exit.OK, result.out + result.err
    names = sorted(snapshot(folder))
    assert names == [f"tue-street_{n:02d}.jpg" for n in range(1, 5)] + ["tue-street_publish.md", "tue-street_qc.jpg"]
    assert all(data != b"old" for name, data in snapshot(folder).items() if name != "tue-street_qc.jpg")
    assert not dead_temp.exists() and live_temp.is_dir()
    assert f"warning: Post 'tue-street': {live_temp} belongs to PID {os.getppid()}, which is still running" in result.out


# ---- --external and external.yaml (Req 13.11, 13.14) ----


@pytest.mark.parametrize("case", ["unknown", "not a reel", "duplicate", "missing", "folder"])
def test_a_bad_external_value_stops_the_run_before_any_write(world: World, tmp_path: Path, case: str) -> None:
    world.write(single(), external_reel())
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video")
    value = {
        "unknown": [("no-such", str(clip))],
        "not a reel": [("mon-cafe", str(clip))],
        "duplicate": [("fri-dance", str(clip)), ("fri-dance", str(clip))],
        "missing": [("fri-dance", str(tmp_path / "gone.mp4"))],
        "folder": [("fri-dance", str(tmp_path))],
    }[case]
    result = world.run(external=value)
    assert result.code == Exit.USAGE
    assert f"command line: --external: {value[-1][0]}={value[-1][1]}: " in result.err
    assert not (world.folder / R.EXTERNAL_FILE).exists()
    assert not world.output_root.exists()


def test_a_registered_video_is_stored_by_absolute_path_and_read_back(world: World, tmp_path: Path,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    world.write(external_reel(), sourced_reel("sat-reel", "tue-street"), carousel())
    clip = tmp_path / "renders" / "dance.mp4"
    clip.parent.mkdir()
    clip.write_bytes(b"video")
    monkeypatch.chdir(tmp_path)
    first = world.run(external=[("fri-dance", "renders/dance.mp4")])
    assert first.code == Exit.OK, first.out + first.err
    assert yaml.safe_load((world.folder / R.EXTERNAL_FILE).read_text()) == {"fri-dance": str(clip)}
    assert "waiting: Post 'fri-dance': missing the Reel" in first.out and "waits for ffmpeg" in first.out
    assert "ffmpeg not available" in first.out
    second = world.run()
    assert "waits for ffmpeg" in second.out  # external.yaml read back: the source is still found
    clip.unlink()
    third = world.run()
    assert third.code == Exit.OK
    assert f"the registered video {clip} no longer exists" in third.out


def test_every_reel_post_waits_without_ffmpeg_while_the_stills_export(world: World, tmp_path: Path) -> None:
    world.write(single(), external_reel(), sourced_reel())
    world.finish("mon-cafe")
    result = world.run()
    assert result.code == Exit.OK, result.out + result.err
    assert not V.ffmpeg_available()
    assert "has no user-given video" in result.out  # mode external, nothing registered
    assert "has neither a rendered Reel nor a user-given video" in result.out
    assert "1 exported, 0 failed, 2 waiting" in result.out
    assert not world.post_folder("fri-dance").exists() and not world.post_folder("sat-reel").exists()


# ---- the opening steps and the exit statuses (Req 13.14, 14.8, 10.10) ----


@pytest.mark.parametrize("fault", ["lut", "grain"])
def test_an_invalid_grade_exits_1_before_any_folder_is_created(world: World, fault: str) -> None:
    if fault == "lut":
        world.write(single(), lut="LUT_3D_SIZE 2\n0 0 0\n")
    else:
        world.write(single(), persona=dataclasses.replace(PERSONA, grade=Grade("grade/film.cube", 1.5)))
    result = world.run()
    assert result.code == Exit.USAGE
    assert ("film.cube" if fault == "lut" else "grade.grain") in result.err
    assert not world.output_root.exists()


def test_a_held_week_lock_exits_4_with_the_holders_pid(world: World) -> None:
    world.write(single())
    with ProcessLock(world.folder / ".lock"):
        result = world.run()
    assert result.code == Exit.LOCKED
    assert f"PID {os.getpid()}" in result.err


def test_the_export_command_runs_the_stage(world: World, capsys: pytest.CaptureFixture[str]) -> None:
    world.write(external_reel())
    assert cli.main(["export", "--week", WEEK]) == Exit.OK
    assert "0 exported, 0 failed, 1 waiting" in capsys.readouterr().out


# ---- export/video.py: the Phase 1 boundary (Req 13.13) ----


def test_every_video_entry_point_reports_ffmpeg_not_available(world: World, tmp_path: Path) -> None:
    for answer in (V.discover(), V.probe(tmp_path / "x.mp4"), V.grab_frame(tmp_path / "x.mp4", 1),
                   V.export_reel(tmp_path / "x.mp4", tmp_path / "y.mp4", None, WEEK, "fri-dance")):  # fmt: skip
        assert isinstance(answer, V.Unavailable) and not answer
        assert str(answer).startswith("ffmpeg not available: ffmpeg and ffprobe not found on PATH")


def test_the_passthrough_flag_follows_the_ffmpeg_version() -> None:
    assert V.parse_version("ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright") == (4, 4)
    assert V.parse_version("ffmpeg version n5.1.2 Copyright") == (5, 1)
    assert V.parse_version("ffmpeg version N-112233-gabc Copyright") is None
    assert V.passthrough_flags((5, 0)) == ("-vsync", "passthrough")
    assert V.passthrough_flags((5, 1)) == V.passthrough_flags(None) == ("-fps_mode", "passthrough")


def test_the_c8_command_lines() -> None:
    tools = V.Tools("/bin/ffmpeg", "/bin/ffprobe", (6, 1), V.PASSTHROUGH)
    probe = V.parse_probe('{"streams": [{"codec_type": "video", "width": 720, "height": 1280, '
                          '"r_frame_rate": "30000/1001", "nb_read_frames": "240"}, {"codec_type": "audio"}]}')
    assert probe == V.Probe(Fraction(30000, 1001), 240, 720, 1280, True)
    assert V.probe_command(tools, "in.mp4")[1:6] == ["-v", "error", "-count_frames", "-show_entries", V.PROBE_SHOW]
    assert "select=eq(n\\,4)" in V.frame_command(tools, "in.mp4", 5)
    assert V.decode_command(tools, "in.mp4")[-3:] == ["-fps_mode", "passthrough", "-"]
    encode = " ".join(V.encode_command(tools, probe, "in.mp4", "out.mp4"))
    for part in ("-framerate 30000/1001", "-map 1:a:0", "-c:v libx264", "-pix_fmt yuv420p", "-frames:v 240",
                 "-c:a aac -b:a 192k -t 8.008000", "-map_metadata -1", "-map_chapters -1", "-fflags +bitexact",
                 "-flags:v +bitexact", "-movflags +faststart"):  # fmt: skip
        assert part in encode, part
    silent = " ".join(V.encode_command(tools, dataclasses.replace(probe, audio=False), "in.mp4", "out.mp4"))
    assert "-c:a" not in silent and "1:a:0" not in silent
