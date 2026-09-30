#!/usr/bin/env bash
# vast-provision.sh — hands-off first boot on the stock Vast.ai ComfyUI template.
#
# Vast runs a PROVISIONING_SCRIPT once per new instance, before it starts
# ComfyUI, and logs it to /var/log/portal/provisioning.log. In your copy of the
# ComfyUI template (Edit → Environment Variables), set:
#
#   PROVISIONING_SCRIPT  https://raw.githubusercontent.com/iamwicked/comfyui-vastai/main/scripts/vast-provision.sh
#   WORKFLOWS            workflow ids to install, comma-separated, e.g.  krea2,wan-animate
#                        ("all" = every workflow in `all`; empty = tools, model paths
#                        and Ollama only)
#   UPDATE_COMFYUI       optional: 1 = move ComfyUI core to the newest release first
#                        (MiniMax H3 needs >= 0.30.0)
#
# HF_TOKEN and CIVITAI_TOKEN come from the instance environment: set them in Vast's
# account-level Environment Variables (Settings), or in the template if it is private.
#
# It runs: clone or update the repo → stock-setup.sh init → each workflow →
# stock-setup.sh finish. It always exits 0, so ComfyUI starts even if something
# failed; the summary at the end lists what to re-run by hand.
set -uo pipefail
# Nobody can answer a git password prompt during provisioning: fail fast instead.
export GIT_TERMINAL_PROMPT=0

REPO_URL="${REPO_URL:-https://github.com/iamwicked/comfyui-vastai.git}"
REPO_DIR="${REPO_DIR:-/workspace/comfyui-vastai}"
REPO_REF="${REPO_REF:-main}"
log() { echo "[vast-provision] $*"; }

main() {
  local S ids id failed=() t0
  t0="$(date +%s)"
  log "start $(date -u +%FT%TZ)  WORKFLOWS='${WORKFLOWS:-}'"
  [ -n "${HF_TOKEN:-}" ] || log "WARN: HF_TOKEN is not set; gated HuggingFace files will fail"
  [ -n "${CIVITAI_TOKEN:-}" ] || log "WARN: CIVITAI_TOKEN is not set; Civitai files will be skipped"

  if [ -d "$REPO_DIR/.git" ]; then
    log "updating $REPO_DIR"
    git -C "$REPO_DIR" pull --ff-only --progress || log "WARN: could not update the repo; using the current checkout"
  else
    log "cloning $REPO_URL ($REPO_REF) into $REPO_DIR"
    if ! git clone --progress --branch "$REPO_REF" "$REPO_URL" "$REPO_DIR"; then
      log "ERROR: clone failed; nothing installed. ComfyUI starts as the stock template."
      return 0
    fi
  fi
  S="$REPO_DIR/scripts/stock-setup.sh"

  # Vast starts supervisord just before provisioning; let it open its socket so
  # init registers Ollama as a supervisor program instead of a loose process.
  if command -v supervisorctl >/dev/null 2>&1; then
    for _ in $(seq 1 30); do supervisorctl pid >/dev/null 2>&1 && break; sleep 1; done
  fi

  SKIP_PULL=1 bash "$S" init || failed+=("init")

  ids="${WORKFLOWS:-}"
  ids=${ids//[,\"\']/ }   # commas or spaces; stray quotes from the env editor are dropped
  if [ "$(printf '%s' "$ids" | tr -d '[:space:]')" = "all" ]; then
    ids="$(grep -vE '^[[:space:]]*(#|$)' "$REPO_DIR/config/catalog.txt" | awk -F'|' '$2=="yes"{print $1}' | tr '\n' ' ')"
  fi
  for id in $ids; do
    log "workflow: $id"
    bash "$S" "$id" || failed+=("$id")
  done

  bash "$S" finish || failed+=("finish")

  log "done in $(( ($(date +%s) - t0) / 60 )) min"
  if [ "${#failed[@]}" -gt 0 ]; then
    log "problems in: ${failed[*]}"
    log "fix the cause (usually a token), then in a terminal: bash $S <id>   (and: bash $S finish)"
  fi
  return 0
}

main "$@"
exit 0
