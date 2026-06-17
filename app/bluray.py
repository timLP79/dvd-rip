"""MakeMKV front-end: detect Blu-ray discs, list titles, decrypt a title to MKV.

The only module that touches `makemkvcon`. Robot-mode (-r) output is parsed into
the same `Title`/`ScanResult` models the DVD path uses, so the UI is uniform.

NOTE: the robot output format + attribute codes here follow MakeMKV's documented
`-r` protocol. Validate against real `makemkvcon -r info` output on a real disc.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .models import AudioTrack, ScanResult, SubtitleTrack, Title

log = logging.getLogger(__name__)

# MakeMKV ap_ItemAttributeId codes used here (from apdefs.h):
_ATTR_NAME = 2
_ATTR_LANG_CODE = 3
_ATTR_LANG_NAME = 4
_ATTR_CODEC_LONG = 7
_ATTR_CHAPTER_COUNT = 8
_ATTR_DURATION = 9
_ATTR_VIDEO_SIZE = 19

_MAIN_FEATURE_MIN_SECONDS = 20 * 60


def is_bluray_output(robot_output: str) -> bool:
    """True if makemkvcon info output identifies a Blu-ray disc.

    Matches MakeMKV's disc-type text ("Blu-ray disc"); requiring "disc" avoids
    false-matching a DVD whose volume label happens to contain "bluray".
    NOTE: relies on MakeMKV's robot output format — verify against a real disc.
    """
    return bool(re.search(r"blu-?ray\s+disc", robot_output, re.IGNORECASE))


def _duration_to_seconds(value: str) -> int:
    parts = value.strip().split(":")
    if not parts or not all(p.isdigit() for p in parts):
        return 0
    parts = [int(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3], parts[-2], parts[-1]
    return h * 3600 + m * 60 + s


def parse_makemkv_progress(line: str) -> Optional[float]:
    """Parse a `PRGV:current,total,max` line into 0-100 percent (uses `total`)."""
    m = re.match(r"PRGV:(\d+),(\d+),(\d+)", line.strip())
    if not m:
        return None
    _current, total, pmax = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if pmax <= 0:
        return 0.0
    return round(total / pmax * 100.0, 1)


# MakeMKV reports the current operation title on PRGC lines. We surface it as a
# user-facing stage so the progress bar resetting between operations (disc analysis
# vs. the long "Saving to MKV" copy) reads as distinct, labelled steps.
_PRGC_RE = re.compile(r'^PRGC:\d+,\d+,"(.*)"$')


def _stage_label(makemkv_operation: str) -> str:
    """Map a MakeMKV current-operation title to a user-facing decrypt stage.

    "Saving to MKV file" is the long copy of the decrypted title; everything
    before it (opening, analyzing, BD+ processing) is disc preparation.
    """
    return "Extracting title" if "sav" in makemkv_operation.lower() else "Analyzing disc"


@dataclass
class _TitleAcc:
    index: int
    duration_seconds: int = 0
    chapter_count: int = 0
    frame_width: int = 0
    frame_height: int = 0
    audio: list = field(default_factory=list)
    subs: list = field(default_factory=list)


def parse_makemkv_info(robot_output: str) -> list[Title]:
    """Parse robot `info` output (TINFO/SINFO lines) into Title models."""
    titles: dict[int, _TitleAcc] = {}
    # stream type accumulator: (title, stream) -> {"kind","lang_code","lang_name","codec"}
    streams: dict[tuple, dict] = {}

    tinfo_re = re.compile(r'^TINFO:(\d+),(\d+),\d+,"(.*)"$')
    sinfo_re = re.compile(r'^SINFO:(\d+),(\d+),(\d+),\d+,"(.*)"$')

    for line in robot_output.splitlines():
        tm = tinfo_re.match(line)
        if tm:
            tid, code, value = int(tm.group(1)), int(tm.group(2)), tm.group(3)
            acc = titles.setdefault(tid, _TitleAcc(index=tid))
            if code == _ATTR_DURATION:
                acc.duration_seconds = _duration_to_seconds(value)
            elif code == _ATTR_CHAPTER_COUNT and value.isdigit():
                acc.chapter_count = int(value)
            continue
        sm = sinfo_re.match(line)
        if sm:
            tid, sid, code, value = (
                int(sm.group(1)), int(sm.group(2)), int(sm.group(3)), sm.group(4),
            )
            titles.setdefault(tid, _TitleAcc(index=tid))
            st = streams.setdefault((tid, sid), {})
            if code == 1:  # Type
                st["kind"] = value  # "Video" / "Audio" / "Subtitles"
            elif code == _ATTR_LANG_CODE:
                st["lang_code"] = value
            elif code == _ATTR_LANG_NAME:
                st["lang_name"] = value
            elif code == _ATTR_CODEC_LONG:
                st["codec"] = value
            elif code == _ATTR_VIDEO_SIZE:
                mm = re.match(r"(\d+)x(\d+)", value)
                if mm:
                    title_acc = titles[tid]
                    title_acc.frame_width, title_acc.frame_height = int(mm.group(1)), int(mm.group(2))

    # assemble audio/subtitle tracks per title in stream order
    for (tid, sid), st in sorted(streams.items()):
        acc = titles[tid]
        kind = (st.get("kind") or "").lower()
        lang = st.get("lang_name") or st.get("lang_code")
        if kind == "audio":
            acc.audio.append(AudioTrack(index=len(acc.audio) + 1, language=lang,
                                        codec=st.get("codec")))
        elif kind in ("subtitles", "subtitle"):
            acc.subs.append(SubtitleTrack(index=len(acc.subs) + 1, language=lang))

    out: list[Title] = []
    for tid in sorted(titles):
        acc = titles[tid]
        out.append(Title(
            index=acc.index,
            duration_seconds=acc.duration_seconds,
            chapter_count=acc.chapter_count,
            frame_width=acc.frame_width,
            frame_height=acc.frame_height,
            audio_tracks=acc.audio,
            subtitle_tracks=acc.subs,
            is_main_feature=False,
        ))
    # flag the longest title over the threshold as the main feature
    if out:
        longest = max(out, key=lambda t: t.duration_seconds)
        if longest.duration_seconds >= _MAIN_FEATURE_MIN_SECONDS:
            longest.is_main_feature = True
    return out


# BD+ discs make `info` slow: MakeMKV warns the BD+ pass "may take up to few minutes".
# Real measurement on a BD+ disc (The Martian) was ~3-4 min, so a 180s cap aborted it.
_INFO_TIMEOUT = 600.0


async def _spawn(cmd: list[str], timeout: float = _INFO_TIMEOUT) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def detect_and_scan(device: str, *, _run: Callable = _spawn) -> Optional[ScanResult]:
    """Run `makemkvcon info`; return a Blu-ray ScanResult, or None to fall back to DVD."""
    cmd = ["makemkvcon", "-r", "--cache=1", "info", f"dev:{device}"]
    try:
        rc, out, err = await _run(cmd, timeout=_INFO_TIMEOUT)
    except FileNotFoundError:
        log.error("makemkvcon not found on PATH")
        return None
    except asyncio.TimeoutError:
        log.error("makemkvcon info timed out for %s", device)
        return None
    if rc != 0:
        log.info("makemkvcon info rc=%s (not a Blu-ray or key issue): %s", rc, err.strip()[:300])
        return None
    if not is_bluray_output(out):
        return None
    titles = parse_makemkv_info(out)
    if not titles:
        log.info("makemkvcon reported Blu-ray but parsed 0 titles; falling back to DVD")
        return None
    return ScanResult(dvd_label="BLURAY", titles=titles, disc_format="bluray")


async def decrypt_title(
    device: str,
    title_id: int,
    scratch_dir: str,
    *,
    on_progress: Optional[Callable[[float], None]] = None,
    on_stage: Optional[Callable[[str], None]] = None,
    register_proc: Optional[Callable] = None,
) -> Path:
    """Decrypt one title to an MKV in scratch_dir. Returns the produced .mkv path.

    register_proc(proc) lets the caller (ripper) hold the process for cancellation.
    on_stage(label) is called when the MakeMKV operation changes ("Analyzing disc"
    -> "Extracting title"), so the UI can show what's happening.
    Raises RuntimeError on failure (with a tail of makemkvcon output).
    """
    Path(scratch_dir).mkdir(parents=True, exist_ok=True)
    cmd = ["makemkvcon", "-r", "--cache=1", "--progress=-same",
           "mkv", f"dev:{device}", str(title_id), scratch_dir]
    log.info("decrypt_title: %s", shlex.join(cmd))
    started = time.time()
    before = set(Path(scratch_dir).glob("*.mkv"))
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    if register_proc:
        register_proc(proc)
    tail: list[str] = []
    last_stage: Optional[str] = None
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip("\r\n")
            pct = parse_makemkv_progress(line)
            if pct is not None:
                if on_progress:
                    on_progress(pct)
                continue
            sm = _PRGC_RE.match(line)
            if sm:
                stage = _stage_label(sm.group(1))
                if stage != last_stage:
                    last_stage = stage
                    if on_stage:
                        on_stage(stage)
                continue
            if line:
                tail.append(line)
                if len(tail) > 25:
                    tail.pop(0)
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
    rc = proc.returncode
    if rc != 0:
        detail = "\n".join(tail).strip()
        raise RuntimeError(f"makemkvcon exited with code {rc}\n{detail}")
    # Prefer genuinely new files; but makemkvcon names output deterministically and
    # overwrites a same-named leftover in place (set-diff would then be empty even
    # though the decrypt succeeded), so fall back to any .mkv written during this run.
    after = set(Path(scratch_dir).glob("*.mkv"))
    produced = (after - before) or {p for p in after if p.stat().st_mtime >= started - 1}
    if not produced:
        raise RuntimeError("makemkvcon produced no .mkv file")
    return max(produced, key=lambda p: p.stat().st_mtime)
