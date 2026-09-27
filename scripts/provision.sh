#!/usr/bin/env bash
# provision.sh — the install engine (v6). Idempotent: re-runs only fetch what's
# missing, and parallel terminals are safe (per-file and per-phase locks).
#
# Reads three lists from $CONFIG_DIR plus the workflow catalog:
#   catalog.txt        id|in_all|min_comfyui|description   (the workflow ids)
#   models.list        kind|filename|source|source_id|subdir|wf:ids
#   nodes.txt          git_url|ref|wf:ids
#   ollama-models.txt  model|wf:ids
#
# Selection by wf: tag
#   WORKFLOW=<id>     lines whose wf: list contains <id>
#   WORKFLOW=global   untagged lines + lines whose wf: list contains "global"
#   (unset)           full run: untagged lines + lines tagged with any id that
#                     is in `all` (catalog in_all=yes). Opt-in ids such as
#                     `flux` are skipped unless asked for by name.
#
# Usage
#   bash scripts/provision.sh --workflow krea2        # one workflow
#   WORKFLOW=global bash scripts/provision.sh         # shared extras
#   bash scripts/provision.sh                         # full run
#   bash scripts/provision.sh --list-workflows
#   bash scripts/provision.sh --status [id]           # what's installed/missing
#   PHASES=models bash scripts/provision.sh --workflow wan-animate
#
# PHASES: comma-separated subset of nodes,models,ollama (default: all three);
#         `comfyui` is an extra opt-in phase that updates ComfyUI core.
# Progress is always visible (hf / curl -# / wget bar / git --progress).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# ---- paths (stock Vast ComfyUI template) ----
CONFIG_DIR="${CONFIG_DIR:-$REPO_DIR/config}"
COMFYUI_DIR="${COMFYUI_DIR:-/workspace/ComfyUI}"
WORKSPACE="${WORKSPACE:-/workspace}"
MODELS_DIR="${MODELS_DIR:-$WORKSPACE/models}"
USER_DIR="${USER_DIR:-$WORKSPACE/comfyui-user}"
WORKFLOWS_DIR="$USER_DIR/default/workflows"
OLLAMA_MODELS_DIR="${OLLAMA_MODELS_DIR:-$WORKSPACE/ollama-models}"
# Staging lives on the same volume as the models, so the final move is an
# atomic rename and interrupted downloads resume on the next run.
STAGING_DIR="${STAGING_DIR:-$WORKSPACE/.staging}"
STATE_DIR="${STATE_DIR:-$WORKSPACE/.provision}"
LOCK_DIR="$STATE_DIR/locks"

if [ -z "${PYTHON:-}" ]; then
  if [ -x /venv/main/bin/python ]; then PYTHON=/venv/main/bin/python
  else PYTHON="$(command -v python3 || echo python3)"; fi
fi

log()  { echo "[provision] $*"; }
warn() { echo "[provision][WARN] $*" >&2; }
FAILURES=()
fail() { warn "$*"; FAILURES+=("$*"); }

trim() { local s="$1"; s="${s#"${s%%[![:space:]]*}"}"; s="${s%"${s##*[![:space:]]}"}"; printf '%s' "$s"; }

# ---- locking (flock is part of util-linux on every Ubuntu image) ----
HAVE_FLOCK=0
command -v flock >/dev/null 2>&1 && HAVE_FLOCK=1
LOCK_FD=""
lock_acquire() { # $1: lock name
  [ "$HAVE_FLOCK" = 1 ] || return 0
  mkdir -p "$LOCK_DIR"
  exec {LOCK_FD}>"$LOCK_DIR/$1.lock"
  if ! flock -n "$LOCK_FD"; then
    log "another terminal holds '$1' — waiting for it to finish..."
    flock "$LOCK_FD"
  fi
}
lock_release() {
  [ "$HAVE_FLOCK" = 1 ] && [ -n "$LOCK_FD" ] || return 0
  flock -u "$LOCK_FD" || true
  exec {LOCK_FD}>&-
  LOCK_FD=""
}

# ---- catalog + wf: tag selection ----
CATALOG="$CONFIG_DIR/catalog.txt"
catalog_lines() { [ -f "$CATALOG" ] && grep -vE '^[[:space:]]*(#|$)' "$CATALOG" || true; }
catalog_ids()   { catalog_lines | cut -d'|' -f1; }
catalog_all()   { catalog_lines | awk -F'|' '$2=="yes"{print $1}'; }
catalog_field() { catalog_lines | awk -F'|' -v id="$1" -v n="$2" '$1==id{print $n; exit}'; }
catalog_has()   { local ids; ids="$(catalog_ids)"; grep -qxF -- "$1" <<< "$ids"; }
IN_ALL=" $(catalog_all | tr '\n' ' ') "

list_workflows() {
  catalog_lines | awk -F'|' '{ printf "  %-12s %s%s\n", $1, $4, ($2=="yes" ? "" : "  [opt-in, not in all]") }'
}

tag_has() { # $1: wf-field, $2: id -> 0 if the tag lists the id
  local ids
  ids=",$(trim "${1#wf:}"),"
  ids="${ids// /}"
  case "$ids" in *",$2,"*) return 0 ;; *) return 1 ;; esac
}

selected() { # $1: wf-field -> 0 if this line belongs to the current run
  local tag; tag="$(trim "$1")"
  if [ -z "${WORKFLOW:-}" ]; then
    [ -z "$tag" ] && return 0
    local id
    for id in $(printf '%s' "${tag#wf:}" | tr ',' ' '); do
      case "$IN_ALL" in *" $id "*) return 0 ;; esac
    done
    return 1
  fi
  if [ "$WORKFLOW" = "global" ]; then
    [ -z "$tag" ] && return 0
    tag_has "$tag" global
    return
  fi
  [ -n "$tag" ] && tag_has "$tag" "$WORKFLOW"
}

# ---- helpers ----
kind_to_dir() {
  case "$1" in
    checkpoint)     echo checkpoints ;;
    lora)           echo loras ;;
    vae)            echo vae ;;
    vae_approx)     echo vae_approx ;;
    controlnet)     echo controlnet ;;
    upscale)        echo upscale_models ;;
    embedding)      echo embeddings ;;
    clip)           echo clip ;;
    clip_vision)    echo clip_vision ;;
    text_encoder)   echo text_encoders ;;
    diffusion)      echo diffusion_models ;;
    detection)      echo detection ;;
    sams)           echo sams ;;
    model_patch)    echo model_patches ;;
    audio_encoder)  echo audio_encoders ;;
    latent_upscale) echo latent_upscale_models ;;
    *)              echo "$1" ;;
  esac
}

hf_cli() {
  local c
  c="$(dirname "$PYTHON")/hf"
  if [ -x "$c" ]; then printf '%s' "$c"; return; fi
  command -v hf 2>/dev/null || true
}

pip_install() { # args passed to pip; always constrained to the installed torch stack
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PYTHON" -c "$CONSTRAINTS" "$@"
  else
    "$PYTHON" -m pip install -c "$CONSTRAINTS" "$@"
  fi
}

CONSTRAINTS="$STATE_DIR/constraints.txt"
make_constraints() {
  mkdir -p "$STATE_DIR"
  # Custom-node requirements may never replace the CUDA torch build ComfyUI runs on
  # (torchcodec too: Vast's image treats it as part of the same ABI-bound set).
  "$PYTHON" - > "$CONSTRAINTS" <<'EOF' || : > "$CONSTRAINTS"
import importlib.metadata as md
for name in ("torch", "torchvision", "torchaudio", "torchcodec", "xformers"):
    try:
        print(f"{name}=={md.version(name)}")
    except md.PackageNotFoundError:
        pass
EOF
  log "pip constraints: $(tr '\n' ' ' < "$CONSTRAINTS")"
}

looks_like_error_page() { # $1: file -> 0 if it's an HTML/JSON error body instead of a model
  local head
  head="$(head -c 256 "$1" 2>/dev/null | tr -d '\0' | tr '[:upper:]' '[:lower:]')" || true
  case "$head" in *"<!doctype"*|*"<html"*|'{"error"'*|'{"message"'*) return 0 ;; esac
  return 1
}

# ---- 1. ComfyUI core update / pin (PHASES=comfyui, run by `stock-setup.sh init`) ----
# Vast's image checks ComfyUI out at a release tag (detached HEAD), where
# `git pull` cannot work: there, "update" means the newest release tag. A
# checkout on a branch is fast-forwarded. COMFYUI_REF (a tag or commit) pins.
update_comfyui() {
  local target="${COMFYUI_REF:-}" branch
  if [ -z "$target" ] && branch="$(git -C "$COMFYUI_DIR" symbolic-ref -q --short HEAD)"; then
    log "updating ComfyUI core ($COMFYUI_DIR, branch $branch)"
    git -C "$COMFYUI_DIR" pull --ff-only --progress || { fail "ComfyUI pull failed on branch $branch"; return 0; }
  else
    git -C "$COMFYUI_DIR" fetch --progress --tags origin || { fail "could not fetch ComfyUI from origin"; return 0; }
    if [ -z "$target" ]; then
      target="$(git -C "$COMFYUI_DIR" tag -l 'v[0-9]*' --sort=-v:refname)" || target=""
      target="${target%%$'\n'*}"   # newest first; no pipe into head (pipefail + SIGPIPE)
      [ -n "$target" ] || { fail "no ComfyUI release tags found in $COMFYUI_DIR"; return 0; }
      log "updating ComfyUI core to the newest release: $target"
    else
      log "pinning ComfyUI to $target"
    fi
    if ! git -C "$COMFYUI_DIR" -c advice.detachedHead=false checkout "$target"; then
      # A commit that no fetched branch or tag contains: fetch it by id.
      { git -C "$COMFYUI_DIR" fetch --progress origin "$target" \
          && git -C "$COMFYUI_DIR" -c advice.detachedHead=false checkout FETCH_HEAD; } \
        || { fail "could not check out ComfyUI $target (local changes in $COMFYUI_DIR?)"; return 0; }
    fi
  fi
  pip_install -r "$COMFYUI_DIR/requirements.txt" || fail "ComfyUI requirements reinstall failed"
}

# ---- 2. custom nodes ----
# "Dependencies installed" stamps live inside the Python environment they describe.
# The node code sits on /workspace and survives a new instance, but the venv does
# not; stamps kept inside the venv vanish with it, so requirements get reinstalled.
env_stamp_dir() {
  local prefix
  prefix="$("$PYTHON" -c 'import sys; print(sys.prefix)' 2>/dev/null || true)"
  if [ -n "$prefix" ] && [ -d "$prefix" ] && [ -w "$prefix" ]; then
    printf '%s/.comfyui-vastai/nodes' "$prefix"
  fi   # unknown or read-only environment: print nothing, never skip installs
}

install_nodes() {
  local list="$CONFIG_DIR/nodes.txt"
  [ -f "$list" ] || { log "no nodes.txt, skipping"; return 0; }
  lock_acquire nodes   # pip is not safe to run twice at once in one venv
  make_constraints
  local stamp_dir
  stamp_dir="$(env_stamp_dir)"
  if [ -n "$stamp_dir" ]; then mkdir -p "$stamp_dir"; else warn "cannot keep dependency stamps for $PYTHON; requirements reinstall every run"; fi
  mkdir -p "$COMFYUI_DIR/custom_nodes"
  local line url ref tag name dest existing after stamp
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$(trim "$line")" in ''|\#*) continue ;; esac
    IFS='|' read -r url ref tag _ <<< "$line"
    url="$(trim "$url")"; ref="$(trim "${ref:-}")"; tag="$(trim "${tag:-}")"
    selected "$tag" || continue
    name="$(basename "$url" .git)"
    dest="$COMFYUI_DIR/custom_nodes/$name"
    # Same pack installed under another case (ComfyUI-Manager / registry installs)?
    existing="$(find "$COMFYUI_DIR/custom_nodes" -mindepth 1 -maxdepth 1 -iname "$name" -print -quit 2>/dev/null || true)"
    [ -n "$existing" ] && dest="$existing"
    if [ -d "$dest/.git" ]; then
      log "updating node: $name"
      git -C "$dest" pull --ff-only --progress || warn "pull failed for $name (pinned tag or local changes) — keeping current version"
    elif [ -d "$dest" ]; then
      log "node present (not a git checkout, managed elsewhere): $(basename "$dest")"
      continue
    else
      log "cloning node: $name${ref:+ @ $ref}"
      if ! git clone --progress --depth 1 ${ref:+--branch "$ref"} "$url" "$dest"; then
        fail "clone failed: $url"
        continue
      fi
    fi
    after="$(git -C "$dest" rev-parse HEAD 2>/dev/null || echo unknown)"
    stamp="${stamp_dir:+$stamp_dir/$(basename "$dest").head}"
    if [ -n "$stamp" ] && [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$after" ]; then
      log "dependencies already installed for $(basename "$dest") @ ${after:0:10} in this environment"
      continue
    fi
    local ok=1
    if [ -f "$dest/requirements.txt" ]; then
      log "installing requirements for $(basename "$dest")"
      pip_install -r "$dest/requirements.txt" || { ok=0; fail "requirements failed for $(basename "$dest")"; }
    fi
    if [ -f "$dest/install.py" ]; then
      log "running install.py for $(basename "$dest")"
      (cd "$dest" && "$PYTHON" install.py) || { ok=0; fail "install.py failed for $(basename "$dest")"; }
    fi
    if [ "$ok" = 1 ] && [ -n "$stamp" ]; then printf '%s' "$after" > "$stamp"; fi
  done < "$list"
  lock_release
}

# ---- 3. models + workflow files ----
DOWNLOADED=""   # path of the finished download (set by fetch_*)

fetch_hf() { # $1 source_id (repo::path)  $2 filename -> DOWNLOADED
  local repo="${1%%::*}" rpath="${1#*::}" hf stage
  [ "$rpath" = "$1" ] && rpath="$2"
  hf="$(hf_cli)"
  [ -n "$hf" ] || { fail "hf CLI missing (install huggingface_hub[hf_xet]) — cannot fetch $repo"; return 1; }
  stage="$STAGING_DIR/hf/${repo//\//__}"
  mkdir -p "$stage"
  # --local-dir keeps partial files, so an interrupted download resumes here.
  if ! "$hf" download "$repo" "$rpath" --local-dir "$stage"; then
    fail "HF download failed: $repo :: $rpath (gated repos need HF_TOKEN + accepted license)"
    return 1
  fi
  DOWNLOADED="$stage/$rpath"
  rm -f "$stage/.cache/huggingface/download/$rpath.metadata" 2>/dev/null || true
}

curl_civitai() { # $1 version id  $2 output file (resumes a partial file from an earlier run)
  local code rc=0
  # Token goes through stdin (-K -), so it never shows up in the process list.
  # curl does not forward it when Civitai redirects to its CDN.
  code="$(printf 'header = "Authorization: Bearer %s"\n' "$CIVITAI_TOKEN" \
    | curl -K - -L -# --retry 5 --retry-delay 5 -C - -o "$2" -w '%{http_code}' \
        "https://civitai.com/api/download/models/$1")" || rc=$?
  if [ "$rc" -eq 0 ]; then
    case "$code" in
      200|206) return 0 ;;
      416) return 0 ;;  # the partial file was already complete
    esac
  fi
  case "$rc" in
    18|28|52|55|56) warn "network error (curl $rc) on Civitai $1 — partial file kept, re-run to resume" ;;
    *) rm -f "$2"
       warn "Civitai returned HTTP ${code:-none} (curl exit $rc) for version $1 — check CIVITAI_TOKEN and the version id" ;;
  esac
  return 1
}

fetch_civitai() { # $1 version id (optionally id::member-in-archive)  $2 filename -> DOWNLOADED
  local vid="${1%%::*}" member="${1#*::}" part archive members m
  [ "$member" = "$1" ] && member=""
  if [ -z "${CIVITAI_TOKEN:-}" ]; then
    fail "CIVITAI_TOKEN not set — skipped $2 (export it in this terminal)"
    return 1
  fi
  mkdir -p "$STAGING_DIR/civitai"
  if [ -z "$member" ]; then
    part="$STAGING_DIR/civitai/$vid.part"
    curl_civitai "$vid" "$part" || { fail "Civitai download failed (version $vid)"; return 1; }
    DOWNLOADED="$part"
    return 0
  fi
  # Archive release: download once, extract the one member we need.
  archive="$STAGING_DIR/civitai/$vid.zip"
  if [ ! -s "$archive" ]; then
    curl_civitai "$vid" "$archive.part" || { fail "Civitai download failed (version $vid)"; return 1; }
    mv -f "$archive.part" "$archive"
  fi
  members="$(unzip -Z1 "$archive" 2>/dev/null || true)"
  m=""
  if grep -qxF -- "$member" <<< "$members"; then
    m="$member"
  else
    m="$(awk -v want="$(basename "$member")" '{n=$0; sub(/.*\//, "", n); if (tolower(n) == tolower(want)) {print $0; exit}}' <<< "$members")"
  fi
  if [ -z "$m" ]; then
    fail "'$member' not found in Civitai archive $vid — it contains: $(tr '\n' ' ' <<< "$members")"
    return 1
  fi
  part="$STAGING_DIR/civitai/$vid.member.part"
  log "extracting $m from Civitai archive $vid"
  unzip -p "$archive" "$m" > "$part" || { rm -f "$part"; fail "unzip failed for $m"; return 1; }
  rm -f "$archive"
  DOWNLOADED="$part"
}

fetch_url() { # $1 url  $2 filename -> DOWNLOADED
  local part
  mkdir -p "$STAGING_DIR/url"
  part="$STAGING_DIR/url/$(printf '%s' "$1" | cksum | cut -d' ' -f1)-$2.part"
  if wget --progress=bar:force:noscroll -c -O "$part" "$1"; then
    DOWNLOADED="$part"
  else
    rm -f "$part"
    fail "URL download failed: $1"
    return 1
  fi
}

fetch_bundled() { # $1 path relative to $CONFIG_DIR -> DOWNLOADED (a copy, the source stays)
  local src="$CONFIG_DIR/$1" part
  [ -f "$src" ] || { fail "bundled file missing: $src"; return 1; }
  mkdir -p "$STAGING_DIR/bundled"
  part="$STAGING_DIR/bundled/$(basename "$1").part"
  cp -f "$src" "$part"
  DOWNLOADED="$part"
}

finish_workflow() { # $1 downloaded file  $2 filename  $3 marker
  local file="$1" name="$2" marker="$3" patch
  mkdir -p "$WORKFLOWS_DIR"
  case "$name" in
    *.zip)
      log "extracting workflow archive: $name"
      if unzip -o "$file" -d "$WORKFLOWS_DIR"; then
        rm -f "$file"
      else
        fail "unzip failed for $name"; return 1
      fi
      ;;
    *)
      if ! "$PYTHON" -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$file" 2>/dev/null; then
        fail "$name is not valid JSON (login page or error?) — not installed"
        rm -f "$file"; return 1
      fi
      patch="$CONFIG_DIR/patches/$name.patch.json"
      if [ -f "$patch" ]; then
        log "applying patch: patches/$(basename "$patch")"
        "$PYTHON" "$SCRIPT_DIR/patch-workflow.py" "$file" "$patch" || { fail "patch failed for $name (left unpatched)"; }
      fi
      mv -f "$file" "$WORKFLOWS_DIR/$name"
      ;;
  esac
  touch "$marker"
  log "workflow ready: $name"
}

download_models() {
  local list="$CONFIG_DIR/models.list"
  [ -f "$list" ] || { log "no models.list, skipping"; return 0; }
  export HF_XET_HIGH_PERFORMANCE=1
  mkdir -p "$MODELS_DIR" "$STAGING_DIR" "$WORKFLOWS_DIR"
  log "free space on $WORKSPACE: $(df -h "$WORKSPACE" 2>/dev/null | awk 'NR==2{print $4}')"
  local line kind filename source source_id subdir tag dir dest marker lockname
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$(trim "$line")" in ''|\#*) continue ;; esac
    IFS='|' read -r kind filename source source_id subdir tag _ <<< "$line"
    kind="$(trim "$kind")"; filename="$(trim "$filename")"; source="$(trim "$source")"
    source_id="$(trim "${source_id:-}")"; subdir="$(trim "${subdir:-}")"; tag="$(trim "${tag:-}")"
    selected "$tag" || continue
    if [ "$kind" = workflow ]; then
      dir="${WORKFLOWS_DIR#"$WORKSPACE"/}"
      dest="$WORKFLOWS_DIR/$filename"
      marker="$WORKFLOWS_DIR/.provisioned-${filename%.zip}"
      [ -f "$marker" ] && { log "workflow already installed: $filename"; continue; }
    else
      dir="models/${subdir:-$(kind_to_dir "$kind")}"
      dest="$WORKSPACE/$dir/$filename"
      [ -s "$dest" ] && { log "exists, skipping: $dir/$filename"; continue; }
    fi
    lockname="file-$(printf '%s' "$dest" | cksum | cut -d' ' -f1)"
    lock_acquire "$lockname"
    if { [ "$kind" = workflow ] && [ -f "$marker" ]; } || { [ "$kind" != workflow ] && [ -s "$dest" ]; }; then
      log "finished by another terminal: $dir/$filename"
      lock_release; continue
    fi
    log "downloading: $dir/$filename (via $source)"
    DOWNLOADED=""
    case "$source" in
      hf)      fetch_hf "$source_id" "$filename" || true ;;
      civitai) fetch_civitai "$source_id" "$filename" || true ;;
      url)     fetch_url "$source_id" "$filename" || true ;;
      bundled) fetch_bundled "$source_id" || true ;;
      *)       fail "unknown source '$source' for $filename" ;;
    esac
    if [ -n "$DOWNLOADED" ] && [ -s "$DOWNLOADED" ]; then
      if [ "$kind" = workflow ]; then
        finish_workflow "$DOWNLOADED" "$filename" "$marker" || true
      elif looks_like_error_page "$DOWNLOADED"; then
        fail "$filename: server returned an error page instead of the file — not installed"
        rm -f "$DOWNLOADED"
      else
        mkdir -p "$(dirname "$dest")"
        mv -f "$DOWNLOADED" "$dest"
        log "saved: $dir/$filename ($(du -h "$dest" | cut -f1))"
      fi
    elif [ -n "$DOWNLOADED" ]; then
      fail "$filename: download produced an empty file"
      rm -f "$DOWNLOADED"
    fi
    lock_release
  done < "$list"
}

# ---- 4. Ollama models (weights live on the volume) ----
ollama_up() { curl -fsS --max-time 3 "http://127.0.0.1:11434/api/version" >/dev/null 2>&1; }

pull_ollama() {
  local list="$CONFIG_DIR/ollama-models.txt" line model tag want have temp_pid=""
  [ -f "$list" ] || { log "no ollama-models.txt, skipping"; return 0; }
  local wanted=()
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$(trim "$line")" in ''|\#*) continue ;; esac
    IFS='|' read -r model tag _ <<< "$line"
    model="$(trim "$model")"; tag="$(trim "${tag:-}")"
    selected "$tag" && wanted+=("$model")
  done < "$list"
  [ "${#wanted[@]}" -gt 0 ] || { log "no Ollama models for this run"; return 0; }
  if ! command -v ollama >/dev/null 2>&1; then
    fail "ollama is not installed — run: bash scripts/stock-setup.sh init (needed for: ${wanted[*]})"
    return 0
  fi
  lock_acquire ollama
  export OLLAMA_HOST=127.0.0.1:11434
  if ! ollama_up; then
    log "starting a temporary Ollama server (models dir: $OLLAMA_MODELS_DIR)"
    mkdir -p "$OLLAMA_MODELS_DIR" "$STATE_DIR"
    OLLAMA_MODELS="$OLLAMA_MODELS_DIR" ollama serve >>"$STATE_DIR/ollama-provision.log" 2>&1 &
    temp_pid=$!
    for _ in $(seq 1 30); do ollama_up && break; sleep 2; done
    ollama_up || { fail "Ollama server did not start (see $STATE_DIR/ollama-provision.log)"; kill "$temp_pid" 2>/dev/null || true; lock_release; return 0; }
  fi
  have="$(ollama list 2>/dev/null | awk 'NR>1 {print $1}')"
  for model in "${wanted[@]}"; do
    want="$model"
    case "$want" in *:*) ;; *) want="$want:latest" ;; esac
    if grep -qxF -- "$want" <<< "$have"; then
      log "ollama model present: $want"
    else
      log "pulling ollama model: $want"
      ollama pull "$want" || fail "ollama pull failed: $want"
    fi
  done
  if [ -n "$temp_pid" ]; then
    kill "$temp_pid" 2>/dev/null || true
    wait "$temp_pid" 2>/dev/null || true
    log "temporary Ollama server stopped"
  fi
  lock_release
}

# ---- status: what each workflow has / is missing ----
status_report() { # $1: optional workflow id
  local ids id line kind filename source source_id subdir tag url ref name total present
  local -a missing
  if [ -n "${1:-}" ]; then ids="$1"; else ids="$(catalog_ids)"; fi
  for id in $ids; do
    total=0; present=0; missing=()
    while IFS= read -r line || [ -n "$line" ]; do
      line="${line%$'\r'}"
      case "$(trim "$line")" in ''|\#*) continue ;; esac
      IFS='|' read -r kind filename source source_id subdir tag _ <<< "$line"
      kind="$(trim "$kind")"; filename="$(trim "$filename")"; subdir="$(trim "${subdir:-}")"; tag="$(trim "${tag:-}")"
      WORKFLOW="$id" selected "$tag" || continue
      total=$((total + 1))
      if [ "$kind" = workflow ]; then
        if [ -f "$WORKFLOWS_DIR/.provisioned-${filename%.zip}" ]; then present=$((present + 1))
        else missing+=("workflow $filename"); fi
      else
        subdir="${subdir:-$(kind_to_dir "$kind")}"
        if [ -s "$MODELS_DIR/$subdir/$filename" ]; then present=$((present + 1))
        else missing+=("model    $subdir/$filename"); fi
      fi
    done < "$CONFIG_DIR/models.list"
    while IFS= read -r line || [ -n "$line" ]; do
      line="${line%$'\r'}"
      case "$(trim "$line")" in ''|\#*) continue ;; esac
      IFS='|' read -r url ref tag _ <<< "$line"
      WORKFLOW="$id" selected "$(trim "${tag:-}")" || continue
      name="$(basename "$(trim "$url")" .git)"
      if [ -z "$(find "$COMFYUI_DIR/custom_nodes" -mindepth 1 -maxdepth 1 -iname "$name" -print -quit 2>/dev/null)" ]; then
        missing+=("node     $name")
      fi
    done < "$CONFIG_DIR/nodes.txt"
    if [ "${#missing[@]}" -eq 0 ]; then
      printf '  %-12s complete (%d files)\n' "$id" "$total"
    else
      printf '  %-12s %d/%d files present, %d item(s) missing:\n' "$id" "$present" "$total" "${#missing[@]}"
      printf '      - %s\n' "${missing[@]}"
    fi
  done
}

# ---- main ----
main() {
  local status_id="" do_status=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --workflow) WORKFLOW="${2:?--workflow needs a workflow id}"; shift 2 ;;
      --list-workflows) list_workflows; exit 0 ;;
      --status) do_status=1; status_id="${2:-}"; [ $# -ge 2 ] && shift; shift ;;
      -h|--help) awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; exit 0 ;;
      *) warn "ignoring unknown argument: $1"; shift ;;
    esac
  done
  [ -f "$CATALOG" ] || { warn "missing $CATALOG"; exit 1; }
  if [ "$do_status" = 1 ]; then
    if [ -n "$status_id" ] && ! catalog_has "$status_id"; then warn "unknown workflow '$status_id'"; list_workflows; exit 1; fi
    status_report "$status_id"; exit 0
  fi
  if [ -n "${WORKFLOW:-}" ] && ! catalog_has "$WORKFLOW"; then
    warn "unknown workflow '$WORKFLOW' — available:"; list_workflows; exit 1
  fi
  PHASES="${PHASES:-nodes,models,ollama}"
  log "config=$CONFIG_DIR comfyui=$COMFYUI_DIR models=$MODELS_DIR python=$PYTHON"
  log "run: ${WORKFLOW:-full (all)}  phases: $PHASES"
  case ",$PHASES," in *,comfyui,*)
    # Core update or pin on request only (stock-setup.sh init with UPDATE_COMFYUI=1 or COMFYUI_REF).
    lock_acquire nodes; make_constraints; update_comfyui; lock_release ;;
  esac
  case ",$PHASES," in *,nodes,*)  install_nodes ;; esac
  case ",$PHASES," in *,models,*) download_models ;; esac
  case ",$PHASES," in *,ollama,*) pull_ollama ;; esac
  if [ "${#FAILURES[@]}" -gt 0 ]; then
    warn "finished with ${#FAILURES[@]} problem(s):"
    local f; for f in "${FAILURES[@]}"; do warn "  - $f"; done
    warn "fix the cause and re-run the same command — finished files are skipped."
    exit 3
  fi
  log "provisioning complete (${WORKFLOW:-full run})."
}

main "$@"
