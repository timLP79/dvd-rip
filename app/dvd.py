"""DVD scanning: extract titles, audio, subtitles, and the disc label.

We shell out to HandBrakeCLI for the title metadata (since it's the same tool used to rip,
ensuring identical title numbering) and to `lsdvd` for the disc volume label.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional

from .models import AudioTrack, ScanResult, SubtitleTrack, Title

log = logging.getLogger(__name__)

_MAIN_FEATURE_MIN_SECONDS = 20 * 60

# How long HandBrake gets to scan the disc structure. Generous by default: the
# budget is set by the worst realistic disc, not the typical one. A dual-layer
# disc with structural (ARccOS-style) protection presents a dozen-plus decoy
# two-hour titles, and HandBrake walks every one of them over an optical bus at
# a few hundred KB/s — measured well past the previous 120s ceiling on a USB
# drive. Timing out yields an EMPTY title list, which reads as "this disc has
# nothing on it", so an over-tight bound fails misleadingly rather than loudly.
SCAN_TIMEOUT = float(os.getenv("HANDBRAKE_SCAN_TIMEOUT", "600"))


def classify_aspect(width: int, height: int, par_num: int = 1, par_den: int = 1) -> Optional[str]:
    """Classify a title's display aspect ratio into widescreen / fullscreen.

    Display aspect = (width * PAR.num) / (height * PAR.den). DVDs store both 4:3 and
    16:9 at 720x480 and differ only by PAR, so PAR is required for correctness; Blu-ray
    has square pixels (PAR 1:1). Threshold 1.56 is the midpoint of 4:3 (1.333) and
    16:9 (1.778). Returns None when dimensions/PAR are missing.
    """
    if not width or not height or not par_num or not par_den:
        return None
    dar = (width * par_num) / (height * par_den)
    return "widescreen" if dar >= 1.56 else "fullscreen"


@dataclass
class ScannerCommands:
    """Indirection point so tests can inject fake subprocess output."""
    handbrake: list[str]
    lsdvd: list[str]


async def _spawn(cmd: list[str], timeout: float = 120.0, *, register_proc=None) -> tuple[int, str, str]:
    """Run a command via argv (no shell) and capture output."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if register_proc is not None:
        register_proc(proc)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


def parse_handbrake_scan(stdout: str) -> list[Title]:
    """Extract titles from a HandBrakeCLI --json --scan stdout dump.

    HandBrake prefixes JSON blocks with labels like 'JSON Title Set:'. We locate that
    section and parse the brace-balanced JSON that follows.
    """
    marker = "JSON Title Set:"
    pos = stdout.find(marker)
    if pos < 0:
        return []
    start = stdout.find("{", pos)
    if start < 0:
        return []

    depth = 0
    in_str = False
    escape = False
    end = -1
    for i in range(start, len(stdout)):
        ch = stdout[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    if end < 0:
        return []

    payload = json.loads(stdout[start:end])
    main_feature = payload.get("MainFeature")
    titles: list[Title] = []
    for entry in payload.get("TitleList", []):
        duration = entry.get("Duration") or {}
        seconds = (
            duration.get("Hours", 0) * 3600
            + duration.get("Minutes", 0) * 60
            + duration.get("Seconds", 0)
        )
        geometry = entry.get("Geometry") or {}
        audio = [
            AudioTrack(
                index=a.get("TrackNumber", i + 1),
                language=a.get("Language") or a.get("LanguageCode"),
                codec=a.get("CodecName") or a.get("Codec"),
                channels=a.get("ChannelLayoutName") or a.get("ChannelLayout"),
                description=a.get("Description"),
            )
            for i, a in enumerate(entry.get("AudioList", []) or [])
        ]
        subs = [
            SubtitleTrack(
                index=s.get("TrackNumber", i + 1),
                language=s.get("Language") or s.get("LanguageCode"),
                description=s.get("Name"),
            )
            for i, s in enumerate(entry.get("SubtitleList", []) or [])
        ]
        idx = entry.get("Index", len(titles) + 1)
        titles.append(
            Title(
                index=idx,
                duration_seconds=seconds,
                chapter_count=len(entry.get("ChapterList", []) or []),
                frame_width=geometry.get("Width", 0),
                frame_height=geometry.get("Height", 0),
                aspect=classify_aspect(
                    geometry.get("Width", 0), geometry.get("Height", 0),
                    (geometry.get("PAR") or {}).get("Num", 1),
                    (geometry.get("PAR") or {}).get("Den", 1),
                ),
                audio_tracks=audio,
                subtitle_tracks=subs,
                is_main_feature=(idx == main_feature),
            )
        )

    if not any(t.is_main_feature for t in titles) and titles:
        longest = max(titles, key=lambda t: t.duration_seconds)
        if longest.duration_seconds >= _MAIN_FEATURE_MIN_SECONDS:
            longest.is_main_feature = True
    return titles


def parse_lsdvd_label(stdout: str) -> str:
    match = re.search(r"^\s*Disc Title:\s*(.+?)\s*$", stdout, flags=re.MULTILINE)
    if not match:
        return "UNKNOWN_DVD"
    return _safe_label(match.group(1))


def _safe_label(raw: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", raw.strip())
    return cleaned.strip("._-") or "UNKNOWN_DVD"


async def is_dvd_video(device: str, *, _run: Callable = _spawn, register_proc=None) -> bool:
    """Fast probe: is the inserted disc a DVD-Video?

    Lets the scan endpoint route DVDs straight to the DVD scanner instead of first
    running `makemkvcon` (the Blu-ray detector), which is slow and pointless for a
    DVD it will never rip. `lsdvd` reads only the DVD's VIDEO_TS structure — a
    second or two — and exits non-zero on a Blu-ray or data disc.
    """
    kwargs = {} if register_proc is None else {"register_proc": register_proc}
    try:
        rc, stdout, _ = await _run(["lsdvd", device], timeout=15.0, **kwargs)
    except (FileNotFoundError, asyncio.TimeoutError):
        return False
    return rc == 0 and "Disc Title:" in stdout


async def scan(
    device: str,
    *,
    commands: Optional[ScannerCommands] = None,
    register_proc=None,
) -> ScanResult:
    """Run the scan tools against `device` and return a ScanResult.

    On scan failure (no disc, unreadable disc) returns an empty title list with the best
    label we can determine.
    """
    cmds = commands or ScannerCommands(
        handbrake=["HandBrakeCLI", "--json", "--scan", "--title", "0", "--input", device],
        lsdvd=["lsdvd", device],
    )

    titles: list[Title] = []
    warning: Optional[str] = None
    try:
        rc, stdout, stderr = await _spawn(
            cmds.handbrake, timeout=SCAN_TIMEOUT, register_proc=register_proc)
        if rc != 0:
            log.warning("HandBrakeCLI scan returned %s: %s", rc, stderr.strip()[:500])
        titles = parse_handbrake_scan(stdout)
    except FileNotFoundError:
        log.error("HandBrakeCLI not found on PATH")
        warning = "HandBrakeCLI is not installed."
    except asyncio.TimeoutError:
        # Distinguished from "disc has no titles": both end up with an empty list,
        # and without this the UI reports a blank disc when the scan simply ran out
        # of time. Heavily protected discs are the usual cause.
        log.error("HandBrakeCLI scan timed out for %s after %.0fs", device, SCAN_TIMEOUT)
        warning = (
            f"Disc scan timed out after {SCAN_TIMEOUT:.0f}s — the disc may use copy "
            f"protection that makes scanning slow. Raise HANDBRAKE_SCAN_TIMEOUT and rescan."
        )

    label = "UNKNOWN_DVD"
    try:
        rc, stdout, _ = await _spawn(cmds.lsdvd, timeout=15.0, register_proc=register_proc)
        if rc == 0:
            label = parse_lsdvd_label(stdout)
    except (FileNotFoundError, asyncio.TimeoutError):
        pass

    return ScanResult(dvd_label=label, titles=titles, warning=warning)
