#!/usr/bin/env bash
# stock-setup.sh — fresh-instance entrypoint for the STOCK Vast.ai ComfyUI
# template (ComfyUI in /workspace/ComfyUI on :18188 behind Vast's proxy,
# python in /venv/main, processes under supervisor). Never starts a second
# ComfyUI: it restarts the template's own one when needed.
#
#   bash scripts/stock-setup.sh init          # once per instance: tools, model paths, user dir, Ollama
#   bash scripts/stock-setup.sh <id>          # one workflow (same as: dl <id>), parallel terminals OK
#   bash scripts/stock-setup.sh finish        # global nodes + Ollama models, then restart ComfyUI
#   bash scripts/stock-setup.sh all           # init + every workflow in `all` + finish, one terminal
#   bash scripts/stock-setup.sh status [id]   # installed / missing, per workflow
#   bash scripts/stock-setup.sh restart       # restart the running ComfyUI
#   bash scripts/stock-setup.sh list          # workflow ids
#
# Export HF_TOKEN and CIVITAI_TOKEN in EVERY terminal before running.
# UPDATE_COMFYUI=1 with init also updates ComfyUI core to the newest release
# (MiniMax H3 needs >= 0.30.0); COMFYUI_REF=<tag|commit> pins it instead.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TAG=stock-setup
# shellcheck source=scripts/common.sh
. "$SCRIPT_DIR/common.sh"
ARGS=("$@")

in_all_ids() { grep -vE '^[[:space:]]*(#|$)' "$CONFIG_DIR/catalog.txt" | awk -F'|' '$2=="yes"{print $1}'; }

phase_init() {
  local before after have
  if [ -d "$REPO_DIR/.git" ] && [ "${SKIP_PULL:-0}" != 1 ]; then
    before="$(git -C "$REPO_DIR" rev-parse HEAD)"
    git -C "$REPO_DIR" pull --ff-only --progress || warn "could not fast-forward $REPO_DIR (local edits?) — using the current checkout"
    after="$(git -C "$REPO_DIR" rev-parse HEAD)"
    if [ "$before" != "$after" ] && [ -z "${STOCK_SETUP_REEXEC:-}" ]; then
      say "repo updated ${before:0:7} -> ${after:0:7}; re-running with the new scripts"
      STOCK_SETUP_REEXEC=1 exec bash "$REPO_DIR/scripts/stock-setup.sh" "${ARGS[@]}"
    fi
  fi
  ensure_prereqs
  check_disk
  have="$(comfyui_version)"
  say "ComfyUI ${have:-unknown} at $COMFYUI_DIR, python $PYTHON"
  if [ "${UPDATE_COMFYUI:-0}" = 1 ] || [ "${UPDATE_COMFYUI:-}" = true ] || [ -n "${COMFYUI_REF:-}" ]; then
    PHASES=comfyui bash "$SCRIPT_DIR/provision.sh" || warn "ComfyUI core update had problems"
    say "ComfyUI now $(comfyui_version)"
    NEED_RESTART=1
  elif [ -n "$have" ] && ! version_ge "$have" 0.30.0; then
    warn "MiniMax H3 needs ComfyUI >= 0.30.0 (this is $have). Update with: UPDATE_COMFYUI=1 bash $REPO_DIR/scripts/stock-setup.sh init"
  fi
  ensure_model_paths
  ensure_user_link
  ensure_ollama || warn "Ollama is not running; krea2 prompt enhancement needs it"
  if [ "$NEED_RESTART" = 1 ]; then comfy_ctl restart || true; fi
  cat <<EOF

[$TAG] init done. Next, one workflow per terminal (any order, parallel is fine):
    bash $REPO_DIR/scripts/stock-setup.sh <id>      # ids: bash $REPO_DIR/scripts/stock-setup.sh list
  then once, in any terminal:
    bash $REPO_DIR/scripts/stock-setup.sh finish
EOF
}

phase_workflow() { bash "$SCRIPT_DIR/install-workflow.sh" "$1"; }

phase_finish() {
  local rc=0 f
  ensure_prereqs
  ensure_model_paths
  ensure_user_link
  ensure_ollama || warn "Ollama is not running; Ollama models were not pulled"
  say "installing global nodes + global Ollama models"
  WORKFLOW=global PHASES=nodes,ollama bash "$SCRIPT_DIR/provision.sh" || rc=$?
  comfy_ctl restart || true
  echo
  say "status per workflow:"
  bash "$SCRIPT_DIR/provision.sh" --status || true
  echo
  say "workflows in ComfyUI (hard-refresh the browser tab):"
  for f in "$USER_DIR/default/workflows"/*; do
    if [ -e "$f" ]; then echo "    ${f##*/}"; fi
  done
  return "$rc"
}

phase_all() {
  local id failed=()
  phase_init
  for id in $(in_all_ids); do
    phase_workflow "$id" || failed+=("$id")
  done
  phase_finish || true
  if [ "${#failed[@]}" -gt 0 ]; then
    warn "workflows with problems: ${failed[*]} — re-run: bash scripts/stock-setup.sh <id>"
    return 3
  fi
}

main() {
  local cmd="${1:-help}"
  case "$cmd" in
    init)          phase_init ;;
    finish)        phase_finish ;;
    all)           phase_all ;;
    dl)            phase_workflow "${2:?usage: stock-setup.sh dl <workflow-id>}" ;;
    status)        bash "$SCRIPT_DIR/provision.sh" --status ${2:+"$2"} ;;
    restart)       comfy_ctl restart ;;
    list)          bash "$SCRIPT_DIR/provision.sh" --list-workflows ;;
    help|-h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0" ;;
    *)             phase_workflow "$cmd" ;;   # any workflow id
  esac
}

main "$@"
