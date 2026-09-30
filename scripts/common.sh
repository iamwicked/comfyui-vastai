#!/usr/bin/env bash
# shellcheck disable=SC2034  # NEED_RESTART and friends are read by the scripts that source this file
# common.sh — helpers shared by stock-setup.sh and install-workflow.sh.
# Sourced, not executed. Targets the stock Vast.ai ComfyUI template:
# /workspace/ComfyUI, /venv/main, supervisor, ComfyUI on :18188 behind Vast's proxy.

COMMON_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${REPO_DIR:-$(dirname "$COMMON_DIR")}"
CONFIG_DIR="${CONFIG_DIR:-$REPO_DIR/config}"
WORKSPACE="${WORKSPACE:-/workspace}"
COMFYUI_DIR="${COMFYUI_DIR:-/workspace/ComfyUI}"
if [ -z "${PYTHON:-}" ]; then
  if [ -x /venv/main/bin/python ]; then PYTHON=/venv/main/bin/python
  else PYTHON="$(command -v python3 || echo python3)"; fi
fi
USER_DIR="${USER_DIR:-$WORKSPACE/comfyui-user}"
MODELS_DIR="${MODELS_DIR:-$WORKSPACE/models}"
OLLAMA_MODELS_DIR="${OLLAMA_MODELS_DIR:-$WORKSPACE/ollama-models}"
OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-10m}"
MIN_FREE_GB="${MIN_FREE_GB:-40}"
export CONFIG_DIR WORKSPACE COMFYUI_DIR PYTHON USER_DIR MODELS_DIR OLLAMA_MODELS_DIR

TAG="${TAG:-setup}"
say()  { echo "[$TAG] $*"; }
warn() { echo "[$TAG][WARN] $*" >&2; }
NEED_RESTART=0

# ---- ComfyUI process control: restart the template's own server, never start a second one ----
have_supervisor() { command -v supervisorctl >/dev/null 2>&1 && supervisorctl pid >/dev/null 2>&1; }
comfy_program() {
  local out prog
  have_supervisor || return 1
  # `supervisorctl status` exits non-zero when any program is stopped, so read it first.
  out="$(supervisorctl status 2>/dev/null || true)"
  prog="$(awk 'tolower($1) == "comfyui" {print $1; exit}' <<< "$out")"
  [ -n "$prog" ] && printf '%s\n' "$prog"
}
PROVISIONING_MARKER="${PROVISIONING_MARKER:-/.provisioning}"
comfy_ctl() { # $1: stop|start|restart
  local prog
  # During Vast's first-boot provisioning the template holds ComfyUI back until
  # provisioning finishes, so there is nothing to stop or restart yet.
  if [ -f "$PROVISIONING_MARKER" ]; then
    say "Vast provisioning in progress: ComfyUI starts by itself when it finishes (skipping $1)"
    return 0
  fi
  if prog="$(comfy_program)"; then
    say "supervisorctl $1 $prog"
    supervisorctl "$1" "$prog" || warn "supervisorctl $1 $prog failed"
    return 0
  fi
  case "$1" in
    restart|start) warn "no supervisor-managed ComfyUI found. Restart ComfyUI yourself (Vast: instance menu > Restart, or re-run the launch command); do not start a second copy." ;;
  esac
  return 1
}

# ---- versions ----
comfyui_version() { # prints the version, or nothing; never fails (callers run under set -e)
  local f="$COMFYUI_DIR/comfyui_version.py"
  [ -f "$f" ] || return 0
  sed -n 's/^__version__ *= *"\(.*\)".*/\1/p' "$f" | head -n 1 || true
}
version_ge() { # $1 >= $2 ?
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]
}
catalog_field() { # $1 id, $2 field number
  grep -vE '^[[:space:]]*(#|$)' "$CONFIG_DIR/catalog.txt" | awk -F'|' -v id="$1" -v n="$2" '$1==id{print $n; exit}'
}
check_min_comfyui() { # $1 workflow id
  local need have
  need="$(catalog_field "$1" 3)"
  [ -n "$need" ] || return 0
  have="$(comfyui_version)"
  if [ -z "$have" ]; then
    warn "$1 needs ComfyUI >= $need; could not read the installed version"
  elif ! version_ge "$have" "$need"; then
    warn "$1 needs ComfyUI >= $need but this instance has $have."
    warn "update core first:  UPDATE_COMFYUI=1 bash $REPO_DIR/scripts/stock-setup.sh init"
  fi
}

# ---- prerequisites ----
APT_UPDATED=0
apt_install() { # packages...
  command -v apt-get >/dev/null 2>&1 || { warn "apt-get not available; install manually: $*"; return 1; }
  if [ "$APT_UPDATED" = 0 ]; then apt-get update; APT_UPDATED=1; fi
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}

ensure_prereqs() {
  local missing=() tool
  for tool in curl wget unzip zstd git; do command -v "$tool" >/dev/null 2>&1 || missing+=("$tool"); done
  command -v flock >/dev/null 2>&1 || missing+=(util-linux)
  if [ "${#missing[@]}" -gt 0 ]; then
    say "installing system tools: ${missing[*]}"
    apt_install "${missing[@]}" || warn "could not install: ${missing[*]}"
  fi
  if [ ! -x "$(dirname "$PYTHON")/hf" ] && ! command -v hf >/dev/null 2>&1; then
    say "installing the hf CLI into $PYTHON"
    if command -v uv >/dev/null 2>&1; then uv pip install --python "$PYTHON" "huggingface_hub[hf_xet]"
    else "$PYTHON" -m pip install "huggingface_hub[hf_xet]"; fi || warn "could not install huggingface_hub"
  fi
}

check_disk() {
  local free_gb
  free_gb="$(df -Pk "$WORKSPACE" 2>/dev/null | awk 'NR==2{printf "%d", $4/1048576}')"
  [ -n "$free_gb" ] || return 0
  say "free space on $WORKSPACE: ${free_gb} GB"
  if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
    warn "less than ${MIN_FREE_GB} GB free — large workflows will not fit (see REFERENCE.md disk budget)"
  fi
}

# ---- model paths: ComfyUI reads /workspace/models through extra_model_paths.yaml ----
ensure_model_paths() {
  local src="$REPO_DIR/extra_model_paths.yaml" dst="$COMFYUI_DIR/extra_model_paths.yaml" d
  [ -f "$src" ] || { warn "missing $src"; return 0; }
  if [ ! -d "$COMFYUI_DIR" ]; then
    warn "ComfyUI not found at $COMFYUI_DIR: use Vast's ComfyUI template, or set COMFYUI_DIR"
    return 1
  fi
  # Create every relative folder the yaml maps (skips base_path / is_default).
  while IFS= read -r d; do
    if [ -n "$d" ]; then mkdir -p "$MODELS_DIR/$d"; fi
  done < <(awk -F': *' '/^  [a-z_]+: [^\/].*\/$/ {sub(/\/$/, "", $2); print $2}' "$src")
  if [ ! -f "$dst" ] || ! cmp -s "$src" "$dst"; then
    if [ -f "$dst" ] && ! grep -q 'managed by comfyui-vastai' "$dst"; then
      cp -f "$dst" "$dst.bak.$(date +%Y%m%d%H%M%S)"
      say "backed up the existing extra_model_paths.yaml"
    fi
    cp -f "$src" "$dst"
    say "installed $dst (ComfyUI reads it at startup)"
    NEED_RESTART=1
  fi
  ensure_pack_model_link vosr2 || true   # it warns itself; the install goes on
}

# A node pack that reads ComfyUI/models/<name> itself instead of through extra_model_paths.yaml
# (ComfyUI-VOSR2) gets that folder as a link to $MODELS_DIR/<name>, so its weights live on the
# volume. The pack resolves the path when a Job loads it, so NEED_RESTART stays as it is.
ensure_pack_model_link() { # $1 folder name
  local target="$MODELS_DIR/$1" link="$COMFYUI_DIR/models/$1" lost
  mkdir -p "$target" "$COMFYUI_DIR/models"
  if [ -L "$link" ]; then
    if [ "$(readlink "$link")" != "$target" ]; then warn "$link is a link to $(readlink "$link"), not $target; left as it is"; fi
    return 0
  fi
  if [ -d "$link" ]; then
    # Move the folder's files over without overwriting any, and replace the folder only when
    # every file it holds now exists in the target too.
    cp -Rn "$link/." "$target/" 2>/dev/null || true
    lost="$(cd "$link" && find . \( -type f -o -type l \) -print | while IFS= read -r f; do
      if [ ! -e "$target/$f" ] && [ ! -L "$target/$f" ]; then printf '%s\n' "$f"; fi
    done)"
    if [ -n "$lost" ]; then
      warn "could not move every file of $link to $target; left the folder in place"
      return 1
    fi
    rm -rf "$link"
    say "moved the files of $link to $target"
  elif [ -e "$link" ]; then
    warn "$link is a file, not a folder; left as it is (a node pack reads a folder there)"
    return 1
  fi
  ln -s "$target" "$link"
  say "linked $link -> $target"
}

# ---- system tools a workflow needs (apt package named like the tool) ----
need_tool() { # $1 tool; 0 when it is on PATH or installed now, non-zero otherwise
  command -v "$1" >/dev/null 2>&1 && return 0
  say "installing $1"
  apt_install "$1" || { warn "could not install $1"; return 1; }
}

# ---- user dir: workflows + settings live in $USER_DIR, linked into ComfyUI ----
ensure_user_link() {
  local link="$COMFYUI_DIR/user" was_running=0
  mkdir -p "$USER_DIR/default/workflows"
  [ -L "$link" ] && return 0
  say "linking $link -> $USER_DIR"
  if comfy_program >/dev/null 2>&1; then comfy_ctl stop && was_running=1; fi
  if [ -d "$link" ]; then cp -rn "$link/." "$USER_DIR/" 2>/dev/null || true; rm -rf "$link"; fi
  ln -s "$USER_DIR" "$link"
  if [ "$was_running" = 1 ]; then comfy_ctl start || true; else NEED_RESTART=1; fi
}

# ---- Ollama: installed once, served on 127.0.0.1 only, weights on the volume ----
ollama_up() { curl -fsS --max-time 3 "http://127.0.0.1:11434/api/version" >/dev/null 2>&1; }

ensure_ollama() {
  local bin conf=/etc/supervisor/conf.d/ollama.conf new i
  if ! command -v ollama >/dev/null 2>&1; then
    command -v zstd >/dev/null 2>&1 || apt_install zstd || true   # the installer needs zstd
    say "installing Ollama${OLLAMA_VERSION:+ $OLLAMA_VERSION}"
    curl -fL -# https://ollama.com/install.sh | OLLAMA_VERSION="${OLLAMA_VERSION:-}" sh || { warn "Ollama install failed"; return 1; }
  fi
  bin="$(command -v ollama)"
  mkdir -p "$OLLAMA_MODELS_DIR"
  if have_supervisor && [ -d /etc/supervisor/conf.d ]; then
    new="$(cat <<EOF
# managed by comfyui-vastai (stock-setup.sh) — local-only Ollama, weights on the volume
[program:ollama]
environment=OLLAMA_MODELS="$OLLAMA_MODELS_DIR",OLLAMA_HOST="127.0.0.1:11434",OLLAMA_KEEP_ALIVE="$OLLAMA_KEEP_ALIVE"
command=$bin serve
autostart=true
autorestart=true
startsecs=3
stopasgroup=true
killasgroup=true
stdout_logfile=/var/log/ollama.log
stdout_logfile_maxbytes=10MB
redirect_stderr=true
EOF
)"
    if [ ! -f "$conf" ] || [ "$(cat "$conf")" != "$new" ]; then
      printf '%s\n' "$new" > "$conf"
      say "registered Ollama with supervisor ($conf)"
      supervisorctl reread || true
      supervisorctl update ollama || true
    fi
    ollama_up || supervisorctl start ollama || true
  elif ! ollama_up; then
    say "starting Ollama in the background (no supervisor): log at $WORKSPACE/.provision/ollama.log"
    mkdir -p "$WORKSPACE/.provision"
    OLLAMA_MODELS="$OLLAMA_MODELS_DIR" OLLAMA_HOST=127.0.0.1:11434 OLLAMA_KEEP_ALIVE="$OLLAMA_KEEP_ALIVE" \
      nohup "$bin" serve >>"$WORKSPACE/.provision/ollama.log" 2>&1 &
  fi
  for _ in $(seq 1 20); do ollama_up && break; sleep 1; done
  if ollama_up; then say "Ollama is up on 127.0.0.1:11434 (models in $OLLAMA_MODELS_DIR)"
  else warn "Ollama did not come up — check /var/log/ollama.log"; return 1; fi
}

workflow_uses_ollama() { # $1 id -> 0 if ollama-models.txt has a line tagged for it
  grep -vE '^[[:space:]]*(#|$)' "$CONFIG_DIR/ollama-models.txt" 2>/dev/null \
    | awk -F'|' -v id="$1" '{t=$2; gsub(/wf:| /, "", t); n=split(t, a, ","); for (i = 1; i <= n; i++) if (a[i] == id) found = 1} END {exit !found}'
}
