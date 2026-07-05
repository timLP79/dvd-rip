"""Audio-CD ripping: detection, TOC scan, and the per-track rip loop.

The only module that touches `libdiscid`/`cdparanoia`/`flac`/`lame`. Parallels
bluray.py. The disc reader and subprocess spawner are injectable so no test
requires a physical CD or these tools installed.

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


async def is_audio_cd(device: str, *, read_disc: Callable[[str], CDDiscInfo] = _read_disc) -> bool:
    """True if `device` holds an audio CD (libdiscid.read succeeds)."""
    try:
        read_disc(device)
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
    info = read_disc(device)
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
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    wavs: list[Path] = []
    joined = scratch / "joined.wav"
    try:
        for i, t in enumerate(included):
            if on_progress:
                on_progress(round(i / total * (100 if per_track else 90), 1))
            wav = scratch / f"{t.number:02d}.wav"
            if on_stage:
                label = (f"Ripping track {t.number}: {t.title}"
                         if per_track and t.title
                         else f"Ripping track {t.number} of {total}")
                on_stage(label)
            rc, out = await spawn(
                ["cdparanoia", "-d", device, str(t.number), str(wav)],
                register_proc=register_proc)
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
