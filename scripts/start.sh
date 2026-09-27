#!/usr/bin/env bash
# start.sh — golden image only: launches Ollama + ComfyUI. Never returns
# (ComfyUI is exec'd). On the stock Vast template, supervisor runs ComfyUI and
# stock-setup.sh registers Ollama; this script is not used there.
set -euo pipefail

export OLLAMA_MODELS="${WORKSPACE:-/workspace}/ollama-models"
# Ollama has no authentication, so it listens on localhost by default. ComfyUI's
# nodes reach it at 127.0.0.1:11434; from your own machine use an SSH tunnel:
#   ssh -p <port> root@<host> -L 11434:127.0.0.1:11434
# Only set OLLAMA_BIND=0.0.0.0 (and map -p 11434:11434) if you accept a public,
# unauthenticated LLM endpoint on your GPU.
export OLLAMA_HOST="${OLLAMA_BIND:-127.0.0.1}:11434"
# How long Ollama keeps a model loaded after last use (0 = unload immediately,
# frees VRAM for big video generations).
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-10m}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}"

echo "[start] launching ollama serve on $OLLAMA_HOST ..."
# provision.sh stops its temporary server by PID before we get here; make sure
# the port is really free, because a background server that fails to bind would
# not trip set -e.
pkill -f "ollama serve" 2>/dev/null || true
for i in $(seq 1 20); do
  (echo > /dev/tcp/127.0.0.1/11434) 2>/dev/null || break
  [ "$i" = 20 ] && echo "[start][WARN] port 11434 still busy after 20 s"
  sleep 1
done
ollama serve >/tmp/ollama.log 2>&1 &
for i in $(seq 1 30); do
  if ollama list >/dev/null 2>&1; then
    echo "[start] ollama is up"
    break
  fi
  if [ "$i" = 30 ]; then
    echo "[start][WARN] ollama serve did not respond — check /tmp/ollama.log (ComfyUI will still start)"
  fi
  sleep 1
done

echo "[start] launching ComfyUI on :8188 ..."
# ComfyUI has no login. It listens on 0.0.0.0 because Vast's direct port mapping
# needs it; anyone who finds the port can use it. Keep the URL private, or put
# it behind an SSH tunnel.
cd /opt/ComfyUI
# Extra flags via COMFYUI_ARGS, e.g. "--highvram --preview-method auto"
# shellcheck disable=SC2086
exec python3.11 main.py --listen 0.0.0.0 --port 8188 ${COMFYUI_ARGS:-}
