#!/usr/bin/env bash
# provision.sh — idempotent environment setup.
# Safe to re-run any time: it only fetches what's missing.
# Reads lists from $CONFIG_DIR (baked into the image), optionally refreshed
# at boot from URLs so you can edit your model/node lists without rebuilding.
#
# Per-workflow installs: set WORKFLOW=<id> (or pass --workflow <id>) to fetch
# only the models + workflow JSON (+ tagged custom nodes) that ONE workflow
# needs. One command per workflow:
#   WORKFLOW=qwen21 bash scripts/provision.sh
#   bash scripts/provision.sh --workflow wan-i2v
#   bash scripts/provision.sh --list-workflows   # show the catalog
# With WORKFLOW set, PHASES defaults to "nodes,models" (Ollama stays global).
set -euo pipefail

CONFIG_DIR="${CONFIG_DIR:-/opt/comfyui-config}"
COMFYUI_DIR="${COMFYUI_DIR:-/opt/ComfyUI}"
WORKSPACE="${WORKSPACE:-/workspace}"

log()  { echo "[provision] $*"; }
warn() { echo "[provision][WARN] $*" >&2; }

# ---- 0. Optional remote config override (no rebuild needed) ----
# Set MODELS_LIST_URL / NODES_TXT_URL / OLLAMA_MODELS_TXT_URL to raw URLs
# (e.g. a gist) to override the baked-in lists at boot.
if [ -n "${MODELS_LIST_URL:-}" ]; then
  log "Fetching models.list override"
  curl -fsSL "$MODELS_LIST_URL" -o "$CONFIG_DIR/models.list" || warn "fetch failed"
fi
if [ -n "${NODES_TXT_URL:-}" ]; then
  log "Fetching nodes.txt override"
  curl -fsSL "$NODES_TXT_URL" -o "$CONFIG_DIR/nodes.txt" || warn "fetch failed"
fi
if [ -n "${OLLAMA_MODELS_TXT_URL:-}" ]; then
  log "Fetching ollama-models.txt override"
  curl -fsSL "$OLLAMA_MODELS_TXT_URL" -o "$CONFIG_DIR/ollama-models.txt" || warn "fetch failed"
fi

# ---- 1. ComfyUI update (default OFF: pinned at build for reproducibility) ----
# Set UPDATE_COMFYUI=true to pull latest on boot, or COMFYUI_REF=<tag/commit> to pin.
if [ "${UPDATE_COMFYUI:-false}" = "true" ]; then
  log "Updating ComfyUI..."
  git -C "$COMFYUI_DIR" pull --ff-only -q || warn "ComfyUI pull failed"
  python3.11 -m pip install -q -r "$COMFYUI_DIR/requirements.txt" || warn "requirements reinstall failed"
elif [ -n "${COMFYUI_REF:-}" ]; then
  log "Pinning ComfyUI to $COMFYUI_REF"
  git -C "$COMFYUI_DIR" fetch -q --depth 1 origin "$COMFYUI_REF" \
    && git -C "$COMFYUI_DIR" checkout -q "$COMFYUI_REF" \
    || warn "could not checkout $COMFYUI_REF"
fi

# ---- 2. Custom nodes (clone on first boot, pull updates after) ----
install_nodes() {
  local list="$CONFIG_DIR/nodes.txt"
  [ -f "$list" ] || { log "no nodes.txt, skipping"; return 0; }
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    local url="${line%%|*}"
    local tail="${line#*|}"
    local ref="" wftag=""
    if [ "$tail" != "$line" ]; then
      ref="${tail%%|*}"
      wftag="${tail#*|}"
      [ "$wftag" = "$tail" ] && wftag=""   # no 3rd field -> untagged (global)
    fi
    wf_skip "$wftag" && continue
    local name
    name="$(basename "$url" .git)"
    local dest="$COMFYUI_DIR/custom_nodes/$name"
    if [ -d "$dest/.git" ]; then
      log "Updating node: $name"
      git -C "$dest" pull --ff-only -q || warn "pull failed for $name"
    else
      log "Cloning node: $name"
      if [ -n "$ref" ]; then
        git clone -q --depth 1 --branch "$ref" "$url" "$dest" \
          || { warn "clone failed for $url"; continue; }
      else
        git clone -q --depth 1 "$url" "$dest" \
          || { warn "clone failed for $url"; continue; }
      fi
    fi
    if [ -f "$dest/requirements.txt" ]; then
      log "Installing requirements for $name"
      python3.11 -m pip install -q -r "$dest/requirements.txt" \
        || warn "requirements failed for $name"
    fi
    if [ -f "$dest/install.py" ]; then
      log "Running install.py for $name"
      (cd "$dest" && python3.11 install.py) || warn "install.py failed for $name"
    fi
  done < "$list"
}

# ---- 3. Models (HF / Civitai / direct URL) ----
kind_to_dir() {
  case "$1" in
    checkpoint) echo checkpoints ;;
    lora)       echo loras ;;
    vae)        echo vae ;;
    controlnet) echo controlnet ;;
    upscale)    echo upscale_models ;;
    embedding)  echo embeddings ;;
    clip)       echo clip ;;
    text_encoder) echo text_encoders ;;
    diffusion)  echo diffusion_models ;;
    *)          echo "$1" ;;
  esac
}

download_models() {
  local list="$CONFIG_DIR/models.list"
  [ -f "$list" ] || { log "no models.list, skipping"; return 0; }
  # High-performance HF transfers (HF_HUB_ENABLE_HF_TRANSFER is deprecated and ignored).
  export HF_XET_HIGH_PERFORMANCE=1
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    # format: kind|filename|source|source_id|subdir(optional)|wf:ids(optional)
    local kind="${line%%|*}";          local rest="${line#*|}"
    local filename="${rest%%|*}";      rest="${rest#*|}"
    local source="${rest%%|*}";        rest="${rest#*|}"
    local source_id="${rest%%|*}";     rest="${rest#*|}"
    local subdir="${rest%%|*}";        rest="${rest#*|}"
    local wftag="$rest"
    [ "$wftag" = "$subdir" ] && wftag=""   # no 6th field -> untagged (global)
    # Per-workflow mode: keep only lines tagged for $WORKFLOW
    # (or only untagged lines when WORKFLOW=global).
    wf_skip "$wftag" && continue
    local destdir dest marker=""
    if [ "$kind" = "workflow" ]; then
      # Workflows live in ComfyUI's user dir (entrypoint.sh symlinks it to the volume),
      # NOT under models/. Track extraction with a marker so re-runs stay idempotent.
      destdir="$WORKSPACE/comfyui-user/default/workflows"
      marker="$destdir/.provisioned-${filename%.zip}"
      if [ -f "$marker" ]; then
        log "workflow already extracted, skipping: $filename"
        continue
      fi
    else
      [ -z "$subdir" ] && subdir="$(kind_to_dir "$kind")"
      destdir="$WORKSPACE/models/$subdir"
      if [ -s "$destdir/$filename" ]; then
        log "exists, skipping: $subdir/$filename"
        continue
      fi
    fi
    mkdir -p "$destdir"
    dest="$destdir/$filename"
    log "downloading: $subdir/$filename (via $source)"
    case "$source" in
      hf)
        # source_id: repo_id::path/inside/repo  (path defaults to filename at repo root)
        local repo="${source_id%%::*}"
        local rpath="${source_id#*::}"
        [ "$rpath" = "$repo" ] && rpath="$filename"
        local tmpd
        tmpd="$(mktemp -d)"
        if hf download "$repo" "$rpath" --local-dir "$tmpd" --quiet; then
          if mv "$tmpd/$rpath" "$dest"; then
            log "saved: $subdir/$filename"
          else
            warn "downloaded but could not move file for $source_id"
          fi
        else
          warn "HF download failed: $source_id (gated repos need HF_TOKEN + accepted license)"
        fi
        rm -rf "$tmpd"
        ;;
      civitai)
        # source_id: model VERSION id (from the download URL: civitai.com/api/download/models/<id>)
        if [ -z "${CIVITAI_TOKEN:-}" ]; then
          warn "CIVITAI_TOKEN not set, skipping $filename"
          continue
        fi
        if curl -fsSL --retry 3 --retry-delay 5 \
             -H "Authorization: Bearer ${CIVITAI_TOKEN}" \
             -o "$dest.tmp" \
             "https://civitai.com/api/download/models/${source_id}"; then
          mv "$dest.tmp" "$dest"
          log "saved: $subdir/$filename"
        else
          warn "Civitai download failed (version id: $source_id)"
          rm -f "$dest.tmp"
        fi
        ;;
      url)
        if wget -q -c -O "$dest.tmp" "$source_id"; then
          mv "$dest.tmp" "$dest"
          log "saved: $subdir/$filename"
        else
          warn "URL download failed: $source_id"
          rm -f "$dest.tmp"
        fi
        ;;
      *)
        warn "unknown source '$source' for $filename"
        ;;
    esac
    # Workflow archives ship as .zip — extract the .json workflow(s) in place,
    # then drop the zip and leave a marker so the next boot skips re-downloading.
    if [ "$kind" = "workflow" ] && [ "${filename##*.}" = "zip" ] && [ -f "$dest" ]; then
      log "extracting workflow archive: $filename"
      if unzip -o -q "$dest" -d "$destdir"; then
        rm -f "$dest"
        [ -n "$marker" ] && touch "$marker"
      else
        warn "unzip failed for $filename"
      fi
    fi
    # Plain (non-zip) workflows download straight to the workflows dir —
    # leave the marker too so the next boot skips re-downloading.
    if [ "$kind" = "workflow" ] && [ "${filename##*.}" != "zip" ] && [ -s "$dest" ]; then
      [ -n "$marker" ] && touch "$marker"
    fi
  done < "$list"
}

# ---- 4. Ollama models (into the volume; skipped if already present) ----
pull_ollama() {
  local list="$CONFIG_DIR/ollama-models.txt"
  [ -f "$list" ] || { log "no ollama-models.txt, skipping"; return 0; }
  export OLLAMA_MODELS="$WORKSPACE/ollama-models"
  export OLLAMA_HOST="127.0.0.1:11434"
  ollama serve >/tmp/ollama-provision.log 2>&1 &
  local i=0
  until ollama list >/dev/null 2>&1 || [ $i -ge 30 ]; do sleep 2; i=$((i+1)); done
  while IFS= read -r model || [ -n "$model" ]; do
    case "$model" in ''|\#*) continue ;; esac
    # Normalize: "qwen3" means "qwen3:latest" in `ollama list` output, so an
    # untagged entry would otherwise never match and re-pull on every boot.
    local want="$model"
    case "$want" in *:*) ;; *) want="$want:latest" ;; esac
    if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -qx "$want"; then
      log "ollama model present: $model"
    else
      log "pulling ollama model: $model (this can take a while on first boot)"
      ollama pull "$model" || warn "ollama pull failed: $model"
    fi
  done < "$list"
  pkill -f "ollama serve" || true
  sleep 1
}

# ---- 5. Workflow selection ----
# WORKFLOW=<id> (or --workflow <id>): install only what one workflow needs —
# its models, its workflow JSON, and its tagged custom nodes. Untagged
# (global) models/nodes are skipped; WORKFLOW=global installs just those.
# Parallelize across terminals by giving each terminal a different workflow id.
list_workflows() {
  cat <<'EOF'
qwen21       Qwen-Image 2.1 text-to-image (+ Lenovo UltraReal LoRA)
qwen21-edit  Qwen-Image 2.1 image edit — up to 10 refs, no extra models
krea2        Krea 2 text-to-image (+ Lenovo UltraReal LoRA)
edit-2509    Qwen-Image-Edit 2509 multi-image edit (Lightning 4-step turbo)
krea-style   Krea 2 Turbo style reference (1-3 style images)
wan-i2v      Wan2.2 Remix painter image-to-video (uncensored)
global       Shared extras not tied to one workflow (Z-Image Turbo ckpt)
flux         Flux.2 Klein 9B support files (BASE not downloaded — needs HF approval)
EOF
}

workflow_match() { # $1: "wf:a,b" (or ""), $2: wanted id -> 0 on match
  case ",${1#wf:}," in *",$2,"*) return 0 ;; *) return 1 ;; esac
}

wf_skip() { # $1: wftag field -> 0 when this line should be SKIPPED
  [ -z "${WORKFLOW:-}" ] && return 1               # no filter -> keep line
  if [ "$WORKFLOW" = "global" ]; then
    [ -n "$1" ] && return 0 || return 1             # global: skip tagged lines
  fi
  workflow_match "$1" "$WORKFLOW" && return 1 || return 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --workflow) WORKFLOW="${2:?--workflow needs a workflow id}"; shift 2 ;;
    --list-workflows) list_workflows; exit 0 ;;
    *) warn "ignoring unknown argument: $1"; shift ;;
  esac
done

if [ -n "${WORKFLOW:-}" ]; then
  list_workflows | awk '{print $1}' | grep -qx "$WORKFLOW" \
    || { warn "unknown workflow '$WORKFLOW' — available workflows:"; list_workflows; exit 1; }
  # Per-workflow runs default to just nodes+models; set PHASES explicitly to
  # add ollama (it always pulls the full LLM list — not per-workflow).
  if [ -z "${PHASES+x}" ]; then PHASES="nodes,models"; fi
  log "workflow mode: $WORKFLOW (PHASES=$PHASES)"
fi

# ---- 6. Phase dispatch ----
# PHASES: comma-separated subset of nodes,models,ollama (default: all three,
# or nodes,models when WORKFLOW is set).
PHASES="${PHASES:-nodes,models,ollama}"

run_phase() { case ",${PHASES}," in *",${1},"*) return 0 ;; *) return 1 ;; esac; }

if run_phase nodes; then install_nodes; fi
if run_phase models; then download_models; fi
if run_phase ollama; then pull_ollama; fi
log "provisioning complete."
