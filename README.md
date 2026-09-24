# ComfyUI + Ollama on Vast.ai — reproducible sessions

Everything here implements the "golden image + thin script + volume" setup:
**build the Docker image once, boot it in ~30s every session, keep all progress
on a persistent volume, and re-provision the environment with a script.**

## What's in here

```
Dockerfile                  Golden image: CUDA 12.8, PyTorch cu128, ComfyUI,
                            ComfyUI-Manager, Ollama, rclone, sshd
extra_model_paths.yaml      Points ComfyUI at /workspace/models (the volume)
scripts/
  entrypoint.sh             Boot: SSH setup → persistent layout → provision → start
  provision.sh              Idempotent setup: updates nodes, downloads missing
                            models (HF/Civitai/URL), pulls Ollama models.
                            PHASES= / SHARD_INDEX= / SHARD_TOTAL= split work
                            across terminals (see stock-template section below)
  start.sh                  Launches Ollama (port 11434) + ComfyUI (port 8188)
  save-progress.sh          End-of-session: git-push workflows, rclone-sync outputs
  stock-setup.sh            Fresh STOCK-template instance setup (skip with the
                            golden image): prereqs, python3.11 shim, user/ symlink,
                            then provision with stock paths. init/dl/finish phases
                            for parallel downloads across N terminals
config/
  models.list               Your models: kind|filename|source|source_id|subdir
                            ← FILL IN your Civitai version IDs here
  nodes.txt                 Custom nodes (git URLs, one per line)
  ollama-models.txt         LLM models to pull
vastai/
  template.md               Exact Vast.ai template fields + volume/CLI reference
```

### How persistence works

| Thing | Where it lives | Survives destroy? |
|---|---|---|
| ComfyUI app, torch, nodes code | Docker image | yes (image is immutable) |
| Models, Ollama weights | `/workspace` volume | yes (if volume re-linked) |
| Workflows, settings (`user/`) | `/workspace/comfyui-user` (symlinked) | yes |
| Generated images | `/workspace/output` (symlinked) | yes |
| Git backup of workflows / cloud copy of outputs | your repo / cloud | yes (off-box, via `save-progress.sh`) |

Key Vast.ai rule: **stop/start preserves everything** (whole container comes back).
**Destroy wipes the container** — only the mounted volume survives. So:

- **Short break (hours/days):** just **STOP** the instance. Resume = 1 click, zero setup.
- **Long break:** run `save-progress.sh` (optional), then **DESTROY**. Next session:
  re-rent the **same machine** and link your existing volume → boots in ~1 min with
  everything intact. New machine → `provision.sh` re-downloads from `models.list`
  (your scripted reinstall path).

## Setup runbook (do once)

### 0. Prerequisites
- Docker Hub account (or GHCR) + Docker installed locally
- Vast.ai account with credit + API key; `pip install vastai` (CLI)
- HuggingFace token (read) — only if using gated HF models
- Civitai API key — from civitai.com → Account Settings → API Keys

### 1. Fill in your models
Edit `config/models.list`. For each Civitai model: open it on civitai.com, pick the
version, copy the number from its download URL (`.../api/download/models/<ID>`) —
that's the `source_id`. Format: `lora|my-style.safetensors|civitai|12345678|`

### 2. Build & push the image — pick one

**Option A — automatic (recommended):** push the *contents* of this folder as a
GitHub repo root and push to `main`. The included workflow
(`.github/workflows/build.yml`) builds and pushes to
`ghcr.io/<your-github-user>/comfyui-vastai:latest` automatically — no secrets,
no local Docker needed. Rebuilds happen on their own whenever you change the
Dockerfile, scripts, or configs.

**Option B — manual:** on any machine with Docker:
```bash
cd comfyui-vastai
docker build -t <your-dockerhub-user>/comfyui-vastai:latest .
docker push <your-dockerhub-user>/comfyui-vastai:latest
```
(~10 GB; slow the first time. You only rebuild when the *base* changes —
ComfyUI/torch/Manager. Model/node list edits never need a rebuild: set
`MODELS_LIST_URL`/`NODES_TXT_URL` to a gist, or just re-run `provision.sh`.)

### 3. Create the Vast.ai template
Follow `vastai/template.md` — one-time console setup. Recommended GPU for this
build: **A100 80GB** (best value for ComfyUI at ~$1.10–1.45/hr); H100 80GB if you
want maximum speed.

### 4. Create a volume + rent (one command)
Create a 200 GB volume once (console, or `vastai create volume`), find a GPU
offer, then:
```bash
pip install vastai   # if needed
IMAGE=ghcr.io/<github-user>/comfyui-vastai:latest OFFER_ID=<offer> VOLUME_ID=<vol> \
  HF_TOKEN=hf_xxx CIVITAI_TOKEN=xxx PUBLIC_KEY="$(cat ~/.ssh/id_rsa.pub)" \
  ./vastai/setup.sh
```
(Or do it in the console following `vastai/template.md` — same result.)

### 5. First boot
Watch the logs: `provision.sh` clones/updates nodes, downloads your models
(skipping any already on the volume), and pulls Ollama models. Then open ComfyUI
via the instance's **Open** button (port 8188), SSH with your key.

## Everyday use

- **ComfyUI:** `http://<instance>:8188` (via Vast's Open button)
- **Ollama API:** port `11434` (or `ssh -L 11434:localhost:11434 ...` tunnel)
- **Re-run provisioning anytime:** SSH in and run `/opt/scripts/provision.sh`
  (e.g. after editing `models.list` — or set `MODELS_LIST_URL` to a gist and reboot)
- **Free VRAM for a big gen:** `ollama stop <model>` — with 80GB you rarely need
  this, but `OLLAMA_KEEP_ALIVE=0` unloads aggressively if you do
- **End of session:** `save-progress.sh` → STOP (short break) or DESTROY (long break)

## Stock template: fresh-instance setup (no golden image)

If you're on Vast.ai's **stock template** instead of the golden image (ComfyUI
at `/workspace/ComfyUI`, python at `/venv/main`), every fresh instance needs a
one-time setup. It's fully scripted — `scripts/stock-setup.sh` clones this
repo, installs missing prerequisites (`hf` CLI, ollama, unzip), shims
`python3.11` to the stock venv (provision.sh hardcodes it), links ComfyUI's
`user/` dir to the persistent layout (otherwise workflows are invisible in the
UI), then runs `provision.sh` with the stock paths.

Export your tokens in **every** terminal first (gated HF repos need
`HF_TOKEN`, all Civitai downloads need `CIVITAI_TOKEN`):

```bash
export HF_TOKEN=hf_xxx CIVITAI_TOKEN=xxx
```

**Single terminal** (simple; the ~150GB model pull runs serially):

```bash
git clone https://github.com/iamwicked/comfyui-vastai.git /workspace/comfyui-vastai
cd /workspace/comfyui-vastai
bash scripts/stock-setup.sh
```

**Parallel downloads across N terminals** (much faster first boot — the model
pull is the bottleneck, and shards download concurrently). Data line *i* of
`config/models.list` always goes to shard *i % N*, so every model downloads
exactly once; nodes and Ollama run once at the end, not per shard.

Terminal 1:
```bash
cd /workspace/comfyui-vastai
bash scripts/stock-setup.sh init
```

Terminals 1..N — one shard per terminal (`i` = 1..N):
```bash
cd /workspace/comfyui-vastai
bash scripts/stock-setup.sh dl <i> <N>   # e.g. dl 1 4 / dl 2 4 / dl 3 4 / dl 4 4
```

Terminal 1, after all shards finish:
```bash
bash scripts/stock-setup.sh finish
```

Then start everything (nothing on the stock template autostarts it):

```bash
OLLAMA_MODELS=/workspace/ollama-models ollama serve > /tmp/ollama.log 2>&1 &
cd /workspace/ComfyUI && /venv/main/bin/python main.py --listen 0.0.0.0 --port 8188 > /tmp/comfyui.log 2>&1 &
```

Verify: `ls /workspace/comfyui-user/default/workflows/` should show 6 workflow
JSONs. Open ComfyUI via the instance's Open button (port 8188) and hard-refresh.

Advanced: you can drive `provision.sh` directly —
`PHASES` takes a comma-separated subset of `nodes,models,ollama`, and
`SHARD_INDEX`/`SHARD_TOTAL` shard just the model downloads, e.g.
`PHASES=models SHARD_INDEX=0 SHARD_TOTAL=4 bash scripts/provision.sh`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Civitai download fails | Check `CIVITAI_TOKEN` is set and the version ID is a *version* ID, not a model ID |
| HF gated model fails | `HF_TOKEN` set + license accepted on huggingface.co for that repo |
| Boot is slow | Normal on first boot (image pull + model downloads). Pick hosts with `inet_down > 500` |
| ComfyUI won't open | Check port `8188` is mapped and `OPEN_BUTTON_PORT=8188` is set |
| A custom node breaks the UI | Remove its folder from `custom_nodes/`, remove from `nodes.txt`, reboot |
| Out of disk | Models accumulate — `du -sh /workspace/models/*`, prune old ones |

## Security notes

- Tokens (`HF_TOKEN`, `CIVITAI_TOKEN`, `GIT_TOKEN`) go in as **env vars**, never
  baked into the image.
- ComfyUI has no login screen — it listens on `0.0.0.0` because Vast's proxy needs
  it. Don't share the instance URL publicly; use the SSH tunnel for Ollama if
  you're cautious.
