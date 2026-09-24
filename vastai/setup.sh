#!/usr/bin/env bash
# setup.sh — provision the Vast.ai instance for the ComfyUI image in one command.
# Run AFTER the image is pushed (manually or via the GitHub Actions workflow).
#
# Required env:
#   IMAGE       e.g. ghcr.io/<github-user>/comfyui-vastai:latest
#   OFFER_ID    GPU offer id — find one with:
#               vastai search offers 'gpu_name==RTX_A100_80GB verified=true inet_down>500' -o 'dph_total'
#
# Optional env:
#   VOLUME_ID     existing volume id to mount at /workspace (models/outputs/workflows
#                 survive destroy only if this is set). Create in console or:
#                   vastai search volumes 'disk_space > 200' && vastai create volume <VID> -s 200
#   DISK=200      container disk GB
#   LABEL=comfyui-session
#   HF_TOKEN      HuggingFace token (gated models)
#   CIVITAI_TOKEN Civitai API key (your custom models)
#   PUBLIC_KEY    your SSH public key, one line (enables SSH into the instance)
#
# Example:
#   IMAGE=ghcr.io/you/comfyui-vastai:latest OFFER_ID=12345678 VOLUME_ID=42 \
#     HF_TOKEN=hf_xxx CIVITAI_TOKEN=xxx PUBLIC_KEY="$(cat ~/.ssh/id_rsa.pub)" ./vastai/setup.sh
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

ENV_OPTS="-p 8188:8188 -p 11434:11434 -e OPEN_BUTTON_PORT=8188"
[ -n "${HF_TOKEN:-}" ]      && ENV_OPTS="$ENV_OPTS -e HF_TOKEN=$HF_TOKEN"
[ -n "${CIVITAI_TOKEN:-}" ] && ENV_OPTS="$ENV_OPTS -e CIVITAI_TOKEN=$CIVITAI_TOKEN"
[ -n "${PUBLIC_KEY:-}" ]    && ENV_OPTS="$ENV_OPTS -e PUBLIC_KEY=$PUBLIC_KEY"

VOL_OPTS=()
if [ -n "$VOLUME_ID" ]; then
  echo "Using volume $VOLUME_ID mounted at /workspace"
  VOL_OPTS=(--link-volume "$VOLUME_ID" --mount-path /workspace)
else
  echo "WARNING: no VOLUME_ID — models/outputs will NOT survive a destroy."
  echo "         (stop/start still preserves everything)"
fi

echo "Creating instance from offer $OFFER_ID ..."
# shellcheck disable=SC2086
INSTANCE_ID=$(vastai create instance "$OFFER_ID" \
  --image "$IMAGE" \
  --disk "$DISK" \
  --ssh --direct \
  --env "$ENV_OPTS" \
  "${VOL_OPTS[@]}" \
  --label "$LABEL" \
  --raw | tail -1)
echo "Instance ID: $INSTANCE_ID"
echo
echo "Follow the boot:  vastai logs $INSTANCE_ID   (or watch the console)"
echo "Instance status:  vastai show instance $INSTANCE_ID"
echo "ComfyUI will be at the instance's Open button (port 8188) once provision.sh finishes."
if [ -n "${PUBLIC_KEY:-}" ]; then
  echo "SSH:  vastai ssh-url $INSTANCE_ID   (then: ssh -p <port> root@<host>)"
fi
