#!/usr/bin/env bash
# check.sh — the Check_Suite: every check and test in one command (design C11, Req 27).
#
#   bash scripts/check.sh      takes no arguments; runs under the Mac's /bin/bash 3.2 and later
#
# Four steps, always all of them and in this order, even after an earlier one fails:
#   1. config      .check-venv/bin/python -m influencer check-config
#   2. tests       .check-venv/bin/python -m pytest -q tests, with HYPOTHESIS_PROFILE=check
#   3. bash -n     every shell script
#   4. shellcheck  every shell script at default severity; skipped when shellcheck is missing
# A shell script is a file listed by `git ls-files -z --cached --others --exclude-standard`
# (tracked, or untracked and not ignored) whose name ends in .sh or whose first line is a
# bash or sh shebang.
#
# config and tests run in .check-venv/, built from the pinned, hashed
# tests/requirements-check.txt with the first Python 3.10+ among python3.14 ... python3.10
# python3; each name is looked up on PATH, then in ~/.local/bin. The build changes no other
# Python installation. A stamp holds the requirements file's SHA-256 and the env's Python
# version: while it matches, runs reuse the env and need no network. A changed file, a
# changed or broken interpreter, or a build that did not finish triggers a rebuild.
#
# Ends with one "<step>: passed|failed|skipped" line per step, then exits 1 if any step
# failed and 0 otherwise (skipped does not count as failed).
set -uo pipefail   # no -e: a failing step must not stop the steps after it

if [ "$#" -gt 0 ]; then
  echo "usage: bash scripts/check.sh   (takes no arguments)" >&2
  exit 2
fi
# The root is the folder above this script's own, found by parameter expansion: `$(dirname ...)`
# would be empty on a PATH without dirname, and `cd "/.."` would make the root /.
case "${BASH_SOURCE[0]}" in
  */*) ROOT="${BASH_SOURCE[0]%/*}" ;;
  *)   ROOT=. ;;
esac
ROOT="$(CDPATH='' cd -- "${ROOT:-/}/.." 2>/dev/null && pwd)" || ROOT=""
if [ -z "$ROOT" ] || [ ! -f "$ROOT/scripts/check.sh" ]; then
  echo "check.sh: cannot find the repo root: ${ROOT:-?}/scripts/check.sh does not exist (run it as: bash scripts/check.sh from a checkout)" >&2
  exit 2
fi
cd "$ROOT" || exit 1
# The env must hold exactly the pinned packages, whatever Python settings the caller has.
unset PYTHONPATH PYTHONHOME

VENV=.check-venv
REQ=tests/requirements-check.txt
STAMP="$VENV/stamp"
PIP_LOG="$VENV/pip-install.log"
PY_NAMES=(python3.14 python3.13 python3.12 python3.11 python3.10 python3)
# bash or sh shebangs: #!/bin/bash, #!/bin/sh -e, #!/usr/bin/env bash, #!/usr/bin/env -S bash -e
SHEBANG_RE='^#![[:space:]]*(/[^[:space:]]*/)?(env[[:space:]]+(-[^[:space:]]*[[:space:]]+)*)?(ba)?sh([[:space:]]|$)'

STEPS=(config tests "bash -n" shellcheck)
RESULTS=(failed failed failed failed)   # each step overwrites its own entry
RESULT=failed ENV_ERROR="" REASON="" PY="" PY_ID="" SCRIPTS_ERROR=""
SCRIPTS=()

say() { echo "[check] $*"; }

# ---- .check-venv: interpreter search, stamp and build (Req 27.3, 27.9) ----

python_id() { # $1: interpreter; prints e.g. cpython-3.14.7; fails when older than 3.10 or unusable
  "$1" -c 'import sys
if sys.version_info < (3, 10): sys.exit(1)
print("%s-%d.%d.%d" % ((sys.implementation.name,) + tuple(sys.version_info[:3])))' 2>/dev/null
}

# Sets PY and PY_ID to the first Python 3.10+ in PY_NAMES order, skipping older ones such as
# the Mac's system python3 3.9. Each name is looked up on PATH, then in ~/.local/bin, where
# `uv python install` puts interpreters and which is often missing from PATH.
find_python() {
  local name cand
  for name in "${PY_NAMES[@]}"; do
    for cand in "$(command -v "$name" 2>/dev/null)" "${HOME:-}/.local/bin/$name"; do
      [ -x "$cand" ] || continue
      if PY_ID="$(python_id "$cand")"; then PY="$cand"; return 0; fi
    done
  done
  return 1
}

env_stamp() { # prints what the stamp of the current .check-venv must hold
  "$VENV/bin/python" -c 'import hashlib, sys
with open(sys.argv[1], "rb") as f:
    print("requirements-sha256 " + hashlib.sha256(f.read()).hexdigest())
print("python %s-%d.%d.%d" % ((sys.implementation.name,) + tuple(sys.version_info[:3])))' "$REQ" 2>/dev/null
}

env_is_current() { # returns 0 when .check-venv matches its stamp; otherwise sets REASON
  local want
  if [ ! -d "$VENV" ]; then REASON="no $VENV yet"; return 1; fi
  if [ ! -f "$STAMP" ]; then REASON="no stamp, so the last build failed or was interrupted"; return 1; fi
  if ! want="$(env_stamp)"; then REASON="$VENV/bin/python does not run"; return 1; fi
  [ "$want" = "$(< "$STAMP")" ] && return 0
  if [ "$(printf '%s\n' "$want" | sed -n 1p)" != "$(sed -n 1p "$STAMP")" ]; then
    REASON="$REQ changed"
  else
    REASON="its Python is now $(printf '%s\n' "$want" | sed -n 's/^python //p'), the stamp says $(sed -n 's/^python //p' "$STAMP")"
  fi
  return 1
}

failed_packages() { # $1: pip log; prints the pinned packages that pip's errors name
  local errors name pattern found=""
  # pip's ERROR lines, plus the indented "name==version from <url>" lines of its hash report
  errors="$(grep -E '^ERROR:|^[[:space:]]+[A-Za-z0-9][A-Za-z0-9._-]*==' "$1" 2>/dev/null)"
  while IFS= read -r name; do
    pattern="$(printf '%s' "$name" | sed 's/[-_.][-_.]*/[-_.]+/g')"   # pip may print - _ or .
    if printf '%s\n' "$errors" | grep -Eiq "(^|[^A-Za-z0-9._-])$pattern(==|[^A-Za-z0-9._-]|\$)"; then
      found="${found:+$found, }$name"
    fi
  done < <(sed -n 's/^\([A-Za-z0-9][A-Za-z0-9._-]*\)==.*/\1/p' "$REQ" | sort -u)
  printf '%s\n' "$found"
}

build_env() { # sets ENV_ERROR when .check-venv cannot be built
  local status packages
  if ! find_python; then
    ENV_ERROR="Python 3.10 or later is required; none of ${PY_NAMES[*]} on PATH or in ~/.local/bin is 3.10+"
    return
  fi
  say "env: building $VENV with $PY ($PY_ID): $REASON"
  rm -rf "$VENV"
  if ! "$PY" -m venv "$VENV"; then
    ENV_ERROR="could not create $VENV with $PY -m venv"
    return
  fi
  "$VENV/bin/python" -m pip install --disable-pip-version-check --no-input --progress-bar off \
    --require-hashes --no-deps -r "$REQ" 2>&1 | tee "$PIP_LOG"
  status=${PIPESTATUS[0]}
  if [ "$status" -ne 0 ]; then
    packages="$(failed_packages "$PIP_LOG")"
    if [ -n "$packages" ]; then
      ENV_ERROR="pinned package failed to install: $packages"
    else
      ENV_ERROR="pip install -r $REQ failed (exit $status) without naming a package; see $PIP_LOG"
    fi
    return
  fi
  # Written last, so a build that fails or is interrupted leaves no stamp.
  if env_stamp > "$STAMP.tmp" && mv "$STAMP.tmp" "$STAMP"; then
    say "env: $VENV ready ($PY_ID)"
  else
    ENV_ERROR="could not write $STAMP"
  fi
}

prepare_env() { # leaves ENV_ERROR empty when .check-venv is ready for steps 1-2
  if [ ! -f "$REQ" ]; then
    ENV_ERROR="$REQ is missing"
  elif env_is_current; then
    say "env: reusing $VENV ($(sed -n 's/^python //p' "$STAMP"); the stamp matches $REQ)"
  else
    build_env
  fi
  [ -z "$ENV_ERROR" ] || say "env: $ENV_ERROR"
}

# ---- the four steps; each sets RESULT to passed, failed or skipped ----

step_config() {
  RESULT=failed
  say "1/4 config: $VENV/bin/python -m influencer check-config"
  if [ -n "$ENV_ERROR" ]; then say "config: not run: $ENV_ERROR"; return; fi
  PYTHONPATH="$ROOT/scripts" "$VENV/bin/python" -m influencer check-config && RESULT=passed
}

step_tests() {
  local status
  RESULT=failed
  say "2/4 tests: HYPOTHESIS_PROFILE=check $VENV/bin/python -m pytest -q tests"
  if [ -n "$ENV_ERROR" ]; then say "tests: not run: $ENV_ERROR"; return; fi
  PYTHONPATH="$ROOT/scripts" HYPOTHESIS_PROFILE=check "$VENV/bin/python" -m pytest -q tests
  status=$?
  case "$status" in
    0) RESULT=passed ;;
    5) say "tests: pytest collected no tests (exit 5); a run that verifies nothing fails" ;;
  esac
}

# Shell scripts (Req 27.1): tracked files, plus untracked files that .gitignore does not
# exclude, whose name ends in .sh or whose first line is a bash or sh shebang.
find_scripts() { # sets SCRIPTS, or SCRIPTS_ERROR when git cannot list the files
  local f first
  if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    SCRIPTS_ERROR="cannot list the files of $ROOT: git is missing or this is not a git work tree"
    return
  fi
  while IFS= read -r -d '' f; do
    [ -f "$f" ] || continue   # tracked, but deleted from the work tree
    case "$f" in
      *.sh) ;;
      *) first=""
         IFS= read -r first 2>/dev/null < "$f"
         [[ $first =~ $SHEBANG_RE ]] || continue ;;
    esac
    SCRIPTS+=("$f")
  done < <(git ls-files -z --cached --others --exclude-standard)
  if [ "${#SCRIPTS[@]}" -eq 0 ]; then
    SCRIPTS_ERROR="git ls-files listed no shell scripts, not even scripts/check.sh"
  else
    say "shell scripts (${#SCRIPTS[@]}): ${SCRIPTS[*]}"
  fi
}

step_bash_n() {
  local f bad=0
  RESULT=failed
  say "3/4 bash -n: $BASH -n on each shell script"
  if [ -n "$SCRIPTS_ERROR" ]; then say "bash -n: $SCRIPTS_ERROR"; return; fi
  for f in "${SCRIPTS[@]}"; do
    "$BASH" -n -- "$f" || bad=$((bad + 1))
  done
  if [ "$bad" -eq 0 ]; then RESULT=passed; else say "bash -n: $bad of ${#SCRIPTS[@]} scripts do not parse"; fi
}

step_shellcheck() {
  RESULT=failed
  say "4/4 shellcheck: shellcheck on all shell scripts, default severity"
  if ! command -v shellcheck >/dev/null 2>&1; then
    case "$(uname -s)" in
      Darwin) say "shellcheck is not installed; install it with: brew install shellcheck" ;;
      Linux)  say "shellcheck is not installed; install it with: sudo apt-get install -y shellcheck" ;;
      *)      say "shellcheck is not installed; see https://github.com/koalaman/shellcheck#installing" ;;
    esac
    RESULT=skipped
    return
  fi
  if [ -n "$SCRIPTS_ERROR" ]; then say "shellcheck: $SCRIPTS_ERROR"; return; fi
  shellcheck -- "${SCRIPTS[@]}" && RESULT=passed
}

# ---- main ----

prepare_env
step_config;     RESULTS[0]=$RESULT
step_tests;      RESULTS[1]=$RESULT
find_scripts
step_bash_n;     RESULTS[2]=$RESULT
step_shellcheck; RESULTS[3]=$RESULT

echo
status=0
for i in 0 1 2 3; do
  echo "${STEPS[$i]}: ${RESULTS[$i]}"
  if [ "${RESULTS[$i]}" = failed ]; then status=1; fi
done
exit "$status"
