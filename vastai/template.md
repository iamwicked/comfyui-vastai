# Vast.ai template for the ComfyUI + Ollama image

Do this once in the console: **Templates → New Template**.

| Field | Value |
|---|---|
| Image Path:Tag | `ghcr.io/iamwicked/comfyui-vastai:latest` |
| Launch Mode | **Docker ENTRYPOINT** (uses the image's own entrypoint — do *not* pick SSH or Jupyter) |
| Docker Options | `-p 8188:8188 -p 11434:11434 -e OPEN_BUTTON_PORT=8188` |
| Disk Space | `200` GB (models + Ollama weights + outputs live here; size for your library) |
| Environment Variables | `HF_TOKEN` = your HuggingFace token (read) · `CIVITAI_TOKEN` = your Civitai API key · `PUBLIC_KEY` = your SSH public key (one line) |
| On-start Script | leave empty — the image entrypoint runs provisioning itself |

Optional env vars (add in Docker Options with `-e`, or the template env field):

| Variable | Purpose | Default |
|---|---|---|
| `COMFYUI_ARGS` | extra flags, e.g. `--highvram` | empty |
| `UPDATE_COMFYUI` | `true` = git-pull ComfyUI on every boot | `false` (pinned at build) |
| `COMFYUI_REF` | pin ComfyUI to a tag/commit | build-time version |
| `OLLAMA_KEEP_ALIVE` | how long Ollama keeps a model in VRAM (`0` = unload immediately) | `10m` |
| `MODELS_LIST_URL` / `NODES_TXT_URL` / `OLLAMA_MODELS_TXT_URL` | raw URLs (e.g. a gist) overriding the baked-in lists — edit without rebuilding | unset |
| `WORKFLOWS_REPO` / `GIT_TOKEN` | git repo for `save-progress.sh` workflow backups | unset |
| `RCLONE_REMOTE` | e.g. `myremote:comfyui-output` for output backups | unset |

## Volumes (persistent storage)

Create **once**, re-link every session. In the console: pick your offer → **Storage/Volumes** →
create a volume on that machine (200 GB), then attach it at mount path `/workspace`
when creating the instance.

CLI equivalent:

```bash
vastai search volumes 'disk_space > 200'        # find a volume offer on your machine
vastai create volume <VOLUME_OFFER_ID> -s 200   # local volume (tied to that machine)
vastai show volumes                             # note the volume ID
```

## Renting from the CLI (alternative to the console)

```bash
vastai search offers 'gpu_name == RTX_A100_80GB verified=true inet_down > 500' -o 'dph_total'
vastai create instance <OFFER_ID> \
  --image ghcr.io/iamwicked/comfyui-vastai:latest \
  --disk 200 --ssh --direct \
  --env "-p 8188:8188 -p 11434:11434 -e OPEN_BUTTON_PORT=8188 -e HF_TOKEN=$HF_TOKEN -e CIVITAI_TOKEN=$CIVITAI_TOKEN -e PUBLIC_KEY=$PUBLIC_KEY" \
  --link-volume <VOLUME_ID> --mount-path /workspace \
  --label comfyui-session
```

Notes:
- **Local volumes are tied to the physical machine.** Re-rent the same offer/machine
  next session to keep your models. For machine-independent persistence use a
  network volume (`vastai create network-volume ...`) — slower, but portable — or
  rely on `provision.sh` re-downloading from `models.list` (that's your reinstall path).
- First boot on a new machine downloads the image (~10 GB) + your models, so prefer
  hosts with `inet_down > 500`.
