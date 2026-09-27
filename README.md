# ComfyUI + Ollama on Vast.ai — reproducible sessions (v6)

Turns a **stock Vast.ai ComfyUI template** instance into a full image + video
workstation with one command per workflow: models, LoRAs, custom nodes, Ollama LLMs
and the workflow JSON, all idempotent and safe to run in parallel terminals.
Everything lands on `/workspace`, so it survives stop/start.

Full runbook, workflow notes and problem log: [`REFERENCE.md`](REFERENCE.md).
What changed per version: [`CHANGELOG.md`](CHANGELOG.md).

## Quick start (stock ComfyUI template, Jupyter terminal)

```bash
read -rsp "HF token: " HF_TOKEN; echo; read -rsp "Civitai token: " CIVITAI_TOKEN; echo; export HF_TOKEN CIVITAI_TOKEN
[ -d /workspace/comfyui-vastai/.git ] || git clone --progress https://github.com/iamwicked/comfyui-vastai.git /workspace/comfyui-vastai
bash /workspace/comfyui-vastai/scripts/stock-setup.sh init
bash /workspace/comfyui-vastai/scripts/stock-setup.sh krea2          # one workflow per terminal, any order
bash /workspace/comfyui-vastai/scripts/stock-setup.sh finish         # once: global nodes + Ollama, restart ComfyUI
```

Then open ComfyUI with the instance's Open button and hard-refresh the tab.
`stock-setup.sh status` shows what each workflow has and is missing.

## Workflows

| id | What it is | GB |
|---|---|---|
| `qwen21` | Qwen-Image 2.1 text-to-image + Lenovo UltraReal LoRA | 17.4 |
| `qwen21-edit` | Qwen-Image 2.1 image edit, up to 10 reference images | 17.4 |
| `krea2` | Krea 2 text-to-image, realism/NSFW LoRA set, Dolphin3 prompt enhancement via Ollama | 38 |
| `edit-2509` | Qwen-Image-Edit 2509 multi-image edit, 4-step Lightning | 31 |
| `krea-style` | Krea 2 Turbo style reference | 19 |
| `wan-i2v` | Wan2.2 Remix/Painter uncensored image-to-video | 71 |
| `minimax-h3` | MiniMax H3 all-in-one: 7 modes + lip sync, native stereo audio | 63 |
| `wan-animate` | Wan2.2 Animate motion transfer + Instareal realism LoRA | 46 |
| `global` | Z-Image Turbo checkpoint, general nodes, `qwen3:8b` | 11 |
| `flux` | Opt-in Flux.2 Klein 9B support files (not in `all`) | 9 |

`stock-setup.sh all` installs everything except `flux` in one terminal (about 290 GB).

## How it's organised

```
config/catalog.txt        workflow ids (the only list of ids)
config/models.list        every file:  kind|filename|source|source_id|subdir|wf:ids
config/nodes.txt          custom nodes: git_url|ref|wf:ids
config/ollama-models.txt  Ollama LLMs: model|wf:ids
config/workflows/         bundled workflow JSONs
config/patches/           small edits applied to a workflow after download
scripts/stock-setup.sh    init / <id> / finish / all / status / restart / list
scripts/install-workflow.sh   one workflow (what stock-setup.sh <id> runs)
scripts/provision.sh      the engine (nodes, models, Ollama; --status)
extra_model_paths.yaml    points ComfyUI at /workspace/models
Dockerfile, scripts/entrypoint.sh, scripts/start.sh, vastai/   golden image (alternative)
scripts/save-progress.sh  optional off-box backup (git + rclone copy)
```

## Golden image (alternative path)

A prebuilt image (`ghcr.io/iamwicked/comfyui-vastai:latest`) that provisions on boot
via `BOOT_WORKFLOWS`. Its build was broken until v6 (the Ollama installer needs
`zstd`); see `REFERENCE.md` §8 and `vastai/template.md` before relying on it.

## Security notes

- Tokens are environment variables only; enter them with `read -rs` as above. They
  are never written to files or committed.
- ComfyUI has no login. On the stock template it sits behind Vast's token proxy; the
  golden image maps port 8188 directly, so keep that URL private.
- Ollama listens on 127.0.0.1 only. Reach it from your machine with
  `ssh -p <port> root@<host> -L 11434:127.0.0.1:11434`.
