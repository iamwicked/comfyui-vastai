# Changelog

Version numbers follow the push bundles (`comfyui-vastai-v4.bundle`, `-v5.bundle`, ...).
The current version is **v7**.

## v7 — 2026-10-01

The AI influencer pipeline, Phase 1 (stills). Runbook: `REFERENCE.md` §13.

### New ids
- **influencer** (in `all`): Krea 2 Raw bf16 realism recipe with the turbo LoRA and
  Qwen3-VL-4B bf16 encoder, Qwen-Image-Edit-2511 bf16 and Qwen2.5-VL-7B for carousel
  Slides, Impact FaceDetailer with `face_yolov8m.pt`, VOSR2 finish, DeGrid, the Wan 2.1
  VAEs, the licence-cleared look LoRAs, and the realism guide graph. About 107.7 GB, 102.1 GB
  of it new (2026-09-28).
- **influencer-video** (opt-in): MiniMax H3 Reels. Not yet available — arrives in Phase 3.
- **influencer-nsfw** (opt-in): adult-content LoRAs under `loras/nsfw/` and `dolphin3:8b`.
  Not yet available — arrives in Phase 4.
- **character-design** (opt-in): sketch-to-Hero_Image and character sheets.
  Not yet available — arrives in Phase 4.

### New tools
- `scripts/influencer.sh`, the launcher of the `scripts/influencer/` package, with the
  Phase 1 subcommands `new-calendar`, `plan`, `render`, `finish`, `export`,
  `persona check`, `calendar check`, `check-config` and `disk --id`.
- The Export_Tool (`influencer.sh export`): graded JPEG exports, QC_Sheets, Publish_Sheets
  and the week index, written atomically per Post.
- The Config_Checker (`influencer.sh check-config`): every Config_Lists, template, example,
  token, private-data, provenance and `.gitignore` rule offline, with the Licence_Gate over
  the `# licence:` records.
- The Disk_Estimator (`influencer.sh disk --id`), also printed by `install-workflow.sh`
  before each install.
- The Check_Suite (`scripts/check.sh`): `check-config`, the tests, `bash -n` and shellcheck
  in one command, in its own pinned `.check-venv/`.
- The Provisioner's read-only `--selection <id>|global|all` mode (`scripts/provision.sh`).
- The Training_Kit (`scripts/train.sh`). Not yet available — arrives in Phase 2.
- The `reels` stage. Not yet available — arrives in Phase 3.
- The Source_Checker (`influencer.sh sources`). Not yet available — arrives in Phase 4.

### Engine, backups and repo
- The Provisioner (`provision.sh`, `install-workflow.sh`, `common.sh`): Civitai early-access
  handling, the DeGrid guard, the VOSR2 prefetch and link, and the disk-estimate hook.
- `save-progress.sh`: with `RCLONE_REMOTE` set, each Influencer_Root and
  `models/loras/characters/` are also copied, each into its own folder under
  `<RCLONE_REMOTE>.state`, which the outputs copy never writes into.
- `.gitignore`: `/influencer/`, `/models/loras/characters/`, `/.check-venv/`,
  `.pytest_cache/` and `.hypothesis/`.

## v6 — rebuilt 2026-09-28 (on top of v5 / `6c973ad`)

The first v6 (local commits `3fc2ae2` … `46adf8e`) never reached GitHub and its
environment is gone. This v6 was rebuilt from the handoff notes, the workflow files
in `~/Downloads`, and live checks of every source. All 57 `models.list` entries were
verified against HuggingFace / Civitai / GitHub on 2026-09-28.

### New and changed workflows
- **minimax-h3**: MiniMax H3 all-in-one graph (Civitai 2913151, EN version 3295505),
  seven modes + lip sync. FL2VA and REF2VA pruned int8 UNets, Qwen3-VL 32B nvfp4 text
  encoder, int8 video VAE, fp32 audio VAE, turbo 8-step LoRA in `loras/MINIMAX/`, taeh3
  preview decoder, nodes rgthree / KJNodes / VideoHelperSuite / WhatDreamsCost.
  Needs ComfyUI >= 0.30.0 (`init` warns and `UPDATE_COMFYUI=1` updates core).
- **wan-animate**: nightfunnels "Motion Control" Wan2.2 Animate graph, bundled as
  `config/workflows/wan-animate-motion-control.json`: Animate 14B fp8 (KJ v2), UMT5 bf16,
  clip_vision_h, relight / Lightning / FastWan / Pusa LoRAs, **Instareal_low @ 1.0** in
  place of Fun-InP HPS2.1 @ 0.5, ViTPose-H + YOLOv10m detection, SeC-4B fp8 segmentation,
  nine node packs. The RunningHub-only `RHHiddenNodes` node was removed (it cannot load
  outside RunningHub and blocked the graph).
- **krea2**: now the realism/NSFW set with **Dolphin3 prompt enhancement via Ollama**:
  Simple Krea2 NSFW+ LLM graph (Civitai 3087685, patched after download to use
  `dolphin3:8b` on the local Ollama), Krea 2 Turbo fp8, NSFW+ LoRA (Civitai 3084588),
  darkbrush LoRA, plus the existing Krea 2 Raw + Lenovo UltraReal.
- **qwen21 / qwen21-edit**: templates pinned to Comfy-Org revision `371a7b7`. The newer
  revision (2026-09-25) adds a 9.5 GB prompt-enhancer encoder that nothing downloaded.
- **edit-2509, krea-style, krea2**: templates pinned to `98fd32c`.
- **wan-i2v**: low-noise Remix DiT now comes from the original `FX-FeiHou/wan2.2-Remix`
  repo (byte-identical to the old `limiao1666` mirror).

### Hands-off first boot (`scripts/vast-provision.sh`)
- New target for Vast's `PROVISIONING_SCRIPT`. In a copy of the stock ComfyUI template,
  set `PROVISIONING_SCRIPT` to the script's raw URL and `WORKFLOWS` to the ids to
  install, comma-separated (`all` = everything in `all`). On first boot it clones or updates the repo,
  runs `init`, each workflow in turn, then `finish`, and prints a summary of what to
  re-run. It always exits 0, so ComfyUI starts even when a download failed and Vast
  marks provisioning complete instead of retrying it.
- While Vast's `/.provisioning` marker exists (the template holds ComfyUI back until
  provisioning ends), the scripts skip their ComfyUI stop/start/restart steps.

### Engine (`scripts/provision.sh`)
- Download progress is always visible: `hf` progress bars, `curl -#`,
  `wget --progress=bar:force:noscroll`, `git --progress`.
- One catalog (`config/catalog.txt`) replaces the three hand-synced workflow lists.
- `wf:global,<ids>` installs a line for `global` and each listed workflow, so every
  per-workflow install is complete on its own; full runs skip opt-in ids (`flux`).
- Downloads stage in `/workspace/.staging` (same volume as the models): interrupted
  HF / Civitai / URL downloads resume on the next run, and the final move is atomic.
- Per-file and per-phase locks (`flock`) make parallel terminals safe even when two
  workflows share files or node packs.
- Civitai: token passed on stdin (not in the process list), resume support, error
  pages detected instead of being saved as models; `version::member` extracts one
  file from a `.zip` release (Instareal).
- New sources: `bundled` (files under `config/`). New kinds: `vae_approx`,
  `clip_vision`, `detection`, `sams`, `model_patch`, `audio_encoder`, `latent_upscale`.
- Workflow patches (`config/patches/<file>.patch.json`, `scripts/patch-workflow.py`),
  idempotent, applied after download.
- Ollama models are per-workflow (`ollama-models.txt` takes `wf:` tags). A temporary
  Ollama server is stopped by PID, so a running server is never killed.
- Custom-node requirements install with a torch/torchvision/torchaudio/torchcodec/xformers
  constraint, and only when the node's commit changed or the Python environment is
  new. The "installed" stamps live in `<venv>/.comfyui-vastai/nodes`; kept on
  `/workspace` they outlived the venv, so a reused volume would have skipped the
  reinstall into the fresh `/venv/main`.
- ComfyUI core changes only in `PHASES=comfyui` (`init` with `UPDATE_COMFYUI=1` or
  `COMFYUI_REF`): Vast's image sits on a release tag (detached HEAD, where `git pull`
  fails), so an update moves to the newest release tag; a branch checkout is
  fast-forwarded; `COMFYUI_REF` pins a tag or commit.
- `--status [id]` reports per workflow what is installed and what is missing.
- Remote list overrides (`MODELS_LIST_URL`, `NODES_TXT_URL`, `OLLAMA_MODELS_TXT_URL`)
  removed along with the golden image they were made for.
- Fixed: lines without a trailing `|` got the source id as their folder.

### Stock template (`scripts/stock-setup.sh`, `install-workflow.sh`, `common.sh`)
- `init` installs `extra_model_paths.yaml` into ComfyUI (without it ComfyUI never saw
  `/workspace/models`), installs `zstd` before Ollama, links the user dir with
  ComfyUI stopped, registers Ollama with supervisor on 127.0.0.1, and restarts the
  template's own ComfyUI (port 18188). Nothing starts a second ComfyUI any more.
- `finish` = global nodes + global Ollama models + restart + status. New `status`,
  `restart`, `dl <id>`. Running with no argument prints help (it used to run `all`,
  which is now about 290 GB).
- No more `python3.11` symlink shim or `/opt` fallbacks: the scripts target
  `/workspace/ComfyUI` and `/venv/main/bin/python`.

### Golden image retired
- Removed `Dockerfile`, `scripts/entrypoint.sh`, `scripts/start.sh`,
  `.github/workflows/build.yml`, `vastai/setup.sh` and `vastai/template.md`; they
  remain in git history up to `d154df9`. Vast's stock image already ships ComfyUI,
  torch, Jupyter, supervisor and the token proxy; the golden image dropped the last
  three, and every Actions build since `c4f5e92` failed (the Ollama installer needs
  `zstd`). Hands-off boots use `PROVISIONING_SCRIPT` instead. Reasoning, and when a
  custom image would pay off: `REFERENCE.md` §9.

### Backups (`scripts/save-progress.sh`)
- `rclone copy` instead of `sync` (sync from an empty volume would delete the backup).
- Inline rclone config moved to `SAVE_RCLONE_CONF` (rclone reads `RCLONE_CONFIG` as a path).
- Git token sent as an env-only HTTP header instead of inside the push URL.
- `OUTPUT_DIR` defaults to `/workspace/ComfyUI/output`.

### Docs
- `README.md` and `REFERENCE.md` rewritten around the two ways to set up an instance
  (hands-off and by hand) and what each way of ending a session means for the next
  one. The `/workspace/.vastai-tokens` helper file is gone (tokens are never written
  to disk).

### Fixes from the code review
- Engine:
  - A Civitai archive or workflow zip that isn't a valid zip (usually a saved error page)
    is deleted instead of being resumed, and failing, on every re-run.
  - The lists are read on fd 3, so nothing inside the loops (git, pip, a node's
    `install.py`) can swallow the remaining lines from stdin.
  - `GIT_TERMINAL_PROMPT=0`: a node repo that went private or was deleted fails instead
    of waiting at a password prompt.
  - Downloads honour `MODELS_DIR`, as `--status` already did.
  - A temporary Ollama server no longer inherits the ollama lock, and it is stopped when
    a run ends early (error, Ctrl-C, SIGTERM). Before, a server left behind by a dead
    run held the lock, so every later Ollama pull waited for it forever.
  - A failing `ollama list` no longer ends the run.
  - An unknown `PHASES` entry is an error instead of a silent no-op. Spaces are allowed
    (`nodes, models`); before, the phase after the space was silently skipped.
  - A failed workflow patch explains how to retry it.
- `init` no longer stops without a message when `comfyui_version.py` is missing, and says
  so when ComfyUI is not at `COMFYUI_DIR`.
- `save-progress.sh`:
  - On a new instance it continues the backup repo's history, so the push is a
    fast-forward instead of being rejected, and workflows that only the backup has stay
    in it.
  - It exits non-zero when the push or the copy fails.
  - `rclone.conf` is created 0600 from the start.
- `patch-workflow.py` keeps the workflow file's permissions.
- The scripts pass ShellCheck at its default severity. The Check_Suite's `shellcheck`
  step uses that severity, and it failed on two info-level notes.
- Hands-off setup docs: delete the template's `PROVISIONING_COMFYUI_WORKFLOWS` row. When
  its downloads fail, Vast never runs `PROVISIONING_SCRIPT`.

## v5 — `6c973ad` (2026-09-24)
Downloads organised by workflow: `wf:` tags, `WORKFLOW=<id>`, `install-workflow.sh`,
terminal sharding removed.

## v4 — `d378f74` (2026-09-24)
Flux.2 Klein 9B base removed (HF approval required); encoder, VAE and InstaPic LoRA kept.
`HF_XET_HIGH_PERFORMANCE` replaces the deprecated hf_transfer flag. `stock-setup.sh` added.

## Earlier (2026-09-24)
Golden image, `provision.sh`, persistent layout (`d95ec69`); Actions and Dockerfile
fixes (`61cab40`, `c4f5e92`); workflows for Qwen-Image 2.1, Krea 2 and image editing,
RES4LYF and comfyui-ollama nodes (`38d5ad8`, `77a4a26`); `REFERENCE.md` (`2fd7843`).
