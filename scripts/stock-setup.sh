#!/usr/bin/env bash
# stock-setup.sh — first-boot setup for a FRESH stock Vast.ai template instance
# (ComfyUI at /workspace/ComfyUI, python at /venv/main). The golden-image flow
# needs none of this; use this script when you're on the stock template instead.
#
# Export HF_TOKEN and CIVITAI_TOKEN in EVERY terminal before running
# (gated HF repos need the first, all Civitai downloads need the second).
#
# Single terminal (simple; the ~150GB model pull runs serially):
#   git clone https://github.com/iamwicked/comfyui-vastai.git /workspace/comfyui-vastai
#   cd /workspace/comfyui-vastai
#   bash scripts/stock-setup.sh
#
# N terminals in parallel (much faster first boot — downloads are the bottleneck):
#   terminal 1:      bash scripts/stock-setup.sh init
#   terminals 1..N:  bash scripts/stock-setup.sh dl <i> <N>   # i = 1..N
#   terminal 1:      bash scripts/stock-setup.sh finish       # after all shards done
set -euo pipefail

REPO_URL="https://github.com/iamwicked/comfyui-vastai.git"
REPO_DIR="${REPO_DIR:-/workspace/comfyui-vastai}"
COMFYUI_DIR="${COMFYUI_DIR:-/workspace/ComfyUI}"
WORKSPACE="${WORKSPACE:-/workspace}"
CONFIG_DIR="$REPO_DIR/config"

log() { echo "[stock-setup] $*"; }

phase_init() {
  if [ ! -d "$REPO_DIR/.git" ]; then
    log "cloning $REPO_URL -> $REPO_DIR"
    git clone -q "$REPO_URL" "$REPO_DIR"
  else
    log "repo present, pulling latest"
    git -C "$REPO_DIR" pull --ff-only -q || true
  fi
  # Prerequisites the stock template may lack (install only what's missing).
  command -v hf >/dev/null || /venv/main/bin/python -m pip install -q "huggingface_hub[hf_transfer]"
  command -v ollama >/dev/null || curl -fsSL https://ollama.com/install.sh | sh
  command -v unzip >/dev/null || (apt-get update -qq && apt-get install -y -qq unzip)
  command -v wget >/dev/null || (apt-get update -qq && apt-get install -y -qq wget)
  # provision.sh hardcodes python3.11 — point it at the stock venv's python.
  local vpy=/venv/main/bin/python
  [ -x "$vpy" ] || vpy="$(command -v python3)"
  ln -sf "$vpy" /usr/local/bin/python3.11
  log "python3.11 -> $(readlink /usr/local/bin/python3.11)"
  # Link ComfyUI's user/ dir to the persistent layout BEFORE first start,
  # otherwise provisioned workflows are invisible in the UI.
  mkdir -p "$WORKSPACE/comfyui-user"
  if [ ! -L "$COMFYUI_DIR/user" ]; then
    log "linking $COMFYUI_DIR/user -> $WORKSPACE/comfyui-user"
    cp -rn "$COMFYUI_DIR/user/." "$WORKSPACE/comfyui-user/"
    rm -rf "$COMFYUI_DIR/user"
    ln -s "$WORKSPACE/comfyui-user" "$COMFYUI_DIR/user"
  else
    log "user symlink already in place"
  fi
}

phase_dl() {
  local i="${1:?usage: stock-setup.sh dl <i> <N>}"
  local n="${2:?usage: stock-setup.sh dl <i> <N>}"
  log "downloading models: shard $i of $n"
  PHASES=models SHARD_INDEX=$((i - 1)) SHARD_TOTAL="$n" \
    CONFIG_DIR="$CONFIG_DIR" COMFYUI_DIR="$COMFYUI_DIR" WORKSPACE="$WORKSPACE" \
    bash "$REPO_DIR/scripts/provision.sh"
}

phase_finish() {
  log "installing nodes + pulling ollama models (runs once, not per shard)"
  PHASES=nodes,ollama \
    CONFIG_DIR="$CONFIG_DIR" COMFYUI_DIR="$COMFYUI_DIR" WORKSPACE="$WORKSPACE" \
    bash "$REPO_DIR/scripts/provision.sh"
  log "verify:"
  ls "$WORKSPACE/comfyui-user/default/workflows/"
  echo "---"
  find "$WORKSPACE/models" -type f ! -name "*.tmp" | wc -l | xargs echo "model files:"
}

cmd="${1:-all}"
case "$cmd" in
  init)   phase_init ;;
  dl)     phase_dl "${2:-}" "${3:-}" ;;
  finish) phase_finish ;;
  all)    phase_init; phase_dl 1 1; phase_finish ;;
  *) echo "usage: $0 [init | dl <i> <N> | finish]  (default: all)" >&2; exit 1 ;;
esac
log "done."
