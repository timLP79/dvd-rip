"""Host-OS differences in one place: device naming, tool names, storage defaults.

This project was built for a Linux host where the optical drive is `/dev/sr0`, the
GPU is Nvidia, and everything runs in a container with `/output` and `/data`
bind-mounted. On macOS none of that holds:

- **Docker can't help.** Its Linux VM has no optical/USB passthrough, so the
  `devices:` mapping in docker-compose.yml can never see a drive. On macOS the app
  runs natively against Homebrew tools instead.
- **The drive has no stable path.** It enumerates as `/dev/diskN`, and N shifts with
  whatever else is attached, so it must be detected per scan rather than hardcoded —
  that also lets a drive plugged in *after* startup work without a restart.
- **Every consumer wants a different name for the same drive:**
  HandBrake/lsdvd/MakeMKV take the raw device path (`/dev/rdiskN`), while libdiscid
  takes an opaque OS-defined id which on macOS is a drive *index* (`"1"`), not a path.
  Passing a `/dev/...` path to libdiscid on macOS just fails.
- **Homebrew has no `cdparanoia`** — libcdio ships the same tool as `cd-paranoia`.
- **There is no NVENC**, so Speed mode has no encoder at all (see `SPEED_ENCODER`).

Every value here is overridable by the env var named in its docstring, so anything
auto-detection gets wrong can be pinned by hand.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

IS_MACOS = sys.platform == "darwin"

# Fallback when detection finds nothing and no override is set. On Linux this is
# simply correct; on macOS it is a placeholder that produces a clear "no drive"
# scan failure rather than a confusing one.
_LINUX_DEFAULT_DEVICE = "/dev/sr0"

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

#: cdparanoia's binary name. Homebrew ships libcdio's build as `cd-paranoia`;
#: the CLI flags this project uses (`-d <dev> <track> <out.wav>`) are identical.
CDPARANOIA_BIN = os.getenv("CDPARANOIA_BIN") or ("cd-paranoia" if IS_MACOS else "cdparanoia")

# MakeMKV on macOS installs an .app bundle and puts nothing on PATH.
_MACOS_MAKEMKVCON = "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon"


def makemkvcon_bin() -> str:
    """Path to `makemkvcon`. Override with `MAKEMKVCON_BIN`.

    Resolved per call rather than at import so installing MakeMKV doesn't require
    restarting the app.
    """
    override = os.getenv("MAKEMKVCON_BIN")
    if override:
        return override
    if IS_MACOS and not shutil.which("makemkvcon") and os.path.exists(_MACOS_MAKEMKVCON):
        return _MACOS_MAKEMKVCON
    return "makemkvcon"


#: HandBrake encoder backing "Speed" mode, or None if this host has no hardware
#: encoder we're willing to use. Apple Silicon has VideoToolbox (`vt_h264`), but
#: exposing it was declined deliberately: on an M-series CPU, x264 on SD DVD
#: content already runs many times faster than realtime, so the quality cost of a
#: hardware encoder buys nothing. None makes Speed mode unavailable rather than
#: silently downgraded — see `presets.build_handbrake_args`.
SPEED_ENCODER: Optional[str] = None if IS_MACOS else "nvenc_h264"


def speed_mode_available() -> bool:
    """True if this host can serve an EncoderMode.SPEED rip."""
    return SPEED_ENCODER is not None


# ---------------------------------------------------------------------------
# Storage defaults
# ---------------------------------------------------------------------------
# The container defaults (/output, /data) are bind-mount targets that don't exist
# on a Mac. Natively we use the platform's conventional locations; the rip ledger
# and scratch stay OUT of the media tree so Plex never scans them.

def default_output_root() -> str:
    """Where finished rips go. Override with `OUTPUT_ROOT`."""
    if IS_MACOS:
        return str(Path.home() / "Movies" / "dvd-rip")
    return "/output"


def default_library_db() -> str:
    """SQLite rip ledger. Override with `LIBRARY_DB`."""
    if IS_MACOS:
        return str(Path.home() / "Library" / "Application Support" / "dvd-rip" / "library.db")
    return "/data/library.db"


def default_scratch_dir() -> str:
    """Transient MKV/WAV scratch. Override with `MAKEMKV_SCRATCH`.

    Needs ~40 GB free during a Blu-ray rip.
    """
    if IS_MACOS:
        return str(Path.home() / "Library" / "Application Support" / "dvd-rip" / "scratch")
    return "/data/scratch"


# ---------------------------------------------------------------------------
# Optical device
# ---------------------------------------------------------------------------

# `drutil status` prints a block per drive; the device path is on the "Name:" field:
#
#      Vendor   Product           Rev
#      HL-DT-ST DVDRAM GP60NB50   RA00
#
#                Type: DVD-ROM               Name: /dev/disk4
#
# With no disc inserted the Type reads "No Media" but the Name is still present,
# so this detects the *drive*, not the disc.
_DRUTIL_NAME_RE = re.compile(r"^\s*Name:\s*(/dev/disk\d+)\s*$", re.MULTILINE)


def parse_drutil_device(stdout: str) -> Optional[str]:
    """Extract the optical drive's device path from `drutil status` output.

    Returns the *cooked* path (`/dev/disk4`); callers convert to raw. None when no
    optical drive is attached — `drutil status` prints nothing at all in that case.
    """
    match = _DRUTIL_NAME_RE.search(stdout)
    if not match:
        # Fall back to a looser match: the field is printed on a shared line with
        # Type:, and drutil's column padding has changed across macOS releases.
        loose = re.search(r"Name:\s*(/dev/disk\d+)", stdout)
        return loose.group(1) if loose else None
    return match.group(1)


def raw_device(device: str) -> str:
    """Map a cooked BSD disk path to its raw character device.

    `/dev/disk4` -> `/dev/rdisk4`. The raw node is what HandBrake, lsdvd and
    MakeMKV want: reads bypass the buffer cache, which matters for a 4 GB+ linear
    read off optical media. Anything that isn't a `/dev/diskN` path is returned
    unchanged, so an explicit override is never rewritten.
    """
    if device.startswith("/dev/disk"):
        return "/dev/r" + device[len("/dev/"):]
    return device


async def detect_optical_device() -> Optional[str]:
    """Detect the attached optical drive on macOS, or None if there isn't one.

    Returns a raw device path (`/dev/rdisk4`). Linux always returns None — there
    the device is a fixed, configured path, not something to go hunting for.
    """
    if not IS_MACOS:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "drutil", "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except (FileNotFoundError, asyncio.TimeoutError, OSError) as exc:
        log.warning("optical drive detection failed: %s", exc)
        return None
    cooked = parse_drutil_device(stdout.decode(errors="replace"))
    return raw_device(cooked) if cooked else None


async def resolve_optical_device() -> str:
    """The device path to hand HandBrake / lsdvd / MakeMKV for this scan or rip.

    Resolution order:
      1. `DVD_DEVICE` env var — an explicit override is never second-guessed.
      2. macOS: live detection via `drutil`, so a drive plugged in after startup
         is picked up on the next scan with no restart.
      3. The Linux default, `/dev/sr0`.
    """
    override = os.getenv("DVD_DEVICE")
    if override:
        return override
    detected = await detect_optical_device()
    return detected or _LINUX_DEFAULT_DEVICE


async def device_label() -> str:
    """The device string to show in the UI header.

    `resolve_optical_device` must always return something runnable, so with no
    drive attached it falls back to the Linux default — and printing "/dev/sr0"
    in the header of a Mac is just misleading. Report the absence instead.
    """
    override = os.getenv("DVD_DEVICE")
    if override:
        return override
    if not IS_MACOS:
        return _LINUX_DEFAULT_DEVICE
    return await detect_optical_device() or "no drive detected"


def discid_device(device: str) -> str:
    """Translate an optical device path into what libdiscid expects.

    libdiscid's device id is explicitly OS-defined (see `discid_get_default_device`
    in discid.h). On Linux it's the same `/dev/sr0` path everything else uses; on
    macOS it's a drive *index* string (`"1"`), and handing it a `/dev/...` path
    makes `discid.read()` fail — which this app would misread as "not an audio CD"
    and quietly fall through to the DVD scanner.

    Override with `DISCID_DEVICE`.
    """
    override = os.getenv("DISCID_DEVICE")
    if override:
        return override
    if not IS_MACOS:
        return device
    try:
        import discid  # lazily imported so tests never need libdiscid installed
        return discid.get_default_device()
    except Exception as exc:  # pragma: no cover - depends on host libdiscid
        log.warning("libdiscid default device lookup failed (%s); falling back to '1'", exc)
        return "1"


def makemkv_source(device: str) -> str:
    """The disc argument for `makemkvcon` (`dev:/dev/rdisk4`, `disc:0`, ...).

    Override with `MAKEMKV_SOURCE`. MakeMKV's drive addressing differs between
    platforms and builds; if `dev:<path>` isn't recognised on your host, run
    `makemkvcon -r info disc:9999` to list drives and set MAKEMKV_SOURCE to the
    matching `disc:N`.
    """
    override = os.getenv("MAKEMKV_SOURCE")
    if override:
        return override
    return f"dev:{device}"


def describe() -> dict[str, object]:
    """Snapshot of the resolved host configuration, for logging and diagnostics."""
    return {
        "platform": sys.platform,
        "cdparanoia": CDPARANOIA_BIN,
        "makemkvcon": makemkvcon_bin(),
        "speed_encoder": SPEED_ENCODER,
        "output_root": os.getenv("OUTPUT_ROOT", default_output_root()),
        "library_db": os.getenv("LIBRARY_DB", default_library_db()),
        "scratch_dir": os.getenv("MAKEMKV_SCRATCH", default_scratch_dir()),
        "dvd_device_override": os.getenv("DVD_DEVICE") or None,
    }
