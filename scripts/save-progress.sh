#!/usr/bin/env bash
# save-progress.sh — optional end-of-session backup, off-box:
#   1) commits the ComfyUI user dir (workflows, settings) to a git repo
#   2) copies outputs to cloud storage with rclone, then each Influencer_Root of
#      $WORKSPACE/influencer/ and models/loras/characters/ to "<RCLONE_REMOTE>.state"
# The volume already keeps everything on-box; this is disaster recovery.
# Exits non-zero when the push or the copy fails.
#
# Env:
#   WORKFLOWS_REPO    e.g. github.com/you/comfyui-workflows.git
#   GIT_TOKEN         token with push access (sent as an HTTP header, never in the URL)
#   RCLONE_REMOTE     e.g. myremote:comfyui-output
#   SAVE_RCLONE_CONF  optional rclone.conf contents, written once to ~/.config/rclone
#                     (RCLONE_CONFIG is rclone's own variable for the config PATH)
#   OUTPUT_DIR        default: /workspace/ComfyUI/output
set -euo pipefail

WORKSPACE="${WORKSPACE:-/workspace}"
USER_DIR="${USER_DIR:-$WORKSPACE/comfyui-user}"
OUTPUT_DIR="${OUTPUT_DIR:-$WORKSPACE/ComfyUI/output}"
status=0
log() { echo "[save] $*"; }

# git with GIT_TOKEN as an env-only HTTP header, so the token never lands in the
# remote URL, argv or error messages. A missing or wrong token fails, never prompts.
remote_git() {
  if [ -n "${GIT_TOKEN:-}" ]; then
    GIT_TERMINAL_PROMPT=0 GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=http.extraHeader \
      GIT_CONFIG_VALUE_0="Authorization: Basic $(printf 'x-access-token:%s' "$GIT_TOKEN" | base64 | tr -d '\n')" \
      git "$@"
  else
    GIT_TERMINAL_PROMPT=0 git "$@"
  fi
}

# ---- 1. Workflows -> git ----
if [ -n "${WORKFLOWS_REPO:-}" ]; then
  cd "$USER_DIR"
  repo_url="https://${WORKFLOWS_REPO#https://}"
  add_mode=-A
  if [ ! -d .git ]; then
    git init
    git checkout -b main 2>/dev/null || true
    log "initialized git repo in $USER_DIR"
    # A new instance and an existing backup: continue the backup's history, so the
    # push below is a fast-forward (an unrelated fresh history would be rejected).
    # This first snapshot records no deletions, so workflows that only the backup
    # has stay in it.
    if remote_git fetch "$repo_url" main; then
      git reset -q FETCH_HEAD
      add_mode=--ignore-removal
      log "continuing the backup history of $repo_url"
    else
      log "no backup history to continue at $repo_url (first backup, or not reachable): starting a new one"
    fi
  fi
  if [ ! -f .gitignore ]; then
    printf '%s\n' '# written by save-progress.sh: instance-local state, not worth backing up' '.provisioned-*' '*.tmp' > .gitignore
  fi
  git add "$add_mode" .
  if git -c user.email="${GIT_EMAIL:-vastai@local}" -c user.name="${GIT_NAME:-vastai}" \
       commit -m "session snapshot $(date -u +%F-%T)"; then
    log "committed workflow snapshot"
  else
    log "nothing new to commit"
  fi
  if [ -n "${GIT_TOKEN:-}" ]; then
    if remote_git push "$repo_url" HEAD:main; then
      log "pushed to $repo_url"
    else
      log "push failed (check GIT_TOKEN scope and that the repo exists)"
      status=1
    fi
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
    # umask in a subshell: the file is created 600, never readable by others even briefly.
    ( umask 077; printf '%s\n' "$SAVE_RCLONE_CONF" > "$conf" )
    log "wrote $conf"
  fi
  # copy, never sync: sync mirrors deletions, so running it from a fresh or empty
  # volume would wipe the backup.
  log "copying $OUTPUT_DIR to $RCLONE_REMOTE ..."
  if rclone copy "$OUTPUT_DIR" "$RCLONE_REMOTE" --progress; then
    log "output copy complete"
  else
    log "rclone copy failed"
    status=1
  fi
  # Influencer pipeline state (design §C12, Req 25.5): each Influencer_Root and the Character_LoRAs go
  # into their own folder under <remote>.state, which the outputs copy above never writes into. A missing
  # folder is skipped; a failed copy is logged and the rest still run, without changing the exit status.
  state="${RCLONE_REMOTE%/}.state"
  for d in "$WORKSPACE"/influencer/*/; do
    [ -d "$d" ] || continue
    log "copying $d to $state/influencer/$(basename "$d") ..."
    rclone copy "$d" "$state/influencer/$(basename "$d")" --progress || log "rclone copy failed: $d"
  done
  if [ -d "$WORKSPACE/models/loras/characters" ]; then
    log "copying $WORKSPACE/models/loras/characters to $state/characters ..."
    rclone copy "$WORKSPACE/models/loras/characters" "$state/characters" --progress \
      || log "rclone copy failed: $WORKSPACE/models/loras/characters"
  fi
else
  log "RCLONE_REMOTE not set — outputs stay on the volume only"
fi

if [ "$status" -eq 0 ]; then log "done."; else log "done, with problems (see above)."; fi
exit "$status"
