#!/usr/bin/env bash
# influencer.sh — runs the influencer Python package, scripts/influencer/ (design §C6).
#
#   bash scripts/influencer.sh <command> [options]    e.g.  bash scripts/influencer.sh check-config
#   bash scripts/influencer.sh --help                  the commands and the exit statuses
#
# The interpreter is the first of (Req 25.6):
#   1. $INFLUENCER_PYTHON               a path, or a command name looked up on PATH
#   2. /venv/main/bin/python            the ComfyUI_Env on the Instance
#   3. <repo>/.check-venv/bin/python    the Check_Suite env, which is also the Mac's plan env
#   4. the first of python3.14 ... python3.10 python3 on PATH that is Python 3.10 or later
# and the launcher then runs
#   PYTHONPATH=<repo>/scripts[:$PYTHONPATH] exec <python> -m influencer "$@"
# with the absolute scripts path, so it works from any working folder.
#
# Only step 4 checks the version here. The package checks it again on import, and checks the
# packages each command needs (Req 25.10), so a wrong INFLUENCER_PYTHON exits with a message
# naming the version or the package rather than a traceback. The launcher itself exits 2 when
# it finds no interpreter; otherwise the exit status is the package's (see --help).
#
# bash 3.2 compatible: no arrays, and no command besides the interpreter it runs.
set -euo pipefail

COMFYUI_ENV_PYTHON=/venv/main/bin/python

case "${BASH_SOURCE[0]}" in
  */*) SCRIPT_DIR="${BASH_SOURCE[0]%/*}" ;;
  *) SCRIPT_DIR=. ;;
esac
SCRIPT_DIR="$(CDPATH='' cd -- "${SCRIPT_DIR:-/}" && pwd)"
REPO_DIR="$(CDPATH='' cd -- "$SCRIPT_DIR/.." && pwd)"
CHECK_ENV_PYTHON="$REPO_DIR/.check-venv/bin/python"

die() { printf 'influencer.sh: %s\n' "$*" >&2; exit 2; }

is_file_exe() { [ -f "$1" ] && [ -x "$1" ]; }

is_python_310() { # $1: an interpreter; succeeds when it runs and is Python 3.10 or later
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

PY=""
if [ -n "${INFLUENCER_PYTHON:-}" ]; then
  # An explicit choice is used as given or refused; it never falls through to the next step.
  case "$INFLUENCER_PYTHON" in
    */*) PY="$INFLUENCER_PYTHON" ;;
    *) PY="$(type -P -- "$INFLUENCER_PYTHON" || true)" ;;
  esac
  if [ -z "$PY" ] || ! is_file_exe "$PY"; then
    die "INFLUENCER_PYTHON is '$INFLUENCER_PYTHON', which is not an executable file"
  fi
elif is_file_exe "$COMFYUI_ENV_PYTHON"; then
  PY="$COMFYUI_ENV_PYTHON"
elif is_file_exe "$CHECK_ENV_PYTHON"; then
  PY="$CHECK_ENV_PYTHON"
else
  for name in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    cand="$(type -P -- "$name" || true)"
    if [ -n "$cand" ] && is_python_310 "$cand"; then
      PY="$cand"
      break
    fi
  done
  if [ -z "$PY" ]; then
    die "no Python 3.10 or later: INFLUENCER_PYTHON is unset, $COMFYUI_ENV_PYTHON and" \
      "$CHECK_ENV_PYTHON are missing, and none of python3.14 ... python3.10 python3 on PATH" \
      "is 3.10 or later. Set INFLUENCER_PYTHON, or run bash scripts/check.sh to build .check-venv"
  fi
fi

export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"
exec "$PY" -m influencer "$@"
