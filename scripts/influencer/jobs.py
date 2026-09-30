"""Jobs and the Job builders of the render and finish stages, and the approval gate every Post stage
shares (design §A13 Builders, §M5, Decision 27; Req 9.11, 10.3, 10.4, 10.11, 11.2, 11.7-11.10, 12.1,
12.4, 12.5).

    Job                                       one unit of ComfyUI work, built once per pass (task 20.1)
    Files                                     the file access a builder needs, injected by the engine
    approval_gate(calendar, sheet, types)     which Posts may produce Jobs (Req 9.11)
    build_render(week, calendar, persona, sheet, manifest, templates, files) -> Build   (task 20.2)
    build_finish(week, calendar, persona, sheet, manifest, templates, files) -> Build   (task 20.3)
    done_output(job, manifest, files)         the output of the done record with the Job's current key
    finished_image(...)                       a Slide's finished image (Req 12.5)
    template_names(stage, persona)            the Render_Templates a builder needs
    preflight_input(jobs, output_root, ...)   the PreflightInput of task 19 for these Jobs

A Job holds its Template, the placeholder values that do not depend on file bytes (`values`) and, in
`inputs`, the local path of each image placeholder (`reference_image`, `pick_image`). The filled prompt
names an input by its upload name, influencer/<sha256 of the bytes><ext>, so Job.prompt(read) reads the
bytes, fills the template and returns the prompt with the uploads, and Job.key(read) is ids.job_key over
both (Req 10.2): a new Pick gives a new key, an unchanged Job keeps its own. A Job whose input is not known
or not on disk yet has `waiting_for` set, naming what it lacks (Req 10.4); it has no key and is never
filled.

A Pick's file is the first output of the done Manifest record of the Pick's render Job whose job_key equals
that Job's current key, with every recorded output on disk and over 0 bytes (Req 10.3). An Edit Slide's
render Job has the Slide-1 Pick as its input, so its key, and with it the Slide's own Pick, changes when the
Slide-1 Pick does. The same rule gives a Slide's finished image (Req 12.5), so an image finished from an
earlier Pick is never used.

Pure: nothing here reads a clock or the environment, or touches a file or the network. Every file access
goes through the injected Files, so the builders are tested without I/O.
"""
from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from influencer import ids
from influencer.assembler import edit_prompt_value, identity_prompt
from influencer.calendar_io import STILL_TYPES, Calendar, Post
from influencer.checks.template_rules import EXPORT_SIZES
from influencer.errors import Problem, ValidationError
from influencer.geometry import finish_factor
from influencer.manifest import DONE, JobRecord, Manifest
from influencer.persona import LoraRef, Persona
from influencer.preflight import FilledPrompt, ModelFile, PreflightInput
from influencer.prompt_sheet import CalendarPost, Entry, PromptSheet, pick_candidate
from influencer.templates import Template, fill

RENDER, FINISH, REELS = "render", "finish", "reels"
STILL, STILL_SHARP, EDIT, FINISH_TEMPLATE = "still", "still-sharp", "edit", "finish"
REEL_TYPES = ("reel",)
STAGE_TYPES: Mapping[str, tuple[str, ...]] = {RENDER: STILL_TYPES, FINISH: STILL_TYPES, REELS: REEL_TYPES}
IMAGE_INPUTS = ("reference_image", "pick_image")  # the placeholders whose file bytes enter the Job_Key
FINISH_CANDIDATE = 1  # a finish Job's seed is seed_for(W, post, slide, 1)
PROMPT_SHEET = "prompts.yaml"
STAND_IN_UPLOAD = "influencer/preflight-stand-in.png"  # an image value for a preflight-only fill


# ---- the Job (task 20.1) ----


@dataclass(frozen=True, eq=True)
class Job:
    """One unit of ComfyUI work (see the module docstring). id, output_prefix and seed follow §M5 and
    ids.py; timeout_s is the template's (900 s for the stills kinds by default, Req 10.7). post, slide and
    candidate name the Job in the Manifest and the summary (candidate None for a finish Job).
    expect_size is the output size the engine checks an `edit` output against (§C7), else None."""

    id: str
    stage: str
    template: Template
    values: Mapping[str, Any]
    inputs: Mapping[str, Path]
    waiting_for: str | None
    output_prefix: str
    seed: int
    timeout_s: int
    post: str | None = None
    slide: int | None = None
    candidate: int | None = None
    expect_size: tuple[int, int] | None = None

    __hash__ = None  # type: ignore[assignment]  # values holds lists

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "inputs", MappingProxyType({name: Path(path) for name, path in self.inputs.items()}))

    @property
    def template_name(self) -> str:
        return self.template.name

    def prompt(self, read: Callable[[Path], bytes]) -> tuple[dict[str, Any], dict[str, bytes]]:
        """(the filled prompt, {upload name: bytes}) with each input read through read. Raises ValueError
        for a waiting Job and templates.FillError for values the template refuses."""
        if self.waiting_for is not None:
            raise ValueError(f"{self.id} waits for {self.waiting_for}, so it has no prompt yet")
        values = dict(self.values)
        uploads: dict[str, bytes] = {}
        for name, path in self.inputs.items():
            data = read(path)
            upload = ids.upload_name(data, _extension(path))
            values[name] = upload
            uploads[upload] = data
        return fill(self.template, values), uploads

    def key(self, read: Callable[[Path], bytes]) -> str:
        """The Job_Key: ids.job_key over the filled prompt and the input bytes (Req 10.2)."""
        prompt, uploads = self.prompt(read)
        return ids.job_key(prompt, uploads)

    def preview_prompt(self) -> dict[str, Any]:
        """The prompt filled without reading any file, for preflight and waiting Jobs: each input a stand-in
        upload name, each missing int placeholder (a waiting finish Job's upscale) its minimum."""
        values = dict(self.values)
        for name, spec in self.template.placeholders.items():
            if name in values or name not in self.template.used:
                continue
            if spec.type == "file":
                values[name] = STAND_IN_UPLOAD
            elif spec.type == "int" and spec.min is not None:
                values[name] = int(spec.min)
        return fill(self.template, values)


def _extension(path: Path) -> str:
    suffix = path.suffix.lower()
    return suffix if ids.EXT_RE.fullmatch(suffix) else ""


@dataclass(frozen=True)
class Files:
    """The file access of a builder, injected so jobs.py stays pure.

    output_dir: ComfyUI's output folder; Manifest output paths are relative to it.
    size(path): the file's size in bytes, None when it does not exist.
    read(path): the file's bytes.
    image_size(path): an image file's (width, height)."""

    output_dir: Path
    size: Callable[[Path], int | None]
    read: Callable[[Path], bytes]
    image_size: Callable[[Path], tuple[int, int]]

    def present(self, path: Path) -> bool:
        """The file exists and is over 0 bytes (Req 10.3)."""
        size = self.size(path)
        return size is not None and size > 0


def done_output(job: Job, manifest: Manifest, files: Files, *, key: str | None = None) -> Path | None:
    """The local path of the first output of the done record whose job_key is the Job's current key and
    whose every recorded output exists and is over 0 bytes; None when there is none or the Job waits.
    key, when given, is the Job's key already computed."""
    if job.waiting_for is not None:
        return None
    records = [record for record in manifest.jobs if record.id == job.id and record.status == DONE]
    if not records:
        return None  # no key needed, so no bytes are read
    current = key if key is not None else job.key(files.read)
    for record in records:
        if record.job_key == current and _outputs_present(record, files):
            return files.output_dir / record.outputs[0].path
    return None


def _outputs_present(record: JobRecord, files: Files) -> bool:
    return bool(record.outputs) and all(files.present(files.output_dir / output.path) for output in record.outputs)


# ---- the approval gate (task 20.3, Req 9.11) ----


@dataclass(frozen=True)
class Gate:
    """The Posts of the stage's types: those that may produce Jobs, with their entries, in calendar order;
    those waiting for planning (Post id -> why); those waiting for review; and the entries whose Post left
    the calendar."""

    approved: tuple[tuple[Post, Entry], ...]
    planning: Mapping[str, str]
    review: tuple[str, ...]
    left_calendar: tuple[str, ...] = ()


def approval_gate(calendar: Calendar, sheet: PromptSheet | None, types: Collection[str], *,
                  path: str = PROMPT_SHEET) -> Gate:  # fmt: skip
    """The gate render, finish and reels share (Req 9.11). A Post of one of types with no Prompt_Sheet entry
    (or no Prompt_Sheet at all), or whose entry's Slide count differs from the calendar's, waits for
    planning; one whose entry has no Approval waits for review; only the rest may produce Jobs."""
    posts = [post for post in calendar.posts if post.type in types]
    if sheet is None:
        reason = f"has no Prompt_Sheet entry yet; plan it (plan --week {calendar.week})"
        return Gate((), {post.id: reason for post in posts}, ())
    check = sheet.check((CalendarPost(post.id, post.slides, post.candidates) for post in posts), path=path)
    approved: list[tuple[Post, Entry]] = []
    review: list[str] = []
    for post in posts:
        if post.id in check.planning:
            continue
        entry = sheet.entry(post.id)
        assert entry is not None  # check.planning holds every Post without one
        if entry.approved is True:
            approved.append((post, entry))
        else:
            review.append(post.id)
    return Gate(tuple(approved), dict(check.planning), tuple(review), check.left_calendar)


def review_reason(post: str) -> str:
    return f"Post {post!r} waits for review: set approved: true in its Prompt_Sheet entry"


# ---- builder results ----


@dataclass(frozen=True)
class InvalidPick:
    """A recorded Pick that is not a Candidate number (Req 11.10)."""

    post: str
    slide: int
    value: object
    valid: tuple[int, ...]
    message: str


@dataclass(frozen=True)
class PickWait:
    """A Slide of a Post with 2-4 Candidates and no Pick recorded (Req 11.8): each Candidate's number and
    its rendered output file, None when it is not rendered yet."""

    post: str
    slide: int
    candidates: tuple[tuple[int, Path | None], ...]


@dataclass(frozen=True)
class Build:
    """What a builder made: the Jobs (waiting ones included, done ones too, which the loop skips), the
    Posts waiting for planning or review, every invalid Pick and every Slide waiting for a Pick."""

    jobs: tuple[Job, ...]
    planning: Mapping[str, str] = field(default_factory=dict)
    review: tuple[str, ...] = ()
    invalid_picks: tuple[InvalidPick, ...] = ()
    pick_waits: tuple[PickWait, ...] = ()
    left_calendar: tuple[str, ...] = ()


class NoCharacterLora(ValidationError):
    """The Persona_File names no stills Character_LoRA, so render and finish stop before any submission
    (Req 8.9)."""


def require_character_lora(persona: Persona, stage: str) -> LoraRef:
    lora = persona.character_loras.stills
    if lora is None:
        reason = (f"names no stills Character_LoRA, so the {stage} stage cannot run; train one and set "
                  "character_loras.stills")  # fmt: skip
        raise NoCharacterLora([Problem(persona.source, "character_loras.stills", None, reason)])
    return lora


def still_template_name(persona: Persona) -> str:
    """still-sharp when the Persona_File enables sharp decoding, else still (Req 8.4, 8.7)."""
    return STILL_SHARP if persona.sharp_decode else STILL


def template_names(stage: str, persona: Persona) -> tuple[str, ...]:
    """The Render_Templates a builder needs in its templates mapping. finish needs the render templates
    too, because a Pick's identity is its render Job's key."""
    render = (still_template_name(persona), EDIT)
    if stage == RENDER:
        return render
    if stage == FINISH:
        return (*render, FINISH_TEMPLATE)
    raise ValueError(f"no builder for the stage {stage!r} yet")


def look_loras(persona: Persona, post: Post) -> tuple[LoraRef, ...]:
    """The Post's own looks when it sets a list (an empty one included), else looks.stills.<platform>."""
    if post.looks is not None:
        return post.looks
    return getattr(persona.looks.stills, post.platform)


def still_loras(persona: Persona, post: Post, character: LoraRef) -> list[dict[str, Any]]:
    """A Still Job's `loras` value: the stills Character_LoRA, then the looks (Req 8.2, 8.3)."""
    return [{"name": lora.lora, "strength": lora.strength} for lora in (character, *look_loras(persona, post))]


# ---- per-Post planning ----


_INVALID = object()


class _Planner:
    """The render Jobs, Picks and finish Jobs of one approved Post, each built once."""

    def __init__(self, week: str, persona: Persona, character: LoraRef, templates: Mapping[str, Template],
                 manifest: Manifest, files: Files, post: Post, entry: Entry) -> None:  # fmt: skip
        self.week, self.persona, self.character = week, persona, character
        self.templates, self.manifest, self.files = templates, manifest, files
        self.post, self.entry = post, entry
        self._render: dict[tuple[int, int], Job | None] = {}
        self._picks: dict[int, object] = {}
        self.invalid: list[InvalidPick] = []

    # -- descriptions --

    def _described(self) -> tuple[str | None, str | None]:
        location = next((item.description for item in self.persona.locations if item.name == self.post.location), None)
        outfit = next((item.description for item in self.persona.wardrobe if item.name == self.post.outfit), None)
        return location, outfit

    def scene(self, slide: int) -> str:
        return self.entry.slides[slide - 1].scene

    def is_still(self, slide: int) -> bool:
        """Slide 1 of every Post and every Slide of a regenerate carousel render with the Still_Template."""
        return slide == 1 or self.post.type != "carousel" or self.post.mode == "regenerate"

    def slides(self) -> range:
        return range(1, self.post.slides + 1)

    def candidates(self) -> range:
        return range(1, self.post.candidates + 1)

    # -- Picks --

    def pick(self, slide: int) -> object:
        """The Slide's Pick: a Candidate number, None while it waits for one, or _INVALID (reported once)."""
        if slide not in self._picks:
            recorded = self.entry.slides[slide - 1].pick
            try:
                self._picks[slide] = pick_candidate(recorded, self.post.candidates)
            except ValueError as error:
                self._picks[slide] = _INVALID
                message = f"Post {self.post.id!r}, Slide {slide}: {error}"
                self.invalid.append(InvalidPick(self.post.id, slide, recorded, tuple(self.candidates()), message))
        return self._picks[slide]

    def pick_file(self, slide: int) -> tuple[object, Path | None]:
        """(the Pick, its file): the file is None until the Pick's render Job is done with its current key."""
        chosen = self.pick(slide)
        if chosen is None or chosen is _INVALID:
            return chosen, None
        job = self.render_job(slide, chosen)  # type: ignore[arg-type]
        return chosen, None if job is None else done_output(job, self.manifest, self.files)

    def pick_wait(self, slide: int) -> PickWait:
        found = []
        for candidate in self.candidates():
            job = self.render_job(slide, candidate)
            found.append((candidate, None if job is None else done_output(job, self.manifest, self.files)))
        return PickWait(self.post.id, slide, tuple(found))

    def waiting_for_pick(self, slide: int, chosen: object) -> str:
        if chosen is None:
            return f"a Pick for Slide {slide} of Post {self.post.id} in the Prompt_Sheet"
        render = ids.render_id(self.post.id, slide, chosen)  # type: ignore[arg-type]
        return f"the Pick of Slide {slide}, Candidate {chosen} ({render})"

    # -- render Jobs (task 20.2) --

    def render_job(self, slide: int, candidate: int) -> Job | None:
        """The render Job of (slide, candidate); None for an Edit Slide whose Slide-1 Pick is invalid."""
        if (slide, candidate) not in self._render:
            self._render[(slide, candidate)] = (
                self._still(slide, candidate) if self.is_still(slide) else self._edit(slide, candidate)
            )
        return self._render[(slide, candidate)]

    def _common(self, template: Template, slide: int, candidate: int) -> dict[str, Any]:
        post = self.post
        return {
            "id": ids.render_id(post.id, slide, candidate),
            "stage": RENDER,
            "template": template,
            "output_prefix": ids.render_prefix(self.persona.slug, self.week, post.id, post.platform, slide, candidate),
            "seed": ids.seed_for(self.week, post.id, slide, candidate),
            "timeout_s": template.timeout_s,
            "post": post.id,
            "slide": slide,
            "candidate": candidate,
        }

    def _still(self, slide: int, candidate: int) -> Job:
        template = self.templates[still_template_name(self.persona)]
        common = self._common(template, slide, candidate)
        size = template.meta["sizes"][self.post.aspect]
        location, outfit = self._described()
        values = {
            "prompt": identity_prompt(self.persona, location, outfit, self.scene(slide)),
            "seed": common["seed"],
            "latent_width": size["latent"][0],
            "latent_height": size["latent"][1],
            "width": size["output"][0],
            "height": size["output"][1],
            "crop_x": size["crop"][0],
            "crop_y": size["crop"][1],
            "output_prefix": common["output_prefix"],
            "loras": still_loras(self.persona, self.post, self.character),
        }
        return Job(values=values, inputs={}, waiting_for=None, **common)

    def _edit(self, slide: int, candidate: int) -> Job | None:
        chosen, reference = self.pick_file(1)
        if chosen is _INVALID:
            return None  # no Job uses an invalid Pick (Req 11.10)
        template = self.templates[EDIT]
        common = self._common(template, slide, candidate)
        width, height = template.meta["sizes"][self.post.aspect]["output"]
        values = {
            "scene": edit_prompt_value(self.scene(slide)),
            "seed": common["seed"],
            "width": width,
            "height": height,
            "output_prefix": common["output_prefix"],
        }  # no loras: edit has no loras placeholder
        inputs = {} if reference is None else {"reference_image": reference}
        waiting = None if reference is not None else self.waiting_for_pick(1, chosen)
        return Job(values=values, inputs=inputs, waiting_for=waiting, expect_size=(width, height), **common)

    # -- finish Jobs (task 20.3) --

    def finish_job(self, slide: int) -> Job | None:
        """The Slide's finish Job; None when its Pick is invalid."""
        chosen, picked = self.pick_file(slide)
        if chosen is _INVALID:
            return None
        post = self.post
        template = self.templates[FINISH_TEMPLATE]
        seed = ids.seed_for(self.week, post.id, slide, FINISH_CANDIDATE)
        prefix = ids.finish_prefix(self.persona.slug, self.week, post.id, post.platform, slide)
        location, outfit = self._described()
        values: dict[str, Any] = {
            "prompt": identity_prompt(self.persona, location, outfit, self.scene(slide)),
            "seed": seed,
            "character_lora": self.character.lora,
            "character_strength": self.character.strength,
            "output_prefix": prefix,
        }
        inputs: dict[str, Path] = {}
        waiting = self.waiting_for_pick(slide, chosen)
        if picked is not None:
            export_w, export_h = EXPORT_SIZES[post.aspect]
            width, height = self.files.image_size(picked)
            values["upscale"] = finish_factor(width, height, export_w, export_h)  # Decision 27
            inputs["pick_image"] = picked
            waiting = None
        return Job(
            id=ids.finish_id(post.id, slide), stage=FINISH, template=template, values=values, inputs=inputs,
            waiting_for=waiting, output_prefix=prefix, seed=seed, timeout_s=template.timeout_s, post=post.id,
            slide=slide,
        )  # fmt: skip


def _planners(week: str, calendar: Calendar, persona: Persona, sheet: PromptSheet | None, manifest: Manifest,
              templates: Mapping[str, Template], files: Files, stage: str) -> tuple[Gate, list[_Planner]]:  # fmt: skip
    if calendar.week != week:
        raise ValueError(f"the calendar is for {calendar.week}, not {week}")
    character = require_character_lora(persona, stage)
    gate = approval_gate(calendar, sheet, STAGE_TYPES[stage])
    planners = [_Planner(week, persona, character, templates, manifest, files, post, entry)
                for post, entry in gate.approved]  # fmt: skip
    return gate, planners


def _build(gate: Gate, planners: Sequence[_Planner], jobs: Iterable[Job | None], waits: Iterable[PickWait]) -> Build:
    built = tuple(job for job in jobs if job is not None)
    pick_waits = tuple(waits)
    for planner in planners:  # every recorded Pick of an approved Post is checked, used or not
        for slide in planner.slides():
            planner.pick(slide)
    invalid = tuple(pick for planner in planners for pick in planner.invalid)
    return Build(built, gate.planning, gate.review, invalid, pick_waits, gate.left_calendar)


def build_render(week: str, calendar: Calendar, persona: Persona, sheet: PromptSheet | None, manifest: Manifest,
                 templates: Mapping[str, Template], files: Files) -> Build:  # fmt: skip
    """The render stage's Jobs (task 20.2): every Candidate of every Still Slide (Slide 1, and every Slide of
    a regenerate carousel), then every Candidate of each carousel's Edit Slides 2..N, whose reference is the
    Slide-1 Pick and which wait until that Pick's file exists (Req 11.2, 11.7, 11.8). An invalid Slide-1
    Pick gives no Edit Job; it is reported with every other invalid Pick (Req 11.10). Raises
    NoCharacterLora before building anything when the Persona_File names no stills Character_LoRA."""
    gate, planners = _planners(week, calendar, persona, sheet, manifest, templates, files, RENDER)
    jobs: list[Job | None] = []
    waits: list[PickWait] = []
    for planner in planners:
        for slide in planner.slides():
            jobs += [planner.render_job(slide, candidate) for candidate in planner.candidates()]
        if any(not planner.is_still(slide) for slide in planner.slides()) and planner.pick(1) is None:
            waits.append(planner.pick_wait(1))
    return _build(gate, planners, jobs, waits)


def build_finish(week: str, calendar: Calendar, persona: Persona, sheet: PromptSheet | None, manifest: Manifest,
                 templates: Mapping[str, Template], files: Files) -> Build:  # fmt: skip
    """The finish stage's Jobs (task 20.3): one per Slide of every approved single, carousel and story Post,
    with the Slide's Pick as pick_image and upscale = finish_factor(Pick size, export size) (Req 12.1,
    12.4). A Slide whose finished image exists gets a Job whose key is done, which the loop skips. A Slide
    without a Pick file waits; one with an invalid Pick gets no Job. Raises NoCharacterLora first when the
    Persona_File names no stills Character_LoRA."""
    gate, planners = _planners(week, calendar, persona, sheet, manifest, templates, files, FINISH)
    jobs: list[Job | None] = []
    waits: list[PickWait] = []
    for planner in planners:
        for slide in planner.slides():
            jobs.append(planner.finish_job(slide))
            if planner.pick(slide) is None:
                waits.append(planner.pick_wait(slide))
    return _build(gate, planners, jobs, waits)


def finished_image(job: Job, manifest: Manifest, files: Files) -> Path | None:
    """A Slide's finished image: the output of its finish Job's done record whose job_key equals the key
    computed from the current Pick and Persona_File (Req 12.5); None otherwise, and for a waiting Job."""
    if job.stage != FINISH:
        raise ValueError(f"{job.id} is not a finish Job")
    return done_output(job, manifest, files)


# ---- preflight ----


def preflight_input(jobs: Iterable[Job], output_root: Path, *, files: Sequence[ModelFile] = (),
                    ollama_models: Mapping[str, Collection[str]] | None = None,
                    config: Mapping[str, str | None] | None = None) -> PreflightInput:  # fmt: skip
    """The PreflightInput of task 19 for the Jobs a stage would submit or hold as waiting: one FilledPrompt
    per Job, filled with Job.preview_prompt() so no file is read."""
    prompts = tuple(FilledPrompt(job.template, job.preview_prompt(), job.id) for job in jobs)
    return PreflightInput(prompts, Path(output_root), tuple(files), dict(ollama_models or {}), dict(config or {}))
