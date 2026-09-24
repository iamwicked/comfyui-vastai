#!/usr/bin/env bash
# save-progress.sh — run at the end of a session (optional but recommended).
# 1) commits your workflows/settings to a git repo (off-box backup)
# 2) syncs /workspace/output to cloud storage via rclone (off-box backup)
# The volume already keeps everything on-box; this is your disaster recovery.
set -euo pipefail

WORKSPACE="${WORKSPACE:-/workspace}"
log() { echo "[save] $*"; }

# ---- 1. Workflows -> git ----
if [ -n "${WORKFLOWS_REPO:-}" ]; then
  cd "$WORKSPACE/comfyui-user"
  if [ ! -d .git ]; then
    git init -q
    git checkout -qb main 2>/dev/null || true
    log "initialized git repo in comfyui-user"
  fi
  git add -A
  if git -c user.email="${GIT_EMAIL:-vastai@local}" -c user.name="${GIT_NAME:-vastai}" \
       commit -qm "session snapshot $(date -u +%F-%T)"; then
    log "committed workflow snapshot"
  else
    log "nothing new to commit"
  fi
  if [ -n "${GIT_TOKEN:-}" ]; then
    # WORKFLOWS_REPO like "github.com/you/comfyui-workflows.git"
    repo_path="${WORKFLOWS_REPO#https://}"
    if git push -q "https://${GIT_TOKEN}@${repo_path}" HEAD:main 2>/tmp/git-push.err; then
      log "pushed to $WORKFLOWS_REPO"
    else
      log "push failed (see /tmp/git-push.err)"
    fi
  else
    log "GIT_TOKEN not set — committed locally only (push manually later)"
  fi
else
  log "WORKFLOWS_REPO not set — skipping git backup"
fi

# ---- 2. Outputs -> cloud via rclone ----
if [ -n "${RCLONE_REMOTE:-}" ]; then
  # RCLONE_REMOTE like "myremote:comfyui-output"
  # Provide config inline via RCLONE_CONFIG env, or pre-configure with `rclone config`.
  if [ -n "${RCLONE_CONFIG:-}" ] && [ ! -f ~/.config/rclone/rclone.conf ]; then
    mkdir -p ~/.config/rclone
    printf '%s\n' "$RCLONE_CONFIG" > ~/.config/rclone/rclone.conf
    chmod 600 ~/.config/rclone/rclone.conf
  fi
  log "syncing outputs to $RCLONE_REMOTE ..."
  rclone sync "$WORKSPACE/output" "$RCLONE_REMOTE" --progress \
    && log "output sync complete" \
    || log "rclone sync failed"
else
  log "RCLONE_REMOTE not set — outputs stay on the volume only"
fi

log "done."
