#!/usr/bin/env bash
#
# Check everything the native (non-Docker) run path depends on.
#
# Worth having because nearly every failure mode here is SILENT: a missing
# libdvdcss yields an unreadable disc, a wrong libdiscid device makes an audio CD
# scan as a DVD, and a missing drive just reports "no disc". This turns each of
# those into a named, fixable line.
#
# Exit status is 0 if a rip could run, 1 if something required is missing.

set -uo pipefail

cd "$(dirname "$0")/.."

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; DIM=$'\033[2m'; OFF=$'\033[0m'
fail=0

ok()   { printf "  ${GREEN}✓${OFF} %-22s %s\n" "$1" "${2:-}"; }
bad()  { printf "  ${RED}✗${OFF} %-22s %s\n" "$1" "${2:-}"; fail=1; }
warn() { printf "  ${YELLOW}!${OFF} %-22s %s\n" "$1" "${2:-}"; }
note() { printf "    ${DIM}%s${OFF}\n" "$1"; }

echo
echo "dvd-rip doctor — $(uname -s) $(uname -m)"
echo

# --- required tools --------------------------------------------------------
echo "Tools"
if path=$(command -v HandBrakeCLI); then
  ok "HandBrakeCLI" "$path"
else
  bad "HandBrakeCLI" "missing"; note "brew install handbrake"
fi

if path=$(command -v lsdvd); then
  ok "lsdvd" "$path"
else
  bad "lsdvd" "missing"; note "brew install lsdvd — needed to identify DVDs"
fi

# cdparanoia ships as `cd-paranoia` from libcdio on macOS; hostenv picks the name.
cdp=$(command -v cd-paranoia || command -v cdparanoia)
if [ -n "$cdp" ]; then
  ok "cdparanoia" "$cdp"
else
  bad "cdparanoia" "missing"; note "brew install libcdio-paranoia — audio CD ripping"
fi

for tool in flac lame; do
  if path=$(command -v $tool); then ok "$tool" "$path"; else
    bad "$tool" "missing"; note "brew install $tool"
  fi
done

# --- optional tools --------------------------------------------------------
echo
echo "Optional"
MAKEMKV_APP=/Applications/MakeMKV.app/Contents/MacOS/makemkvcon
if path=$(command -v makemkvcon); then
  ok "makemkvcon" "$path"
elif [ -x "$MAKEMKV_APP" ]; then
  ok "makemkvcon" "$MAKEMKV_APP"
else
  warn "makemkvcon" "not installed — Blu-ray disabled (DVD/CD unaffected)"
  note "Download from https://www.makemkv.com/download/ (the Homebrew cask is"
  note "deprecated: it fails Gatekeeper). Then set MAKEMKV_APP_KEY in .env."
fi

if [ -f .env ] && grep -qE '^MAKEMKV_APP_KEY=.+' .env; then
  ok "MakeMKV key" "set in .env"
else
  warn "MakeMKV key" "MAKEMKV_APP_KEY unset — Blu-ray decryption will fail"
  note "Free beta key (rotates ~monthly): https://forum.makemkv.com/forum/viewtopic.php?t=1053"
fi

# makemkvcon's update check hangs the scan at high CPU when makemkv.com is
# unreachable (dvd-rip-br7), and it is ON by default.
MAKEMKV_CONF="$HOME/Library/MakeMKV/settings.conf"
if [ -f "$MAKEMKV_CONF" ]; then
  if grep -q 'app_UpdateEnable *= *"0"' "$MAKEMKV_CONF"; then
    ok "MakeMKV config" "update check disabled"
  else
    warn "MakeMKV config" "update check still enabled — scans can hang if makemkv.com is down"
    note "Add   app_UpdateEnable = \"0\"   to $MAKEMKV_CONF"
  fi
elif [ -x "$MAKEMKV_APP" ] || command -v makemkvcon >/dev/null 2>&1; then
  warn "MakeMKV config" "no settings.conf — 'make native' writes one once MAKEMKV_APP_KEY is set"
fi

if [ -f .env ] && grep -qE '^TMDB_API_KEY=.+' .env; then
  ok "TMDB key" "set in .env"
else
  warn "TMDB key" "unset — in-UI title search disabled, type metadata manually"
fi

# --- libraries -------------------------------------------------------------
echo
echo "Libraries"
BREW_LIB="$(brew --prefix 2>/dev/null)/lib"

# HandBrake links libdvdcss on macOS (Homebrew makes it a hard dependency), unlike
# Linux where it is dlopened at runtime. Without it, CSS-encrypted discs read as
# garbage rather than failing outright.
if ls "$BREW_LIB"/libdvdcss*.dylib >/dev/null 2>&1 || ls /usr/local/lib/libdvdcss* >/dev/null 2>&1; then
  ok "libdvdcss" "present — CSS decryption available"
else
  bad "libdvdcss" "missing"; note "brew install libdvdcss — encrypted DVDs will not rip"
fi

# python-discid loads libdiscid by bare name through ctypes; resolution depends on
# dyld paths, so check it the same way the app will.
VENV_PY=.venv/bin/python
if [ -x "$VENV_PY" ]; then
  export DYLD_FALLBACK_LIBRARY_PATH="${DYLD_FALLBACK_LIBRARY_PATH:-}${DYLD_FALLBACK_LIBRARY_PATH:+:}$BREW_LIB:/usr/local/lib:/usr/lib"
  if out=$("$VENV_PY" -c 'import discid; print(discid.LIBDISCID_VERSION_STRING)' 2>&1); then
    ok "libdiscid" "$out"
  else
    bad "libdiscid" "python-discid cannot load it"
    note "brew install libdiscid   (detail: $(printf '%s' "$out" | tail -1))"
  fi
  ver=$("$VENV_PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
  ok "venv python" "$ver"
else
  warn "venv" "not created yet — run 'make venv'"
fi

# --- drive -----------------------------------------------------------------
echo
echo "Drive"
if [ -n "${DVD_DEVICE:-}" ]; then
  ok "DVD_DEVICE" "$DVD_DEVICE (pinned; auto-detection skipped)"
elif [ "$(uname -s)" = "Darwin" ]; then
  status=$(drutil status 2>/dev/null)
  # `drutil status` prints nothing at all with no drive attached; with an empty
  # tray it prints the drive but no Name field, because macOS creates no /dev node
  # until media is loaded. Those two cases need different advice.
  model=$(printf '%s' "$status" | sed -n '2p' | sed 's/^ *//;s/ *$//')
  device=$(printf '%s' "$status" | sed -n 's/.*Name: *\(\/dev\/disk[0-9]*\).*/\1/p' | head -1)
  # BSD sed has no non-greedy match, so strip the Name field (and padding) in
  # separate passes rather than trying to express it in one pattern.
  type=$(printf '%s' "$status" | sed -n 's/^ *Type: *//p' | head -1 \
         | sed 's/  *Name:.*//' | sed 's/ *$//')
  if [ -z "$status" ]; then
    bad "optical drive" "none detected"
    note "Plug in a USB optical drive. 'drutil status' shows nothing without one."
  else
    ok "optical drive" "${model:-attached}"
    if [ -n "$device" ]; then
      raw="/dev/r${device#/dev/}"
      ok "disc" "${type:-present} → $raw"
      if [ -r "$raw" ]; then
        ok "read access" "$raw readable"
      else
        bad "read access" "$raw not readable by $(whoami)"
        note "Grant your terminal 'Full Disk Access' in System Settings >"
        note "Privacy & Security, then try again."
      fi
    else
      warn "disc" "${type:-no disc} — insert one to scan"
      note "macOS creates no /dev node until media is loaded, so the device path"
      note "appears only with a disc in the tray. Everything else is ready."
    fi
  fi
else
  if [ -r "${DVD_DEVICE:-/dev/sr0}" ]; then ok "optical drive" "${DVD_DEVICE:-/dev/sr0}"
  else bad "optical drive" "${DVD_DEVICE:-/dev/sr0} not readable"; fi
fi

# --- encoders --------------------------------------------------------------
echo
echo "Encoders"
encoders=$(HandBrakeCLI --help 2>/dev/null)
if printf '%s' "$encoders" | grep -q '^ *x264 *$'; then
  ok "x264 (Quality)" "available"
else
  bad "x264 (Quality)" "HandBrake reports no x264 encoder"
fi
if printf '%s' "$encoders" | grep -q 'nvenc_h264'; then
  ok "nvenc (Speed)" "available"
else
  warn "nvenc (Speed)" "absent — Speed mode is hidden in the UI; use Quality"
  note "Expected on Apple Silicon. x264 on SD DVD content still runs many times"
  note "faster than realtime here."
fi

# --- storage ---------------------------------------------------------------
echo
echo "Storage"
OUT=${OUTPUT_ROOT:-$HOME/Movies/dvd-rip}
free=$(df -h "$(dirname "$OUT")" 2>/dev/null | tail -1 | awk '{print $4}')
ok "output root" "$OUT ${free:+($free free)}"
note "Blu-ray rips need ~40 GB of scratch space during the job."

echo
if [ "$fail" -eq 0 ]; then
  printf "${GREEN}Ready.${OFF} Start with: make native\n\n"
else
  printf "${RED}Not ready${OFF} — fix the ✗ items above.\n\n"
fi
exit $fail
