#!/usr/bin/env bash
# start.sh — launches Ollama + ComfyUI. Never returns (ComfyUI is exec'd).
set -euo pipefail

export OLLAMA_MODELS="${WORKSPACE:-/workspace}/ollama-models"
export OLLAMA_HOST="0.0.0.0:11434"
# How long Ollama keeps a model loaded after last use. On an 80GB card you can
# raise this; set to 0 to unload immediately (frees VRAM for big image gens).
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-10m}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}"

echo "[start] launching ollama serve..."
# provision.sh stops its temporary server before we get here, but make sure the
# port is actually free — a lingering socket would make the new server fail to
# bind, and since it runs in the background set -e would NOT catch that.
pkill -f "ollama serve" 2>/dev/null || true
for _ in $(seq 1 20); do
  (echo > /dev/tcp/127.0.0.1/11434) 2>/dev/null || break
  sleep 1
done
ollama serve >/tmp/ollama.log 2>&1 &
for i in $(seq 1 30); do
  if ollama list >/dev/null 2>&1; then
    echo "[start] ollama is up on :11434"
    break
  fi
  if [ "$i" = 30 ]; then
    echo "[start][WARN] ollama serve did not respond — check /tmp/ollama.log (ComfyUI will still start)"
  fi
  sleep 1
done

echo "[start] launching ComfyUI on :8188 ..."
cd /opt/ComfyUI
# Extra flags via COMFYUI_ARGS, e.g. "--highvram --preview-method auto"
# shellcheck disable=SC2086
exec python3.11 main.py --listen 0.0.0.0 --port 8188 ${COMFYUI_ARGS:-}
