#!/usr/bin/env bash
#
# Run dvd-rip natively (no Docker), against Homebrew-installed tools.
#
# This is the ONLY way to run on macOS: docker-compose.yml device-maps the optical
# drive into the container, and Docker's Linux VM on macOS has no optical/USB
# passthrough — a container can never see the disc. On Linux the container is still
# the supported path; this script is only a convenience there.
#
# Usage:  ./scripts/run-native.sh [extra uvicorn args...]
# Config: reads .env if present; every value below can be overridden in it.

set -euo pipefail

cd "$(dirname "$0")/.."

# --- .env ------------------------------------------------------------------
# docker compose reads .env on its own; running natively, we have to. Only
# KEY=VALUE lines are exported, and existing environment wins so that
# `FOO=bar ./scripts/run-native.sh` behaves the way anyone would expect.
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
      *=*)
        key=${line%%=*}
        key=${key#export }
        key=$(printf '%s' "$key" | tr -d '[:space:]')
        [ -z "$key" ] && continue
        # Don't clobber anything already exported in the caller's shell.
        if [ -z "${!key:-}" ]; then
          value=${line#*=}
          export "$key=$value"
        fi
        ;;
    esac
  done < .env
fi

# --- Python ----------------------------------------------------------------
# The pinned deps (pydantic 2.9.2 / pydantic-core) publish no wheels for 3.14,
# and none at all for the free-threaded 3.14t build, where installing means
# compiling Rust from source. Prefer a version that just works, but respect an
# explicit PYTHON.
if [ -z "${PYTHON:-}" ]; then
  for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      PYTHON=$candidate
      break
    fi
  done
fi
: "${PYTHON:?no python3 found on PATH}"

VENV=${VENV:-.venv}
if [ ! -x "$VENV/bin/python" ]; then
  echo "==> creating $VENV with $($PYTHON --version)"
  "$PYTHON" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -r requirements.txt
fi

# --- Native library lookup -------------------------------------------------
# python-discid loads libdiscid through ctypes by bare filename. dlopen resolves a
# bare name against DYLD paths and the calling binary's rpath — and /opt/homebrew/lib
# is in neither by default, so whether it resolves depends on how the interpreter
# was built. Put Homebrew's libdir on the fallback path so it resolves regardless.
if [ "$(uname -s)" = "Darwin" ]; then
  BREW_LIB=$(brew --prefix 2>/dev/null)/lib
  if [ -d "$BREW_LIB" ]; then
    export DYLD_FALLBACK_LIBRARY_PATH="${DYLD_FALLBACK_LIBRARY_PATH:-}${DYLD_FALLBACK_LIBRARY_PATH:+:}$BREW_LIB:/usr/local/lib:/usr/lib"
  fi
fi

# --- MakeMKV first-run config (Blu-ray only) --------------------------------
# The container's entrypoint writes this file; running natively, nothing does.
# app_UpdateEnable=0 stops makemkvcon phoning home for a version check, which
# hangs the scan at high CPU whenever makemkv.com is unreachable (dvd-rip-br7) —
# and makemkvcon announces that check is on by default. Beta-key validation is
# local and unaffected.
#
# Written only when absent OR EMPTY: this is a real user-level config that the
# MakeMKV GUI also owns, and clobbering someone's settings to inject a key would
# be rude. But the GUI truncates it to 0 bytes on some first-run paths, and an
# `-f` guard treats that empty file as "already configured" — so the key is never
# written, makemkvcon falls back to a 30-day evaluation, and app_UpdateEnable
# stays on (the dvd-rip-br7 phone-home hang). An empty file holds no settings to
# preserve, so `-s` is the honest test.
if [ "$(uname -s)" = "Darwin" ] && [ -n "${MAKEMKV_APP_KEY:-}" ]; then
  MAKEMKV_CONF="$HOME/Library/MakeMKV/settings.conf"
  if [ ! -s "$MAKEMKV_CONF" ]; then
    mkdir -p "$(dirname "$MAKEMKV_CONF")"
    {
      printf 'app_Key = "%s"\n' "$MAKEMKV_APP_KEY"
      printf 'app_UpdateEnable = "0"\n'
      # Disables LibreDrive/SDF for the listed drive id. Only needed on drives
      # that hang in "SDF auto"; standard AACS mode handles 1080p Blu-ray fine.
      if [ -n "${MAKEMKV_SDF_STOP:-}" ]; then
        printf 'sdf_Stop = "%s"\n' "$MAKEMKV_SDF_STOP"
      fi
    } > "$MAKEMKV_CONF"
    echo "==> wrote $MAKEMKV_CONF (MakeMKV key + update check disabled)"
  fi
fi

# --- Defaults --------------------------------------------------------------
# Unset values fall through to app/hostenv.py's per-platform defaults, so we
# deliberately do NOT invent values for OUTPUT_ROOT / LIBRARY_DB / MAKEMKV_SCRATCH
# here — one source of truth for those.
HOST=${HOST:-127.0.0.1}
PORT=${PORT:-8000}

echo "==> dvd-rip on http://localhost:$PORT  (Ctrl-C to stop)"
exec "$VENV/bin/uvicorn" app.main:app --host "$HOST" --port "$PORT" "$@"
