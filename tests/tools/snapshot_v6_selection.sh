#!/usr/bin/env bash
# shellcheck disable=SC2034  # WORKFLOW and the path variables are read by the sourced v6 provision.sh
# snapshot_v6_selection.sh — prints the Selection_Snapshot (design §M11) on stdout:
#   bash tests/tools/snapshot_v6_selection.sh > tests/fixtures/selection-v6.txt
#
# Every input comes from commit a746fda (the v6 HEAD, parent of the ai-influencer-pipeline
# feature's first commit), never from the working tree, so every run prints the same bytes.
# v6's own provision.sh is sourced without its final `main "$@"` line: v6's `trim` and
# `selected` choose the lines, and the loops below parse each list as v6's install_nodes,
# download_models and pull_ollama do. Output: the header line, then per run `### run: <id>`
# and the sections [models.list] (5 fields), [nodes.txt] (2 fields) and
# [ollama-models.txt] (1 field), trimmed, without the wf: tag (design §M10).
#
# Side effects: none outside one temp dir, which is removed on exit. Loading v6's
# provision.sh only defines functions and variables and reads catalog.txt (IN_ALL). None of
# its install phases, main() or catalog_has is called, and its /workspace paths point into
# the temp dir anyway. Where v6 splits a line with `read ... <<< "$line"`, the loops below
# read the same bytes from a file in the temp dir: bash 3.2 writes every here-string to a
# temp file in /var/tmp or /tmp and ignores TMPDIR. No network access.
# bash 3.2 safe: no arrays, no mapfile, no {fd} redirection.
set -euo pipefail
export LC_ALL=C
unset CDPATH
IFS=$' \t\n'

SNAP_COMMIT=a746fdac6c7562a9b7d12c6b5fe757a3b9069029   # a746fda, the v6 HEAD
SNAP_HEADER='# Selection_Snapshot of v6 (a746fda…) — do not edit; regenerate with tests/tools/snapshot_v6_selection.sh'

snap_die() { echo "snapshot_v6_selection: $*" >&2; exit 1; }

snap_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
git -C "$snap_here" rev-parse --quiet --verify "$SNAP_COMMIT^{commit}" >/dev/null \
  || snap_die "cannot read commit $SNAP_COMMIT (v6): run this from a clone of the repo with its full history"

snap_tmp=""
trap '[ -z "$snap_tmp" ] || rm -rf "$snap_tmp"' EXIT
snap_base="${TMPDIR:-/tmp}"
snap_tmp="$(mktemp -d "${snap_base%/}/snapshot_v6_selection.XXXXXX")"
snap_line="$snap_tmp/line"   # holds the config line that `read` splits

# ---- inputs: the fixed commit only ----
git -C "$snap_here" show "$SNAP_COMMIT:scripts/provision.sh" > "$snap_tmp/provision.v6.sh"
for snap_name in catalog.txt models.list nodes.txt ollama-models.txt; do
  git -C "$snap_here" show "$SNAP_COMMIT:config/$snap_name" > "$snap_tmp/$snap_name"
done
[ "$(tail -n 1 "$snap_tmp/provision.v6.sh")" = "main \"\$@\"" ] \
  || snap_die "v6's provision.sh does not end with the line: main \"\$@\""
sed '$d' "$snap_tmp/provision.v6.sh" > "$snap_tmp/provision.sh"

# ---- load v6 ----
CONFIG_DIR="$snap_tmp"   # read while loading: IN_ALL is computed from this catalog.txt
WORKSPACE="$snap_tmp/workspace"   # never created
COMFYUI_DIR="$WORKSPACE/ComfyUI" MODELS_DIR="$WORKSPACE/models" USER_DIR="$WORKSPACE/comfyui-user"
OLLAMA_MODELS_DIR="$WORKSPACE/ollama-models" STAGING_DIR="$WORKSPACE/.staging" STATE_DIR="$WORKSPACE/.provision"
PYTHON=false             # skips v6's interpreter lookup; never run
# shellcheck source=/dev/null
. "$snap_tmp/provision.sh"

# ---- the Selection, one section per list ----
snap_models() {
  local line kind filename source source_id subdir tag
  printf '%s\n' '[models.list]'
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$(trim "$line")" in ''|\#*) continue ;; esac
    printf '%s\n' "$line" > "$snap_line"
    IFS='|' read -r kind filename source source_id subdir tag _ < "$snap_line"
    kind="$(trim "$kind")"; filename="$(trim "$filename")"; source="$(trim "$source")"
    source_id="$(trim "${source_id:-}")"; subdir="$(trim "${subdir:-}")"; tag="$(trim "${tag:-}")"
    selected "$tag" || continue
    printf '%s|%s|%s|%s|%s\n' "$kind" "$filename" "$source" "$source_id" "$subdir"
  done < "$CONFIG_DIR/models.list"
}

snap_nodes() {
  local line url ref tag
  printf '%s\n' '[nodes.txt]'
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$(trim "$line")" in ''|\#*) continue ;; esac
    printf '%s\n' "$line" > "$snap_line"
    IFS='|' read -r url ref tag _ < "$snap_line"
    url="$(trim "$url")"; ref="$(trim "${ref:-}")"; tag="$(trim "${tag:-}")"
    selected "$tag" || continue
    printf '%s|%s\n' "$url" "$ref"
  done < "$CONFIG_DIR/nodes.txt"
}

snap_ollama() {
  local line model tag
  printf '%s\n' '[ollama-models.txt]'
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$(trim "$line")" in ''|\#*) continue ;; esac
    printf '%s\n' "$line" > "$snap_line"
    IFS='|' read -r model tag _ < "$snap_line"
    model="$(trim "$model")"; tag="$(trim "${tag:-}")"
    selected "$tag" || continue
    printf '%s\n' "$model"
  done < "$CONFIG_DIR/ollama-models.txt"
}

# ---- runs: every Existing_Id, then a full run ----
# Built in the temp dir and printed at the end, so a failed run prints nothing on stdout.
snap_ids=" $(catalog_ids | tr '\n' ' ') "   # the ids v6's main() accepts for --workflow
{
  printf '%s\n' "$SNAP_HEADER"
  for snap_id in qwen21 qwen21-edit krea2 edit-2509 krea-style wan-i2v minimax-h3 wan-animate global flux all; do
    if [ "$snap_id" = all ]; then
      unset WORKFLOW   # a full run: v6's provision.sh without --workflow
    else
      case "$snap_ids" in *" $snap_id "*) ;; *) snap_die "'$snap_id' is not an id in v6's catalog.txt" ;; esac
      WORKFLOW="$snap_id"
    fi
    printf '### run: %s\n' "$snap_id"
    snap_models
    snap_nodes
    snap_ollama
  done
} > "$snap_tmp/selection.txt"
cat "$snap_tmp/selection.txt"
