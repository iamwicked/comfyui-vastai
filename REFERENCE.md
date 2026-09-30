# comfyui-vastai — Reference (v7)

Aadi's reproducible ComfyUI + Ollama workstation on Vast.ai. Idempotent scripts turn a
**stock Vast.ai ComfyUI template** instance into the full setup: one command installs
everything one workflow needs (models, LoRAs, custom nodes, Ollama models, workflow
JSON), and the template can run the whole thing by itself on first boot. Built for one
80 GB GPU (A100 / H100 class).

Repo: `https://github.com/iamwicked/comfyui-vastai` · Changes per version: `CHANGELOG.md`

## 1. Layout

| Path | Role |
|---|---|
| `config/catalog.txt` | Workflow ids, whether each is in `all`, minimum ComfyUI version |
| `config/models.list` | Every downloadable file (models, LoRAs, workflow JSONs) with `wf:` tags |
| `config/nodes.txt` | Custom node git repos with `wf:` tags |
| `config/ollama-models.txt` | Ollama LLMs with `wf:` tags |
| `config/workflows/` | Bundled workflow JSONs (login-gated ones are fetched on-instance instead) |
| `config/patches/` | Small, idempotent edits applied to a workflow JSON after download |
| `scripts/vast-provision.sh` | Hands-off first boot: Vast's `PROVISIONING_SCRIPT` target (§2A) |
| `scripts/stock-setup.sh` | Instance commands: `init`, `<id>`, `finish`, `all`, `status`, `restart`, `list` |
| `scripts/install-workflow.sh` | One command per workflow (what `stock-setup.sh <id>` runs) |
| `scripts/provision.sh` | The engine: nodes, models, Ollama models; `--status`, `--list-workflows` |
| `scripts/common.sh` | Shared helpers (supervisor, model paths, user dir, Ollama service) |
| `scripts/patch-workflow.py` | Applies `config/patches/*.patch.json` |
| `extra_model_paths.yaml` | Points ComfyUI at `/workspace/models` |
| `scripts/save-progress.sh` | Optional off-box backup (git for workflows, rclone for outputs and the influencer state) |
| `scripts/influencer.sh`, `scripts/influencer/` | The AI influencer tools: plan, render, finish, export and the checks (§13) |
| `scripts/check.sh` | The Check_Suite: `check-config`, the tests, `bash -n` and shellcheck |
| `config/influencer/` | Render_Templates, LUTs, licence and safety lists, and the example Persona_File and calendars |

Instance paths (stock template): ComfyUI `/workspace/ComfyUI` on **port 18188** behind
Vast's proxy (the Open button), python `/venv/main/bin/python`, repo
`/workspace/comfyui-vastai`, models `/workspace/models`, user dir
`/workspace/comfyui-user` (linked as `ComfyUI/user`), workflows
`/workspace/comfyui-user/default/workflows`, outputs `/workspace/ComfyUI/output`,
Ollama weights `/workspace/ollama-models`. The template runs ComfyUI under supervisor;
the scripts restart that process and never start a second server.

## 2. Setting up a fresh instance

Use Vast's stock **ComfyUI** template on a single 80 GB GPU, with enough disk for the
workflows you want (§8) plus about 50 GB of headroom. Either way below, the first boot
with a couple of workflows takes roughly 20-45 minutes, mostly downloads.

### A. Hands-off (PROVISIONING_SCRIPT)

Vast's templates run a script from the `PROVISIONING_SCRIPT` URL once per new instance
and hold ComfyUI back until it finishes. One-time setup in the console:

1. Templates → find Vast's **ComfyUI** template → **Edit** (this saves your own copy).
2. Environment Variables, add:

   | Variable | Value |
   |---|---|
   | `PROVISIONING_SCRIPT` | `https://raw.githubusercontent.com/iamwicked/comfyui-vastai/main/scripts/vast-provision.sh` |
   | `WORKFLOWS` | ids to install, comma-separated, e.g. `krea2,wan-animate`; `all` for everything in `all`; empty for tools + Ollama only |
   | `UPDATE_COMFYUI` | optional, `1`: move ComfyUI core to the newest release first (MiniMax H3 needs >= 0.30.0; the log warns when core is older) |

   If the template already has a `PROVISIONING_SCRIPT` value, replace it. Write values
   in quotes like the template's own rows (`"krea2,wan-animate"`).

   **Delete `PROVISIONING_COMFYUI_WORKFLOWS`**. The stock template sets it, and it is a
   separate Vast feature: before the script runs, it downloads every model and node of
   the linked example workflow. If any of those lookups fails, Vast stops provisioning
   before it reaches `PROVISIONING_SCRIPT`, so nothing from this repo gets installed.
   Keep the other stock rows (`COMFYUI_ARGS`, `PORTAL_CONFIG`, `OPEN_BUTTON_*`,
   `JUPYTER_DIR`, `DATA_DIRECTORY`, ...).
3. Tokens: put `HF_TOKEN` and `CIVITAI_TOKEN` in Vast's account-level Environment
   Variables (Settings → Environment Variables). If you put them in the template
   instead, keep that template private.
4. Save, then rent with this template.

On boot the script clones (or updates) the repo in `/workspace/comfyui-vastai`, runs
`stock-setup.sh init`, installs each workflow in `WORKFLOWS` one after another, runs
`finish`, and prints a summary. Follow it in the Instance Portal logs, or in a terminal:

```bash
tail -f /var/log/portal/provisioning.log
```

It always exits cleanly so ComfyUI starts even if a download failed; the summary names
what to re-run by hand (§2B commands). It runs once per new instance: a stop/start does
not re-run it. To add a workflow later, run `stock-setup.sh <id>` in a terminal.

### B. By hand (Jupyter terminal)

**Terminal 1** (Jupyter → Terminal). The token prompts keep the values out of shell
history; paste each token when asked (skip this line if the tokens are already
account-level variables: `echo ${HF_TOKEN:+set}` prints `set`):

```bash
read -rsp "HF token: " HF_TOKEN; echo; read -rsp "Civitai token: " CIVITAI_TOKEN; echo; export HF_TOKEN CIVITAI_TOKEN
[ -d /workspace/comfyui-vastai/.git ] || git clone --progress https://github.com/iamwicked/comfyui-vastai.git /workspace/comfyui-vastai
bash /workspace/comfyui-vastai/scripts/stock-setup.sh init
```

`init` installs missing tools (hf CLI, zstd, unzip, wget), installs
`extra_model_paths.yaml`, links the user dir, installs Ollama and registers it with
supervisor on 127.0.0.1, then restarts ComfyUI if anything changed. It warns when
ComfyUI core is older than a workflow needs; `UPDATE_COMFYUI=1 bash .../stock-setup.sh init`
moves core to the newest ComfyUI release (Vast's image sits on a release tag), and
`COMFYUI_REF=<tag> bash .../stock-setup.sh init` pins it instead.

**One terminal per workflow**, in parallel if you like (tokens in each):

```bash
read -rsp "HF token: " HF_TOKEN; echo; read -rsp "Civitai token: " CIVITAI_TOKEN; echo; export HF_TOKEN CIVITAI_TOKEN
bash /workspace/comfyui-vastai/scripts/stock-setup.sh wan-animate
```

```bash
read -rsp "HF token: " HF_TOKEN; echo; read -rsp "Civitai token: " CIVITAI_TOKEN; echo; export HF_TOKEN CIVITAI_TOKEN
bash /workspace/comfyui-vastai/scripts/stock-setup.sh krea2
```

**Once, when the installs are done** (any terminal):

```bash
bash /workspace/comfyui-vastai/scripts/stock-setup.sh finish
```

`finish` installs the global nodes and Ollama models, restarts ComfyUI, and prints a
per-workflow status. Open ComfyUI with the instance's Open button and hard-refresh the
tab; the workflows are under Workflows (top-left menu).

Useful anytime:

```bash
bash /workspace/comfyui-vastai/scripts/stock-setup.sh status        # what's installed / missing
bash /workspace/comfyui-vastai/scripts/stock-setup.sh restart       # restart ComfyUI
bash /workspace/comfyui-vastai/scripts/stock-setup.sh all           # everything in `all`, one terminal (~290 GB)
```

Everything is idempotent: re-running a command skips finished files, resumes
interrupted downloads, and waits for another terminal that is fetching the same file.
A command that hit problems lists them and exits non-zero; fix the cause (usually a
token or a licence to accept) and run the same command again.

## 3. Between sessions

| How you end a session | Next boot | Time to first render |
|---|---|---|
| **Stop** the instance | Start it; supervisor brings ComfyUI and Ollama back, provisioning does not re-run | ~2-3 min |
| **Destroy**, with a Vast volume mounted at `/workspace` | Rent the same machine with the volume; the provisioning script (or `init`, your workflows, `finish` by hand) reinstalls tools and node dependencies into the fresh venv; models and workflows are already there | ~10-15 min |
| **Destroy**, nothing kept | Same as a fresh instance (§2) | 20-45 min |

- A stopped instance keeps billing for its disk, and if someone rents that GPU in the
  meantime you cannot start it again until they're done.
- Volumes are tied to one physical machine: create one from the machine's Storage
  options, mount it at `/workspace`, and re-rent that machine next time.
- On a reused volume the node code survives but `/venv/main` is new. The engine keeps
  its "dependencies installed" records inside the Python environment, so they vanish
  with the old venv and the requirements are installed again. (If Vast keeps the venv
  on the volume too, `sync_environment=true`, the records travel with it and nothing
  is reinstalled.)

## 4. Workflow catalog

`bash scripts/stock-setup.sh <id>`. Sizes are downloads, verified 2026-09-28
(shared files count once per workflow).

| id | What it is | GB |
|---|---|---|
| `qwen21` | Qwen-Image 2.1 text-to-image + Lenovo UltraReal LoRA | 17.4 |
| `qwen21-edit` | Qwen-Image 2.1 image edit, up to 10 reference images (same weights as qwen21) | 17.4 |
| `krea2` | Krea 2 text-to-image: realism/NSFW LoRA set, Dolphin3 prompt enhancement via Ollama | 33.2 + 4.9 (dolphin3) |
| `edit-2509` | Qwen-Image-Edit 2509 multi-image edit, 4-step Lightning | 30.9 |
| `krea-style` | Krea 2 Turbo style reference (1-3 style images) | 19.4 |
| `wan-i2v` | Wan2.2 Remix/Painter uncensored image-to-video | 71.0 |
| `minimax-h3` | MiniMax H3 (Hailuo 3.0) all-in-one: 7 modes + lip sync, native stereo audio | 63.0 |
| `wan-animate` | Wan2.2 Animate driving-video motion transfer + Instareal realism LoRA | 46.4 |
| `global` | Shared extras: Z-Image Turbo checkpoint, general nodes, `qwen3:8b` | 6.2 + 5.2 |
| `flux` | Opt-in Flux.2 Klein 9B support files (not in `all`; base needs HF approval) | 9.2 |

`all` = `qwen21 qwen21-edit krea2 edit-2509 krea-style wan-i2v minimax-h3 wan-animate global`:
**281.7 GB** of files plus about 10 GB of Ollama models (v6).

The AI influencer pipeline (v7) adds four ids; their use is in the Runbook, §13, and their
sizes in its disk table, §13.11:

| id | What it is | GB |
|---|---|---|
| `influencer` | Stills: Krea 2 Raw realism recipe, Qwen-Image-Edit-2511 carousel edits, face detail, VOSR2 upscale, DeGrid; in `all` | 107.7 (102.1 new), measured 2026-09-28 |
| `influencer-video` | Reels: MiniMax H3 image- and reference-to-video, H3 look LoRAs, ffmpeg; opt-in. Not yet available — arrives in Phase 3 | not yet measured |
| `influencer-nsfw` | Adult-content LoRAs under `loras/nsfw/` and `dolphin3:8b`, for platform-`other` Posts only; opt-in. Not yet available — arrives in Phase 4 | not yet measured |
| `character-design` | Krea 2 sketch-to-Hero_Image (lineart edit) and QuadView/DynamicCharacterSheet sheets; opt-in. Not yet available — arrives in Phase 4 | not yet measured |

With `influencer` in `all`, the v7 `all` total is not yet measured (§13.11).

## 5. Workflow notes

### qwen21 / qwen21-edit
- Templates: `image_qwen_image_2_1_t2i.json`, `image_qwen_image_2_1_image_edit.json`,
  pinned to Comfy-Org revision `371a7b7`.
- Lenovo LoRA: insert `LoraLoaderModelOnly` (`lenovo_qwen21.safetensors` @ 0.7) after
  the UNETLoader. Creator settings: 50 steps, cfg 4 (1 if the image turns to slop),
  `res_2s` + beta (RES4LYF installs with these workflows).
- Edit: `image_1` is the edit target, the rest are references; mention them as
  `<img>1</img>`, `<img>2</img>` in the prompt. 25 steps, euler, cfg 1.
- Prompt enhancer (opt-in): the 2026-09-25 template revision adds a Qwen3.5-9B encoder.
  To use it, change both template URLs in `models.list` from `371a7b7…` to `99e3d43745926b78466d99f937c0cd2bb622423a`,
  add `text_encoder|qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors|hf|Comfy-Org/Qwen-Image-2.1::text_encoders/qwen3.5_9b_qwen_image_2.1_pe_t2i.int8_convrot.safetensors||wf:qwen21`
  (and the `_pe_i2i` twin for qwen21-edit, 9.5 GB each), delete the two
  `.provisioned-image_qwen_image_2_1_*` markers in the workflows folder, and re-run.

### krea2
- **Simple Krea2 NSFW+ LLM** (`simpleKrea2NSFWLLM_v10.json`, Civitai 3087685): Krea 2
  Turbo fp8, NSFW+ LoRA @ 0.85 ("Enable LoRA?"), prompt refinement ON ("Refine Prompt?")
  through the Plush Advanced Prompt Enhancer, patched to `Ollama (URL)` /
  `dolphin3:8b` / `http://127.0.0.1:11434/v1`. Ollama must be running with dolphin3
  pulled before ComfyUI starts; `install-workflow.sh krea2` and `finish` handle both.
  10 steps, cfg 1, euler / beta57.
- **Official Krea 2 Turbo t2i** (`image_krea2_turbo_t2i.json`, pinned `98fd32c`): runs as
  downloaded (Turbo fp8 + darkbrush LoRA).
- **Raw + Lenovo UltraReal** for maximum realism: in either graph set `unet_name` to
  `krea2_raw_fp8_scaled.safetensors`, LoRA `lenovo_krea2_3000.safetensors` @ 0.8,
  cfg 3.5-4, about 52 steps. On Turbo the Lenovo LoRA wants 1.2-2.0.
- `Krea2 NSFW+.safetensors` lives in `loras/Krea2/` because the graph loads it by that path.

### edit-2509
`image` plus optional `image2`/`image3`. Lightning LoRA on by default (4 steps, cfg 1);
toggle "Enable Lightning LoRA" off for the 20-step, cfg 2.5 quality path.

### krea-style
Generates a new image in the style of 1-3 reference images (not an edit of them).
`prompt_enhance` off by default, LoRA strength 1, 1024×1024.

### wan-i2v
Graph from the Civitai zip (`uncensoredWan22Remix_v10.zip`). Wiring that works: high
loader → `Wan2.2_Remix_NSFW_i2v_14b_high_lighting_v2.0`, low loader → `…low_lighting_v2.0`,
CLIP → `nsfw_wan_umt5-xxl_fp8_scaled`, VAE → `wan_2.1_vae`, "Video Upscale With Model"
→ `4x_foolhardy_Remacri.pth`.

### minimax-h3
Graph: "MiniMax H3 All-in-One Workflow" by Alice_AIisinWonderland (Civitai 2913151,
EN version 3295505; 3295435 is the same graph with Japanese notes). Login-gated, so it
is fetched on-instance and never committed. Needs **ComfyUI >= 0.30.0**.

| Mode | Job | UNet |
|---|---|---|
| 01 | Text to video | FL2VA |
| 02 | Image to video (first frame) | FL2VA |
| 03 | First / last frame | FL2VA |
| 04 | Reference to video (up to 5 images, 3 videos, 3 audio clips) | REF2VA |
| 05 | Motion tracking, DWPose skeleton video as Video 1 | REF2VA |
| 06 | Motion tracking, DensePose surface video as Video 1 | REF2VA |
| 07 | Motion tracking, SCAIL / NLF mesh video as Video 1 | REF2VA |

- Pick the UNet in panel (1), the mode in panel (2), run. **Never recolour the groups**:
  the panels find their groups by colour.
- Lip sync works on top of any mode: put the voice clip in Audio 1 and the spoken lines
  in `<d>…</d>` tags in the prompt.
- The graph has no pose preprocessors: modes 05-07 expect a pre-made skeleton / surface
  / mesh video. (`comfyui_controlnet_aux`, installed globally, has DWPose and DensePose
  preprocessors; whether H3 accepts their output as-is is untested.)
- Turbo 8-step LoRA is on by default (about 6× faster than 20 steps), loaded from
  `loras/MINIMAX/minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors`. The taeh3
  decoder feeds the live preview, which is on by default.
- H3 rules: no CFG and no negative prompt (BasicGuider), 24 fps fixed, frame count
  17n+5, width/height multiples of 32, 768 px short edge baseline (1344×768 at 16:9,
  pixel-area cap 768×1344).
- Reference-to-video prompt guide:
  https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/docs/VIDEO_PROMPT_WRITING_GUIDE_ref_en.md
- Weights are the variants Comfy-Org's own H3 templates use (pruned int8 FL2VA/REF2VA,
  Qwen3-VL 32B nvfp4 encoder, int8 video VAE, fp32 audio VAE). On first load, check
  that the graph's loaders show these files; if a loader shows a missing file, pick the
  matching one from its dropdown and save (see §11, open item 1).
- Evaluated and rejected: the Spectrum cache node (approximate: output differs from
  native even with the same seed).

### wan-animate
Graph: `wan-animate-motion-control.json` (nightfunnels "Motion Control", an unofficial
port of RunningHub workflow 2042741071045726210; credits are kept in the graph notes).
- Inputs: a driving video with the motion, and a reference image of the person with a
  clearly visible face and hands (a photorealistic Krea 2 render works well). FPS 30,
  duration in seconds, longest side 960 (fast) in the size node.
- LoRA stack: relight 1.5, Lightning LOW 1, FastWan 1, PusaV1 1, **Instareal_low 1.0**.
  If results look harsh or oversharpened, lower Instareal toward 0.8 first.
- SeC-4B-fp8 segmentation is kept for parity with the uploaded graph; current
  SecNodes upcasts fp8 to fp16. If segmentation fails, un-comment the fp16 line in
  `models.list`, re-run `stock-setup.sh wan-animate`, and pick `SeC-4B-fp16` in the
  SeC Model Loader.
- The model loader uses `sageattn` (the stock image ships `sageattention` 1.0.6). If it
  errors on SageAttention, set attention to `sdpa`. Block swap is 30 (made for
  smaller GPUs); on 80 GB you can lower it for speed.
- Changes vs the nightfunnels download are listed in
  `config/patches/wan-animate-motion-control.json.patch.json`.

### global / flux
`global`: IntoRealism ZIT V9.0 (Z-Image Turbo checkpoint), ComfyUI_essentials,
rgthree, KJNodes, controlnet_aux, Custom-Scripts, GGUF, comfyui-ollama, `qwen3:8b`
(append `/nothink` to skip Qwen3 reasoning). `flux`: Qwen3-8B encoder, Flux2 VAE,
InstaPic LoRA; the base `black-forest-labs/FLUX.2-klein-9B` needs HF *approval*, so
it and the two Flux workflows stay commented in `models.list`.

## 6. Config formats

```
models.list        kind|filename|source|source_id|subdir|wf:ids
nodes.txt          git_url|ref|wf:ids
ollama-models.txt  model[:tag]|wf:ids
catalog.txt        id|in_all|min_comfyui|description
```

- `source`: `hf` → `repo_id::path/in/repo` (path defaults to the filename; a different
  local filename renames the file), `civitai` → model **version** id, or
  `version_id::member` to take one file out of a `.zip` release, `url` → direct link,
  `bundled` → a file under `config/`.
- `subdir` overrides the folder under `models/` (e.g. `loras/MINIMAX`). With no subdir
  but a tag the line ends `||wf:<ids>`; with neither, a trailing `|` is optional.
- `wf:` selection: `WORKFLOW=<id>` takes lines listing `<id>`; `WORKFLOW=global` takes
  untagged lines plus lines listing `global`; a full run takes untagged lines plus
  lines tagged with any id that is in `all`. Tag shared files with every workflow
  that needs them, and add `global` to keep a line in the global set.
- Adding a workflow: a `catalog.txt` line, tagged lines in the lists, and optionally a
  bundled JSON or a patch. Nothing else to keep in sync.
- Patches: `config/patches/<workflow-file>.patch.json` with `set_widgets`
  (`node_type`, optional `node_id`, `index`, `value`, optional `expect`) and
  `remove_nodes`. Applied after download; a patch whose `expect` no longer matches
  aborts, leaving the file unpatched and reported.
- Re-fetching a workflow JSON: delete `.provisioned-<name>` in the workflows folder
  (and the JSON) and re-run. Deleted workflows are not re-downloaded otherwise.
- Influencer tags (v7): a line that an Influencer_Id needs is tagged with that id, and a
  shared line gains the id in its existing tag (`wf:krea2,influencer`) instead of a second
  line for the same file. `bash scripts/provision.sh --selection <id>|global|all` prints
  what a run selects, read-only. The adult-content LoRAs sit under `loras/nsfw/`, tagged
  with their own opt-in id only (§13.12).
- Provenance block (v7): each line an Influencer_Id adds has comment lines directly
  above it (or above the first line of its run of lines): `# source: <URL>`,
  `# licence: <record>` and `# verified: YYYY-MM-DD`. A blank line ends the block. The
  record grammar is `# licence: <id> [(note)] [; conditions: <text>] [; terms: <text>]`,
  or `# licence: Civitai permissions: commercial use <uses>; credit required|not required; ...`.
  HF sources are `repo::path` and pinned URLs carry a commit SHA. A line that fails the
  Licence_Gate stays commented out below a one-line reason. The licence identifiers and
  the conditions text are in `config/influencer/licences/`; what they mean is in §13.15.
  `bash scripts/influencer.sh check-config` checks all of this offline.

## 7. How the engine works

- Phases: `nodes` (clone or pull each pack; requirements and `install.py` run when the
  commit changed or the Python environment is new, constrained to the installed
  torch/torchvision/torchaudio/torchcodec/xformers), `models`, `ollama`;
  `PHASES=comfyui` (run by `init` on request) updates core to the newest release tag,
  fast-forwards it if it is on a branch, or pins `COMFYUI_REF`.
- "Dependencies installed" records live in `<venv>/.comfyui-vastai/nodes`, next to the
  packages they describe, so a new venv on a reused volume reinstalls them.
- Downloads land in `/workspace/.staging` on the same volume, then move into place, so
  interrupted downloads resume and half-files never appear under the final name.
  Civitai tokens are handed to curl on stdin. HTML/JSON error pages are rejected.
- Locks in `/workspace/.provision/locks`: node installs run one terminal at a time,
  and two terminals never download the same file at once.
- Ollama: an already running server is used; otherwise a temporary one is started and
  stopped by PID afterwards.
- While Vast's first-boot provisioning runs (`/.provisioning` present), ComfyUI is held
  back by the template, so the scripts skip their ComfyUI stop/restart steps.
- `bash scripts/provision.sh --status [id]` lists missing files and node packs.
- Progress is always visible (`hf` bars, `curl -#`, wget bar, `git --progress`).

## 8. Disk budget

Per-workflow sizes are in §4; `all` is 281.7 GB of files plus about 10 GB of Ollama
weights. Staging needs room for the largest file in flight (21 GB for an H3 UNet, more
with parallel terminals), and outputs grow over time. Rent at least 350-400 GB for
`all`, or size the disk (or volume) for just the workflows you use.

The AI influencer pipeline's figures (`influencer` about 107.7 GB, 102.1 GB of it new,
measured 2026-09-28) and the disk size to rent for it are in the Runbook's disk table,
§13.11. `bash scripts/influencer.sh disk --id <id>` estimates any id's downloads.

## 9. Why there is no custom Docker image

Vast's stock ComfyUI image is already a prebuilt image with ComfyUI, torch,
ComfyUI-Manager, SageAttention, Jupyter, supervisor and the token proxy. The slow parts
of a session are model downloads (solved by keeping `/workspace`, §3) and node
dependencies, not the base install, and hands-off boots work through
`PROVISIONING_SCRIPT` (§2A). The earlier golden image duplicated the stock image
without Jupyter, the proxy or supervisor, and never built successfully, so v6 retired
it. It is in git history up to commit `d154df9`.

A custom image would only pay off if it were built FROM Vast's ComfyUI image with the
custom nodes and their Python packages baked in, and fresh instances were frequent.

## 10. Credentials

- `HF_TOKEN` and `CIVITAI_TOKEN` exist only as environment variables: Vast account-level
  variables (Settings → Environment Variables), a private template, or typed with
  `read -rs` in the terminal that needs them (§2B), which keeps them out of shell
  history. They are never written to files, the repo, or chat logs.
- Gated HF repos also need the licence accepted on huggingface.co with the same
  account (the NSFW UMT5 repo is flagged "not for all audiences").
- `save-progress.sh` sends `GIT_TOKEN` as an env-only HTTP header, not in the URL.
- ComfyUI has no login of its own; Vast's proxy puts a token in front of it. Don't share
  the instance link.
- Ollama listens on 127.0.0.1 only. To use it from your own machine (e.g. the
  "AI Prompt Assistant Pro" desktop app), tunnel it:
  `ssh -p <port> root@<host> -L 11434:127.0.0.1:11434`.

## 11. Open items (to confirm on the next instance)

1. **H3 loader filenames**: the all-in-one graph is login-gated, so its loaders could
   not be compared with `models.list`. On first load, check each loader (or drop the EN
   JSON into `~/Downloads` on the Mac so the list can be matched to it).
2. **wan-i2v stock files (35.3 GB)**: the stock Wan2.2 I2V high/low DiTs and stock UMT5
   are downloaded but the Remix wiring doesn't load them. Keep or drop.
3. **Bundled nightfunnels graph** in a public repo (credits kept). Keep, or fetch it at
   install time instead.
4. **Word guide**: its minimax-h3 section predates the seven-mode graph.
5. **AI influencer pipeline**: the design's open items, each with the check that resolves
   it, are in §13.18 (hosts, H3 frame count, H3 training memory, LoRA key loading, the H3
   display territory and the circumvention question), and the first-session Instance
   checks in §13.19.

## 12. Problem log

### 2026-09-29 — code review
- The stock template's `PROVISIONING_COMFYUI_WORKFLOWS` runs before `PROVISIONING_SCRIPT`,
  and a failure there ends provisioning before this repo's script starts. The setup
  steps now say to delete it (§2A).
- `save-progress.sh` on a new instance used to start an unrelated git history, so every
  push after the first instance's was rejected. It now continues the backup's history.
- A saved error page in place of a Civitai archive or workflow zip used to be reused on
  every re-run. It is now deleted, and the next run downloads again.
- A temporary Ollama server outlived a run that died and kept the ollama lock, so later
  Ollama pulls hung at "another terminal holds 'ollama'". The server no longer holds
  the lock and is stopped with the run. Details in `CHANGELOG.md`.

### 2026-09-28 — v6 rebuilt
- The first v6 (local commits `3fc2ae2` … `46adf8e`) lived only in a previous agent's
  environment and was lost; GitHub stopped at v5 (`6c973ad`). Rebuilt as documented in
  `CHANGELOG.md`.
- Every Actions build of the golden image since `c4f5e92` failed at
  `curl … ollama.com/install.sh | sh`: Ollama now ships `.tar.zst` and the installer
  exits without `zstd` (reproduced on linux/amd64). `init` installs `zstd` before
  Ollama; the golden image itself was then retired (§9).
- Comfy-Org's Qwen-Image 2.1 templates gained a 9.5 GB prompt-enhancer encoder on
  2026-09-25, one day after the templates were wired in from `main`. All template URLs
  are now pinned to commit SHAs.
- On the stock template nothing installed `extra_model_paths.yaml`, so ComfyUI never saw
  `/workspace/models`; the README also started a second ComfyUI on 8188, where Vast's
  proxy already listens. Both fixed.
- `rclone sync` in `save-progress.sh` would have deleted the backup when run from an
  empty volume; `RCLONE_CONFIG` held config text although rclone reads it as a path.
- The nightfunnels graph contains a RunningHub-only `RHHiddenNodes` feeding the LoRA
  chain; it cannot load elsewhere and was removed.
- `provision.sh` (v5) gave lines without a trailing `|` their source id as a folder, and
  its `pkill -f "ollama serve"` killed any running Ollama. Both fixed.
- Node "dependencies installed" records were first kept on `/workspace`, which survives
  a new instance while `/venv/main` does not, so a reused volume would have skipped the
  reinstall. They now live inside the venv.
- Vast's ComfyUI image checks core out at a release tag (detached HEAD), so the
  `git pull` behind `UPDATE_COMFYUI=1` could not work there. An update now moves to
  the newest release tag.

### 2026-09-24 — v1 to v5
- Actions `docker/*@v5` tags did not exist (use v4); `ensurepip` is disabled on
  Ubuntu's system python (use `get-pip.py`).
- Moody Krea 2 Mix was misfiled as a LoRA; it is a Krea 2 diffusion model. Replaced by
  the official Krea 2 Raw base (`krea/Krea-2-Raw` is approval-gated, the Comfy-Org
  repack is not).
- Wan2.2 KSampler matrix mismatch: a Qwen3-VL encoder was wired where UMT5 belongs.
- Missing `4x_foolhardy_Remacri.pth` for the Wan upscale node.
- Workflows invisible in the UI: on the stock template `ComfyUI/user` was a real
  directory while workflows went to `/workspace/comfyui-user`. Now linked by `init`
  with ComfyUI stopped.
- Flux.2 Klein 9B base needs HF approval, not just a licence click; removed.
- `HF_HUB_ENABLE_HF_TRANSFER` is deprecated; `HF_XET_HIGH_PERFORMANCE=1` instead.
- The TryCloudflare URL was unreachable from the managed browser; diagnosis moved to
  the terminal.

## 13. AI influencer pipeline (Runbook)

One fictional adult persona, one Instagram account, one week of Posts per session. The
week is planned on the Mac against the Mac's own Ollama, rendered, finished and exported
on the Instance, reviewed in the QC_Sheets and published by hand. Phase 1 delivers the
`influencer` id and the `new-calendar`, `plan`, `render`, `finish` and `export` stages;
every step that needs a later phase says so with "Not yet available — arrives in Phase N".

Commands run from the repo: `bash scripts/influencer.sh <command> [--persona <slug>]`
on the Mac, `bash /workspace/comfyui-vastai/scripts/influencer.sh <command>` on the
Instance. `--persona` can be left out while the Influencer home holds one persona. Exit
statuses: `0` done (waiting Jobs or Posts included), `1` invalid input, `2` environment,
preflight or connection failure, `3` a Job or Post failed, `4` another process holds the
lock (its PID is printed).

| Name | Instance | Mac |
|---|---|---|
| Influencer home (`INFLUENCER_HOME`) | `/workspace/influencer` | `<repo>/influencer` (git-ignored) |
| Influencer_Root | `<home>/<slug>/` | same |
| Persona_File | `<Influencer_Root>/persona.yaml` | same |
| Week folder | `<Influencer_Root>/weeks/<Week_Id>/`: `calendar.yaml` (or `.yml`, `.csv`), `prompts.yaml`, `manifest.json`, `external.yaml` | same |
| Output_Root (`INFLUENCER_OUTPUT` is its parent) | `/workspace/ComfyUI/output/influencer/<slug>/` | `<repo>/influencer/_output/<slug>/` |
| ComfyUI_API, Ollama_API | `http://127.0.0.1:18188`, `http://127.0.0.1:11434` (loopback only) | same |

A Week_Id is `YYYY-Www`, for example `2026-W40`.

### 13.1 Persona_File

Manual, then checked by `bash scripts/influencer.sh persona check`.

- Reads: `<Influencer_Root>/persona.yaml`, the Hero_Image, the Reference_Pack images and
  the LUT it names. Writes: nothing; you write the Persona_File.

1. Copy the example: `mkdir -p influencer/<slug> && cp config/influencer/examples/persona.yaml influencer/<slug>/persona.yaml`.
2. Change every value. The slug is 1-32 of `a-z`, `0-9` and `-`; the age is 25 to 99;
   the Trigger_Word is 4-32 of `a-z`, `0-9` and `_`. Give 3 or 4 locations (other counts
   warn), each with a description and optional Location_Shots, and the wardrobe.
3. Put the Hero_Image, up to 5 Reference_Pack images and the grade LUT inside the
   Influencer_Root at the relative paths the file names (for example `refs/hero.png`,
   `grade/<slug>.cube`; `config/influencer/luts/identity-33.cube` is a neutral start).
   LoRA paths are relative to `models/loras`, and the stills Character_LoRA goes under
   `models/loras/characters/<slug>/`.
4. Run `bash scripts/influencer.sh persona check`. It names every rejected field at once.

### 13.2 Closed-model character design (manual)

Manual: this repo runs no closed model.

- Reads: your own prompts. Writes: the Hero_Image and the Reference_Pack images, saved
  into the Influencer_Root, and the Identity_Paragraph in the Persona_File.

1. Design the face and body with a closed image model (for example Nano Banana Pro or
   GPT-Image-2.5), as an invented person: never a real person's likeness (§13.16).
2. Choose one canonical front image as the Hero_Image and up to 5 more angles as the
   Reference_Pack; save them as `refs/…` in the Influencer_Root.
3. Write the Identity_Paragraph (`identity:`) from the chosen image: one paragraph, no
   blank line, the same words every week.

### 13.3 Dataset curation (Not yet available — arrives in Phase 2)

Commands: `bash scripts/influencer.sh dataset check <version>` and
`bash scripts/influencer.sh dataset captions <version>`, and `bash scripts/influencer.sh refine --dataset <version>`.
Not yet available — arrives in Phase 2.

- Reads: `<Influencer_Root>/dataset/<version>/` and the Persona_File. Writes: caption
  files next to the images, and a new `<version>-refined/` folder from `refine`.

Shot list of person images, 34 in total. Curation keeps 25-35 of them, the range in which
the Training_Kit's Dataset check gives no warning:

| Images | Angle | Expression | Outfit | Lighting |
|---|---|---|---|---|
| 6 | front | neutral | outfit 1 | soft window light |
| 4 | front | smiling | outfit 2 | overcast daylight |
| 4 | left three-quarter | smiling | outfit 1 | golden hour |
| 4 | right three-quarter | neutral | outfit 2 | soft window light |
| 3 | left profile | pensive | outfit 3 | overcast daylight |
| 3 | right profile | laughing | outfit 1 | golden hour |
| 3 | left three-quarter | laughing | outfit 3 | warm indoor lamp |
| 3 | right three-quarter | pensive | outfit 3 | overcast daylight |
| 2 | front | laughing | outfit 2 | warm indoor lamp |
| 2 | front | pensive | outfit 1 | golden hour |

Location_Shots: at least 1 empty shot, with no person, of each recurring location. They
stay outside the Dataset, listed under the location's `shots:` in the Persona_File, so
no Trigger_Word caption is written for them.

### 13.4 Character_LoRA training and Test_Grid checkpoint choice (Not yet available — arrives in Phase 2)

Commands on the Instance: `bash /workspace/comfyui-vastai/scripts/train.sh install`,
`train.sh config <run> <version>`, `train.sh run <run>`, `train.sh status`,
`train.sh publish <run>`, then `bash /workspace/comfyui-vastai/scripts/influencer.sh grid`.
Not yet available — arrives in Phase 2.

- Reads: the Dataset version and the Persona_File. Writes: `<Influencer_Root>/training/<run>/`
  (`config.yaml`, `train.log`, `output/`), the published checkpoints in
  `models/loras/characters/<slug>/`, and the grid QC_Sheets under `<Output_Root>/grid/`.
- Choose the checkpoint whose Test_Grid QC_Sheet keeps the face closest to the Hero_Image
  across all 20 prompts, and set it as the Persona_File's `character_loras.stills`.
- The MiniMax H3 character-LoRA example config is experimental and unverified on one
  80 GB GPU (open item 3). Not yet available — arrives in Phase 2.

### 13.5 Weekly session

Commands: `new-calendar`, `calendar check`, `plan` on the Mac; `render`, `finish` and
`export` on the Instance.

- Reads: the Persona_File, the week's Content_Calendar, Prompt_Sheet and Manifest, and
  the Picks. Writes: `weeks/<Week_Id>/prompts.yaml` (plan), `weeks/<Week_Id>/manifest.json`
  (render, finish), and the renders and finished images under the Output_Root.

One-time Mac prerequisites:

- A Python 3.10 or later environment with the packages the `plan` stage imports (PyYAML):
  `bash scripts/check.sh` builds `.check-venv/`, which `influencer.sh` then uses.
- The Persona_File's Ollama models in the Mac's Ollama: `ollama pull qwen3:8b` for
  Instagram Posts, and `ollama pull dolphin3:8b` if you plan platform-`other` Posts (the
  `ollama:` field names them).
- A local Influencer_Root in the repo's `influencer/` folder: `influencer/<slug>/`, which
  git ignores.

Weekly checklist:

1. Write the week's Content_Calendar on the Mac: `bash scripts/influencer.sh new-calendar --week <Week_Id>`
   (add `--csv` for a spreadsheet, §13.10), edit it, then `bash scripts/influencer.sh calendar check --week <Week_Id>`.
2. Run the `plan` stage on the Mac against the Mac's local Ollama: `bash scripts/influencer.sh plan --week <Week_Id>`
   (`--replan <id>,<id>` plans those Posts again).
3. Review and edit the Prompt_Sheet (manual): `influencer/<slug>/weeks/<Week_Id>/prompts.yaml`.
   Edit scenes, alt texts, captions and hashtags, check every `review:` flag, and set
   `approved: true` on each Post to render.
4. Start the Instance (manual, in the Vast console). Rent one NVIDIA GPU of at least
   80 GB, sized for a 141-192 GB H200 or B200 so the Still, Edit and Finish stacks stay
   loaded together; the disk from §13.11. On a B200, first run open item 12's check,
   `/venv/main/bin/python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.get_arch_list())"`,
   and rent an H200 instead while the list has no `sm_100`. Install with
   `bash /workspace/comfyui-vastai/scripts/install-workflow.sh influencer` (§2).
   - Weeks with Reels need a Vast host outside the H3-excluded territories (the EU, the
     UK, the Republic of Korea and the USA); Vast's offer list shows each host's
     location (§13.15). Not yet available — arrives in Phase 3.
5. Copy the Persona_File, Content_Calendar and Prompt_Sheet into the Instance's
   Influencer_Root (manual), with the files the Persona_File names:
   `ssh -p <port> root@<host> mkdir -p /workspace/influencer`, then
   `scp -P <port> -r influencer/<slug> root@<host>:/workspace/influencer/`. The stills
   Character_LoRA the Persona_File names must be in `/workspace/models/loras/characters/<slug>/`;
   `render` stops naming it when it is missing.
6. Run the `render` stage: `bash /workspace/comfyui-vastai/scripts/influencer.sh render --week <Week_Id>`
   (`--dry-run` writes the prompts and submits nothing; `--only <id>,<id>`).
7. Record the Picks in the Prompt_Sheet (manual): in `/workspace/influencer/<slug>/weeks/<Week_Id>/prompts.yaml`
   set each Slide's `pick:` to the number of the chosen Candidate. A Post with one
   Candidate needs no Pick.
8. Run the `finish` stage: `bash /workspace/comfyui-vastai/scripts/influencer.sh finish --week <Week_Id>`.
9. Run the `reels` stage, in weeks with reel Posts: `bash /workspace/comfyui-vastai/scripts/influencer.sh reels --week <Week_Id>`.
   Not yet available — arrives in Phase 3.
10. Run the export stage: `bash /workspace/comfyui-vastai/scripts/influencer.sh export --week <Week_Id>` (§13.6).
11. Review the QC_Sheets (manual, §13.7).
12. Copy the week's Output_Root files, Prompt_Sheet and Manifest from the Instance to the
    Mac (manual):
    `scp -P <port> -r root@<host>:/workspace/ComfyUI/output/influencer/<slug>/<Week_Id> influencer/_output/<slug>/` and
    `scp -P <port> root@<host>:/workspace/influencer/<slug>/weeks/<Week_Id>/prompts.yaml root@<host>:/workspace/influencer/<slug>/weeks/<Week_Id>/manifest.json influencer/<slug>/weeks/<Week_Id>/`.
    Optionally back up with `bash /workspace/comfyui-vastai/scripts/save-progress.sh`
    (`RCLONE_REMOTE` also copies each Influencer_Root and `models/loras/characters/` to `<remote>.state`).
13. Stop the Instance by hand (manual, in the Vast console). Nothing stops it for you.

### 13.6 Export

Command: `bash /workspace/comfyui-vastai/scripts/influencer.sh export --week <Week_Id>`, with
`--external <post-id>=<path>` to register a video made outside the pipeline for a reel Post.

- Reads: the Persona_File (LUT and `grade.grain`), the Content_Calendar, Prompt_Sheet and
  Manifest, and the finished images. Writes: per Post, under `<Output_Root>/<Week_Id>/<post-id>/`
  (`<Output_Root>/<Week_Id>/other/<post-id>/` for platform `other`): `<post-id>_NN.jpg`,
  the QC_Sheet `<post-id>_qc.jpg` and the Publish_Sheet `<post-id>_publish.md`; the week
  index `<Output_Root>/<Week_Id>/index.md`; and `weeks/<Week_Id>/external.yaml`.
- Each Post is written into a temp folder and moved into place only when all its files
  succeed, so a failure leaves the earlier exports as they were. Each Post ends
  `exported`, `waiting` or `failed`, and the run prints the counts and the Output_Root.
- Reel Posts get their status but no encoded file yet: Reel encoding. Not yet available — arrives in Phase 3.

### 13.7 QC

Manual review of the QC_Sheets the export stage writes.

- Reads: `<post-id>_qc.jpg` of every Post and `index.md`. Writes: nothing, or edits to
  the Prompt_Sheet (a new Pick) before `finish` and `export` run again.
- The QC_Sheet review is the human review that the Krea 2 Community License accepts as a
  content filter (4.2), so every image is reviewed there before it is posted.
- Each row shows the Hero_Image first, then the Slide's Candidates. Check the face,
  hands, teeth, jewellery continuity and legible background text, and the Persona_File's
  signature items the sheet lists.
- A rejected Slide: record another Pick and run `finish` and `export` again; they redo
  only what changed.

### 13.8 Manual publishing

Manual: nothing in this repo posts. Publish from each Post's Publish_Sheet.

- Reads: `<post-id>_publish.md` and the exported `<post-id>_NN.jpg` files. Writes: nothing.

1. Upload the files in the order the Publish_Sheet lists, with each file's alt text.
2. Paste the caption from its fenced block unchanged; it already ends with the
   disclosure line.
3. Post on the planned date, after ticking the Instagram disclosure checklist below.

Instagram disclosure checklist (the Publish_Sheet's list, same items and order):

- [ ] "AI-generated profile" label on
- [ ] AI info label on the post
- [ ] AI disclosure in the bio
- [ ] no health claims
- [ ] no DMs implying a real person
- [ ] Instagram's nudity rules followed

Platform-`other` Posts have no checklist; they follow §13.12.

### 13.9 Character design with `character-design` (Not yet available — arrives in Phase 4)

Install: `bash /workspace/comfyui-vastai/scripts/install-workflow.sh character-design`. The
workflows run in the ComfyUI UI. Not yet available — arrives in Phase 4.

Sketch to Hero_Image:

1. Load the sketch into `lineart_example.json`.
2. Choose the LoRA: `krea2_lineart_edit_v1` for clean, closed line art, and
   `krea2_lineart_coarse_edit_v1` for rough, loose sketches (as their names say; compare
   both on your first sketch).
3. Save the chosen render into the Influencer_Root and set it as the Persona_File's
   `hero_image`, then run `persona check`.

Filling missing Dataset angles:

1. Find the missing angles in the Test_Grid QC_Sheets, or by comparing the Dataset with
   the shot list in §13.3.
2. Render a character sheet from the Hero_Image or a Dataset image with a QuadView or
   DynamicCharacterSheet workflow.
3. Crop each view into its own image file whose shorter side is at least 1024 pixels.
4. Copy the crops into a new Dataset version and leave the source Dataset unchanged.
5. Prepare the captions and run the Dataset check with the Training_Kit, and confirm that
   the new version holds 25-35 images.

### 13.10 Content_Calendar CSV format

`new-calendar --week <Week_Id> --csv` writes `weeks/<Week_Id>/calendar.csv`; a week has one
calendar file, YAML or CSV. The file is UTF-8 (a BOM is fine) with RFC 4180 quoting: one
header row naming each column once, then one row per Post. An empty cell is an absent
field, and rows whose cells are all empty are skipped. Problems name the spreadsheet row,
counting the header as row 1.

| Column | Required | Values |
|---|---|---|
| `week` | yes | the Week_Id, the same in every row |
| `id` | yes | 1-48 of `a-z`, `0-9` and `-`, unique, not `other` |
| `type` | yes | `single`, `carousel`, `story` or `reel` |
| `date` | yes | `YYYY-MM-DD`; a date outside the week only warns |
| `location` | yes | a Persona_File location name; the cell is required for single, carousel and story Posts and empty for reels |
| `outfit` | yes | a Persona_File wardrobe name; as for `location` |
| `platform` | no | `instagram` (the default) or `other` |
| `idea` | no | free text for the planner |
| `aspect` | no | `3:4` (default) or `4:5`, single and carousel Posts |
| `slides` | no | 2-20, required for a carousel |
| `candidates` | no | 1-4 (default 1), single, carousel and story Posts |
| `mode` | no | carousel `regenerate`; reel `reference` or `external` |
| `looks` | no | the Post's own look LoRAs, replacing the Persona_File's stacks |
| `duration` | no | seconds, 5-10, required for a reel |
| `source_post` | no | a reel's first-frame Post (single, carousel or story) |
| `source_slide` | no | the Slide of `source_post` (default 1) |
| `dialogue` | no | a reel's spoken lines, with `voice` |
| `voice` | no | a reel's voice clip, relative to the Influencer_Root |

The six required columns must be in the header; the cells of `location` and `outfit` are
required only for single, carousel and story Posts. List values in one cell: `looks` holds
`name[@strength]` items separated by `;` (`looks/a.safetensors@0.6;looks/b.safetensors`,
strength 1.0 when left out), and a lone `;` is the empty list, meaning no look LoRAs.
`dialogue` holds one line per line break inside the quoted cell. A reel takes exactly one
of `source_post`, mode `reference` and mode `external`.

One example row per Post type:

```csv
week,id,type,date,location,outfit,platform,idea,aspect,slides,candidates,mode,looks,duration,source_post,source_slide,dialogue,voice
2026-W40,mon-cafe,single,2026-09-28,corner-cafe,linen-set,,"first coffee of the week, reading by the window",,,2,,,,,,,
2026-W40,tue-street,carousel,2026-09-29,old-town-street,denim-knit,,a slow walk to the bakery,4:5,5,,,looks/RealisticSnapshotKrea2.safetensors@0.6,,,,,
2026-W40,wed-story,story,2026-09-30,loft-apartment,linen-set,,,,,,,,,,,,
2026-W40,thu-reel,reel,2026-10-01,,,,she turns towards the camera and laughs,,,,,,8,tue-street,2,,
```

`config/influencer/examples/calendar.csv` and `calendar.yaml` hold the same week for the
example persona.

### 13.11 Disk table

Download sizes from the Disk_Estimator, in GB of 10^9 bytes. On the Mac every item counts
as still to download. A figure marked "not yet measured" needs a networked run of the
command beside it; record the result and the date here.

| Row | GB | Measured | Command |
|---|---|---|---|
| `influencer` | 107.7, of which 102.1 new | 2026-09-28 | `bash scripts/influencer.sh disk --id influencer` |
| `influencer-video`. Not yet available — arrives in Phase 3 | not yet measured | — | `bash scripts/influencer.sh disk --id influencer-video` |
| `influencer-nsfw`. Not yet available — arrives in Phase 4 | not yet measured | — | `bash scripts/influencer.sh disk --id influencer-nsfw` |
| `character-design`. Not yet available — arrives in Phase 4 | not yet measured | — | `bash scripts/influencer.sh disk --id character-design` |
| Training base `krea/Krea-2-Raw`. Not yet available — arrives in Phase 2 | not yet measured | — | `bash scripts/influencer.sh disk --base krea/Krea-2-Raw` |
| Training base `MiniMaxAI/MiniMax-H3` (experimental). Not yet available — arrives in Phase 2 | not yet measured | — | `bash scripts/influencer.sh disk --base MiniMaxAI/MiniMax-H3` |
| `all` (the `in_all=yes` ids) | not yet measured | — | `bash scripts/influencer.sh disk --id qwen21 --id qwen21-edit --id krea2 --id edit-2509 --id krea-style --id wan-i2v --id minimax-h3 --id wan-animate --id influencer --id global` |
| Disk to rent, Phase 1: `influencer` plus 50 GB | at least 157.7 | from the 2026-09-28 figure | — |
| Disk to rent: the four Influencer_Ids combined, plus the Krea 2 Raw base model, plus 50 GB | not yet measured | — | the combined estimate of the four ids plus the base-model estimate. Not yet available — arrives in Phase 4 |

### 13.12 Instagram rules and the NSFW separation

Instagram's Community Guidelines: https://help.instagram.com/477434105621119.
Date checked: not yet checked. The summary below is unverified; read the guidelines at
the link and date this line before the first post.

- Not allowed: photos or videos of sexual intercourse, genitals, close-ups of fully nude
  buttocks, and female nipples outside the exceptions below; sexual solicitation.
- Allowed: breastfeeding, childbirth, health contexts such as post-mastectomy scarring,
  acts of protest, and nudity in paintings and sculptures.

NSFW separation:

- Adult content goes only into Posts with platform `other`, published only on platforms
  whose terms allow adult content from AI-generated creators labelled as AI.
- Every file of an `other` Post is written under `<Output_Root>/<Week_Id>/other/`, and
  every Instagram Post's files outside it.
- An Instagram Post may use only LoRAs of the Instagram_Allowlist: the stills and Reels
  ids' active models.list lines and the Persona_File's Character_LoRAs. A Post whose own
  list leaves it gets no Job and is reported.
- `plan` uses the Persona_File's `other` model (default `dolphin3:8b`) only for `other`
  Posts.
- The adult-content LoRAs install under `models/loras/nsfw/` with
  `install-workflow.sh influencer-nsfw` only. Not yet available — arrives in Phase 4.

### 13.13 ai-toolkit UI tunnel (Not yet available — arrives in Phase 2)

`train.sh ui` serves the ai-toolkit UI on `127.0.0.1:8675` only and prints this tunnel;
run it on the Mac and open `http://127.0.0.1:8675`:

```bash
ssh -p <ssh-port> root@<ssh-host> -L 8675:127.0.0.1:8675
```

### 13.14 Clip and voice rights

Driving clips and voice clips must be your own or licensed.

### 13.15 Licences

Check the licence and the Civitai commercial-use permissions of every model used for
monetised posts before the first monetised post. The Source_Checker lists them with the
Licence_Gate results for the four Influencer_Ids. Not yet available — arrives in Phase 4:

```bash
bash scripts/influencer.sh sources --id influencer --id influencer-video --id influencer-nsfw --id character-design
```

Until then, `bash scripts/influencer.sh check-config` runs the offline Licence_Gate over the
`# licence:` records in `config/models.list` and `config/ollama-models.txt`. Every item below
was checked on 2026-09-28. The design records the licence terms; it does not interpret them.

**Krea 2 Community License v1, of 22 June 2026** (checked 2026-09-28). It covers Krea 2 Raw
and Turbo, the turbo LoRA, every Comfy-Org/Krea-2 file (the Qwen3-VL-4B encoders and the
Qwen-Image VAE line included), the lineart LoRAs, and every output of a Krea 2 Job: all
stills and finished images, whatever a look LoRA's own licence says.

- Commercial use only while total annual revenue, affiliates included, over the trailing
  12 months is under US$1,000,000. At or above it an enterprise licence is needed first,
  and commercial use stops at once if the ceiling is reached during use (2.3).
- Reasonable content-filter measures, of which manual human review is one (4.2): the
  QC_Sheet review of §13.7.
- AI disclosure wherever law, regulation or platform policy requires it (4.3): Instagram's
  AI label and the disclosure line.
- No circumventing or removing security, usage restrictions, provenance or watermarking (4.1(c)).
- Revocable: it ends automatically on breach (9.1), and Krea may end it for any reason on
  30 days' notice (9.2).
- The distribution terms (Krea-prefixed names, a Notice file) apply only when a model or
  derivative is distributed, which includes sharing a Character_LoRA file, but not posting
  images or backing up to your own remote.

**Krea 2 Acceptable Use Policy** (checked 2026-09-28). It bans:

- sexual content involving minors
- non-consensual intimate imagery, including sexual deepfakes of real people
- deceptive deepfakes or impersonation of real people, and violations of publicity or personality rights
- content that is obscene or unlawful under applicable law
- circumventing safety measures, usage restrictions, content filters, provenance or watermarks (item 8)
- spam, false engagement, and presenting outputs as human-made to deceive (item 9)

**MiniMax H3 Community License Agreement** (checked 2026-09-28), over the seven shared H3
lines and every Reel. Not yet available — arrives in Phase 3.

- It covers only the Applicable Territory: the world except the EU, the UK, the Republic
  of Korea and the USA. Using, reproducing, distributing or displaying H3 Outputs outside
  it is not licensed (V.4), and the AUP bars use outside it (item 1). People in excluded
  territories may apply for a licence (II, https://platform.minimax.io/h3-license).
- H3 sessions therefore run on Vast hosts outside the excluded territories.
- Reels shown to audiences in the excluded territories are outside the licence unless
  MiniMax authorises it (open item 10). Reels are not cleared for publishing until open
  item 10 is settled.
- H3 Outputs may not be used to improve any other AI model (V.3): no H3 Output frame,
  from an exported Reel, a QC_Sheet or a still pulled from a video, goes into a Dataset
  for the Krea 2 Character_LoRA.
- Written authorisation from MiniMax above US$20M yearly revenue (IV.1).
- "MiniMax H3" displayed on the UI of a commercial product or service (IV.2); whether an
  Instagram account counts is part of open item 10.
- No circumventing MiniMax's safeguards (AUP item 5; open item 11 for "H3 NSFW Unlocked").
- A clear, prominent machine-generated disclosure on public content (AUP item 12): the
  Instagram AI label plus the disclosure line.

**Llama 3.1 Community License** (checked 2026-09-28), for `dolphin3:8b`, the default model
for `other` captions:

- a licence from Meta is needed above 700M monthly active users
- the Llama 3.1 AUP bans illegal content, sexual solicitation included, and presenting
  outputs as human-generated
- "Built with Llama" applies only when distributing the model or a product that contains it

**Civitai permissions** (checked 2026-09-28). Credit the creator of NiceGirls UltraReal,
Amateur Hour H3 and MiniMax H3 NSFW Unlocked. Aesthetic Amateur V4, the Smartphone
Photography Slider and SNOFS are commented out because Civitai lists no `Image`
commercial use for them.

**No conditions on outputs, Apache-2.0** (checked 2026-09-28): Qwen-Image-Edit-2511 and
Qwen2.5-VL-7B from the Comfy-Org repacks, the Wan 2.1 upscale2x VAE, the face detector,
the VOSR2 bundle, Lenovo UltraReal Krea 2, taeh3 and `qwen3:8b`. Kijai's Wan 2.1 VAE
states no licence; its upstream Wan 2.1 is Apache-2.0. A LoRA's permissive licence does
not lift the conditions of the base model it runs on.

**Excluded from monetised posts** (checked 2026-09-28): Qwen-Image 2.1 and the `qwen21` and
`qwen21-edit` graphs, under the Qwen Research License (research or evaluation only). The
Licence_Gate keeps its files out of every Influencer_Id.

Decisions recorded here: none yet. Open item 10 (MiniMax's written authorisation) is
recorded here before the first Reel is published, and open item 11 (the ruling on the
Fedor and "H3 NSFW Unlocked" LoRAs) before either is used.

### 13.16 Persona and exclusions

The Persona is fictional and aged 25 or older; the Persona_Reader rejects an age below 25.
Real-person likeness, face, head and body swaps, minor-coded resources and age sliders are
excluded from this repo and must not be used with it. `check-config` rejects Config_Lists
lines and template packs that name them.

### 13.17 HF_TOKEN

`HF_TOKEN` is set only as an environment variable (§10): never in ai-toolkit's settings
page, a `.env` file or any other file.

### 13.18 Open items

Unresolved after the design (its Open Items numbers). Items 10 and 11 must be settled
before Reels, or the two LoRAs of item 11, are published.

| # | Item | Check that resolves it |
|---|---|---|
| 1 | Krea 2 Identity Edit and Krea2-Reference LoRA hosts: Hugging Face answered 401 and only mirrors were found (2026-09-28) | Find a publisher-linked download page, then uncomment the lines with a provenance block (§6) and re-run the licence check of §13.15 |
| 3 | Whether H3 LoRA training fits one 80 GB GPU. Not yet available — arrives in Phase 2 | Run the experimental H3 config for 100 steps and record the peak memory here |
| 2 | H3 maximum frame count: `max_frames` starts at 243 (10 s), unmeasured at 736×1312. Not yet available — arrives in Phase 3 | Render 243 frames on an 80 GB card with nothing else loaded, and lower `max_frames` in both Reel templates if it fails; record the peak on the session's larger card too |
| 4 | Whether Krea 2 LoRAs trained with ai-toolkit load in ComfyUI without missing-key warnings. Not yet available — arrives in Phase 2 | Load a published checkpoint with `LoraLoaderModelOnly` and check the ComfyUI log for "lora key not loaded" |
| 10 | H3 display territory: the H3 licence does not cover showing H3 Outputs in the EU, UK, Republic of Korea and USA, where Instagram shows Reels; IV.2 asks for "MiniMax H3" on a commercial UI. Reels are not cleared for publishing until this is settled | Before the first Reel is published, apply at https://platform.minimax.io/h3-license and record MiniMax's written authorisation in §13.15, or reconsider the Reels model |
| 11 | Fedor ("Krea2 Filter Bypass") and "H3 NSFW Unlocked" against the circumvention clauses (Krea 2 4.1(c) and AUP item 8, H3 AUP item 5); both pass the Licence_Gate, which cannot decide this | Before either is used for an `other` Post, decide and record the decision in §13.15. A LoRA judged to be circumvention is commented out below a reason comment and removed from the Persona_File's `other` stacks |

### 13.19 First-session Instance checklist

The design's Instance acceptance checks, run by hand on the first session: none of them
can run on the Mac, so they are documentation, not tests. A Phase 1 first session runs
items 1 to 4, 9, 10 and the torch half of item 11.

1. `bash /workspace/comfyui-vastai/scripts/install-workflow.sh influencer` on a fresh
   Instance, then `bash /workspace/comfyui-vastai/scripts/provision.sh --status influencer`.
   Watch the install's summary. Pass: exit 0, and the status lists no missing file or node pack.
2. Run `bash /workspace/comfyui-vastai/scripts/install-workflow.sh influencer` a second time.
   Watch the log for downloads, clones and Ollama pulls. Pass: none, and exit 0 (node packs
   may still `git pull`).
3. Run the first `finish` Job (`bash /workspace/comfyui-vastai/scripts/influencer.sh finish --week <Week_Id>`)
   while watching `ls -la /workspace/models/vosr2/VOSR2` and the ComfyUI log. Pass: VOSR2
   downloads nothing; its files were fetched at install.
4. Finish a Pick with no visible face (for example a back view). Pass: `finish` records the
   Job done and writes the finished image, with no failure.
5. `train.sh install` twice, then `train.sh ui`, then `cat /proc/net/tcp`. Pass: the second
   install changes nothing, and port 8675 (hex `21E3`) listens on `0100007F` (127.0.0.1)
   only. Not yet available — arrives in Phase 2.
6. One Krea 2 LoRA training run, `train.sh publish`, then `grid`. Pass: the checkpoints are
   in `models/loras/characters/<slug>/` and a grid QC_Sheet is written for each.
   Not yet available — arrives in Phase 2.
7. `install-workflow.sh influencer-video`, then one Reel of each kind (first frame from a
   Slide, and reference). Pass: both export as Reels that meet the H3 rules.
   Not yet available — arrives in Phase 3.
8. The `influencer-nsfw` and `character-design` installs. Pass: each exits 0 and its
   `--status` lists nothing missing. Not yet available — arrives in Phase 4.
9. One carousel at each aspect. Pass: Slides 2..N come out at 864×1152 (3:4) or 896×1120
   (4:5) and finish at ×3, 2592×3456 or 2688×3360, with the QC review of open-item check 14
   below.
10. Peak VRAM of each template: run
    `nvidia-smi --query-gpu=memory.used --format=csv -lms 500` through one Job of each of
    Still, Edit and Finish. Pass: the peaks are recorded here, and each template also runs
    alone on an 80 GB card.
11. On a B200 Instance:
    - `/venv/main/bin/python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.get_arch_list())"`.
      Pass: the list has `sm_100`; otherwise rent an H200 until the template's torch has it.
    - `train.sh install` picks the `cu130` build. Not yet available — arrives in Phase 2.

Open-item checks for the same session:

- FaceDetailer (open item 5): A/B three denoise values around 0.4 on 10 Picks. If the
  Impact Subpack refuses `face_yolov8m.pt` under the Instance's torch, check the file's
  SHA-256 against the one recorded in its `config/models.list` comment, then whitelist it by hand.
- RES4LYF sampler names at the current HEAD (open item 7): the first `render` Job. Pass:
  ComfyUI accepts `exponential/res_2s`, `multistep/deis_3m` and `bong_tangent`; an unknown
  name is rejected with a clear message.
- H3 frame count on the minimum 80 GB card (open item 2). Not yet available — arrives in Phase 3.
- H3 training memory (open item 3). Not yet available — arrives in Phase 2.
- B200 torch (open item 12): item 11 above, with an H200 as the fallback host.
- Real VRAM (open item 13): item 10 above; correct the design's Performance table with the peaks.
- Edit size and upscale factor (open item 14): render 3 carousels per aspect and compare
  Slides 2..N with Slide 1 for identity, outfit and framing drift, and inspect finished
  edit Slides at 100 % for grid patterns. If framing drifts, try 912×1216; if
  Qwen-Image-Edit-2511 itself drifts, fall back to Qwen-Image-Edit-2509.

Open items 10 and 11 are decisions rather than Instance checks: see §13.15 for MiniMax's
authorisation before the first Reel is published, and the ruling on the Fedor and
"H3 NSFW Unlocked" LoRAs before either is used.
