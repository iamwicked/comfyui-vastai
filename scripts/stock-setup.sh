#!/usr/bin/env bash
# stock-setup.sh — first-boot setup for a FRESH stock Vast.ai template instance
# (ComfyUI at /workspace/ComfyUI, python at /venv/main). The golden-image flow
# needs none of this; use this script when you're on the stock template instead.
#
# Downloads are organized BY WORKFLOW: one command installs everything a single
# workflow needs (its models + its custom nodes + its workflow JSON).
#
#   bash scripts/stock-setup.sh init       # clone repo, prereqs, user-dir link
#   bash scripts/stock-setup.sh qwen21     # install ONE workflow (any id below)
#   bash scripts/stock-setup.sh all        # init + every workflow + finish
#   bash scripts/stock-setup.sh finish     # global nodes + ollama (run once)
#   bash scripts/stock-setup.sh list       # show workflow ids
#
# To saturate bandwidth, run several workflow installs in parallel terminals
# (e.g. one terminal per workflow) — each is idempotent, so overlaps are safe.
# Then run `finish` once in any terminal.
#
# Export HF_TOKEN and CIVITAI_TOKEN in EVERY terminal before running
# (gated HF repos need the first, all Civitai downloads need the second).
set -euo pipefail

REPO_URL="https://github.com/iamwicked/comfyui-vastai.git"
REPO_DIR="${REPO_DIR:-/workspace/comfyui-vastai}"
COMFYUI_DIR="${COMFYUI_DIR:-/workspace/ComfyUI}"
WORKSPACE="${WORKSPACE:-/workspace}"
CONFIG_DIR="$REPO_DIR/config"

# Every installable workflow (matches provision.sh --list-workflows, minus the
# disabled `flux` stack whose base needs HF approval).
ALL_WORKFLOWS="qwen21 qwen21-edit krea2 edit-2509 krea-style wan-i2v global"

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
  command -v hf >/dev/null || /venv/main/bin/python -m pip install -q "huggingface_hub[hf_xet]"
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

phase_workflow() {
  local id="$1"
  log "installing workflow: $id"
  REPO_DIR="$REPO_DIR" COMFYUI_DIR="$COMFYUI_DIR" WORKSPACE="$WORKSPACE" \
    bash "$REPO_DIR/scripts/install-workflow.sh" "$id"
}

phase_finish() {
  log "installing global nodes + pulling ollama models (runs once)"
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
  finish) phase_finish ;;
  list)   bash "$REPO_DIR/scripts/install-workflow.sh" --list ;;
  all)
    phase_init
    for id in $ALL_WORKFLOWS; do phase_workflow "$id"; done
    phase_finish
    ;;
  *)      phase_workflow "$cmd" ;;   # any workflow id -> one-command install
esac
log "done."
