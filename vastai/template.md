# Vast.ai template for the golden image (alternative path)

The primary path is Vast's **stock ComfyUI template** plus `scripts/stock-setup.sh`
(see `REFERENCE.md`). Use this page only once the golden image has been built and
published to GHCR. Check that first: the package page
`https://github.com/iamwicked/comfyui-vastai/pkgs/container/comfyui-vastai` must exist
and be **public** (GHCR packages start private; Vast cannot pull a private image
without registry credentials).

Do this once in the console: **Templates → New Template**.

| Field | Value |
|---|---|
| Image Path:Tag | `ghcr.io/iamwicked/comfyui-vastai:latest` |
| Launch Mode | **Docker ENTRYPOINT** (the image's own entrypoint; SSH or Jupyter mode would replace it and nothing would provision or start) |
| Docker Options | `-p 8188:8188 -e OPEN_BUTTON_PORT=8188` |
| Disk Space | `200` GB without a volume; with a volume, size the **volume** instead (see below) |
| Environment Variables | `HF_TOKEN`, `CIVITAI_TOKEN`, `PUBLIC_KEY` (SSH public key, one line), optional `BOOT_WORKFLOWS` |
| On-start Script | leave empty — the image entrypoint provisions and starts everything |

Optional env vars:

| Variable | Purpose | Default |
|---|---|---|
| `BOOT_WORKFLOWS` | workflow ids to install on every boot, e.g. `global krea2 wan-animate`; `all` = everything in `all` (~300 GB); `none` = skip | `global` |
| `COMFYUI_ARGS` | extra ComfyUI flags, e.g. `--highvram` | empty |
| `UPDATE_COMFYUI` | `true` = git-pull ComfyUI core on boot (before node installs) | `false` |
| `COMFYUI_REF` | pin ComfyUI core to a tag/commit on boot | build-time version |
| `OLLAMA_KEEP_ALIVE` | how long Ollama keeps a model in VRAM (`0` = unload immediately) | `10m` |
| `OLLAMA_BIND` | `0.0.0.0` exposes Ollama beyond localhost (no auth, also map `-p 11434:11434`) | `127.0.0.1` |
| `MODELS_LIST_URL` / `NODES_TXT_URL` / `OLLAMA_MODELS_TXT_URL` | raw URLs (e.g. a gist) overriding the baked-in lists without a rebuild | unset |
| `WORKFLOWS_REPO` / `GIT_TOKEN` | git repo for `save-progress.sh` workflow backups | unset |
| `RCLONE_REMOTE` / `SAVE_RCLONE_CONF` | output backups with rclone (`copy`, never `sync`) | unset |

Install more workflows on a running instance (SSH or Jupyter terminal):

```bash
bash /opt/scripts/install-workflow.sh wan-animate
```

## Volumes (persistent storage)

Create **once**, re-link every session. Size it for the workflows you use
(per-workflow sizes are in `REFERENCE.md`; everything in `all` is about 300 GB plus
headroom for outputs and in-flight downloads, so 400 GB).

```bash
vastai search volumes 'disk_space>400'          # a volume offer on your machine
vastai create volume <VOLUME_OFFER_ID> -s 400   # local volume (tied to that machine)
vastai show volumes                             # note the volume ID
```

## Renting from the CLI (alternative to the console)

```bash
vastai search offers 'num_gpus=1 gpu_ram>=79 verified=true inet_down>500' -o 'dph_total'
vastai create instance <OFFER_ID> \
  --image ghcr.io/iamwicked/comfyui-vastai:latest \
  --disk 200 \
  --env "-p 8188:8188 -e OPEN_BUTTON_PORT=8188 -e HF_TOKEN='$HF_TOKEN' -e CIVITAI_TOKEN='$CIVITAI_TOKEN' -e PUBLIC_KEY='$PUBLIC_KEY'" \
  --link-volume <VOLUME_ID> --mount-path /workspace \
  --label comfyui-session \
  --args
```

`--args` has to be the last flag: it selects the entrypoint launch mode.
`vastai/setup.sh` wraps all of this.

Notes:
- **Local volumes are tied to the physical machine.** Re-rent the same machine next
  session to keep your models. For machine-independent persistence use a network
  volume (`vastai create network-volume ...`), or let `provision.sh` re-download.
- First boot on a new machine pulls the image plus your models, so prefer hosts with
  `inet_down > 500`.
- ComfyUI has no login and is reachable on the mapped port. Keep the URL private.
