#!/usr/bin/env bash
# setup.sh — rent a Vast.ai instance running the GOLDEN IMAGE, from your own machine.
# (The primary path is the stock Vast ComfyUI template + scripts/stock-setup.sh;
# see REFERENCE.md. Use this only once the image has been built and published.)
#
# Required env:
#   IMAGE       e.g. ghcr.io/<github-user>/comfyui-vastai:latest
#   OFFER_ID    GPU offer id — find one (any single 80 GB-class GPU, cheapest first) with:
#               vastai search offers 'num_gpus=1 gpu_ram>=79 verified=true inet_down>500' -o 'dph_total'
#
# Optional env:
#   VOLUME_ID       existing volume id to mount at /workspace (models/outputs/workflows
#                   survive destroy only if this is set). Create one with:
#                     vastai search volumes 'disk_space>400' && vastai create volume <VID> -s 400
#   DISK=200        container disk GB
#   LABEL=comfyui-session
#   BOOT_WORKFLOWS  workflow ids to install on boot (default: global; "all" is ~300 GB)
#   HF_TOKEN / CIVITAI_TOKEN   passed to the instance as env vars
#   PUBLIC_KEY      your SSH public key, one line (enables SSH into the instance)
#
# Tokens: export them in your shell first (not inline on this command line, which
# lands in shell history), e.g.  read -rs HF_TOKEN; export HF_TOKEN
#
# Example:
#   IMAGE=ghcr.io/you/comfyui-vastai:latest OFFER_ID=12345678 VOLUME_ID=42 \
#     PUBLIC_KEY="$(cat ~/.ssh/id_ed25519.pub)" ./vastai/setup.sh
set -euo pipefail

: "${IMAGE:?set IMAGE, e.g. ghcr.io/<github-user>/comfyui-vastai:latest}"
: "${OFFER_ID:?set OFFER_ID (see header for how to find one)}"
DISK="${DISK:-200}"
LABEL="${LABEL:-comfyui-session}"
VOLUME_ID="${VOLUME_ID:-}"

command -v vastai >/dev/null 2>&1 || {
  echo "error: 'vastai' CLI not found. Install it:  pip install vastai" >&2
  exit 1
}

# Vast parses this string shell-style: single quotes keep values with spaces
# (like an SSH key) in one piece. Ollama stays on localhost, so only 8188 is mapped.
ENV_OPTS="-p 8188:8188 -e OPEN_BUTTON_PORT=8188"
[ -n "${BOOT_WORKFLOWS:-}" ] && ENV_OPTS="$ENV_OPTS -e BOOT_WORKFLOWS='$BOOT_WORKFLOWS'"
[ -n "${HF_TOKEN:-}" ]       && ENV_OPTS="$ENV_OPTS -e HF_TOKEN='$HF_TOKEN'"
[ -n "${CIVITAI_TOKEN:-}" ]  && ENV_OPTS="$ENV_OPTS -e CIVITAI_TOKEN='$CIVITAI_TOKEN'"
[ -n "${PUBLIC_KEY:-}" ]     && ENV_OPTS="$ENV_OPTS -e PUBLIC_KEY='$PUBLIC_KEY'"

VOL_OPTS=()
if [ -n "$VOLUME_ID" ]; then
  echo "Using volume $VOLUME_ID mounted at /workspace"
  VOL_OPTS=(--link-volume "$VOLUME_ID" --mount-path /workspace)
else
  echo "WARNING: no VOLUME_ID — models/outputs will NOT survive a destroy."
  echo "         (stop/start still preserves everything)"
fi

echo "Creating instance from offer $OFFER_ID ..."
# --args (must be last) selects the entrypoint launch mode, so the image's
# entrypoint.sh runs. --ssh / --jupyter would replace it with Vast's own.
RESPONSE="$(vastai create instance "$OFFER_ID" \
  --image "$IMAGE" \
  --disk "$DISK" \
  --env "$ENV_OPTS" \
  "${VOL_OPTS[@]}" \
  --label "$LABEL" \
  --raw \
  --args)"
INSTANCE_ID="$(printf '%s' "$RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("new_contract", ""))' 2>/dev/null || true)"
if [ -z "$INSTANCE_ID" ]; then
  echo "error: could not read the new instance id. Vast answered:" >&2
  printf '%s\n' "$RESPONSE" >&2
  exit 1
fi
echo "Instance ID: $INSTANCE_ID"
echo
echo "Follow the boot:  vastai logs $INSTANCE_ID   (or watch the console)"
echo "Instance status:  vastai show instance $INSTANCE_ID"
echo "ComfyUI will be at the instance's Open button (port 8188) once provisioning finishes."
if [ -n "${PUBLIC_KEY:-}" ]; then
  echo "SSH:  vastai ssh-url $INSTANCE_ID   (then: ssh -p <port> root@<host>)"
  echo "Ollama from your machine:  ssh -p <port> root@<host> -L 11434:127.0.0.1:11434"
fi
