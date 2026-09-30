# ComfyUI + Ollama on Vast.ai — reproducible sessions (v7)

Turns a **stock Vast.ai ComfyUI template** instance into a full image + video
workstation. One command per workflow (or one template setting for all of them)
installs its models, LoRAs, custom nodes, Ollama LLMs and workflow JSON. Idempotent,
safe to run in parallel terminals, and everything lands on `/workspace`.

Full runbook, workflow notes and problem log: [`REFERENCE.md`](REFERENCE.md).
What changed per version: [`CHANGELOG.md`](CHANGELOG.md).

## Two ways to set up an instance

### Hands-off: the template provisions itself

In your copy of Vast's ComfyUI template (Edit → Environment Variables):

| Variable | Value |
|---|---|
| `PROVISIONING_SCRIPT` | `https://raw.githubusercontent.com/iamwicked/comfyui-vastai/main/scripts/vast-provision.sh` |
| `WORKFLOWS` | the ids you want, comma-separated, e.g. `krea2,wan-animate` (or `all`) |
| `UPDATE_COMFYUI` | optional: `1` moves ComfyUI core to the newest release first (MiniMax H3 needs >= 0.30.0) |

Delete the template's `PROVISIONING_COMFYUI_WORKFLOWS` row: if its example-workflow
downloads fail, Vast stops before it runs this repo's script (REFERENCE.md §2A).

Put `HF_TOKEN` and `CIVITAI_TOKEN` in Vast's account-level Environment Variables
(Settings), or in the template if it stays private. Rent with the template: Vast runs
the script on first boot and opens ComfyUI when it finishes. Progress is in the
Instance Portal logs (`/var/log/portal/provisioning.log`).

### By hand: Jupyter terminal

```bash
read -rsp "HF token: " HF_TOKEN; echo; read -rsp "Civitai token: " CIVITAI_TOKEN; echo; export HF_TOKEN CIVITAI_TOKEN
[ -d /workspace/comfyui-vastai/.git ] || git clone --progress https://github.com/iamwicked/comfyui-vastai.git /workspace/comfyui-vastai
bash /workspace/comfyui-vastai/scripts/stock-setup.sh init
bash /workspace/comfyui-vastai/scripts/stock-setup.sh krea2          # one workflow per terminal, any order
bash /workspace/comfyui-vastai/scripts/stock-setup.sh finish         # once: global nodes + Ollama, restart ComfyUI
```

Either way: open ComfyUI with the instance's Open button, hard-refresh, pick a
workflow from the Workflows menu. `stock-setup.sh status` shows what's installed.

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

`all` is every id with `in_all=yes` in `config/catalog.txt`, so it now includes
`influencer`. The v6 figure was about 290 GB; the v7 total is not yet measured
(REFERENCE.md §13.11).

## AI influencer pipeline

Weekly stills (and later Reels) for one fictional adult persona, planned on the Mac and
rendered on the Instance. Four Catalog ids:

| id | What it is | GB |
|---|---|---|
| `influencer` | Krea 2 Raw stills, Qwen-Image-Edit-2511 carousel edits, face detail, VOSR2 finish, DeGrid | 107.7 (102.1 new), measured 2026-09-28 |
| `influencer-video` | MiniMax H3 Reels. Not yet available — arrives in Phase 3 | not yet measured |
| `influencer-nsfw` | adult-content LoRAs for platform-`other` Posts. Not yet available — arrives in Phase 4 | not yet measured |
| `character-design` | sketch to Hero_Image and character sheets. Not yet available — arrives in Phase 4 | not yet measured |

Install on the Instance (tokens exported as above):

```bash
bash /workspace/comfyui-vastai/scripts/install-workflow.sh influencer
```

The tools run through `bash scripts/influencer.sh <command>` (`--help` lists them). The
Runbook, with the weekly checklist, disk table and licences, is
[REFERENCE.md §13](REFERENCE.md#13-ai-influencer-pipeline-runbook).

## Between sessions

| How you end a session | Next time |
|---|---|
| Stop the instance | Start it: ComfyUI and Ollama come back in a couple of minutes. Disk keeps billing, and the GPU may be rented by someone else meanwhile. |
| Destroy it, models on a Vast volume mounted at `/workspace` | Rent that machine with the volume: tools and node dependencies reinstall, models stay (about 10-15 min). |
| Destroy it, nothing kept | A full install again. |

## How it's organised

```
config/catalog.txt        workflow ids (the only list of ids)
config/models.list        every file:  kind|filename|source|source_id|subdir|wf:ids
config/nodes.txt          custom nodes: git_url|ref|wf:ids
config/ollama-models.txt  Ollama LLMs: model|wf:ids
config/workflows/         bundled workflow JSONs
config/patches/           small edits applied to a workflow after download
scripts/vast-provision.sh hands-off first boot (Vast PROVISIONING_SCRIPT)
scripts/stock-setup.sh    init / <id> / finish / all / status / restart / list
scripts/install-workflow.sh   one workflow (what stock-setup.sh <id> runs)
scripts/provision.sh      the engine (nodes, models, Ollama; --status)
extra_model_paths.yaml    points ComfyUI at /workspace/models
scripts/save-progress.sh  optional off-box backup (git + rclone copy)
scripts/influencer.sh     the AI influencer tools (plan, render, finish, export, checks)
scripts/check.sh          every check and test in one command (the Check_Suite)
config/influencer/        templates, LUTs, licence lists, example persona and calendars
```

## Security notes

- Tokens are environment variables only: Vast account-level variables, or typed with
  `read -rs` as above. They are never written to files or committed.
- ComfyUI has no login of its own; Vast's proxy puts a token in front of it. Don't
  share the instance link.
- Ollama listens on 127.0.0.1 only. Reach it from your machine with
  `ssh -p <port> root@<host> -L 11434:127.0.0.1:11434`.
