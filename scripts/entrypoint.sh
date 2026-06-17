#!/usr/bin/env bash
# Drop root before launching the app so files written to /output are owned by the host user.
#
# Inputs (env vars, all optional):
#   PUID       host UID to run as            (default 1000)
#   PGID       host GID to run as            (default 1000)
#   CDROM_GID  GID of the host 'cdrom' group (default 24)
#
# Why a runtime entrypoint instead of a fixed USER in the Dockerfile?
# The host's cdrom GID can vary (24 on Ubuntu, sometimes different on other distros), and
# different hosts run as different UIDs. Dynamic setup keeps one image portable across hosts.

set -euo pipefail

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
CDROM_GID="${CDROM_GID:-24}"

# Create or rename the 'app' group to match PGID.
if ! getent group app >/dev/null; then
    if getent group "$PGID" >/dev/null; then
        existing_group="$(getent group "$PGID" | cut -d: -f1)"
        groupmod -n app "$existing_group"
    else
        groupadd -g "$PGID" app
    fi
else
    current_gid="$(getent group app | cut -d: -f3)"
    if [[ "$current_gid" != "$PGID" ]]; then
        groupmod -g "$PGID" app
    fi
fi

# Create or update the 'app' user to match PUID.
if ! id app >/dev/null 2>&1; then
    if getent passwd "$PUID" >/dev/null; then
        existing_user="$(getent passwd "$PUID" | cut -d: -f1)"
        usermod -l app -d /home/app -m "$existing_user" 2>/dev/null || true
    else
        useradd -u "$PUID" -g app -d /home/app -m -s /bin/bash app
    fi
else
    current_uid="$(id -u app)"
    if [[ "$current_uid" != "$PUID" ]]; then
        usermod -u "$PUID" app
    fi
fi

# Ensure a group with CDROM_GID exists and 'app' is in it (so /dev/sr0 is readable).
if ! getent group "$CDROM_GID" >/dev/null; then
    groupadd -g "$CDROM_GID" hostcdrom
fi
cdrom_group_name="$(getent group "$CDROM_GID" | cut -d: -f1)"
usermod -a -G "$cdrom_group_name" app

# Make sure output dir and /app are writable by the dropped user.
mkdir -p "${OUTPUT_ROOT:-/output}"
chown -R "$PUID:$PGID" "${OUTPUT_ROOT:-/output}" 2>/dev/null || true
mkdir -p "$(dirname "${LIBRARY_DB:-/data/library.db}")"
chown -R "$PUID:$PGID" "$(dirname "${LIBRARY_DB:-/data/library.db}")" 2>/dev/null || true

SCRATCH="${MAKEMKV_SCRATCH:-/data/scratch}"
mkdir -p "$SCRATCH" && chown -R "$PUID:$PGID" "$SCRATCH" 2>/dev/null || true
if [ -n "${MAKEMKV_APP_KEY:-}" ]; then
    APP_HOME="$(getent passwd "$PUID" | cut -d: -f6)"
    APP_HOME="${APP_HOME:-/home/app}"
    mkdir -p "$APP_HOME/.MakeMKV"
    # app_UpdateEnable = 0 stops makemkvcon from contacting the MakeMKV web server to
    # check for new versions. That online check otherwise hangs the disc scan at 99% CPU
    # whenever makemkv.com is unreachable (e.g. their DNSSEC outage, or :443 refused).
    # Beta-key validation is local and unaffected. See dvd-rip-br7.
    {
        printf 'app_Key = "%s"\n' "$MAKEMKV_APP_KEY"
        printf 'app_UpdateEnable = "0"\n'
        # sdf_Stop disables LibreDrive/SDF for the listed drive id(s). Non-LibreDrive
        # drives on Linux otherwise hang at 100%% CPU in "SDF auto" (a known MakeMKV
        # bug, worsened while makemkv.com can't serve the SDF download). Standard AACS
        # mode handles 1080p Blu-ray fine; LibreDrive is only needed for UHD/4K.
        # Find your drive id in MakeMKV_log.txt ("SDF auto ... <id>") or the DRV: line.
        if [ -n "${MAKEMKV_SDF_STOP:-}" ]; then
            printf 'sdf_Stop = "%s"\n' "$MAKEMKV_SDF_STOP"
        fi
    } > "$APP_HOME/.MakeMKV/settings.conf"
    chown -R "$PUID:$PGID" "$APP_HOME/.MakeMKV" 2>/dev/null || true
fi

echo "[entrypoint] running as uid=$PUID gid=$PGID (cdrom gid=$CDROM_GID)"
exec gosu app "$@"
