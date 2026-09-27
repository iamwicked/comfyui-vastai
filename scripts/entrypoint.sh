#!/usr/bin/env bash
# entrypoint.sh — golden image boot: SSH, persistent /workspace layout,
# provisioning of the workflows you ask for, then ComfyUI + Ollama.
# Provisioning problems are logged and never stop ComfyUI from starting.
#
# BOOT_WORKFLOWS  space-separated workflow ids to install on boot
#                 (default: global; "all" = every workflow in `all`, roughly 300 GB;
#                 "none" = skip). Install more later with:
#                 bash /opt/scripts/install-workflow.sh <id>
set -euo pipefail

WORKSPACE="${WORKSPACE:-/workspace}"
export WORKSPACE COMFYUI_DIR=/opt/ComfyUI CONFIG_DIR=/opt/comfyui-config

# --- SSH (Vast.ai does not inject it into custom images in entrypoint mode) ---
SSH_KEY="${PUBLIC_KEY:-${SSH_PUBLIC_KEY:-}}"
if [ -n "$SSH_KEY" ]; then
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys
  grep -qxF -- "$SSH_KEY" /root/.ssh/authorized_keys || printf '%s\n' "$SSH_KEY" >> /root/.ssh/authorized_keys
  mkdir -p /run/sshd
  /usr/sbin/sshd || echo "[entrypoint] sshd failed to start (continuing anyway)"
fi

# --- Persistent layout on the volume (one folder per extra_model_paths.yaml entry) ---
for d in $(awk -F': *' '/^  [a-z_]+: [^\/].*\/$/ {sub(/\/$/, "", $2); print $2}' /opt/ComfyUI/extra_model_paths.yaml); do
  mkdir -p "$WORKSPACE/models/$d"
done
mkdir -p "$WORKSPACE"/output "$WORKSPACE"/comfyui-user/default/workflows "$WORKSPACE"/ollama-models

# Persist ComfyUI's user dir (workflows, settings) and output dir across sessions.
if [ ! -L /opt/ComfyUI/user ]; then
  cp -rn /opt/ComfyUI/user/. "$WORKSPACE/comfyui-user/" 2>/dev/null || true
  rm -rf /opt/ComfyUI/user
  ln -s "$WORKSPACE/comfyui-user" /opt/ComfyUI/user
fi
if [ ! -L /opt/ComfyUI/output ]; then
  rm -rf /opt/ComfyUI/output
  ln -s "$WORKSPACE/output" /opt/ComfyUI/output
fi

# --- Provision (idempotent: only fetches what's missing) ---
BOOT_WORKFLOWS="${BOOT_WORKFLOWS:-global}"
case "$BOOT_WORKFLOWS" in
  none|"") echo "[entrypoint] BOOT_WORKFLOWS=none — skipping provisioning" ;;
  all)     /opt/scripts/provision.sh || echo "[entrypoint][WARN] full provisioning had problems — ComfyUI starts anyway" ;;
  *)
    for id in $BOOT_WORKFLOWS; do
      WORKFLOW="$id" /opt/scripts/provision.sh || echo "[entrypoint][WARN] provisioning '$id' had problems — ComfyUI starts anyway"
    done ;;
esac

# --- Start services (never returns) ---
exec /opt/scripts/start.sh
