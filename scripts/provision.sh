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
#   bash scripts/provision.sh --selection <id>|global|all   # what a run selects (read-only)
#   PHASES=models bash scripts/provision.sh --workflow wan-animate
#
# PHASES: comma-separated subset of nodes,models,ollama (default: all three);
#         `comfyui` is an extra opt-in phase that updates ComfyUI core.
# Progress is always visible (hf / curl -# / wget bar / git --progress).
set -euo pipefail
# A node repo that went private or was deleted must fail fast instead of
# waiting for a username at a prompt nobody answers.
export GIT_TERMINAL_PROMPT=0

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
catalog_has() { # $1: id -> 0 if the catalog lists it. A case match, not grep <<<: bash 3.2
  # writes every here-string to a temp file in /var/tmp or /tmp, and --selection writes nothing.
  local nl=$'\n' ids
  case "$1" in ''|*"$nl"*) return 1 ;; esac   # no id is empty or spans two lines
  ids="$nl$(catalog_ids)$nl"
  case "$ids" in *"$nl$1$nl"*) return 0 ;; esac
  return 1
}
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
  local line url ref tag name lname clash dest existing after stamp
  # The list is read on fd 3, so nothing in the loop (git, pip, a node's
  # install.py) can swallow the remaining lines from stdin.
  while IFS= read -r line <&3 || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$(trim "$line")" in ''|\#*) continue ;; esac
    IFS='|' read -r url ref tag _ <<< "$line"
    url="$(trim "$url")"; ref="$(trim "${ref:-}")"; tag="$(trim "${tag:-}")"
    selected "$tag" || continue
    name="$(basename "$url" .git)"
    dest="$COMFYUI_DIR/custom_nodes/$name"
    lname="$(printf '%s' "$name" | tr '[:upper:]' '[:lower:]')"
    if [ "$lname" = comfyui-degrid ]; then
      # ComfyUI-SaveSimple registers the same node id, VAEDeGrid (Req 3.4): no clone, pull or requirements.
      clash="$(find "$COMFYUI_DIR/custom_nodes" -mindepth 1 -maxdepth 1 -iname ComfyUI-SaveSimple -print -quit 2>/dev/null || true)"
      if [ -n "$clash" ]; then
        fail "skipped $name: node id VAEDeGrid collides with $clash"
        continue
      fi
    fi
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
    if [ "$lname" = comfyui-impact-pack ] && [ ! -e "$COMFYUI_DIR/custom_nodes/skip_download_model" ]; then
      # Impact Pack's install.py fetches its SAM model (375 MB, unused here) unless this empty
      # marker exists; ComfyUI ignores a plain file in custom_nodes.
      : > "$COMFYUI_DIR/custom_nodes/skip_download_model"
      log "created custom_nodes/skip_download_model (Impact Pack skips its SAM download)"
    fi
    if [ -f "$dest/install.py" ]; then
      log "running install.py for $(basename "$dest")"
      (cd "$dest" && "$PYTHON" install.py) || { ok=0; fail "install.py failed for $(basename "$dest")"; }
    fi
    if [ "$ok" = 1 ] && [ -n "$stamp" ]; then printf '%s' "$after" > "$stamp"; fi
  done 3< "$list"
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

civitai_probe() { # $1 version id  $2 output file  $3 filename -> 2 on an early-access answer, else 0
  # One request without following redirects, for the first 64 KB only, before the download
  # (Req 2.7-2.9). Civitai answers an early-access version with 403 and a body that says so.
  local probe code rc=0
  probe="$STAGING_DIR/civitai/${2##*/}.probe"
  mkdir -p "$STAGING_DIR/civitai"
  code="$(printf 'header = "Authorization: Bearer %s"\n' "$CIVITAI_TOKEN" \
    | curl -K - -sS --range 0-65535 -o "$probe" -w '%{http_code}' \
        "https://civitai.com/api/download/models/$1")" || rc=$?
  if [ "$code" = 403 ] && [ -f "$probe" ] \
      && grep -qi -- 'early access' "$probe"; then
    rm -f "$probe"
    fail "early access: $3 (Civitai version $1) — the version is in early access; it downloads once early access ends"
    return 2
  fi
  # 2xx/3xx: the download goes ahead; any other answer (curl exit $rc) keeps the download's own handling.
  rm -f "$probe"
  return 0
}

curl_civitai() { # $1 version id  $2 output file (resumes a partial file from an earlier run)  $3 filename
  # -> 0 when downloaded, 2 on an early-access answer (already reported), 1 otherwise
  local code rc=0
  civitai_probe "$1" "$2" "${3:-${2##*/}}" || return 2
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
  local rc=0
  if [ -z "$member" ]; then
    part="$STAGING_DIR/civitai/$vid.part"
    curl_civitai "$vid" "$part" "$2" || rc=$?
    if [ "$rc" -ne 0 ]; then
      [ "$rc" -eq 2 ] || fail "Civitai download failed (version $vid)"   # 2: early access, reported
      return 1
    fi
    DOWNLOADED="$part"
    return 0
  fi
  # Archive release: download once, extract the one member we need.
  archive="$STAGING_DIR/civitai/$vid.zip"
  if [ ! -s "$archive" ]; then
    curl_civitai "$vid" "$archive.part" "$2" || rc=$?
    if [ "$rc" -ne 0 ]; then
      [ "$rc" -eq 2 ] || fail "Civitai download failed (version $vid)"
      return 1
    fi
    mv -f "$archive.part" "$archive"
  fi
  if ! members="$(unzip -Z1 "$archive" 2>/dev/null)"; then
    # Not a zip (usually an error or login page). Kept, it would be reused on every re-run.
    rm -f "$archive"
    fail "Civitai version $vid did not return a zip archive (error page?); removed it, re-run to download again"
    return 1
  fi
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
        # Kept, a broken archive would be resumed (and fail) on every re-run.
        rm -f "$file"
        fail "unzip failed for $name (not a zip, or truncated); removed it, re-run to download again"
        return 1
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
        "$PYTHON" "$SCRIPT_DIR/patch-workflow.py" "$file" "$patch" \
          || fail "patch failed for $name, so it is installed unpatched. Re-runs skip it: fix the patch, delete $WORKFLOWS_DIR/.provisioned-$name, then re-run"
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
  # fd 3 for the list: hf, curl, wget, unzip and python never see it on stdin.
  while IFS= read -r line <&3 || [ -n "$line" ]; do
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
      dir="models/${subdir:-$(kind_to_dir "$kind")}"   # as shown in messages
      dest="$MODELS_DIR/${dir#models/}/$filename"     # MODELS_DIR, like --status
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
  done 3< "$list"
}

# ComfyUI-VOSR2 loads DINOv2 as dinov2_vitl14.safetensors and would convert the .pth at the first
# Job otherwise (Req 3.3). The .pth stays, so a re-run skips its download; the conversion runs
# only while the target is missing (Req 3.7).
vosr2_convert() {
  local dir="$MODELS_DIR/vosr2/VOSR2" src dst tmp
  src="$dir/dinov2_vitl14_pretrain.pth"
  dst="$dir/dinov2_vitl14.safetensors"
  tmp="$dst.tmp"
  if [ ! -f "$src" ] || [ -f "$dst" ]; then return 0; fi
  log "converting models/vosr2/VOSR2/dinov2_vitl14_pretrain.pth to dinov2_vitl14.safetensors"
  rm -f "$tmp"
  if "$PYTHON" - "$src" "$tmp" <<'EOF' && [ -s "$tmp" ]; then
import sys

import torch
from safetensors.torch import save_file

state = torch.load(sys.argv[1], map_location="cpu", weights_only=True)
save_file({key: value.contiguous() for key, value in state.items()}, sys.argv[2])
EOF
    mv -f "$tmp" "$dst"
    log "saved: models/vosr2/VOSR2/dinov2_vitl14.safetensors"
  else
    rm -f "$tmp"
    fail "VOSR2: converting dinov2_vitl14_pretrain.pth to dinov2_vitl14.safetensors failed (the first VOSR2 Job converts it instead)"
  fi
}

# ---- 4. Ollama models (weights live on the volume) ----
ollama_up() { curl -fsS --max-time 3 "http://127.0.0.1:11434/api/version" >/dev/null 2>&1; }

# A temporary server lives only as long as this run: stopped at the end of
# pull_ollama, or by the EXIT trap if the run ends early (error, Ctrl-C, SIGTERM).
OLLAMA_TEMP_PID=""
stop_temp_ollama() {
  [ -n "$OLLAMA_TEMP_PID" ] || return 0
  kill "$OLLAMA_TEMP_PID" 2>/dev/null || true
  wait "$OLLAMA_TEMP_PID" 2>/dev/null || true
  OLLAMA_TEMP_PID=""
  log "temporary Ollama server stopped"
}

pull_ollama() {
  local list="$CONFIG_DIR/ollama-models.txt" line model tag want have
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
    # The server must not inherit the lock's fd: a server left behind by a run that
    # died would hold the ollama lock, and every later run would wait for it forever.
    ( if [ -n "$LOCK_FD" ]; then exec {LOCK_FD}>&-; fi
      OLLAMA_MODELS="$OLLAMA_MODELS_DIR" exec ollama serve ) >>"$STATE_DIR/ollama-provision.log" 2>&1 &
    OLLAMA_TEMP_PID=$!
    trap stop_temp_ollama EXIT
    trap 'exit 130' INT; trap 'exit 143' TERM; trap 'exit 129' HUP   # all of them run the EXIT trap
    for _ in $(seq 1 30); do ollama_up && break; sleep 2; done
    if ! ollama_up; then
      fail "Ollama server did not start (see $STATE_DIR/ollama-provision.log)"
      stop_temp_ollama; trap - EXIT INT TERM HUP
      lock_release; return 0
    fi
  fi
  # `|| have=""`: under pipefail a failing `ollama list` would otherwise end the
  # whole run here. Pulling a model that is already present is harmless.
  have="$(ollama list 2>/dev/null | awk 'NR>1 {print $1}')" || have=""
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
  if [ -n "$OLLAMA_TEMP_PID" ]; then
    stop_temp_ollama
    trap - EXIT INT TERM HUP
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

# ---- read-only Selection mode: --selection <id>|global|all ----
# Prints the lines a run would install and does nothing else: no directory, lock, staging or
# log file, and no network. It also runs on the Mac, so it stays bash 3.2 safe (no arrays,
# mapfile, {fd} or flock) and uses no here-string, which bash 3.2 writes to a temp file in
# /var/tmp or /tmp whatever TMPDIR says.
print_selection() { # the Selection of the current run (WORKFLOW) in the design's §M10 format
  local file n line rest out i field tag
  for file in models.list nodes.txt ollama-models.txt; do
    case "$file" in models.list) n=5 ;; nodes.txt) n=2 ;; *) n=1 ;; esac   # fields before the tag
    printf '[%s]\n' "$file"
    [ -f "$CONFIG_DIR/$file" ] || continue   # the install phases skip a missing list too
    while IFS= read -r line <&3 || [ -n "$line" ]; do
      line="${line%$'\r'}"
      case "$(trim "$line")" in ''|\#*) continue ;; esac
      # Split as the install loops' `IFS='|' read -r <n fields> tag _ <<< "$line"` does: field k
      # is the text before the k-th `|`, and a field past the end of the line is empty.
      rest="$line" out="" i=0
      while [ "$i" -lt "$n" ]; do
        case "$rest" in *'|'*) field="${rest%%|*}"; rest="${rest#*|}" ;; *) field="$rest"; rest="" ;; esac
        out="$out|$(trim "$field")"
        i=$((i + 1))
      done
      tag="$(trim "${rest%%|*}")"
      selected "$tag" || continue
      printf '%s\n' "${out#|}"
    done 3< "$CONFIG_DIR/$file"
  done
}

selection_mode() { # $1: <id>, global or all (a full run) -> prints that run's Selection, or exits 1
  export LC_ALL=C   # trim's [[:space:]], grep and awk work on bytes, whatever the caller's locale
  [ -f "$CATALOG" ] || { warn "missing $CATALOG"; exit 1; }
  IN_ALL=" $(catalog_all | tr '\n' ' ') "   # computed again, now under LC_ALL=C
  case "$1" in
    all)    unset WORKFLOW ;;
    global) WORKFLOW=global ;;
    *)      catalog_has "$1" || { warn "unknown workflow '$1' — available:"; list_workflows >&2; exit 1; }
            WORKFLOW="$1" ;;
  esac
  print_selection
}

# ---- main ----
main() {
  local status_id="" do_status=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --workflow) WORKFLOW="${2:?--workflow needs a workflow id}"; shift 2 ;;
      --list-workflows) list_workflows; exit 0 ;;
      --selection) selection_mode "${2-}"; exit 0 ;;   # read-only, before any side effect
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
  PHASES="${PHASES// /}"   # "nodes, models" means nodes,models (the checks below match ",models,")
  local p
  for p in ${PHASES//,/ }; do   # a typo must not end in "provisioning complete" with nothing done
    case "$p" in nodes|models|ollama|comfyui) ;; *) fail "unknown phase '$p' in PHASES (valid: nodes, models, ollama, comfyui)" ;; esac
  done
  log "config=$CONFIG_DIR comfyui=$COMFYUI_DIR models=$MODELS_DIR python=$PYTHON"
  log "run: ${WORKFLOW:-full (all)}  phases: $PHASES"
  case ",$PHASES," in *,comfyui,*)
    # Core update or pin on request only (stock-setup.sh init with UPDATE_COMFYUI=1 or COMFYUI_REF).
    lock_acquire nodes; make_constraints; update_comfyui; lock_release ;;
  esac
  case ",$PHASES," in *,nodes,*)  install_nodes ;; esac
  case ",$PHASES," in *,models,*) download_models; vosr2_convert ;; esac
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
