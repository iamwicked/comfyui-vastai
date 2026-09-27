#!/usr/bin/env bash
# save-progress.sh — optional end-of-session backup, off-box:
#   1) commits the ComfyUI user dir (workflows, settings) to a git repo
#   2) copies outputs to cloud storage with rclone
# The volume already keeps everything on-box; this is disaster recovery.
#
# Env:
#   WORKFLOWS_REPO    e.g. github.com/you/comfyui-workflows.git
#   GIT_TOKEN         token with push access (sent as an HTTP header, never in the URL)
#   RCLONE_REMOTE     e.g. myremote:comfyui-output
#   SAVE_RCLONE_CONF  optional rclone.conf contents, written once to ~/.config/rclone
#                     (RCLONE_CONFIG is rclone's own variable for the config PATH)
#   OUTPUT_DIR        default: /workspace/output (golden image) or /workspace/ComfyUI/output
set -euo pipefail

WORKSPACE="${WORKSPACE:-/workspace}"
USER_DIR="${USER_DIR:-$WORKSPACE/comfyui-user}"
if [ -z "${OUTPUT_DIR:-}" ]; then
  if [ -d "$WORKSPACE/output" ]; then OUTPUT_DIR="$WORKSPACE/output"
  else OUTPUT_DIR="$WORKSPACE/ComfyUI/output"; fi
fi
log() { echo "[save] $*"; }

# ---- 1. Workflows -> git ----
if [ -n "${WORKFLOWS_REPO:-}" ]; then
  cd "$USER_DIR"
  if [ ! -d .git ]; then
    git init
    git checkout -b main 2>/dev/null || true
    log "initialized git repo in $USER_DIR"
  fi
  if [ ! -f .gitignore ]; then
    printf '%s\n' '# written by save-progress.sh: instance-local state, not worth backing up' '.provisioned-*' '*.tmp' > .gitignore
  fi
  git add -A
  if git -c user.email="${GIT_EMAIL:-vastai@local}" -c user.name="${GIT_NAME:-vastai}" \
       commit -m "session snapshot $(date -u +%F-%T)"; then
    log "committed workflow snapshot"
  else
    log "nothing new to commit"
  fi
  if [ -n "${GIT_TOKEN:-}" ]; then
    repo_url="https://${WORKFLOWS_REPO#https://}"
    # The token travels in an env-only git config header, so it never lands in
    # the remote URL, argv or error messages.
    auth="$(printf 'x-access-token:%s' "$GIT_TOKEN" | base64 | tr -d '\n')"
    if GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader GIT_CONFIG_VALUE_0="Authorization: Basic $auth" \
         git push "$repo_url" HEAD:main; then
      log "pushed to $repo_url"
    else
      log "push failed (check GIT_TOKEN scope and that the repo exists)"
    fi
    unset auth
  else
    log "GIT_TOKEN not set — committed locally only (push manually later)"
  fi
else
  log "WORKFLOWS_REPO not set — skipping git backup"
fi

# ---- 2. Outputs -> cloud via rclone ----
if [ -n "${RCLONE_REMOTE:-}" ]; then
  command -v rclone >/dev/null 2>&1 || { log "rclone is not installed (apt-get install -y rclone)"; exit 1; }
  # Older versions of this script put config *contents* in RCLONE_CONFIG, which
  # rclone reads as a file path. Accept that once, then get out of rclone's way.
  if [ -n "${RCLONE_CONFIG:-}" ] && [ ! -f "$RCLONE_CONFIG" ]; then
    log "RCLONE_CONFIG holds config text, not a path — treating it as SAVE_RCLONE_CONF"
    SAVE_RCLONE_CONF="${SAVE_RCLONE_CONF:-$RCLONE_CONFIG}"
    unset RCLONE_CONFIG
  fi
  conf="$HOME/.config/rclone/rclone.conf"
  if [ -n "${SAVE_RCLONE_CONF:-}" ] && [ ! -f "$conf" ]; then
    mkdir -p "$(dirname "$conf")"
    printf '%s\n' "$SAVE_RCLONE_CONF" > "$conf"
    chmod 600 "$conf"
    log "wrote $conf"
  fi
  # copy, never sync: sync mirrors deletions, so running it from a fresh or empty
  # volume would wipe the backup.
  log "copying $OUTPUT_DIR to $RCLONE_REMOTE ..."
  if rclone copy "$OUTPUT_DIR" "$RCLONE_REMOTE" --progress; then
    log "output copy complete"
  else
    log "rclone copy failed"
  fi
else
  log "RCLONE_REMOTE not set — outputs stay on the volume only"
fi

log "done."
