"""Tests of the QC_Sheet, Publish_Sheet and week index writers, scripts/influencer/export/qc.py, publish.py
and index.py (task 24; design §C8; Req 15.1-15.9).
- the QC_Sheet's layout: one row per Slide in order, the Hero_Image then every rendered Candidate, labels
  with the Post id, NN, Candidate number, the Manifest's seed and template, PICK on the Pick; the Pillow
  version check
- the Publish_Sheet: files and alt texts in upload order, the caption verbatim in a fenced block, the
  planned date, the disclosure checklist on Instagram Posts only (Property 36)
- the week index: every Post once with its status, sheet paths and flags; the counts (Property 37)
Every run happens with no ffmpeg on PATH, and the images are small: only the sheets' rows are 1,024 px tall.
"""
from __future__ import annotations

import dataclasses
import io
import sys
from fractions import Fraction
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

from helpers.harness import REPO, TempEnv

SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:  # scripts/check.sh sets PYTHONPATH; a bare pytest run does not
    sys.path.insert(0, str(SCRIPTS))

from influencer import env  # noqa: E402
from influencer import jobs as J  # noqa: E402
from influencer.calendar_io import Calendar, Post, print_calendar_yaml  # noqa: E402
from influencer.captions import add_disclosure, full_text  # noqa: E402
from influencer.engine import disk_files  # noqa: E402
from influencer.errors import Exit  # noqa: E402
from influencer.export import index as X  # noqa: E402
from influencer.export import publish as P  # noqa: E402
from influencer.export import qc as Q  # noqa: E402
from influencer.export import run as R  # noqa: E402
from influencer.export import video as V  # noqa: E402
from influencer.export.images import UPSCALED  # noqa: E402
from influencer.manifest import DONE, FAILED, JobRecord, Manifest, Output, read_manifest, write_manifest  # noqa: E402
from influencer.persona import Grade, print_persona  # noqa: E402
from influencer.prompt_sheet import Entry, PromptSheet, Slide, print_prompt_sheet  # noqa: E402
from influencer.templates import load_template  # noqa: E402
from strategies import texts  # noqa: E402
from strategies_calendar import calendars  # noqa: E402
from strategies_jobs import BASE_PERSONA  # noqa: E402

WEEK = "2026-W40"
PERSONA = dataclasses.replace(BASE_PERSONA, slug="test-persona", grade=Grade("grade/film.cube", 0.0),
                              signature_items=("gold hoop earrings", "freckles"))  # fmt: skip
IDENTITY_2 = "LUT_3D_SIZE 2\n" + "".join(f"{r} {g} {b}\n" for b in (0, 1) for g in (0, 1) for r in (0, 1))
PICK_SIZE = (720, 960)  # finish factor 3, inside finish.json's 2-4
FINISHED_SIZE = (12, 16)  # tiny, so each export is fast and flagged upscaled
TEMPLATES = {name: load_template(name) for name in J.template_names(J.FINISH, PERSONA)}


def png(path: Path, size: tuple[int, int], colour: tuple[int, int, int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, colour).save(path, "PNG")
    return path


class World:
    """A temp Influencer_Root with the persona, an identity LUT, a calendar and an approved Prompt_Sheet;
    finish() puts done render records for every Candidate and done finish records for the Picks in place."""

    def __init__(self, temp: TempEnv) -> None:
        self.root = temp.influencer_root
        self.folder = self.root / "weeks" / WEEK
        self.output_dir = temp.comfyui_dir / "output"
        self.output_root = temp.output_root
        self.calendar: Calendar | None = None
        self.sheet: PromptSheet | None = None

    def write(self, *posts: Post, entries: dict[str, Entry] | None = None) -> None:
        png(self.root / "refs" / "hero.png", (4, 4), (1, 2, 3))
        (self.root / "grade").mkdir(exist_ok=True)
        (self.root / "grade" / "film.cube").write_text(IDENTITY_2)
        (self.root / "persona.yaml").write_text(print_persona(PERSONA))
        self.folder.mkdir(parents=True, exist_ok=True)
        self.calendar = Calendar(WEEK, posts)
        (self.folder / "calendar.yaml").write_text(print_calendar_yaml(self.calendar))
        given_entries = entries or {}
        made = [given_entries.get(post.id) or Entry(post.id, approved=True, slides=tuple(
            Slide(f"scene {n}", f"alt {n}", 1) for n in range(1, post.slides + 1)))
            for post in posts if post.type != "reel"]  # fmt: skip
        self.sheet = PromptSheet(WEEK, tuple(made))
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
        return JobRecord(job.id, job.stage, job.template_name, DONE, post=job.post, slide=job.slide,
                         candidate=job.candidate, job_key=job.key(Path.read_bytes), seed=job.seed,
                         outputs=(Output(path.relative_to(self.output_dir).as_posix(), path.stat().st_size),))  # fmt: skip

    def finish(self, post: str) -> None:
        records = list(self.manifest().jobs)
        for job in self.build(J.RENDER, self.manifest()).jobs:
            if job.post == post:
                colour = (job.slide * 20, job.candidate * 40, 60)
                records.append(self._done(job, png(self.output_dir / f"{job.output_prefix}_00001_.png", PICK_SIZE, colour)))
        manifest = Manifest("week", WEEK, tuple(records))
        for job in self.build(J.FINISH, manifest).jobs:
            if job.post == post and job.waiting_for is None:
                path = png(self.output_dir / f"{job.output_prefix}_00001_.png", FINISHED_SIZE, (job.slide * 30, 40, 200))
                records.append(self._done(job, path))
        write_manifest(self.manifest_path, Manifest("week", WEEK, tuple(records)))

    def fail(self, post: str) -> None:
        records = list(self.manifest().jobs)
        job = next(job for job in self.build(J.RENDER, self.manifest()).jobs if job.post == post)
        records.append(JobRecord(job.id, job.stage, job.template_name, FAILED, post=post, slide=job.slide,
                                 candidate=job.candidate, job_key="a" * 64, seed=job.seed, error="CUDA out of memory"))  # fmt: skip
        write_manifest(self.manifest_path, Manifest("week", WEEK, tuple(records)))

    def post_folder(self, post: str, platform: str = "instagram") -> Path:
        return self.output_root / WEEK / ("other/" if platform == "other" else "") / post

    def run(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = R.run_export(WEEK, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def prepared(self) -> R.ExportRun:
        folder = self.folder
        return R.prepare(WEEK, env.paths(), self.root, folder, templates=TEMPLATES)


@pytest.fixture
def world(temp_env: TempEnv, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    V.reset()
    yield World(temp_env)
    V.reset()


def carousel(ident: str = "tue-street", slides: int = 3, **fields: object) -> Post:
    return Post(ident, "carousel", "2026-09-29", location="street", outfit="coat", slides=slides, mode="regenerate",
                **fields)  # fmt: skip


def single(ident: str = "mon-cafe", **fields: object) -> Post:
    return Post(ident, "single", "2026-09-28", location="cafe", outfit="cream", **fields)


# ---- the QC_Sheet (Req 15.1, 15.3, 15.7) ----


def test_a_qc_sheet_has_a_row_per_slide_with_the_hero_then_each_candidate_and_their_labels(world: World) -> None:
    post = carousel(candidates=2)
    slides = tuple(Slide(f"scene {n}", f"alt {n}", pick) for n, pick in ((1, 2), (2, 1), (3, 2)))
    world.write(post, entries={post.id: Entry(post.id, approved=True, slides=slides)})
    world.finish(post.id)
    code, out, err = world.run()
    assert code == Exit.OK, out + err
    with Image.open(world.post_folder(post.id) / "tue-street_qc.jpg") as image:
        assert image.format == "JPEG" and image.height > 3 * Q.ROW_HEIGHT

    run = world.prepared()
    staged = [R.Staged(f"tue-street_{n:02d}.jpg", n) for n in (3, 1, 2)]
    sheet = Q.layout(run, post, world.post_folder(post.id), staged)
    assert sheet.header == ("Signature items: gold hoop earrings, freckles",
                            "Check: face, hands, teeth, jewellery continuity, legible background text")  # fmt: skip
    assert [row.title for row in sheet.rows] == ["Slide 01", "Slide 02", "Slide 03"]
    records = {(r.slide, r.candidate): r for r in run.manifest.jobs if r.stage == J.RENDER}
    for number, row in enumerate(sheet.rows, 1):
        assert len(row.tiles) == 3
        assert row.tiles[0].label == Q.HERO_LABEL and row.tiles[0].source == world.root / "refs" / "hero.png"
        for candidate, tile in enumerate(row.tiles[1:], 1):
            record = records[(number, candidate)]
            pick = candidate == slides[number - 1].pick
            expected = f"tue-street {number:02d} c{candidate} seed {record.seed} {record.template}"
            assert tile.label == (expected + " PICK" if pick else expected)
            assert tile.pick is pick
            assert tile.source == world.output_dir / record.outputs[0].path


def test_images_are_scaled_to_1024_high_with_their_ratio_kept() -> None:
    assert Q.scaled_size(720, 960) == (768, 1024)
    assert Q.scaled_size(4, 4) == (1024, 1024)
    assert Q.frame_numbers(7) == (1, 4, 7) and Q.frame_numbers(1) == (1, 1, 1)


def test_a_pillow_older_than_10_1_is_reported_with_its_version() -> None:
    assert Q.pillow_problem("10.1.0") is None and Q.pillow_problem("12.3.0") is None
    problem = Q.pillow_problem("10.0.1")
    assert problem is not None and "Pillow 10.0.1" in problem and "10.1 or later" in problem
    assert "Pillow 9.5.0" in (Q.pillow_problem("9.5.0") or "")


# ---- the Publish_Sheet (Req 15.5, 15.9) ----


@pytest.mark.parametrize("caption, hashtags", [("", ()), ("Made with AI", ()), ("rainy café ```js\nx```", ())],
                         ids=["empty caption", "only the disclosure", "a caption with its own fence"])  # fmt: skip
def test_a_publish_sheet_keeps_the_fenced_caption_and_the_alt_texts_in_upload_order(caption: str,
                                                                                     hashtags: tuple[str, ...]) -> None:  # fmt: skip
    post = carousel(slides=3)
    entry = Entry(post.id, slides=tuple(Slide("s", f"alt {n}") for n in (1, 2, 3)), caption=caption, hashtags=hashtags)
    staged = [R.Staged(f"tue-street_{n:02d}.jpg", n) for n in (2, 3, 1)]
    text = P.print_publish(P.layout(post, entry, staged))
    assert P.caption_text(text) == full_text(caption, hashtags)
    blocks = P.fenced_blocks(text)
    assert [(heading, alt) for heading, alt in blocks[:3]] == [
        (f"### {n}. tue-street_{n:02d}.jpg", f"alt {n}") for n in (1, 2, 3)]
    assert text.index(P.CAPTION_HEADING) < text.index(P.DATE_HEADING) < text.index("2026-09-29")


def test_an_instagram_sheet_ends_with_the_six_checklist_items_and_an_other_sheet_has_none() -> None:
    staged = [R.Staged("mon-cafe_01.jpg", 1)]
    entry = Entry("mon-cafe", slides=(Slide("s", "alt"),), caption="hi", hashtags=("#a", "#b"))
    instagram = P.print_publish(P.layout(single(), entry, staged))
    items = [line[len("- [ ] "):] for line in instagram.split("\n") if line.startswith("- [ ] ")]
    assert items == list(P.DISCLOSURE_CHECKLIST) and len(items) == 6
    assert items[0] == '"AI-generated profile" label on' and items[-1] == "Instagram's nudity rules followed"
    other = P.print_publish(P.layout(single(platform="other"), entry, staged))
    assert "- [ ]" not in other and P.CHECKLIST_HEADING not in other
    assert P.caption_text(instagram) == "hi\n\n#a #b"


# Feature: ai-influencer-pipeline, Property 36: Publish_Sheets keep caption text verbatim
@given(
    pieces=st.lists(st.one_of(texts(max_size=12), st.integers(1, 6).map(lambda n: "`" * n)), max_size=8),
    disclosure=texts(min_size=1, max_size=20, single_line=True).filter(lambda t: t.strip() and "#" not in t),
    hashtags=st.lists(st.one_of(texts(max_size=8, single_line=True), st.integers(1, 6).map(lambda n: "#" + "`" * n)),
                      max_size=4),
    alt=st.lists(st.one_of(texts(max_size=8), st.just("```")), max_size=3).map("".join),
)
def test_property_36_publish_sheets_keep_caption_text_verbatim(pieces: list[str], disclosure: str,
                                                                hashtags: list[str], alt: str) -> None:  # fmt: skip
    """**Validates: Requirements 15.5**"""
    stored = add_disclosure("".join(pieces), disclosure)  # the caption as the Prompt_Sheet stores it
    post = carousel(slides=2)
    entry = Entry(post.id, slides=(Slide("s", alt), Slide("s", "`")), caption=stored, hashtags=tuple(hashtags))
    text = P.print_publish(P.layout(post, entry, [R.Staged("tue-street_02.jpg", 2), R.Staged("tue-street_01.jpg", 1)]))
    assert P.caption_text(text) == full_text(stored, hashtags)
    assert [block for _, block in P.fenced_blocks(text)] == [alt, "`", full_text(stored, hashtags)]


# ---- the week index (Req 15.4, 15.6, 15.8) ----


def test_the_index_lists_an_exported_a_failed_and_a_waiting_post_with_flags_paths_and_counts(world: World) -> None:
    exported, failed, waiting = single(), carousel(slides=2), single("wed-note", platform="other")
    review = Entry(exported.id, approved=True, review=("detox",), slides=(Slide("scene", "alt", 1),))
    world.write(exported, failed, waiting, entries={exported.id: review})
    world.fail(failed.id)
    world.finish(exported.id)
    code, out, err = world.run()
    assert code == Exit.FAILED, out + err
    assert "export 2026-W40: 1 exported, 1 failed, 1 waiting" in out
    text = (world.output_root / WEEK / X.FILE_NAME).read_text()
    assert "3 Posts: 1 exported, 1 failed, 1 waiting" in text
    rows = [line for line in text.split("\n") if line.startswith("| ") and "---" not in line][1:]
    assert [row.split(" | ")[0][2:] for row in rows] == ["mon-cafe", "tue-street", "wed-note"]
    assert rows[0].endswith(f"| {exported.date} | instagram | exported | [mon-cafe/mon-cafe_qc.jpg](<mon-cafe/mon-cafe_qc.jpg>) "
                            f"| [mon-cafe/mon-cafe_publish.md](<mon-cafe/mon-cafe_publish.md>) | review, {UPSCALED} |")  # fmt: skip
    assert rows[1].endswith("| failed | - | - | failed Jobs |")
    assert rows[2].endswith("| other | waiting | - | - | - |")
    assert (world.post_folder("mon-cafe") / "mon-cafe_publish.md").is_file()


def _slides(post: Post) -> int:
    return 1 if post.type == "reel" else post.slides


@st.composite
def week_states(draw: st.DrawFn):
    calendar = draw(calendars(PERSONA, week=WEEK, max_posts=5))
    present = {post.id: draw(st.lists(st.booleans(), min_size=_slides(post), max_size=_slides(post)))
               for post in calendar.posts}  # fmt: skip
    failed_jobs = {post.id: draw(st.booleans()) for post in calendar.posts}
    errors = {post.id: draw(st.booleans()) for post in calendar.posts}
    return calendar, present, failed_jobs, errors


# Feature: ai-influencer-pipeline, Property 37: Every Post gets exactly one status
@given(state=week_states())
def test_property_37_every_post_gets_exactly_one_status(state, tmp_path_factory: pytest.TempPathFactory) -> None:
    """**Validates: Requirements 15.6, 15.8**"""
    calendar, present, failed_jobs, errors = state
    output_root = tmp_path_factory.getbasetemp() / "no-sheets"
    results = []
    for post in calendar.posts:
        complete = all(present[post.id]) and not errors[post.id]
        status = R.post_status(complete, failed_jobs[post.id], errors[post.id])
        jobs = (("render/x", "boom"),) if failed_jobs[post.id] else ()
        results.append(R.PostResult(post, output_root / post.id, status, failed_jobs=jobs))
    index = X.build_index(WEEK, calendar, results, None, output_root)
    assert [row.post for row in index.rows] == [post.id for post in calendar.posts]
    for post, row in zip(calendar.posts, index.rows):
        if all(present[post.id]) and not errors[post.id]:
            expected = R.EXPORTED
        elif failed_jobs[post.id] or errors[post.id]:
            expected = R.FAILED
        else:
            expected = R.WAITING
        assert row.status == expected
        assert (X.FAILED_JOBS in row.flags) == failed_jobs[post.id]
    assert sum(index.counts.values()) == len(calendar.posts)
    assert index.counts == R.counts(results)
    text = X.print_index(index)
    total = index.counts
    assert f"{len(calendar.posts)} Posts: {total[R.EXPORTED]} exported, {total[R.FAILED]} failed" in text


def test_a_calendar_post_without_a_result_is_listed_as_waiting(tmp_path: Path) -> None:
    posts = (single(), carousel(slides=2), Post("fri-dance", "reel", "2026-10-02", mode="external", duration=Fraction(8)))
    index = X.build_index(WEEK, Calendar(WEEK, posts), [], None, tmp_path)
    assert [(row.post, row.status) for row in index.rows] == [(p.id, R.WAITING) for p in posts]
    assert index.counts == {R.EXPORTED: 0, R.FAILED: 0, R.WAITING: 3}
