#!/usr/bin/env bash
# entrypoint.sh — runs on every Vast.ai boot.
# Sets up SSH, the persistent /workspace layout, then provisions and starts.
set -euo pipefail

WORKSPACE="${WORKSPACE:-/workspace}"

# --- SSH (Vast.ai does not inject it into custom images) ---
SSH_KEY="${PUBLIC_KEY:-${SSH_PUBLIC_KEY:-}}"
if [ -n "$SSH_KEY" ]; then
  mkdir -p /root/.ssh && chmod 700 /root/.ssh
  printf '%s\n' "$SSH_KEY" >> /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
  mkdir -p /run/sshd
  /usr/sbin/sshd || echo "[entrypoint] sshd failed to start (continuing anyway)"
fi

# --- Persistent layout on the volume ---
mkdir -p "$WORKSPACE"/models/{checkpoints,loras,vae,controlnet,upscale_models,embeddings,clip,clip_vision,diffusion_models,text_encoders,configs} \
         "$WORKSPACE"/output "$WORKSPACE"/comfyui-user "$WORKSPACE"/ollama-models

# Persist ComfyUI's user dir (workflows, settings) and output dir across sessions.
# (Symlinks survive inside the image; the data lives on the volume.)
if [ ! -L /opt/ComfyUI/user ]; then
  rm -rf /opt/ComfyUI/user
  ln -s "$WORKSPACE/comfyui-user" /opt/ComfyUI/user
fi
if [ ! -L /opt/ComfyUI/output ]; then
  rm -rf /opt/ComfyUI/output
  ln -s "$WORKSPACE/output" /opt/ComfyUI/output
fi

# --- Provision (idempotent: only fetches what's missing) ---
/opt/scripts/provision.sh

# --- Start services (never returns) ---
exec /opt/scripts/start.sh
