"""Audio-CD ripping: detection, TOC scan, and the per-track rip loop.

The only module that touches `libdiscid`/`cdparanoia`/`flac`/`lame`. Parallels
bluray.py. The disc reader and subprocess spawner are injectable so no test
requires a physical CD or these tools installed.

Note the two device conventions in play: `device` here is whatever the *reading*
tool expects, and libdiscid does not agree with cdparanoia about that on macOS —
callers resolve both through `hostenv` (see `hostenv.discid_device`).

Audiobook mode joins all included tracks into ONE MP3 per disc, or (when the
user opts to split) emits one titled MP3 per track; music mode emits one FLAC
per track (design decisions 8-9).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import hostenv
from .models import CDTrack, CDTrackSelection, MBRelease, ScanResult
from .paths import (
    audiobook_path,
    audiobook_track_path,
    clean_track_title,
    music_path,
    unique_path,
)

log = logging.getLogger(__name__)

_LAME_VBR_QUALITY = "6"   # ~115 kbps VBR — ample for spoken word


class RipError(RuntimeError):
    """A cdparanoia/flac/lame step failed; carries a tail of tool output."""


@dataclass
class CDDiscInfo:
    disc_id: str
    tracks: list[CDTrack]


def _read_disc(device: str) -> CDDiscInfo:
    """Read the disc ID + TOC via libdiscid. Raises on a non-audio disc."""
    import discid  # lazily imported so tests never need libdiscid installed
    disc = discid.read(device)
    tracks = [
        CDTrack(number=t.number, duration_seconds=t.seconds)
        for t in disc.tracks
    ]
    return CDDiscInfo(disc_id=disc.id, tracks=tracks)


# A libdiscid TOC read is normally ~1-2s. Bound it so a stuck read on a bad disc
# can't hang the scan forever — which, since is_audio_cd runs under the scan
# coordinator, would otherwise leave the coordinator's busy flag set (a cancel
# kills subprocesses, not this worker thread) and lock out all future scans.
_TOC_READ_TIMEOUT = 30.0


async def is_audio_cd(device: str, *, read_disc: Callable[[str], CDDiscInfo] = _read_disc) -> bool:
    """True if `device` holds an audio CD (libdiscid.read succeeds).

    `read_disc` is a blocking libdiscid call; run it in a worker thread so a
    slow/stuck TOC read never blocks the asyncio event loop.
    """
    try:
        await asyncio.wait_for(asyncio.to_thread(read_disc, device), timeout=_TOC_READ_TIMEOUT)
        return True
    except Exception:
        return False


async def scan(
    device: str,
    *,
    read_disc: Callable[[str], CDDiscInfo] = _read_disc,
    mb_client=None,
) -> ScanResult:
    """Read the TOC, then best-effort attach MusicBrainz candidates.

    The TOC is always returned even if MusicBrainz is down/empty.
    """
    info = await asyncio.to_thread(read_disc, device)
    releases: list[MBRelease] = []
    if mb_client is not None:
        try:
            releases = await mb_client.lookup_disc(info.disc_id)
        except Exception as exc:
            log.info("MusicBrainz lookup errored for %s: %s", info.disc_id, exc)
            releases = []
    return ScanResult(
        dvd_label="AUDIO_CD",
        titles=[],
        disc_format="cd",
        tracks=info.tracks,
        disc_id=info.disc_id,
        musicbrainz=releases,
    )


def flac_args(wav_path, out_path, *, artist, album, title, track_number,
              track_total, date=None, disc_number=None) -> list[str]:
    args = [
        "flac", "--best", "-s", "-f", "-o", str(out_path),
        f"--tag=ARTIST={artist}", f"--tag=ALBUM={album}",
        f"--tag=ALBUMARTIST={artist}", f"--tag=TITLE={title}",
        f"--tag=TRACKNUMBER={track_number}", f"--tag=TRACKTOTAL={track_total}",
    ]
    if date:
        args.append(f"--tag=DATE={date}")
    if disc_number:
        args.append(f"--tag=DISCNUMBER={disc_number}")
    args.append(str(wav_path))
    return args


def lame_args(wav_path, out_path, *, artist, title, album, disc_number,
              total_discs, year=None) -> list[str]:
    args = [
        "lame", "-V", _LAME_VBR_QUALITY, "--vbr-new", "--quiet",
        "--ta", artist, "--tt", title, "--tl", album, "--tn", str(disc_number),
    ]
    if total_discs:
        args += ["--tv", f"TPOS={disc_number}/{total_discs}"]
    if year:
        args += ["--ty", str(year)]
    args += [str(wav_path), str(out_path)]
    return args


def lame_track_args(wav_path, out_path, *, artist, title, album, track_number,
                    disc_number, total_discs, year=None) -> list[str]:
    """Per-track MP3 tags for a split audiobook (TITLE = story/track title)."""
    args = [
        "lame", "-V", _LAME_VBR_QUALITY, "--vbr-new", "--quiet",
        "--ta", artist, "--tt", title, "--tl", album, "--tn", str(track_number),
    ]
    if total_discs and total_discs > 1:
        args += ["--tv", f"TPOS={disc_number}/{total_discs}"]
    elif disc_number > 1:
        args += ["--tv", f"TPOS={disc_number}"]
    if year:
        args += ["--ty", str(year)]
    args += [str(wav_path), str(out_path)]
    return args


def _concat_wavs(wav_paths, out_path) -> Path:
    """Join equal-format (CDDA) WAVs into one. All CD audio is 44100/16/stereo."""
    import wave
    with wave.open(str(out_path), "wb") as out:
        params_set = False
        for p in wav_paths:
            with wave.open(str(p), "rb") as w:
                if not params_set:
                    out.setparams(w.getparams())
                    params_set = True
                out.writeframes(w.readframes(w.getnframes()))
    return Path(out_path)


async def _spawn(cmd: list[str], *, register_proc: Optional[Callable] = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    if register_proc:
        register_proc(proc)
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


_CDDA_BYTES_PER_SEC = 44100 * 2 * 2   # 16-bit stereo @ 44.1 kHz
_WAV_HEADER_BYTES = 44


def _wav_frac(size_bytes: int, expected_bytes: int) -> float:
    """Fraction of a track's WAV written so far, clamped to [0, 0.99]."""
    if not expected_bytes:
        return 0.0
    return min(max((size_bytes - _WAV_HEADER_BYTES) / expected_bytes, 0.0), 0.99)


async def _extract_track(cmd, *, wav, expected_bytes, on_frac, register_proc, spawn):
    """Run cdparanoia, polling the growing WAV so progress moves within a track.

    cdparanoia's own progress bar is fiddly to parse; the output WAV grows
    predictably with the (known) track length, so file size gives smooth,
    real-time progress with no dependency on cdparanoia's stderr format.
    """
    poll = None
    if on_frac and expected_bytes:
        async def _poll():
            try:
                while True:
                    try:
                        size = Path(wav).stat().st_size
                    except OSError:
                        size = 0
                    try:
                        on_frac(_wav_frac(size, expected_bytes))
                    except Exception:
                        return   # progress is best-effort; never let it affect the rip
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                pass
        poll = asyncio.create_task(_poll())
    try:
        return await spawn(cmd, register_proc=register_proc)
    finally:
        if poll:
            poll.cancel()
            try:
                await poll
            except asyncio.CancelledError:
                pass


async def rip_disc(
    device: str,
    *,
    mode: str,
    output_root: Path,
    creator: str,
    title: str,
    year: Optional[int],
    disc_number: int,
    total_discs: Optional[int],
    tracks: list[CDTrackSelection],
    scratch_dir: str,
    split: bool = False,
    on_progress: Optional[Callable[[float], None]] = None,
    on_stage: Optional[Callable[[str], None]] = None,
    register_proc: Optional[Callable] = None,
    spawn: Callable[..., Awaitable[tuple[int, str]]] = _spawn,
    join_wavs: Callable = _concat_wavs,
) -> list[Path]:
    """Rip the included tracks.

    - Music: one FLAC per track.
    - Audiobook, split=True: one MP3 per track, named by its MusicBrainz title.
    - Audiobook, split=False: all tracks joined into ONE MP3 for the disc.

    Returns the files written. On any failure removes everything this call wrote
    (no-partial-garbage rule) and re-raises RipError. Scratch WAVs are always swept.
    """
    included = [t for t in tracks if t.include]
    total = len(included)
    # Per-track output: every music rip, and audiobook rips the user chose to split.
    per_track = mode == "music" or (mode == "audiobook" and split)
    scale = 100 if per_track else 90   # ripping is the whole job (split) or the first 90% (join)
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    wavs: list[Path] = []
    joined = scratch / "joined.wav"
    try:
        for i, t in enumerate(included):
            if on_progress:
                on_progress(round(i / total * scale, 1))
            wav = scratch / f"{t.number:02d}.wav"
            if on_stage:
                label = (f"Ripping track {t.number}: {t.title}"
                         if per_track and t.title
                         else f"Ripping track {t.number} of {total}")
                on_stage(label)
            # Poll the growing WAV against the track's known length for smooth,
            # within-track progress (cdparanoia's own progress bar is unreliable).
            on_frac = (
                (lambda f, _i=i: on_progress(round((_i + f) / total * scale, 1)))
                if on_progress else None)
            rc, out = await _extract_track(
                [hostenv.CDPARANOIA_BIN, "-d", device, str(t.number), str(wav)],
                wav=wav, expected_bytes=(t.duration_seconds or 0) * _CDDA_BYTES_PER_SEC,
                on_frac=on_frac, register_proc=register_proc, spawn=spawn)
            if rc != 0:
                raise RipError(f"cdparanoia failed on track {t.number} (rc={rc})\n{out}".strip())
            wavs.append(wav)

            if per_track:
                if mode == "music":
                    out_path = unique_path(music_path(
                        output_root, artist=creator, album=title, disc_number=disc_number,
                        total_discs=total_discs, track_number=t.number, track_title=t.title))
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    rc, out = await spawn(flac_args(
                        wav, out_path, artist=creator, album=title,
                        title=clean_track_title(t.title, t.number),
                        track_number=t.number, track_total=total, date=year,
                        disc_number=(disc_number if (total_discs or disc_number > 1) else None)),
                        register_proc=register_proc)
                    if rc != 0:
                        raise RipError(f"flac failed on track {t.number} (rc={rc})\n{out}".strip())
                else:  # audiobook split -> one titled MP3 per track
                    out_path = unique_path(audiobook_track_path(
                        output_root, author=creator, book=title, disc_number=disc_number,
                        total_discs=total_discs, track_number=t.number, track_title=t.title))
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    rc, out = await spawn(lame_track_args(
                        wav, out_path, artist=creator,
                        title=clean_track_title(t.title, t.number), album=title,
                        track_number=t.number, disc_number=disc_number,
                        total_discs=total_discs, year=year),
                        register_proc=register_proc)
                    if rc != 0:
                        raise RipError(f"lame failed on track {t.number} (rc={rc})\n{out}".strip())
                written.append(out_path)
                wav.unlink(missing_ok=True)

        if mode == "audiobook" and not split:
            if on_stage:
                on_stage("Encoding disc")
            out_path = unique_path(audiobook_path(
                output_root, author=creator, book=title,
                disc_number=disc_number, total_discs=total_discs))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            join_wavs(wavs, joined)
            rc, out = await spawn(lame_args(
                joined, out_path, artist=creator, title=title, album=title,
                disc_number=disc_number, total_discs=total_discs, year=year),
                register_proc=register_proc)
            if rc != 0:
                raise RipError(f"lame failed encoding disc (rc={rc})\n{out}".strip())
            written.append(out_path)

        if on_progress:
            on_progress(100)
        return written
    except BaseException:
        for p in written:
            Path(p).unlink(missing_ok=True)
        raise
    finally:
        for w in wavs:
            Path(w).unlink(missing_ok=True)
        joined.unlink(missing_ok=True)
