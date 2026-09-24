# comfyui-vastai — Reference

Reproducible **ComfyUI + Ollama** setup on a stock Vast.ai instance: a golden
Docker image, an idempotent provisioning script, and persistent volumes, so
every session boots in ~1 minute with all models, nodes, and workflows intact.

Repo: `https://github.com/iamwicked/comfyui-vastai` · Image:
`ghcr.io/iamwicked/comfyui-vastai:latest` (built by GitHub Actions on push to
`main`).

---

## 1. How it works

| Piece | Role |
|---|---|
| `Dockerfile` | Golden image: CUDA 12.8, PyTorch cu128, ComfyUI, ComfyUI-Manager, Ollama, rclone, sshd |
| `scripts/provision.sh` | Idempotent setup: updates custom nodes, downloads every entry in `config/models.list` (skips files already present), pulls Ollama models. Safe to re-run anytime. |
| `scripts/entrypoint.sh` | Boot sequence: SSH setup → persistent layout → provision → start |
| `scripts/start.sh` | Launches Ollama on `:11434`, then ComfyUI on `:8188` |
| `scripts/save-progress.sh` | End-of-session: git-push workflows, rclone-sync outputs |
| `config/models.list` | The manifest: `kind|filename|source|source_id|subdir`. The single source of truth for every model + workflow |
| `config/nodes.txt` | Custom-node git URLs installed by `provision.sh` |
| `config/ollama-models.txt` | LLM models to `ollama pull` |
| `extra_model_paths.yaml` | Points ComfyUI at `/workspace/models` (the volume) |
| `vastai/template.md` | Exact Vast.ai template fields + volume/CLI reference |

### Persistence model

| Thing | Where it lives | Survives destroy? |
|---|---|---|
| ComfyUI app, torch, node code | Docker image | yes (immutable) |
| Models, Ollama weights | `/workspace` volume | yes (if volume re-linked) |
| Workflows, settings (`user/`) | `/workspace/comfyui-user` (symlinked into ComfyUI) | yes |
| Generated images | `/workspace/output` (symlinked) | yes |
| Off-box backup | your repo / cloud via `save-progress.sh` | yes |

**Short break:** just STOP the instance (container comes back whole).
**Long break:** DESTROY, then re-rent the *same machine* and link the existing
volume. `provision.sh` is the reinstall path for a fresh volume.

### Credentials policy

- Tokens (`HF_TOKEN`, `CIVITAI_TOKEN`) are passed as **env vars on the instance**,
  never baked into the image or committed to the repo.
- Raw tokens are never stored in chat logs, memory files, or the repo.
- Local helper file `/workspace/.vastai-tokens` on the instance holds them
  during setup — delete it (`rm /workspace/.vastai-tokens`) only after every
  required download has finished.

---

## 2. Problems faced and how they were solved

### 2026-09-24 — GitHub Actions build failure (`docker/*` v5 tags)
First automated build failed: the workflow referenced `docker/*` action tags
that don't exist. Fixed to **v4** tags and re-pushed.

### 2026-09-24 — Docker build failure (`ensurepip` disabled)
Build failed on `ensurepip` — Debian/Ubuntu system Python disables it. Fixed by
installing pip via `get-pip.py` instead.

### 2026-09-24 — Moody Krea 2 Mix misclassified
The file was first treated as a LoRA. It is a **diffusion model** (Krea 2
family), so it was reclassified: the Krea 2 Raw base
(`krea2_raw_fp8_scaled.safetensors`, from the official `Comfy-Org/Krea-2`
repack — the `krea/Krea-2-Raw` repo is approval-gated and not needed) plus the
Qwen3-VL 4B encoder (`qwen3vl_4b_fp8_scaled.safetensors`) and the Qwen Image VAE
(`qwen_image_vae.safetensors`). The obsolete
`diffusion_models/moodyKrea2Mix_v70BF16.safetensors` was removed from the plan.

### 2026-09-24 — Wan2.2 KSampler matrix mismatch
The Wan workflow errored because the text encoder wired in was a Qwen3-VL
encoder instead of UMT5. Corrected mapping:
low loader → `Wan2.2_Remix_NSFW_i2v_14b_low_lighting_v2.0.safetensors`,
high loader → `Wan2.2_Remix_NSFW_i2v_14b_high_lighting_v2.0.safetensors`,
CLIP → `nsfw_wan_umt5-xxl_fp8_scaled.safetensors`,
VAE → `wan_2.1_vae.safetensors`.

### 2026-09-24 — Missing upscaler for the Wan workflow
The Wan2.2-Remix "Video Upscale With Model" node failed on the missing
`/workspace/models/upscale_models/4x_foolhardy_Remacri.pth`. Added
`upscale|4x_foolhardy_Remacri.pth|hf|FacehugmanIII/4x_foolhardy_Remacri|` to
`models.list`.

### 2026-09-24 — Workflows invisible in the UI (the symlink bug)
**Symptom:** the ComfyUI sidebar listed only the Wan workflow, although three
more had been provisioned.
**Root cause:** on the stock template `/workspace/ComfyUI/user` was a **real
directory** (containing only 2 files), while provisioned workflows lived in
`/workspace/comfyui-user/default/workflows/`. ComfyUI reads workflows from its
own `user/` dir, so it never saw the others.
**Fix** (run with ComfyUI stopped):
```bash
cp -rn /workspace/ComfyUI/user/. /workspace/comfyui-user/
rm -rf /workspace/ComfyUI/user
ln -s /workspace/comfyui-user /workspace/ComfyUI/user
```
Then restart ComfyUI and hard-refresh the browser tab.

### 2026-09-24 — Stock-template vs image path mismatch (open)
The automation was written for `/opt/ComfyUI` (Python 3.11); the stock Vast
template runs ComfyUI from `/workspace/ComfyUI` (`/venv/main`, Python 3.12).
`provision.sh`/`start.sh` should be taught to detect and support the stock
path. Until then, provision manually with `bash scripts/provision.sh` and the
symlink fix above.

### 2026-09-24 — Lenovo LoRA variants resolved
- **Lenovo Qwen2.1:** resolved via Civitai version ID **3349565**
  ("v1.0 Qwen2.1", baseModel "Qwen 2", published 2026-09-22). Wired as
  `lora|lenovo_qwen21.safetensors|civitai|3349565|` with Qwen-Image-2.1 base
  weights from `Comfy-Org/Qwen-Image-2.1` (verified against the creator's own
  example workflow in the version metadata). Creator settings: cfg 3–4
  (1 if slop), beta scheduler, res_2s/res_2m/res_multistep samplers
  (RES4LYF nodes), example at 50 steps / cfg 4 / res_2s, LoRA 0.7.
- **Lenovo Krea 2:** taken straight from the creator's HuggingFace
  (`Danrisi/Lenovo_Krea2::lenovo_krea2_3000.safetensors`); safetensors header
  confirms it was trained on the Krea 2 **Raw** base. Creator settings:
  strength 0.8 on Raw, 1.2–2.0 on Turbo; cfg 4 on Raw, cfg 1 + Turbo LoRA.

### 2026-09-24 — TryCloudflare URL unreachable from the managed browser
Repeated HTTPS/HTTP attempts to the instance's TryCloudflare URL failed from
the managed browser route (it may still work from a personal device). All
diagnosis since has been terminal-based.

### 2026-09-24 — Flux.2 Klein 9B stack removed (HF approval required)
`black-forest-labs/FLUX.2-klein-9B` doesn't just need a license click-through —
it requires **approval**: `hf download` failed with "Access denied. This
repository requires approval." Removed the base, its Qwen3-8B encoder, the Flux2
VAE, the InstaPic LoRA (`V3_flux_klein.safetensors`), and both Flux workflows
(text-to-image + image edit). The exact re-add lines are commented in
`config/models.list` if approval is ever granted.
Also fixed the `FutureWarning` in `provision.sh`: `HF_HUB_ENABLE_HF_TRANSFER`
is deprecated and ignored — it now sets `HF_XET_HIGH_PERFORMANCE=1` instead.

---

## 3. Model inventory (from `config/models.list`)

### Image generation stacks

| Stack | Diffusion / base | Text encoder | VAE | LoRA |
|---|---|---|---|---|
| Qwen-Image 2.1 + Lenovo | `qwen_image_2.1_int8_convrot.safetensors` | `qwen3vl_8b_int8_convrot.safetensors` | `qwen_image_2.1_vae_bf16.safetensors` | `lenovo_qwen21.safetensors` (Civitai 3349565) |
| Krea 2 Raw + Lenovo | `krea2_raw_fp8_scaled.safetensors` | `qwen3vl_4b_fp8_scaled.safetensors` | `qwen_image_vae.safetensors` | `lenovo_krea2_3000.safetensors` (creator HF) |
| Krea 2 Turbo (style-ref) | `krea2_turbo_int8_convrot.safetensors` | *(same as Raw)* | *(same as Raw)* | `krea2_style_reference.safetensors` (official, not Lenovo) |
| Qwen-Image-Edit 2509 | `qwen_image_edit_2509_fp8_e4m3fn.safetensors` (~20 GB) | `qwen_2.5_vl_7b_fp8_scaled.safetensors` (~7 GB) | *(shared `qwen_image_vae.safetensors`)* | `Qwen-Image-Edit-2509-Lightning-4steps-V1.0-bf16.safetensors` (from `lightx2v/Qwen-Image-Lightning`) |
| Z-Image Turbo | `intorealism_zitV90.safetensors` (checkpoint, Civitai 3258780) | — | — | — |

### Video (Wan2.2 I2V)

| Purpose | File |
|---|---|
| High-noise DiT | `wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors` |
| Low-noise DiT | `wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors` |
| Text encoder | `umt5_xxl_fp8_e4m3fn_scaled.safetensors` |
| VAE | `wan_2.1_vae.safetensors` |
| NSFW-unlocked high DiT | `Wan2.2_Remix_NSFW_i2v_14b_high_lighting_v2.0.safetensors` (`FX-FeiHou/wan2.2-Remix`) |
| NSFW-unlocked low DiT | `Wan2.2_Remix_NSFW_i2v_14b_low_lighting_v2.0.safetensors` (`limiao1666/qw_nsfw`) |
| NSFW UMT5 encoder | `nsfw_wan_umt5-xxl_fp8_scaled.safetensors` (`Osrivers/…`, gated — needs `HF_TOKEN`) |
| Upscaler | `4x_foolhardy_Remacri.pth` (`FacehugmanIII/4x_foolhardy_Remacri`) |

### Custom nodes (`config/nodes.txt`)
ComfyUI_essentials, rgthree-comfy, KJNodes, comfyui_controlnet_aux,
ComfyUI-Custom-Scripts, ComfyUI-GGUF, VideoHelperSuite, PainterI2V,
Frame-Interpolation (video packs for the Wan workflow), **comfyui-ollama**
(LLM prompt expansion), **RES4LYF** (`res_2s`/`res_2m` samplers for Qwen 2.1).
ComfyUI-Manager is baked into the image — not listed.

### Ollama
`qwen3:8b` served on `:11434`. Wiring: `OllamaConnectivity` →
`http://127.0.0.1:11434`, `OllamaGenerate` → `qwen3:8b`, STRING output into the
positive prompt; append `/nothink` to suppress Qwen3 reasoning traces.

---

## 4. Workflow inventory

All provisioned from `models.list` (`workflow|…|url|…` entries, plain JSONs).

### Text-to-image

| File | Stack | First-load setup |
|---|---|---|
| `image_qwen_image_2_1_t2i.json` | Qwen-Image 2.1 + Lenovo | Insert `LoraLoaderModelOnly` (`lenovo_qwen21.safetensors` @ 0.7) after UNETLoader. KSampler: 50 steps / cfg 4 / `res_2s` / beta (RES4LYF). |
| `image_krea2_turbo_t2i.json` | Krea 2 Raw + Lenovo | Native nodes: `lora_name` → `lenovo_krea2_3000.safetensors` @ 0.8, `unet_name` → `krea2_raw_fp8_scaled.safetensors`, ~52 steps / cfg 3.5–4. (Turbo fast path: 8 steps / cfg 1, LoRA 1.2–2.0.) |

### Image-to-image / reference-image (added 2026-09-24)

| File | What it does | References | New downloads | Notes |
|---|---|---|---|---|
| `image_qwen_image_2_1_image_edit.json` | Native Qwen-Image 2.1 edit | **Up to 10** (`image_1` = edit target, rest are refs) | **None** — uses your exact 2.1 base/encoder/VAE | Mention refs as `<img>1</img>` … in the prompt; refs may differ in size/aspect; output follows `image_1`. 25 steps / euler / cfg 1 |
| `image_qwen_image_edit_2509.json` | Dedicated multi-image edit model | `image` + optional `image2`/`image3` | 2509 fp8 base (~20 GB) + Qwen2.5-VL 7B (~7 GB) + Lightning LoRA | **Turbo mode ON by default**: 4 steps / cfg 1 (the LoRA is required for this). Toggle "Enable Lightning LoRA" off for the 20-step / cfg 2.5 quality path. `ComfySwitchNode` is a core node (`comfy_extras.nodes_logic`) — no extra pack needed |
| `image_krea2_turbo_int8_image_style_reference.json` | Style reference: generates a **new** image in the style of your refs | **Up to 3** | Turbo int8 base + `krea2_style_reference` LoRA (official, separate from Lenovo) | `prompt_enhance` off by default; LoRA strength 1, 1024×1024 |

Also present: the Wan2.2-Remix painter I2V workflow
(`Wan2.2-Remix painter I2V-Ai Verse (1).json`, from the Civitai zip
`uncensoredWan22Remix_v10.zip`) and `sdxlturbo_example.json`.

---

## 5. Instance runbook

### Provision / update everything (idempotent — skips what's present)
```bash
cd /workspace/comfyui-vastai && bash scripts/provision.sh
```

### If workflows don't show in the UI (symlink bug, §2)
```bash
# stop ComfyUI first
cp -rn /workspace/ComfyUI/user/. /workspace/comfyui-user/
rm -rf /workspace/ComfyUI/user
ln -s /workspace/comfyui-user /workspace/ComfyUI/user
# restart ComfyUI, hard-refresh the browser tab
```

### Verification checks
```bash
ls -lh /workspace/models/upscale_models/4x_foolhardy_Remacri.pth
ls -lh /workspace/models/diffusion_models/krea2_raw_fp8_scaled.safetensors
grep -iE "import failed|failed to import|cannot import" /tmp/comfyui.log
ls /workspace/comfyui-user/default/workflows/
```

### Housekeeping
```bash
# remove a stale partial download if present
rm -f /workspace/models/diffusion_models/moodyKrea2Mix_v70BF16.safetensors.tmp
# delete the local token helper ONLY after all downloads finished
rm /workspace/.vastai-tokens
```

### Suggested smoke-test order
1. Qwen-Image 2.1 text-to-image (fast single-stack validation).
2. Qwen-Image 2.1 image edit with one target + one reference (zero extra downloads).
3. Then the heavier stacks (2509 edit, Krea style-ref, Wan I2V).

---

*Last updated: 2026-09-24*
