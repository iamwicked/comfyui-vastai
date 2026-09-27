#!/usr/bin/env bash
# install-workflow.sh — ONE command per workflow: its models, custom nodes,
# Ollama models and workflow JSON. Idempotent; parallel terminals are safe
# (each file and the node/pip phase are locked).
#
#   bash scripts/install-workflow.sh krea2
#   bash scripts/install-workflow.sh --list
#
# Export HF_TOKEN and CIVITAI_TOKEN in this terminal first. Restart ComfyUI
# afterwards so new nodes load:  bash scripts/stock-setup.sh restart
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG=install-workflow
# shellcheck source=scripts/common.sh
. "$SCRIPT_DIR/common.sh"

main() {
  local id="${1:-}" rc=0
  case "$id" in
    ""|--list|-l|list)
      echo "One command installs everything a workflow needs:"
      echo
      bash "$SCRIPT_DIR/provision.sh" --list-workflows
      echo
      echo "usage:   bash scripts/install-workflow.sh <id>"
      exit 0 ;;
  esac
  local ids
  ids="$(grep -vE '^[[:space:]]*(#|$)' "$CONFIG_DIR/catalog.txt" | cut -d'|' -f1)"
  if ! grep -qxF -- "$id" <<< "$ids"; then
    warn "unknown workflow '$id'"; bash "$SCRIPT_DIR/provision.sh" --list-workflows; exit 1
  fi
  [ -n "${HF_TOKEN:-}" ] || warn "HF_TOKEN is not set in this terminal (gated HF files will fail)"
  [ -n "${CIVITAI_TOKEN:-}" ] || warn "CIVITAI_TOKEN is not set in this terminal (Civitai files will be skipped)"

  # The same safe, idempotent layout steps as `stock-setup.sh init`, so this
  # also works on an instance where init was skipped.
  ensure_prereqs
  check_disk
  ensure_model_paths
  ensure_user_link
  check_min_comfyui "$id"
  if workflow_uses_ollama "$id"; then ensure_ollama || true; fi

  say "installing workflow: $id"
  WORKFLOW="$id" PHASES="${PHASES:-nodes,models,ollama}" bash "$SCRIPT_DIR/provision.sh" || rc=$?
  echo
  bash "$SCRIPT_DIR/provision.sh" --status "$id" || true
  echo
  if [ "$rc" -eq 0 ]; then
    say "done. Restart ComfyUI once your installs finish so new nodes load:"
  else
    warn "finished with problems (see above). Fix them and re-run: bash scripts/install-workflow.sh $id"
  fi
  say "  bash $REPO_DIR/scripts/stock-setup.sh restart   (or run: finish)"
  if [ "$NEED_RESTART" = 1 ]; then say "model paths / user dir changed in this run, so a restart is required."; fi
  return "$rc"
}

main "$@"
