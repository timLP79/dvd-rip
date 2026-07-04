"""Audio-CD ripping: detection, TOC scan, and the per-track rip loop.

The only module that touches `libdiscid`/`cdparanoia`/`flac`/`lame`. Parallels
bluray.py. The disc reader and subprocess spawner are injectable so no test
requires a physical CD or these tools installed.

Audiobook mode joins all included tracks into ONE MP3 per disc; music mode
emits one FLAC per track (design decisions 8-9).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .models import CDTrack, CDTrackSelection, MBRelease, ScanResult
from .paths import clean_track_title, audiobook_path, music_path, unique_path

log = logging.getLogger(__name__)


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
