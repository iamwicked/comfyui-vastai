# comfyui-vastai — Reference (v6)

Aadi's reproducible ComfyUI + Ollama workstation on Vast.ai. Idempotent scripts turn a
**stock Vast.ai ComfyUI template** instance into the full setup: one command installs
everything one workflow needs (models, LoRAs, custom nodes, Ollama models, workflow
JSON). Built for one 80 GB GPU (A100 / H100 class).

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
| `scripts/stock-setup.sh` | Fresh-instance entrypoint: `init`, `<id>`, `finish`, `all`, `status`, `restart`, `list` |
| `scripts/install-workflow.sh` | One command per workflow (what `stock-setup.sh <id>` runs) |
| `scripts/provision.sh` | The engine: nodes, models, Ollama models; `--status`, `--list-workflows` |
| `scripts/common.sh` | Shared helpers (supervisor, model paths, user dir, Ollama service) |
| `scripts/patch-workflow.py` | Applies `config/patches/*.patch.json` |
| `extra_model_paths.yaml` | Points ComfyUI at `/workspace/models` |
| `Dockerfile`, `scripts/entrypoint.sh`, `scripts/start.sh`, `vastai/` | Golden image (alternative path, §8) |
| `scripts/save-progress.sh` | Optional off-box backup (git for workflows, rclone for outputs) |

Instance paths (stock template): ComfyUI `/workspace/ComfyUI` on **port 18188** behind
Vast's proxy (the Open button), python `/venv/main/bin/python`, repo
`/workspace/comfyui-vastai`, models `/workspace/models`, user dir
`/workspace/comfyui-user` (linked as `ComfyUI/user`), workflows
`/workspace/comfyui-user/default/workflows`, Ollama weights `/workspace/ollama-models`.
The template runs ComfyUI under supervisor; the scripts restart that process and
never start a second server.

## 2. Fresh stock-template instance (runbook)

Rent Vast's stock **ComfyUI** template on a single 80 GB GPU. Give it enough disk for
the workflows you want (§7) plus about 50 GB of headroom.

**Terminal 1** (Jupyter → Terminal). The token prompts keep the values out of shell
history; paste each token when asked:

```bash
read -rsp "HF token: " HF_TOKEN; echo; read -rsp "Civitai token: " CIVITAI_TOKEN; echo; export HF_TOKEN CIVITAI_TOKEN
[ -d /workspace/comfyui-vastai/.git ] || git clone --progress https://github.com/iamwicked/comfyui-vastai.git /workspace/comfyui-vastai
bash /workspace/comfyui-vastai/scripts/stock-setup.sh init
```

`init` installs missing tools (hf CLI, zstd, unzip, wget), installs
`extra_model_paths.yaml`, links the user dir, installs Ollama and registers it with
supervisor on 127.0.0.1, then restarts ComfyUI. It warns when ComfyUI core is older
than a workflow needs; `UPDATE_COMFYUI=1 bash .../stock-setup.sh init` updates core.

**One terminal per workflow**, in parallel if you like (export the tokens in each):

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

## 3. Workflow catalog

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
**281.7 GB** of files plus about 10 GB of Ollama models.

## 4. Workflow notes

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
  matching one from its dropdown and save (see §10, open item 1).
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

## 5. Config formats

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

## 6. How the engine works

- Phases: `nodes` (clone or pull each pack; requirements and `install.py` only when the
  commit changed, constrained to the installed torch/torchvision/torchaudio/xformers),
  `models`, `ollama`; `PHASES=comfyui` updates ComfyUI core.
- Downloads land in `/workspace/.staging` on the same volume, then move into place, so
  interrupted downloads resume and half-files never appear under the final name.
  Civitai tokens are handed to curl on stdin. HTML/JSON error pages are rejected.
- Locks in `/workspace/.provision/locks`: node installs run one terminal at a time,
  and two terminals never download the same file at once.
- Ollama: an already running server is used; otherwise a temporary one is started and
  stopped by PID afterwards.
- `bash scripts/provision.sh --status [id]` lists missing files and node packs.
- Progress is always visible (`hf` bars, `curl -#`, wget bar, `git --progress`).

## 7. Disk budget

Per-workflow sizes are in §3; `all` is 281.7 GB of files plus about 10 GB of Ollama
weights. Staging needs room for the largest file in flight (21 GB for an H3 UNet, more
with parallel terminals), and outputs grow over time. Rent at least 350-400 GB for
`all`, or size the disk for just the workflows you plan to use.

## 8. Golden image (alternative path)

`Dockerfile` → GitHub Actions → `ghcr.io/iamwicked/comfyui-vastai:latest`, booted by
`entrypoint.sh` (SSH, persistent layout, `BOOT_WORKFLOWS` provisioning, default
`global`) and `start.sh` (Ollama on 127.0.0.1, ComfyUI on 8188). Status: every build
up to v5 failed because the Ollama installer needs `zstd`; v6 adds it, and the next
push will show whether the build now passes (disk cleanup and non-fatal cache export
were added too). After the first green build, make the GHCR package public or give
Vast registry credentials. Template fields and CLI: `vastai/template.md`,
`vastai/setup.sh` (entrypoint launch mode via `--args`).

ComfyUI has no login. On the stock template Vast's proxy puts a token in front of it;
the golden image maps port 8188 directly, so keep that URL private.

## 9. Credentials

- `HF_TOKEN` and `CIVITAI_TOKEN` exist only as environment variables in the terminal
  that needs them. Enter them with `read -rs` (§2) so they stay out of shell history.
  They are never written to files, the repo, or chat logs.
- Gated HF repos also need the licence accepted on huggingface.co with the same
  account (the NSFW UMT5 repo is flagged "not for all audiences").
- `save-progress.sh` sends `GIT_TOKEN` as an env-only HTTP header, not in the URL.
- Ollama listens on 127.0.0.1 only. To use it from your own machine (e.g. the
  "AI Prompt Assistant Pro" desktop app), tunnel it:
  `ssh -p <port> root@<host> -L 11434:127.0.0.1:11434`.

## 10. Open items (to confirm on the next instance)

1. **H3 loader filenames**: the all-in-one graph is login-gated, so its loaders could
   not be compared with `models.list`. On first load, check each loader (or drop the EN
   JSON into `~/Downloads` on the Mac so the list can be matched to it).
2. **wan-i2v stock files (35.3 GB)**: the stock Wan2.2 I2V high/low DiTs and stock UMT5
   are downloaded but the Remix wiring doesn't load them. Keep or drop.
3. **Golden image**: fixed but unproven until the next Actions run; keep or retire.
4. **Bundled nightfunnels graph** in a public repo (credits kept). Keep, or fetch it at
   install time instead.
5. **Word guide**: its minimax-h3 section predates the seven-mode graph.

## 11. Problem log

### 2026-09-28 — v6 rebuilt
- The first v6 (local commits `3fc2ae2` … `46adf8e`) lived only in a previous agent's
  environment and was lost; GitHub stopped at v5 (`6c973ad`). Rebuilt as documented in
  `CHANGELOG.md`.
- Every Actions build since `c4f5e92` failed at `curl … ollama.com/install.sh | sh`:
  Ollama now ships `.tar.zst` and the installer exits without `zstd`. Reproduced in
  `ubuntu:22.04`. Fixed in the Dockerfile and in `init`.
- Comfy-Org's Qwen-Image 2.1 templates gained a 9.5 GB prompt-enhancer encoder on
  2026-09-25, one day after the templates were wired in from `main`. All template URLs
  are now pinned to commit SHAs.
- On the stock template nothing installed `extra_model_paths.yaml`, so ComfyUI never saw
  `/workspace/models`; the README also started a second ComfyUI on 8188, where Vast's
  proxy already listens. Both fixed.
- `rclone sync` in `save-progress.sh` would have deleted the backup when run from an
  empty volume; `RCLONE_CONFIG` held config text although rclone reads it as a path.
- `vastai/setup.sh` used `--ssh`, which makes Vast skip the image entrypoint, and an
  invalid GPU name (`RTX_A100_80GB`).
- The nightfunnels graph contains a RunningHub-only `RHHiddenNodes` feeding the LoRA
  chain; it cannot load elsewhere and was removed.
- `provision.sh` (v5) gave lines without a trailing `|` their source id as a folder, and
  its `pkill -f "ollama serve"` killed any running Ollama. Both fixed.

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
