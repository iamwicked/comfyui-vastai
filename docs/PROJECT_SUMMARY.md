# comfyui-vastai: project summary

Last updated 2026-09-30. For the next working session, see [AGENT_HANDOFF.md](AGENT_HANDOFF.md).

## What this project is

comfyui-vastai turns a stock Vast.ai ComfyUI template instance into a reproducible image and video workstation. Idempotent bash scripts install each workflow's models, LoRAs, custom nodes, Ollama models and workflow graphs onto `/workspace`, so a Vast volume keeps them between sessions. One command installs one workflow; one template variable can install all of them on first boot. `HF_TOKEN` and `CIVITAI_TOKEN` only ever live in environment variables.

`README.md` is the quick start, `REFERENCE.md` the runbook and problem log, `CHANGELOG.md` the version history.

On an instance, ComfyUI runs under supervisor on 127.0.0.1:18188 behind Vast's proxy (its Python is `/venv/main`), and Ollama on 127.0.0.1:11434. The repo lives in `/workspace/comfyui-vastai`, models in `/workspace/models`, workflow graphs in `/workspace/comfyui-user/default/workflows`, outputs in `/workspace/ComfyUI/output`.

## How it's organised

| Path | Role |
|---|---|
| `config/catalog.txt` | the workflow ids (the only list of them) |
| `config/models.list`, `config/nodes.txt`, `config/ollama-models.txt` | what each id installs, tagged `wf:<ids>` |
| `config/workflows/`, `config/patches/` | bundled graphs and edits applied after download |
| `scripts/stock-setup.sh` | `init`, `<id>`, `finish`, `all`, `status`, `restart`, `list` |
| `scripts/vast-provision.sh` | hands-off first boot through `PROVISIONING_SCRIPT` |
| `scripts/install-workflow.sh`, `scripts/provision.sh`, `scripts/common.sh` | the install engine and shared helpers |
| `scripts/influencer.sh`, `scripts/influencer/`, `config/influencer/` | the AI influencer pipeline |
| `scripts/check.sh`, `tests/` | the check suite: config check, pytest with Hypothesis, `bash -n`, ShellCheck |
| `.kiro/specs/` | the specs: `ai-influencer-pipeline` and `studio` |

## Workflows

| id | What it is |
|---|---|
| `qwen21` | Qwen-Image 2.1 text-to-image |
| `qwen21-edit` | Qwen-Image 2.1 image edit, up to 10 reference images |
| `krea2` | Krea 2 text-to-image with a realism/NSFW LoRA set and prompt enhancement |
| `edit-2509` | Qwen-Image-Edit 2509 multi-image edit, 4-step Lightning |
| `krea-style` | Krea 2 Turbo style reference (1 to 3 style images) |
| `wan-i2v` | Wan2.2 Remix image-to-video |
| `minimax-h3` | MiniMax H3 all-in-one video with lip sync and audio |
| `wan-animate` | Wan2.2 Animate motion transfer |
| `influencer` | AI influencer stills |
| `global` | shared nodes, Z-Image Turbo, `qwen3:8b` |
| `flux`, `influencer-video`, `influencer-nsfw`, `character-design` | opt-in ids |

## Where things stand

The last commit is `a746fda` (v6) on branch `v6`. Everything since is uncommitted in the working tree: the v7 influencer work (edits to tracked scripts, config lists and docs, plus the untracked `scripts/influencer/`, `scripts/influencer.sh`, `scripts/check.sh`, `tests/` and `config/influencer/`) and both specs under `.kiro/`.

### AI influencer pipeline (v7)

Weekly stills, and later Reels, for one fictional adult persona: planned on the Mac with a local Ollama, rendered on the instance through ComfyUI, then graded and exported. Four catalog ids: `influencer`, `influencer-video`, `influencer-nsfw`, `character-design`.

- Phase 1 (Foundation): implemented. The Phase 1 checkpoint task is still open.
- Phase 2 (Identity: training kit, refine stage, test grid), Phase 3 (Video: Reels) and Phase 4 (optional add-ons, source checker, runbook): not started.

### Studio (new)

A simple web front end for the instance's ComfyUI: pick a workflow and a model, upload input images, stack LoRAs, adjust settings, press Generate, watch progress and previews, and get the images or videos back. FastAPI backend serving plain HTML, CSS and JavaScript (no build step), run as a supervisor program on 127.0.0.1:18190 in its own venv under `/workspace/studio`, opened through an SSH tunnel. It runs the graphs the repo already installs, converts them with the image's `/workflow/convert` endpoint, and describes each workflow with a small committed manifest. First version: krea2 (both graphs), qwen21, qwen21-edit, krea-style, edit-2509, wan-i2v.

- Requirements: done and reviewed.
- Design: mostly written. Still missing: Testing Strategy, Documentation, Approval-gated changes.
- Task list: not generated yet.
- Code: none yet.

## What needs to be done

1. Decide when to commit the influencer work. Studio's rule that every existing test keeps passing uses the last commit before Studio as its baseline, so the influencer code should be committed first.
2. Finish the Studio design, then generate its task list.
3. Answer the Studio decisions below, above all which install-command proposals you approve. With none approved, Studio installs with its own `bash scripts/studio.sh install` and no existing command changes.
4. Build Studio from the task list. The core logic, its tests and the installer tests all run on the Mac.
5. On an instance: capture the converted-graph fixtures, write the seven manifests against them, and run the acceptance checks: Krea 2 Turbo with a LoRA at 0.8 and a fixed seed shows the image and the same settings reproduce it; qwen21-edit runs with three uploaded images; Cancel stops a running job; a missing model is reported by file name; Studio listens on 127.0.0.1 only; a keyboard-only run works.
6. Influencer pipeline: run the Phase 1 checkpoint, then Phases 2 to 4.

## Decisions waiting on you (Studio)

| Decision | Default until you answer |
|---|---|
| P-1: `stock-setup.sh studio` runs the Studio installer | not approved |
| P-2: `finish` restarts Studio | not approved |
| P-3: first boot installs Studio when `STUDIO=1` | not approved |
| P-4: docs add a PORTAL_CONFIG entry and map port 8190 at instance creation (Vast's token auth in front) | not approved, so SSH tunnel only |
| P-5: the installer adds Studio to `/etc/portal.yaml` and restarts Caddy | not approved |
| P-6: pin FastAPI and httpx in the check suite, so the HTTP routes are tested on the Mac | not approved |
| P-7: add the converter node pack to `config/nodes.txt` | not approved; it would break the Selection_Snapshot test |
| wan-i2v's graph unpacks into a subfolder of the workflows folder | the manifest names a top-level file |
| krea2's two graphs share one catalog description | shared text |
| a model name that matches only when subfolders are ignored | shown as not installed |
| edit-2509 with its Lightning LoRA off | steps and cfg follow the graph's switch |
| limits | batch 1 to 4; wan-i2v 5 to 121 frames at 1 to 60 fps; 20 queued jobs; 100 presets per workflow; uploads up to 64 MiB and 16,384 px per side |
| prompt-refinement switches as form controls | no |
| deleting a history record whose image the open form shows | the form empties that slot |
| presets after a manifest is renamed | they stay under the old name |
| CHANGELOG and REFERENCE numbering, since the influencer work uses v7 and §13 | Studio takes the next free number when it lands |

## Only an instance can confirm

- The rented image tag ships the converter, and its output matches ComfyUI's own API export for each graph.
- The layout of `/etc/portal.yaml`, how Portal changes take effect, and the Host header Studio sees through the Portal.
- Whether this ComfyUI release sends `/ws` previews and honours `/interrupt` with a prompt id.
- The wan-i2v graph's file name and structure.
- Whether the same seed gives identical pixels.
- Whether ports 18190 and 8190 are free.
