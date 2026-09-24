#!/usr/bin/env bash
# install-workflow.sh — ONE command per workflow.
# Downloads every model that workflow needs, installs its custom nodes, and
# drops its workflow JSON into ComfyUI's user dir. Idempotent: re-running only
# fetches what's missing, so overlapping runs in parallel terminals are safe.
#
#   bash scripts/install-workflow.sh qwen21
#   bash scripts/install-workflow.sh --list     # show all workflow ids
#
# Export HF_TOKEN and CIVITAI_TOKEN in this terminal first
# (gated HF repos need the first, all Civitai downloads need the second).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG_DIR="${CONFIG_DIR:-$REPO_DIR/config}"
WORKSPACE="${WORKSPACE:-/workspace}"

cmd="${1:-}"
case "$cmd" in ""|--list|-l|list)
  echo "One command installs everything each workflow needs:"
  echo
  bash "$SCRIPT_DIR/provision.sh" --list-workflows
  echo
  echo "usage:   bash scripts/install-workflow.sh <id>"
  echo "example: bash scripts/install-workflow.sh qwen21"
  exit 0 ;;
esac

if [ -z "${COMFYUI_DIR:-}" ]; then
  if [ -d /workspace/ComfyUI ]; then COMFYUI_DIR=/workspace/ComfyUI      # stock template
  elif [ -d /opt/ComfyUI ]; then COMFYUI_DIR=/opt/ComfyUI                # golden image
  else echo "[install-workflow] error: no ComfyUI found (set COMFYUI_DIR)" >&2; exit 1; fi
fi

# Minimal prereqs (idempotent; the full env setup lives in stock-setup.sh init).
if ! command -v hf >/dev/null; then
  for py in /venv/main/bin/python python3; do
    if command -v "$py" >/dev/null 2>&1; then
      "$py" -m pip install -q "huggingface_hub[hf_xet]" \
        && break || echo "[install-workflow][WARN] could not install 'hf' CLI" >&2
    fi
  done
fi
command -v unzip >/dev/null || (apt-get update -qq && apt-get install -y -qq unzip)
command -v wget >/dev/null || (apt-get update -qq && apt-get install -y -qq wget)
# provision.sh hardcodes python3.11 — shim it at the stock venv's python.
if ! command -v python3.11 >/dev/null; then
  vpy=/venv/main/bin/python
  [ -x "$vpy" ] || vpy="$(command -v python3)"
  ln -sf "$vpy" /usr/local/bin/python3.11
fi
# ComfyUI's user/ dir must be the persistent symlink, otherwise provisioned
# workflows are invisible in the UI.
mkdir -p "$WORKSPACE/comfyui-user"
if [ ! -L "$COMFYUI_DIR/user" ]; then
  cp -rn "$COMFYUI_DIR/user/." "$WORKSPACE/comfyui-user/" 2>/dev/null || true
  rm -rf "$COMFYUI_DIR/user"
  ln -s "$WORKSPACE/comfyui-user" "$COMFYUI_DIR/user"
fi

echo "[install-workflow] installing workflow: $cmd"
WORKFLOW="$cmd" \
  CONFIG_DIR="$CONFIG_DIR" COMFYUI_DIR="$COMFYUI_DIR" WORKSPACE="$WORKSPACE" \
  bash "$SCRIPT_DIR/provision.sh"
echo "[install-workflow] done — open ComfyUI → Workflows (top-left menu) to load it."
