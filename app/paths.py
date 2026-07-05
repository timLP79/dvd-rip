"""Plex-compliant output path construction.

Movies:
    <root>/Movies/<Title> (<Year>)/<Title> (<Year>).mp4
    <root>/Movies/<Title> (<Year>)/<Title> (<Year>) - cd2.mp4    # multi-part

TV Shows:
    <root>/TV Shows/<Show>/Season <NN>/<Show> S<NN>E<NN>.mp4

Filenames are sanitized of characters that break Linux/macOS/Windows filesystems
or that Plex's matcher mishandles.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


# Strip OS-illegal chars and a few Plex-confusing ones; collapse whitespace.
_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")

# MusicBrainz titles carry typographic punctuation; map to filesystem-safe ASCII
# BEFORE stripping forbidden chars (straight double-quote is itself forbidden).
_UNICODE_MAP = str.maketrans({
    "‘": "'", "’": "'",   # curly single quotes -> straight apostrophe
    "“": "", "”": "",     # curly double quotes -> drop
    "–": "-", "—": "-",   # en/em dash -> hyphen
    "…": "...",                # ellipsis -> ...
})


def sanitize(name: str) -> str:
    """Make `name` safe as a filesystem component.

    - Drops control chars and characters illegal on Windows/macOS (so the same
      output dir can be served from a SMB share or moved between hosts).
    - Normalizes Unicode punctuation (curly quotes, dashes, ellipsis) to ASCII.
    - Collapses runs of whitespace.
    - Strips leading/trailing dots and spaces (Windows trims those silently).
    - Never returns the empty string; falls back to 'Untitled'.
    """
    cleaned = name.translate(_UNICODE_MAP)
    cleaned = _FORBIDDEN_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip().strip(".")
    return cleaned or "Untitled"


def movie_path(
    output_root: Path,
    *,
    title: str,
    year: Optional[int],
    part_index: Optional[int] = None,
    ext: str = "mp4",
) -> Path:
    """Build the path for a movie rip.

    `part_index` is 1-based. None or 1 produces the main filename; 2+ produces
    a `- cd<N>` suffix that Plex auto-stacks into a single movie entry.
    `ext` is the container extension (`mp4` for DVD, `mkv` for Blu-ray).
    """
    title_clean = sanitize(title)
    name = f"{title_clean} ({year})" if year else title_clean
    folder = output_root / "Movies" / name
    if part_index is None or part_index == 1:
        return folder / f"{name}.{ext}"
    return folder / f"{name} - cd{part_index}.{ext}"


def tv_episode_path(
    output_root: Path,
    *,
    show: str,
    season: int,
    episode: int,
    ext: str = "mp4",
) -> Path:
    """Build the path for a TV episode rip.

    Matches the user's existing convention: no year on the show folder, no
    episode title in the filename, uppercase SxxExx. `ext` is the container
    extension (`mp4` for DVD, `mkv` for Blu-ray).
    """
    show_clean = sanitize(show)
    folder = output_root / "TV Shows" / show_clean / f"Season {season:02d}"
    filename = f"{show_clean} S{season:02d}E{episode:02d}.{ext}"
    return folder / filename


def unique_path(path: Path) -> Path:
    """Return `path` if it doesn't exist; otherwise append a counter.

    Used to avoid clobbering a previous rip of the same disc when the user
    re-rips. Suffix lands before the extension: `Foo.mp4` → `Foo_2.mp4`.
    """
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    parent = path.parent
    n = 2
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def clean_track_title(title: str, number: int) -> str:
    """A per-track filename component. Empty or '[untitled]' -> 'Track NN'."""
    raw = (title or "").strip()
    if not raw or raw.lower() == "[untitled]":
        return f"Track {number:02d}"
    return sanitize(raw)


def _disc_prefix(disc_number: int, total_discs: Optional[int], track_number: int) -> str:
    multi = (total_discs is not None and total_discs > 1) or disc_number > 1
    return f"{disc_number}-{track_number:02d}" if multi else f"{track_number:02d}"


def music_path(
    output_root: Path,
    *,
    artist: str,
    album: str,
    disc_number: int,
    total_discs: Optional[int],
    track_number: int,
    track_title: str,
    ext: str = "flac",
) -> Path:
    """One FLAC per song: <root>/Music/<Artist>/<Album>/PREFIX - <Title>.flac"""
    folder = output_root / "Music" / sanitize(artist) / sanitize(album)
    prefix = _disc_prefix(disc_number, total_discs, track_number)
    return folder / f"{prefix} - {clean_track_title(track_title, track_number)}.{ext}"


def audiobook_path(
    output_root: Path,
    *,
    author: str,
    book: str,
    disc_number: int,
    total_discs: Optional[int],
    ext: str = "mp3",
) -> Path:
    """One file per disc: <root>/Audiobooks/<Author>/<Book>/<Book>[, Disc NN].mp3"""
    book_c = sanitize(book)
    folder = output_root / "Audiobooks" / sanitize(author) / book_c
    multi = (total_discs is not None and total_discs > 1) or disc_number > 1
    name = f"{book_c}, Disc {disc_number:02d}" if multi else book_c
    return folder / f"{name}.{ext}"


def audiobook_track_path(
    output_root: Path,
    *,
    author: str,
    book: str,
    disc_number: int,
    total_discs: Optional[int],
    track_number: int,
    track_title: str,
    ext: str = "mp3",
) -> Path:
    """One MP3 per track (MusicBrainz split): <root>/Audiobooks/<Author>/<Book>/PREFIX - <Title>.mp3"""
    folder = output_root / "Audiobooks" / sanitize(author) / sanitize(book)
    prefix = _disc_prefix(disc_number, total_discs, track_number)
    return folder / f"{prefix} - {clean_track_title(track_title, track_number)}.{ext}"
