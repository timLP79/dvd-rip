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


def sanitize(name: str) -> str:
    """Make `name` safe as a filesystem component.

    - Drops control chars and characters illegal on Windows/macOS (so the same
      output dir can be served from a SMB share or moved between hosts).
    - Collapses runs of whitespace.
    - Strips leading/trailing dots and spaces (Windows trims those silently).
    - Never returns the empty string; falls back to 'Untitled'.
    """
    cleaned = _FORBIDDEN_RE.sub("", name)
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
