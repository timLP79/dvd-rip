# Audio CD Ripping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add audio-CD ripping to the app — music discs → per-track FLAC, audiobook discs → one joined MP3 per disc — reusing the existing queue/SSE/ledger.

**Architecture:** A new `cd.py` (the only module touching `cdparanoia`/`libdiscid`/`flac`/`lame`) parallels `bluray.py`; a new `musicbrainz.py` (async HTTP metadata client) parallels `tmdb.py`. `/api/scan` gains an audio-CD probe that routes to `cd.scan`; a new `POST /api/cd/rip` enqueues one job per disc. The ripper's single worker dispatches CD jobs to `cd.rip_disc`, which extracts each track with `cdparanoia` and then either encodes per track (music) or joins-and-encodes once (audiobook).

**Tech Stack:** Python 3.12, FastAPI, Pydantic v2, asyncio subprocesses, `discid` (libdiscid binding), `cdparanoia`, `flac`, `lame`, MusicBrainz web service; pytest + respx for tests.

## Global Constraints

- **Source of design truth:** `docs/superpowers/specs/2026-07-04-cd-rip-design.md`. Read it before starting; every task traces to it.
- **Subprocesses:** always argv lists via `asyncio.create_subprocess_exec` — never shell strings (command-injection safety with user-supplied titles).
- **Injectable seams:** every module that touches the drive/tools exposes an injected runner/reader default (mirror `dvd.py` `_run`/`commands`, `bluray.py` `_run`, `bluray.decrypt_title(..., register_proc=...)`), so **no test ever touches a physical CD or requires `cdparanoia`/`flac`/`lame`/`libdiscid` to be installed.**
- **One job at a time:** reuse the existing single-worker `asyncio.Queue`; do not add concurrency.
- **No-partial-garbage rule:** on cancel/fail, remove every file the job wrote plus scratch WAVs (mirror `_cleanup_partial`/`_sweep_scratch`).
- **Audiobook aggregation:** audiobook mode joins to ONE file per disc (never per-track). Music mode stays one file per track. (Field-evaluation finding — Design decisions 8–9.)
- **MusicBrainz:** exact disc-ID lookup ONLY (no fuzzy-TOC, no free-text). Best-effort; a miss yields `[]` and the manual-entry path takes over. No API key; descriptive `User-Agent` required.
- **Frontend:** no build step. Add `<template>`/elements to `index.html`, functions to `app.js`, rules to `style.css`. Existing video UI must stay untouched.
- **Tests:** `pytest` (asyncio_mode=auto). Run from repo root. Fixtures live in `tests/fixtures/`.
- **Commit style:** conventional commits, `feat(cd): …` / `test(cd): …`. End the feature with `bd close dvd-rip-ojo` only after the real-disc verification in the spec passes.

---

### Task 1: Models — CD content types and request/response shapes

**Files:**
- Modify: `app/models.py`
- Test: `tests/test_models_cd.py` (create)

**Interfaces:**
- Consumes: existing `ContentType`, `ScanResult`, `RipRecord`, `Title`.
- Produces (later tasks rely on these EXACT names/types):
  - `ContentType.MUSIC = "music"`, `ContentType.AUDIOBOOK = "audiobook"`
  - `CDTrack(number:int, duration_seconds:int, title:Optional[str]=None)`
  - `MBRelease(artist:str, album:str, year:Optional[int]=None, disc_position:Optional[int]=None, total_discs:Optional[int]=None, tracks:list[CDTrack]=[])`
  - `ScanResult` gains `disc_format: Literal["dvd","bluray","cd"]`, `tracks:list[CDTrack]=[]`, `disc_id:Optional[str]=None`, `musicbrainz:list[MBRelease]=[]`; `titles` becomes `default_factory=list`.
  - `CDTrackSelection(number:int, title:str="", include:bool=True)`
  - `CDRipRequest(mode:Literal["music","audiobook"], disc_id:Optional[str]=None, creator:str, title:str, year:Optional[int]=None, disc_number:int=1, total_discs:Optional[int]=None, tracks:list[CDTrackSelection])`
  - `RipRecord` gains `artist:Optional[str]=None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_models_cd.py`:

```python
from app.models import (
    CDRipRequest, CDTrack, CDTrackSelection, ContentType, MBRelease,
    RipRecord, ScanResult,
)


def test_content_type_has_audio_modes():
    assert ContentType.MUSIC.value == "music"
    assert ContentType.AUDIOBOOK.value == "audiobook"


def test_scan_result_cd_shape_defaults():
    sr = ScanResult(dvd_label="AUDIO_CD", disc_format="cd",
                    tracks=[CDTrack(number=1, duration_seconds=343)],
                    disc_id="abc", musicbrainz=[])
    assert sr.disc_format == "cd"
    assert sr.titles == []           # now optional
    assert sr.tracks[0].number == 1
    assert sr.tracks[0].title is None


def test_scan_result_still_accepts_video():
    sr = ScanResult(dvd_label="X", titles=[])
    assert sr.disc_format == "dvd"


def test_cd_rip_request_defaults():
    req = CDRipRequest(mode="audiobook", creator="Neil Gaiman",
                       title="Fragile Things", disc_number=5, total_discs=9,
                       tracks=[CDTrackSelection(number=1)])
    assert req.tracks[0].include is True
    assert req.tracks[0].title == ""
    assert req.year is None


def test_mbrelease_and_riprecord_artist():
    rel = MBRelease(artist="a", album="b", disc_position=5, total_discs=9,
                    tracks=[CDTrack(number=1, duration_seconds=1, title="t")])
    assert rel.disc_position == 5
    rec = RipRecord(content_type=ContentType.AUDIOBOOK, title="b", artist="a",
                    output_path="/x")
    assert rec.artist == "a"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_models_cd.py -q`
Expected: FAIL — `ImportError`/`AttributeError` (CDTrack etc. not defined).

- [ ] **Step 3: Implement the models**

In `app/models.py`, extend `ContentType`:

```python
class ContentType(str, Enum):
    MOVIE = "movie"
    TV = "tv"
    MUSIC = "music"
    AUDIOBOOK = "audiobook"
```

Add after the `Title` class:

```python
class CDTrack(BaseModel):
    number: int
    duration_seconds: int
    title: Optional[str] = None


class MBRelease(BaseModel):
    """One MusicBrainz release candidate matched by exact disc ID."""
    artist: str
    album: str
    year: Optional[int] = None
    disc_position: Optional[int] = None   # which disc this is (1-based)
    total_discs: Optional[int] = None     # media count of the release
    tracks: list[CDTrack] = Field(default_factory=list)
```

Replace `ScanResult` with:

```python
class ScanResult(BaseModel):
    dvd_label: str
    titles: list[Title] = Field(default_factory=list)
    disc_format: Literal["dvd", "bluray", "cd"] = "dvd"
    # Audio-CD fields (empty for video discs):
    tracks: list[CDTrack] = Field(default_factory=list)
    disc_id: Optional[str] = None
    musicbrainz: list[MBRelease] = Field(default_factory=list)
```

Add near `RipRequest`:

```python
class CDTrackSelection(BaseModel):
    number: int
    title: str = ""
    include: bool = True


class CDRipRequest(BaseModel):
    mode: Literal["music", "audiobook"]
    disc_id: Optional[str] = None
    creator: str                      # Artist (music) / Author (audiobook)
    title: str                        # Album (music) / Book (audiobook)
    year: Optional[int] = None
    disc_number: int = 1
    total_discs: Optional[int] = None
    tracks: list[CDTrackSelection]
```

In `RipRecord`, add after `title`:

```python
    artist: Optional[str] = None               # Artist/Author for music/audiobook rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_models_cd.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the full suite (no regressions from widening ScanResult)**

Run: `.venv/bin/pytest -q`
Expected: PASS (all existing tests still green).

- [ ] **Step 6: Commit**

```bash
git add app/models.py tests/test_models_cd.py
git commit -m "feat(cd): add CD content types, CDTrack/MBRelease/CDRipRequest models"
```

---

### Task 2: Paths — music_path, audiobook_path, sanitize hardening

**Files:**
- Modify: `app/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Consumes: existing `sanitize`, `unique_path`.
- Produces:
  - `sanitize(name)` now normalizes Unicode punctuation (curly quotes, dashes, ellipsis) before stripping.
  - `music_path(output_root, *, artist, album, disc_number, total_discs, track_number, track_title, ext="flac") -> Path`
  - `audiobook_path(output_root, *, author, book, disc_number, total_discs, ext="mp3") -> Path`
  - `clean_track_title(title, number) -> str` (empty/`[untitled]` → `Track NN`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_paths.py`:

```python
from app.paths import audiobook_path, music_path


def test_music_path_single_disc(tmp_path):
    p = music_path(tmp_path, artist="Radiohead", album="OK Computer",
                   disc_number=1, total_discs=None, track_number=2,
                   track_title="Paranoid Android")
    assert p == (tmp_path / "Music" / "Radiohead" / "OK Computer"
                 / "02 - Paranoid Android.flac")


def test_music_path_multi_disc_prefixes(tmp_path):
    p = music_path(tmp_path, artist="The Wall", album="The Wall",
                   disc_number=2, total_discs=2, track_number=3,
                   track_title="Hey You")
    assert p.name == "2-03 - Hey You.flac"


def test_music_path_untitled_falls_back(tmp_path):
    p = music_path(tmp_path, artist="A", album="B", disc_number=1,
                   total_discs=None, track_number=7, track_title="[untitled]")
    assert p.name == "07 - Track 07.flac"
    p2 = music_path(tmp_path, artist="A", album="B", disc_number=1,
                    total_discs=None, track_number=8, track_title="   ")
    assert p2.name == "08 - Track 08.flac"


def test_audiobook_path_multi_disc(tmp_path):
    p = audiobook_path(tmp_path, author="Neil Gaiman", book="Fragile Things",
                       disc_number=5, total_discs=9)
    assert p == (tmp_path / "Audiobooks" / "Neil Gaiman" / "Fragile Things"
                 / "Fragile Things, Disc 05.mp3")


def test_audiobook_path_single_disc(tmp_path):
    p = audiobook_path(tmp_path, author="Author", book="Novella",
                       disc_number=1, total_discs=1)
    assert p.name == "Novella.mp3"


def test_sanitize_normalizes_unicode_punct():
    from app.paths import sanitize
    assert sanitize("It’s a “Test”…") == "It's a Test"   # trailing dots stripped (SMB-safe)
    assert sanitize("Wait… what") == "Wait... what"        # mid-string ellipsis preserved
    assert sanitize("Two—Rooms") == "Two-Rooms"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_paths.py -q`
Expected: FAIL — `ImportError` (music_path/audiobook_path missing).

- [ ] **Step 3: Implement**

In `app/paths.py`, add above `sanitize` (after the two regexes):

```python
# MusicBrainz titles carry typographic punctuation; map to filesystem-safe ASCII
# BEFORE stripping forbidden chars (straight double-quote is itself forbidden).
_UNICODE_MAP = str.maketrans({
    "‘": "'", "’": "'",     # ' ' -> '
    "“": "", "”": "",       # " " -> drop
    "–": "-", "—": "-",      # – — -> -
    "…": "...",                   # … -> ...
})
```

Change `sanitize` body's first line to translate first:

```python
def sanitize(name: str) -> str:
    """... (existing docstring) ..."""
    cleaned = name.translate(_UNICODE_MAP)
    cleaned = _FORBIDDEN_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip().strip(".")
    return cleaned or "Untitled"
```

Add at the end of the file:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_paths.py -q`
Expected: PASS (existing + 6 new).

- [ ] **Step 5: Commit**

```bash
git add app/paths.py tests/test_paths.py
git commit -m "feat(cd): add music_path/audiobook_path and Unicode-punct sanitization"
```

---

### Task 3: MusicBrainz client (exact disc-ID lookup)

**Files:**
- Create: `app/musicbrainz.py`
- Create: `tests/test_musicbrainz.py`
- Create: `tests/fixtures/musicbrainz_multi_disc.json`, `tests/fixtures/musicbrainz_single.json`

**Interfaces:**
- Consumes: `MBRelease`, `CDTrack` (Task 1).
- Produces:
  - `MusicBrainzClient(user_agent:Optional[str]=None, http_client:Optional[httpx.AsyncClient]=None)`
  - `MusicBrainzClient.lookup_disc(disc_id:str) -> list[MBRelease]`
  - `MusicBrainzClient.aclose() -> None`
  - Only ever GETs `https://musicbrainz.org/ws/2/discid/{disc_id}` (never the fuzzy `/discid/-` endpoint).

- [ ] **Step 1: Write the fixtures**

Create `tests/fixtures/musicbrainz_multi_disc.json` (a release whose disc matches medium 5 of 9):

```json
{
  "id": "DISCID5OF9",
  "releases": [
    {
      "id": "rel-1",
      "title": "Fragile Things: Short Fictions and Wonders",
      "date": "2006",
      "artist-credit": [{"name": "Neil Gaiman", "joinphrase": "",
                          "artist": {"name": "Neil Gaiman"}}],
      "media": [
        {"position": 1, "track-count": 8, "discs": [{"id": "OTHERDISC1"}], "tracks": []},
        {"position": 2, "track-count": 9, "discs": [{"id": "OTHERDISC2"}], "tracks": []},
        {"position": 3, "track-count": 8, "discs": [{"id": "OTHERDISC3"}], "tracks": []},
        {"position": 4, "track-count": 7, "discs": [{"id": "OTHERDISC4"}], "tracks": []},
        {"position": 5, "track-count": 3, "discs": [{"id": "DISCID5OF9"}],
         "tracks": [
           {"number": "1", "title": "The Fourth Room", "length": 343000},
           {"number": "2", "title": "Strange Little Girls", "length": 565000},
           {"number": "3", "title": "Locks", "length": 234000}
         ]},
        {"position": 6, "track-count": 9, "discs": [{"id": "OTHERDISC6"}], "tracks": []},
        {"position": 7, "track-count": 7, "discs": [{"id": "OTHERDISC7"}], "tracks": []},
        {"position": 8, "track-count": 8, "discs": [{"id": "OTHERDISC8"}], "tracks": []},
        {"position": 9, "track-count": 10, "discs": [{"id": "OTHERDISC9"}], "tracks": []}
      ]
    }
  ]
}
```

Create `tests/fixtures/musicbrainz_single.json`:

```json
{
  "id": "SINGLEDISC",
  "releases": [
    {
      "id": "rel-2",
      "title": "OK Computer",
      "date": "1997-05-21",
      "artist-credit": [{"name": "Radiohead", "joinphrase": "",
                          "artist": {"name": "Radiohead"}}],
      "media": [
        {"position": 1, "track-count": 2, "discs": [{"id": "SINGLEDISC"}],
         "tracks": [
           {"number": "1", "title": "Airbag", "length": 284000},
           {"number": "2", "title": "Paranoid Android", "length": 383000}
         ]}
      ]
    }
  ]
}
```

- [ ] **Step 2: Write the failing test**

Create `tests/test_musicbrainz.py`:

```python
import json
from pathlib import Path

import httpx
import pytest
import respx

from app.musicbrainz import MusicBrainzClient

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / name).read_text())


@pytest.mark.asyncio
@respx.mock
async def test_lookup_multi_disc_extracts_position_and_tracks():
    route = respx.get("https://musicbrainz.org/ws/2/discid/DISCID5OF9").mock(
        return_value=httpx.Response(200, json=_load("musicbrainz_multi_disc.json")))
    client = MusicBrainzClient(user_agent="test/1.0")
    releases = await client.lookup_disc("DISCID5OF9")
    await client.aclose()

    assert route.called
    assert len(releases) == 1
    r = releases[0]
    assert r.artist == "Neil Gaiman"
    assert r.album == "Fragile Things: Short Fictions and Wonders"
    assert r.year == 2006
    assert r.disc_position == 5
    assert r.total_discs == 9
    assert [t.number for t in r.tracks] == [1, 2, 3]
    assert r.tracks[0].title == "The Fourth Room"
    assert r.tracks[0].duration_seconds == 343  # 343000 ms -> s


@pytest.mark.asyncio
@respx.mock
async def test_lookup_single_disc():
    respx.get("https://musicbrainz.org/ws/2/discid/SINGLEDISC").mock(
        return_value=httpx.Response(200, json=_load("musicbrainz_single.json")))
    client = MusicBrainzClient(user_agent="test/1.0")
    releases = await client.lookup_disc("SINGLEDISC")
    await client.aclose()
    assert releases[0].year == 1997
    assert releases[0].disc_position == 1
    assert releases[0].total_discs == 1


@pytest.mark.asyncio
@respx.mock
async def test_lookup_miss_returns_empty():
    respx.get("https://musicbrainz.org/ws/2/discid/NOPE").mock(
        return_value=httpx.Response(404, json={"error": "Not Found"}))
    client = MusicBrainzClient(user_agent="test/1.0")
    assert await client.lookup_disc("NOPE") == []
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_lookup_http_error_returns_empty():
    respx.get("https://musicbrainz.org/ws/2/discid/BOOM").mock(
        return_value=httpx.Response(503))
    client = MusicBrainzClient(user_agent="test/1.0")
    assert await client.lookup_disc("BOOM") == []
    await client.aclose()
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_musicbrainz.py -q`
Expected: FAIL — `ModuleNotFoundError: app.musicbrainz`.

- [ ] **Step 4: Implement `app/musicbrainz.py`**

```python
"""Thin async MusicBrainz client — exact disc-ID lookup only.

Parallels tmdb.py. No API key; MusicBrainz only requires a descriptive
User-Agent. Best-effort: any failure (network, 404, malformed) yields [].

Deliberately NOT implemented: fuzzy-TOC (`/discid/-?toc=`) and free-text
search. The fuzzy-TOC endpoint matches on track count and returns unrelated
discs (bird-song guides, jingle libraries) — worse than nothing. See the
design doc's "Field evaluation".
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from .models import CDTrack, MBRelease

log = logging.getLogger(__name__)

_BASE_URL = "https://musicbrainz.org/ws/2/discid"
_TIMEOUT = httpx.Timeout(8.0, connect=5.0)
_DEFAULT_UA = os.getenv(
    "MUSICBRAINZ_USER_AGENT",
    "dvd-rip/1.0 (https://github.com/timLP79/dvd-rip)",
)


def _year(date_str: Optional[str]) -> Optional[int]:
    if not date_str or len(date_str) < 4:
        return None
    try:
        return int(date_str[:4])
    except ValueError:
        return None


def _artist_name(artist_credit: list) -> str:
    return "".join(
        (ac.get("name") or (ac.get("artist") or {}).get("name") or "")
        + ac.get("joinphrase", "")
        for ac in (artist_credit or [])
    ).strip() or "Unknown Artist"


def _release_to_mbrelease(disc_id: str, release: dict) -> MBRelease:
    media = release.get("media") or []
    # The medium whose disc list contains our exact disc id.
    matched = next(
        (m for m in media
         if any(d.get("id") == disc_id for d in (m.get("discs") or []))),
        media[0] if media else {},
    )
    tracks = [
        CDTrack(
            number=int(t.get("number") or i + 1),
            duration_seconds=int((t.get("length") or 0) // 1000),
            title=t.get("title") or None,
        )
        for i, t in enumerate(matched.get("tracks") or [])
    ]
    return MBRelease(
        artist=_artist_name(release.get("artist-credit")),
        album=release.get("title") or "Unknown Album",
        year=_year(release.get("date")),
        disc_position=matched.get("position"),
        total_discs=len(media) or None,
        tracks=tracks,
    )


class MusicBrainzClient:
    def __init__(self, user_agent: Optional[str] = None,
                 http_client: Optional[httpx.AsyncClient] = None):
        self.user_agent = user_agent or _DEFAULT_UA
        self._client = http_client or httpx.AsyncClient(
            timeout=_TIMEOUT, headers={"User-Agent": self.user_agent})
        self._owns_client = http_client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def lookup_disc(self, disc_id: str) -> list[MBRelease]:
        if not disc_id:
            return []
        try:
            resp = await self._client.get(
                f"{_BASE_URL}/{disc_id}",
                params={"inc": "recordings+artist-credits", "fmt": "json"},
                headers={"User-Agent": self.user_agent},
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.info("MusicBrainz disc lookup failed for %s: %s", disc_id, exc)
            return []
        return [_release_to_mbrelease(disc_id, r) for r in (data.get("releases") or [])]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_musicbrainz.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add app/musicbrainz.py tests/test_musicbrainz.py tests/fixtures/musicbrainz_*.json
git commit -m "feat(cd): add MusicBrainz exact disc-ID client"
```

---

### Task 4: cd.py — detection + TOC scan

**Files:**
- Create: `app/cd.py`
- Create: `tests/test_cd.py`

**Interfaces:**
- Consumes: `CDTrack`, `ScanResult`, `MBRelease` (Task 1); `MusicBrainzClient` (Task 3).
- Produces:
  - `CDDiscInfo(disc_id:str, tracks:list[CDTrack])` (dataclass)
  - `_read_disc(device:str) -> CDDiscInfo` (lazily imports `discid`)
  - `is_audio_cd(device, *, read_disc=_read_disc) -> bool`
  - `scan(device, *, read_disc=_read_disc, mb_client:Optional[MusicBrainzClient]=None) -> ScanResult`

- [ ] **Step 1: Write the failing test**

Create `tests/test_cd.py`:

```python
import pytest

from app import cd
from app.cd import CDDiscInfo
from app.models import CDTrack, MBRelease


# Two real TOCs captured from physical discs (durations in seconds):
GAIMAN_D5 = CDDiscInfo(disc_id="mxai5MH2MHHH.p_sfzF29lYkvsk-", tracks=[
    CDTrack(number=n, duration_seconds=d) for n, d in
    [(1, 343), (2, 570), (3, 565), (4, 615), (5, 662), (6, 234), (7, 580), (8, 596)]
])
NOVEL_96 = CDDiscInfo(disc_id="ZYqFwanoTkVj5j7gAf.q9TxDNww-",
                      tracks=[CDTrack(number=n, duration_seconds=45) for n in range(1, 97)])


@pytest.mark.asyncio
async def test_is_audio_cd_true_when_read_succeeds():
    assert await cd.is_audio_cd("/dev/sr0", read_disc=lambda dev: GAIMAN_D5) is True


@pytest.mark.asyncio
async def test_is_audio_cd_false_on_read_error():
    def boom(dev):
        raise RuntimeError("DiscError: not an audio disc")
    assert await cd.is_audio_cd("/dev/sr0", read_disc=boom) is False


@pytest.mark.asyncio
async def test_scan_builds_toc_without_musicbrainz():
    sr = await cd.scan("/dev/sr0", read_disc=lambda dev: NOVEL_96, mb_client=None)
    assert sr.disc_format == "cd"
    assert sr.disc_id == "ZYqFwanoTkVj5j7gAf.q9TxDNww-"
    assert len(sr.tracks) == 96
    assert sr.titles == []
    assert sr.musicbrainz == []


@pytest.mark.asyncio
async def test_scan_attaches_musicbrainz_when_hit():
    class FakeMB:
        async def lookup_disc(self, disc_id):
            assert disc_id == GAIMAN_D5.disc_id
            return [MBRelease(artist="Neil Gaiman", album="Fragile Things",
                              disc_position=5, total_discs=9,
                              tracks=[CDTrack(number=1, duration_seconds=343, title="The Fourth Room")])]
    sr = await cd.scan("/dev/sr0", read_disc=lambda dev: GAIMAN_D5, mb_client=FakeMB())
    assert len(sr.musicbrainz) == 1
    assert sr.musicbrainz[0].disc_position == 5


@pytest.mark.asyncio
async def test_scan_survives_musicbrainz_failure():
    class BrokenMB:
        async def lookup_disc(self, disc_id):
            raise RuntimeError("network down")
    sr = await cd.scan("/dev/sr0", read_disc=lambda dev: GAIMAN_D5, mb_client=BrokenMB())
    assert sr.musicbrainz == []          # TOC still returned
    assert len(sr.tracks) == 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cd.py -q`
Expected: FAIL — `ModuleNotFoundError: app.cd`.

- [ ] **Step 3: Implement the detection/scan portion of `app/cd.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cd.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add app/cd.py tests/test_cd.py
git commit -m "feat(cd): add audio-CD detection and TOC scan"
```

---

### Task 5: cd.py — encoder arg builders, WAV join, and rip_disc

**Files:**
- Modify: `app/cd.py`
- Test: `tests/test_cd.py`

**Interfaces:**
- Consumes: `CDDiscInfo`, `RipError`, `music_path`, `audiobook_path`, `unique_path`, `clean_track_title`.
- Produces:
  - `flac_args(wav, out_path, *, artist, album, title, track_number, track_total, date=None, disc_number=None) -> list[str]`
  - `lame_args(wav, out_path, *, artist, title, album, disc_number, total_discs, year=None) -> list[str]`
  - `_concat_wavs(wav_paths, out_path) -> Path`
  - `_spawn(cmd, *, register_proc=None) -> tuple[int, str]`
  - `rip_disc(device, *, mode, output_root, creator, title, year, disc_number, total_discs, tracks, scratch_dir, on_progress=None, on_stage=None, register_proc=None, spawn=_spawn, join_wavs=_concat_wavs) -> list[Path]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_cd.py`:

```python
import wave
from pathlib import Path

from app.cd import flac_args, lame_args, rip_disc
from app.models import CDTrackSelection


def test_flac_args_carry_tags(tmp_path):
    args = flac_args(tmp_path / "in.wav", tmp_path / "out.flac", artist="Radiohead",
                     album="OK Computer", title="Airbag", track_number=1,
                     track_total=12, date=1997)
    assert args[0] == "flac"
    assert "--tag=ARTIST=Radiohead" in args
    assert "--tag=ALBUMARTIST=Radiohead" in args
    assert "--tag=TITLE=Airbag" in args
    assert "--tag=TRACKNUMBER=1" in args
    assert "--tag=TRACKTOTAL=12" in args
    assert "--tag=DATE=1997" in args
    assert args[-1] == str(tmp_path / "in.wav")


def test_lame_args_carry_disc_tags(tmp_path):
    args = lame_args(tmp_path / "j.wav", tmp_path / "out.mp3", artist="Neil Gaiman",
                     title="Fragile Things", album="Fragile Things",
                     disc_number=5, total_discs=9, year=2006)
    assert args[0] == "lame"
    assert "--ta" in args and "Neil Gaiman" in args
    assert "--tl" in args and "Fragile Things" in args
    i = args.index("--tv")
    assert args[i + 1] == "TPOS=5/9"
    assert args[-2:] == [str(tmp_path / "j.wav"), str(tmp_path / "out.mp3")]


def _fake_wav(path, seconds=1):
    """A minimal valid CDDA-format WAV so _concat_wavs (real) can join them."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * (44100 * seconds))


def _out_path(cmd):
    """The file a fake tool 'produces': cdparanoia/lame write the LAST arg;
    flac writes the arg after -o (its last arg is the INPUT wav)."""
    if cmd[0] == "flac":
        return cmd[cmd.index("-o") + 1]
    return cmd[-1]


def _spawn_factory(recorder):
    """Fake spawn: records the argv, fabricates the file the tool would write."""
    async def fake_spawn(cmd, *, register_proc=None):
        recorder.append(cmd)
        if cmd[0] == "cdparanoia":
            _fake_wav(Path(_out_path(cmd)))     # valid WAV so the real join works
        else:                                    # flac/lame -> create the output file
            Path(_out_path(cmd)).write_bytes(b"encoded")
        return 0, ""
    return fake_spawn


@pytest.mark.asyncio
async def test_rip_disc_music_writes_one_file_per_track(tmp_path):
    cmds = []
    tracks = [CDTrackSelection(number=1, title="Airbag"),
              CDTrackSelection(number=2, title="Paranoid Android"),
              CDTrackSelection(number=3, title="", include=False)]
    written = await rip_disc(
        "/dev/sr0", mode="music", output_root=tmp_path, creator="Radiohead",
        title="OK Computer", year=1997, disc_number=1, total_discs=None,
        tracks=tracks, scratch_dir=str(tmp_path / "scratch"),
        spawn=_spawn_factory(cmds))
    assert len(written) == 2                                   # track 3 excluded
    assert (tmp_path / "Music" / "Radiohead" / "OK Computer" / "01 - Airbag.flac").exists()
    assert not list((tmp_path / "scratch").glob("*.wav"))      # wavs cleaned up
    assert [c[0] for c in cmds] == ["cdparanoia", "flac", "cdparanoia", "flac"]


@pytest.mark.asyncio
async def test_rip_disc_audiobook_joins_to_one_file(tmp_path):
    cmds = []
    tracks = [CDTrackSelection(number=n) for n in range(1, 5)]
    progress = []
    written = await rip_disc(
        "/dev/sr0", mode="audiobook", output_root=tmp_path, creator="Neil Gaiman",
        title="Fragile Things", year=2006, disc_number=5, total_discs=9,
        tracks=tracks, scratch_dir=str(tmp_path / "scratch"),
        on_progress=progress.append, spawn=_spawn_factory(cmds))
    assert len(written) == 1
    assert written[0].name == "Fragile Things, Disc 05.mp3"
    assert written[0].exists()
    # 4 cdparanoia extracts + exactly one lame encode
    assert [c[0] for c in cmds].count("cdparanoia") == 4
    assert [c[0] for c in cmds].count("lame") == 1
    assert progress[-1] == 100
    assert not list((tmp_path / "scratch").glob("*.wav"))


@pytest.mark.asyncio
async def test_rip_disc_removes_written_files_on_failure(tmp_path):
    calls = {"n": 0}
    async def flaky_spawn(cmd, *, register_proc=None):
        if cmd[0] == "cdparanoia":
            _fake_wav(Path(_out_path(cmd)))
            return 0, ""
        calls["n"] += 1
        if calls["n"] == 2:                      # second flac encode fails
            return 1, "flac: disk full"
        Path(_out_path(cmd)).write_bytes(b"x")   # first flac writes its real output
        return 0, ""
    tracks = [CDTrackSelection(number=1, title="A"), CDTrackSelection(number=2, title="B")]
    with pytest.raises(Exception):
        await rip_disc("/dev/sr0", mode="music", output_root=tmp_path,
                       creator="Art", title="Alb", year=None, disc_number=1,
                       total_discs=None, tracks=tracks,
                       scratch_dir=str(tmp_path / "scratch"), spawn=flaky_spawn)
    # First (successful) track file was rolled back — no partial garbage.
    assert not list((tmp_path / "Music").rglob("*.flac"))
    # Scratch WAVs are swept even on the failure path (both modes).
    assert not list((tmp_path / "scratch").glob("*.wav"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cd.py -q`
Expected: FAIL — `ImportError` (flac_args/lame_args/rip_disc missing).

- [ ] **Step 3: Implement the rip portion in `app/cd.py`**

Add the spoken-word constant near the top (after `log = ...`):

```python
_LAME_VBR_QUALITY = "6"   # ~115 kbps VBR — ample for spoken word
```

Append to `app/cd.py`:

```python
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
    on_progress: Optional[Callable[[float], None]] = None,
    on_stage: Optional[Callable[[str], None]] = None,
    register_proc: Optional[Callable] = None,
    spawn: Callable[..., Awaitable[tuple[int, str]]] = _spawn,
    join_wavs: Callable = _concat_wavs,
) -> list[Path]:
    """Rip the included tracks. Music: one FLAC/track. Audiobook: one joined MP3.

    Returns the files written. On any failure removes everything this call wrote
    (no-partial-garbage rule) and re-raises RipError. Scratch WAVs are always swept.
    """
    included = [t for t in tracks if t.include]
    total = len(included)
    scratch = Path(scratch_dir)
    scratch.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    wavs: list[Path] = []
    joined = scratch / "joined.wav"
    try:
        for i, t in enumerate(included):
            if on_progress:
                on_progress(round(i / total * (100 if mode == "music" else 90), 1))
            wav = scratch / f"{t.number:02d}.wav"
            if on_stage:
                label = (f"Ripping track {t.number}: {t.title}"
                         if mode == "music" and t.title
                         else f"Ripping track {t.number} of {total}")
                on_stage(label)
            rc, out = await spawn(
                ["cdparanoia", "-d", device, str(t.number), str(wav)],
                register_proc=register_proc)
            if rc != 0:
                raise RipError(f"cdparanoia failed on track {t.number} (rc={rc})\n{out}".strip())
            wavs.append(wav)   # track now so the finally-sweep cleans it in BOTH modes,
                               # even if the flac encode below raises mid-track

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
                written.append(out_path)
                wav.unlink(missing_ok=True)   # immediate; finally-sweep is idempotent (missing_ok)

        if mode == "audiobook":
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cd.py -q`
Expected: PASS (5 scan + 5 rip tests).

- [ ] **Step 5: Commit**

```bash
git add app/cd.py tests/test_cd.py
git commit -m "feat(cd): add rip_disc (music per-track / audiobook join) + encoders"
```

---

### Task 6: Library — persist the `artist` column

**Files:**
- Modify: `app/library.py`
- Test: `tests/test_library.py`

**Interfaces:**
- Consumes: `RipRecord.artist` (Task 1).
- Produces: `rips` table gains an `artist TEXT` column (additive migration); `record_rip`/`_row_to_record` round-trip it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_library.py` (match its existing import style):

```python
def test_record_and_read_artist(tmp_path):
    from app.library import Library
    from app.models import ContentType, JobStatus, RipRecord
    lib = Library(str(tmp_path / "lib.db"))
    lib.init_db()
    lib.record_rip(RipRecord(
        content_type=ContentType.AUDIOBOOK, title="Fragile Things",
        artist="Neil Gaiman", output_path=str(tmp_path / "book"),
        status=JobStatus.DONE))
    rows = lib.list_rips()
    assert rows[0].artist == "Neil Gaiman"
    assert rows[0].content_type == ContentType.AUDIOBOOK


def test_artist_column_added_to_legacy_db(tmp_path):
    """A pre-artist DB upgrades in place without data loss."""
    import sqlite3
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE rips (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,"
        " content_type TEXT NOT NULL, title TEXT NOT NULL, year INTEGER, season INTEGER,"
        " episode INTEGER, tmdb_id INTEGER, output_path TEXT NOT NULL, encoder_mode TEXT,"
        " burn_forced INTEGER NOT NULL DEFAULT 1, file_size_bytes INTEGER, rip_seconds INTEGER,"
        " status TEXT NOT NULL, error TEXT, source TEXT NOT NULL DEFAULT 'rip',"
        " disc_format TEXT NOT NULL DEFAULT 'dvd', aspect TEXT);")
    conn.execute("INSERT INTO rips (created_at, content_type, title, output_path, status)"
                 " VALUES ('2020-01-01','movie','Old Movie','/x','done')")
    conn.commit(); conn.close()

    from app.library import Library
    lib = Library(db)
    lib.init_db()                       # must ALTER TABLE ADD COLUMN artist
    rows = lib.list_rips()
    assert rows[0].title == "Old Movie"
    assert rows[0].artist is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_library.py -q`
Expected: FAIL — `sqlite3.OperationalError: table rips has no column named artist` (record_rip INSERT) / `KeyError: 'artist'` (_row_to_record).

- [ ] **Step 3: Implement**

In `app/library.py` `_SCHEMA`, add `artist TEXT` to the `rips` table (after `aspect TEXT`):

```python
    disc_format TEXT NOT NULL DEFAULT 'dvd',
    aspect TEXT,
    artist TEXT
);
```

In `init_db`, add another migration guard alongside the existing ones:

```python
            if "artist" not in cols:
                conn.execute("ALTER TABLE rips ADD COLUMN artist TEXT")
```

In `_row_to_record`, add:

```python
        artist=row["artist"],
```

In `record_rip`, add `artist` to the column list, the `VALUES` placeholders, and the params tuple:

```python
                """INSERT INTO rips (created_at, content_type, title, year, season,
                       episode, tmdb_id, output_path, encoder_mode, burn_forced,
                       file_size_bytes, rip_seconds, status, error, source, disc_format,
                       aspect, artist)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.created_at or _now_iso(),
                    ct, record.title, record.year, record.season, record.episode,
                    record.tmdb_id, record.output_path, enc,
                    1 if record.burn_forced else 0,
                    record.file_size_bytes, record.rip_seconds, status,
                    record.error, record.source, record.disc_format,
                    record.aspect, record.artist,
                ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_library.py -q`
Expected: PASS (existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add app/library.py tests/test_library.py
git commit -m "feat(cd): persist artist column with additive migration"
```

---

### Task 7: Ripper — enqueue_cd and _run_cd_job

**Files:**
- Modify: `app/ripper.py`
- Test: `tests/test_ripper.py`

**Interfaces:**
- Consumes: `cd.rip_disc` (Task 5); `CDRipRequest`, `ContentType.MUSIC/AUDIOBOOK`, `RipRecord.artist` (Task 1); `audiobook_path`, `sanitize` (Task 2).
- Produces:
  - `Ripper.enqueue_cd(*, request: CDRipRequest) -> Job`
  - `Ripper._run_cd_job(job_id) -> None`
  - `_run_job` dispatches CD jobs before the video path.
  - `_save_record` no longer overwrites a pre-set `file_size_bytes`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_ripper.py`:

```python
def test_enqueue_cd_seeds_record_and_job(tmp_path):
    from app.library import Library
    from app.models import CDRipRequest, CDTrackSelection, ContentType
    from app.ripper import Ripper
    lib = Library(str(tmp_path / "lib.db")); lib.init_db()
    r = Ripper(device="/dev/sr0", output_root=tmp_path, library=lib)
    job = r.enqueue_cd(request=CDRipRequest(
        mode="audiobook", creator="Neil Gaiman", title="Fragile Things",
        disc_number=5, total_discs=9, tracks=[CDTrackSelection(number=1)]))
    assert job.id in r._cd_requests
    rec = r._records[job.id]
    assert rec.content_type == ContentType.AUDIOBOOK
    assert rec.artist == "Neil Gaiman"
    assert rec.disc_format == "cd"
    assert "Fragile Things" in job.output_path


def test_run_cd_job_calls_rip_disc_and_records(tmp_path, monkeypatch):
    import asyncio
    from app import cd
    from app.library import Library
    from app.models import CDRipRequest, CDTrackSelection, JobStatus
    from app.ripper import Ripper

    async def fake_rip_disc(device, **kw):
        out = tmp_path / "Audiobooks" / "Neil Gaiman" / "Fragile Things" / "Fragile Things, Disc 05.mp3"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 99)
        if kw.get("on_progress"):
            kw["on_progress"](100)
        return [out]

    monkeypatch.setattr(cd, "rip_disc", fake_rip_disc)
    lib = Library(str(tmp_path / "lib.db")); lib.init_db()
    r = Ripper(device="/dev/sr0", output_root=tmp_path, library=lib,
               scratch_dir=str(tmp_path / "scratch"))
    job = r.enqueue_cd(request=CDRipRequest(
        mode="audiobook", creator="Neil Gaiman", title="Fragile Things",
        disc_number=5, total_discs=9, tracks=[CDTrackSelection(number=1)]))
    asyncio.run(r._run_job(job.id))
    assert r.get(job.id).status == JobStatus.DONE
    rows = lib.list_rips()
    assert rows[0].artist == "Neil Gaiman"
    assert rows[0].file_size_bytes == 99


def test_run_cd_job_failure_marks_failed_and_cleans(tmp_path, monkeypatch):
    import asyncio
    from app import cd
    from app.library import Library
    from app.models import CDRipRequest, CDTrackSelection, JobStatus
    from app.ripper import Ripper

    async def boom_rip(device, **kw):
        raise cd.RipError("cdparanoia failed on track 3\nscratched disc")

    monkeypatch.setattr(cd, "rip_disc", boom_rip)
    lib = Library(str(tmp_path / "lib.db")); lib.init_db()
    r = Ripper(device="/dev/sr0", output_root=tmp_path, library=lib,
               scratch_dir=str(tmp_path / "scratch"))
    job = r.enqueue_cd(request=CDRipRequest(
        mode="music", creator="Art", title="Alb",
        tracks=[CDTrackSelection(number=1)]))
    asyncio.run(r._run_job(job.id))
    assert r.get(job.id).status == JobStatus.FAILED
    assert "scratched disc" in (r.get(job.id).error or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_ripper.py -q`
Expected: FAIL — `AttributeError: 'Ripper' object has no attribute 'enqueue_cd'`.

- [ ] **Step 3: Implement**

In `app/ripper.py` imports, extend the models import and add `cd`/paths:

```python
from .models import (
    ContentType,
    EncoderMode,
    Job,
    JobStatus,
    MovieMetadata,
    RipRecord,
    TitleSelection,
    TVMetadata,
    CDRipRequest,
)
from .paths import audiobook_path, movie_path, sanitize, tv_episode_path, unique_path
from . import bluray, cd
```

In `__init__`, add the CD request store (after `self._records = {}`):

```python
        self._cd_requests: dict[str, CDRipRequest] = {}
```

Add `enqueue_cd` after `enqueue`:

```python
    def enqueue_cd(self, *, request: CDRipRequest) -> Job:
        # Display path = the album/book folder (one disc = one queue job).
        if request.mode == "audiobook":
            folder = audiobook_path(
                self.output_root, author=request.creator, book=request.title,
                disc_number=request.disc_number, total_discs=request.total_discs).parent
        else:
            folder = self.output_root / "Music" / sanitize(request.creator) / sanitize(request.title)
        job_id = secrets.token_hex(6)
        job = Job(id=job_id, title_index=request.disc_number,
                  output_path=str(folder), status=JobStatus.QUEUED)
        self._jobs[job_id] = job
        self._job_order.append(job_id)
        self._cd_requests[job_id] = request
        self._subscribers[job_id] = []
        self._records[job_id] = RipRecord(
            content_type=ContentType(request.mode),
            title=request.title,
            artist=request.creator,
            year=request.year,
            output_path=str(folder),
            disc_format="cd",
            status=JobStatus.QUEUED,
        )
        self._queue.put_nowait(job_id)
        self._trim_finished()
        return job
```

In `_run_job`, dispatch CD jobs first:

```python
    async def _run_job(self, job_id: str) -> None:
        if job_id in self._cd_requests:
            await self._run_cd_job(job_id)
            return
        selection, encoder_mode, output_path, burn_forced, disc_format = self._selections[job_id]
        started = time.monotonic()
        ...
```

Add `_run_cd_job` after `_run_bluray_job`:

```python
    async def _run_cd_job(self, job_id: str) -> None:
        request = self._cd_requests[job_id]
        started = time.monotonic()
        self._update(job_id, status=JobStatus.RUNNING, phase="Starting", percent=0.0)
        self._current_job_id = job_id
        try:
            written = await cd.rip_disc(
                self.device, mode=request.mode, output_root=self.output_root,
                creator=request.creator, title=request.title, year=request.year,
                disc_number=request.disc_number, total_discs=request.total_discs,
                tracks=request.tracks, scratch_dir=self.scratch_dir,
                on_progress=lambda p: self._update(job_id, percent=p),
                on_stage=lambda s: self._update(job_id, phase=s),
                register_proc=lambda pr: setattr(self, "_current_process", pr),
            )
        except Exception as exc:
            self._current_process = None
            self._current_job_id = None
            _sweep_scratch_wavs(self.scratch_dir)
            if job_id in self._cancelling or self._jobs[job_id].status == JobStatus.CANCELLED:
                self._update(job_id, status=JobStatus.CANCELLED, phase=None)
                self._save_record(job_id, JobStatus.CANCELLED)
            else:
                msg = str(exc)
                self._update(job_id, status=JobStatus.FAILED, phase=None, error=msg)
                self._save_record(job_id, JobStatus.FAILED, error=msg)
            return
        finally:
            self._cancelling.discard(job_id)
        self._current_process = None
        self._current_job_id = None
        elapsed = int(time.monotonic() - started)
        record = self._records.get(job_id)
        if record is not None:
            try:
                record.file_size_bytes = sum(p.stat().st_size for p in written)
            except OSError:
                record.file_size_bytes = None
        self._update(job_id, status=JobStatus.DONE, phase=None, percent=100.0)
        self._save_record(job_id, JobStatus.DONE, rip_seconds=elapsed)
```

Guard `_save_record` from clobbering the size set above — change its DONE block:

```python
        if status is JobStatus.DONE and record.file_size_bytes is None:
            try:
                record.file_size_bytes = os.path.getsize(record.output_path)
            except OSError:
                record.file_size_bytes = None
```

Add `_cd_requests` cleanup to `_trim_finished` (alongside the other `.pop` calls):

```python
            self._cd_requests.pop(jid, None)
```

Add the scratch-WAV sweeper near `_sweep_scratch`:

```python
def _sweep_scratch_wavs(scratch_dir: str) -> None:
    """Remove leftover .wav files from a failed/cancelled CD rip."""
    try:
        for wav in Path(scratch_dir).glob("*.wav"):
            wav.unlink(missing_ok=True)
    except OSError:
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ripper.py -q`
Expected: PASS (existing + 3 new).

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/ripper.py tests/test_ripper.py
git commit -m "feat(cd): enqueue_cd + _run_cd_job worker dispatch"
```

---

### Task 8: API — scan routing + POST /api/cd/rip + wiring

**Files:**
- Modify: `app/main.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `cd.is_audio_cd`, `cd.scan` (Task 4); `Ripper.enqueue_cd` (Task 7); `MusicBrainzClient` (Task 3); `CDRipRequest` (Task 1).
- Produces:
  - `/api/scan` probes audio-CD FIRST, then DVD, then Blu-ray.
  - `POST /api/cd/rip` (body `CDRipRequest`) → `{"job_ids":[...]}`.
  - `app.state.musicbrainz` created/closed in `lifespan`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api.py`:

```python
def test_scan_routes_audio_cd_first(client):
    from app.models import CDTrack, ScanResult
    fake = ScanResult(dvd_label="AUDIO_CD", disc_format="cd",
                      tracks=[CDTrack(number=1, duration_seconds=343)],
                      disc_id="abc", musicbrainz=[])

    async def fake_is_audio(device, **kw):
        return True

    async def fake_cd_scan(device, **kw):
        return fake

    with patch("app.cd.is_audio_cd", side_effect=fake_is_audio), \
         patch("app.cd.scan", side_effect=fake_cd_scan):
        res = client.get("/api/scan")
    assert res.status_code == 200
    body = res.json()
    assert body["disc_format"] == "cd"
    assert body["tracks"][0]["number"] == 1
    assert body["disc_id"] == "abc"


def test_cd_rip_enqueues(client, monkeypatch):
    from app.models import Job, JobStatus
    captured = {}

    def stub(*, request):
        captured["req"] = request
        return Job(id="cdjob1", title_index=request.disc_number,
                   output_path="/x", status=JobStatus.QUEUED)

    monkeypatch.setattr(client.app.state.ripper, "enqueue_cd", stub)
    res = client.post("/api/cd/rip", json={
        "mode": "audiobook", "creator": "Neil Gaiman", "title": "Fragile Things",
        "disc_number": 5, "total_discs": 9,
        "tracks": [{"number": 1, "title": "", "include": True}],
    })
    assert res.status_code == 200
    assert res.json()["job_ids"] == ["cdjob1"]
    assert captured["req"].creator == "Neil Gaiman"


def test_cd_rip_rejects_no_included_tracks(client):
    res = client.post("/api/cd/rip", json={
        "mode": "music", "creator": "A", "title": "B",
        "tracks": [{"number": 1, "include": False}],
    })
    assert res.status_code == 400


def test_cd_rip_rejects_blank_metadata(client):
    res = client.post("/api/cd/rip", json={
        "mode": "music", "creator": "   ", "title": "B",
        "tracks": [{"number": 1, "include": True}],
    })
    assert res.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_api.py -q`
Expected: FAIL — 404 on `/api/cd/rip`; scan test fails because audio-CD routing not present.

- [ ] **Step 3: Implement**

In `app/main.py` imports, add:

```python
from . import cd
from .musicbrainz import MusicBrainzClient
from .models import (
    ContentType,
    CDRipRequest,
    Job,
    PushSubscription,
    PushUnsubscribe,
    RipRecord,
    RipRequest,
    ScanResult,
    TMDBEpisode,
    TMDBMovieResult,
    TMDBTVResult,
)
```

In `lifespan`, create the client and store it (after `tmdb = TMDBClient()`):

```python
    musicbrainz = MusicBrainzClient()
    ...
    app.state.musicbrainz = musicbrainz
```

and close it in the `finally` (after `await tmdb.aclose()`):

```python
        await musicbrainz.aclose()
```

In `api_scan`, add the audio-CD probe as the first route (right after the `is_busy()` guard):

```python
    # Audio CD is the cheapest, most specific probe (libdiscid, ~1s). A data
    # DVD/BD makes discid.read raise, so this falls through to the video paths.
    if await cd.is_audio_cd(DVD_DEVICE):
        return await cd.scan(DVD_DEVICE, mb_client=request.app.state.musicbrainz)
    if await is_dvd_video(DVD_DEVICE):
        return await scan(DVD_DEVICE)
    ...
```

Add the endpoint after `api_rip`:

```python
@app.post("/api/cd/rip")
async def api_cd_rip(payload: CDRipRequest, request: Request) -> dict:
    if not any(t.include for t in payload.tracks):
        raise HTTPException(status_code=400, detail="No tracks selected")
    if not payload.creator.strip() or not payload.title.strip():
        raise HTTPException(status_code=400, detail="creator and title are required")
    ripper: Ripper = request.app.state.ripper
    job = ripper.enqueue_cd(request=payload)
    return {"job_ids": [job.id]}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_api.py -q`
Expected: PASS (existing + 4 new).

- [ ] **Step 5: Run full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/main.py tests/test_api.py
git commit -m "feat(cd): /api/scan audio-CD routing + POST /api/cd/rip"
```

---

### Task 9: Container — cdparanoia/flac/lame/libdiscid + discid dep

**Files:**
- Modify: `Dockerfile`
- Modify: `requirements.txt`

**Interfaces:**
- Produces: the runtime image has `cdparanoia`, `flac`, `lame`, `libdiscid0`; Python has `discid`. No new device mapping (sr0 + BD_SG_DEVICE already cover CD).

> No unit test — this is build/runtime config. Verification is the image build + an import check.

- [ ] **Step 1: Add the apt packages**

In `Dockerfile`, add to the FIRST `apt-get install` package list (the one with `handbrake-cli`), keeping alphabetical-ish grouping:

```dockerfile
        ca-certificates \
        cdparanoia \
        flac \
        gosu \
        handbrake-cli \
        lame \
        libdiscid0 \
        libdvd-pkg \
        libdvdread8t64 \
        libdvdnav4 \
        lsdvd \
        python3.12 \
        python3-pip \
```

- [ ] **Step 2: Add the Python binding**

In `requirements.txt`, add:

```
discid==1.4.1
```

- [ ] **Step 3: Verify the Python binding imports against the native lib**

> Requires Docker; skip if building later. This confirms `discid` finds `libdiscid0`.

Run:
```bash
docker build -t dvd-rip:cd-test . && \
docker run --rm dvd-rip:cd-test python3 -c "import discid; print('discid loaded libdiscid OK')"
```
Expected: prints `discid loaded libdiscid OK` with no `OSError: cannot open shared object` (proves the binding found `libdiscid0`).

- [ ] **Step 4: Verify the CLI tools are on PATH**

Run:
```bash
docker run --rm dvd-rip:cd-test sh -c "command -v cdparanoia flac lame"
```
Expected: three paths printed.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile requirements.txt
git commit -m "build(cd): install cdparanoia/flac/lame/libdiscid0 + discid binding"
```

---

### Task 10: UI — CD panel (music track table / audiobook whole-disc)

**Files:**
- Modify: `app/templates/index.html`
- Modify: `app/static/app.js`
- Modify: `app/static/style.css`
- Test: `tests/test_api.py` (template smoke assertion)

**Interfaces:**
- Consumes: the `/api/scan` CD response shape and `POST /api/cd/rip` (Task 8).
- Produces: a `#cd-section` panel shown when `scan.disc_format === "cd"`; JS `renderCDPanel(scan)`, `collectCDRequest()`, `startCDRip()`; the video panels stay hidden for CD.

> Front-end has no JS test harness in this repo; the automated gate is a template smoke test (the panel markup is served). Full behavior is verified manually in Task 11.

- [ ] **Step 1: Write the failing (template smoke) test**

Append to `tests/test_api.py`:

```python
def test_index_includes_cd_panel_markup(client):
    res = client.get("/")
    assert res.status_code == 200
    assert 'id="cd-section"' in res.text
    assert 'id="cd-track-body"' in res.text
    assert 'id="btn-cd-rip"' in res.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_api.py::test_index_includes_cd_panel_markup -q`
Expected: FAIL — markup absent.

- [ ] **Step 3: Add the CD panel to `index.html`**

Insert this `<section>` immediately after the `titles-section` `</section>` (before `jobs-section`):

```html
    <section id="cd-section" class="panel" hidden>
      <div class="section-head">
        <h2 id="cd-disc-label">Audio CD</h2>
        <div class="content-type-toggle" role="radiogroup" aria-label="CD mode">
          <label><input type="radio" name="cd_mode" value="music" checked /> <span>Music</span></label>
          <label><input type="radio" name="cd_mode" value="audiobook" /> <span>Audiobook</span></label>
        </div>
      </div>

      <div id="cd-release-picker" class="cd-release-picker" hidden></div>

      <div class="manual-grid">
        <label><span id="cd-creator-label">Artist</span>
          <input id="cd-creator" type="text" placeholder="Artist" /></label>
        <label><span id="cd-title-label">Album</span>
          <input id="cd-title" type="text" placeholder="Album" /></label>
        <label>Year <input id="cd-year" type="number" min="1900" max="2100" /></label>
        <label id="cd-disc-num-field" hidden>Disc #
          <input id="cd-disc-number" type="number" min="1" value="1" /></label>
        <label id="cd-total-discs-field" hidden>Total discs
          <input id="cd-total-discs" type="number" min="1" /></label>
      </div>

      <table id="cd-tracks" class="titles">
        <thead>
          <tr>
            <th class="col-pick"><input id="cd-select-all" type="checkbox" checked title="Include all" /></th>
            <th>#</th><th>Title</th><th>Duration</th>
          </tr>
        </thead>
        <tbody id="cd-track-body"></tbody>
      </table>
      <p id="cd-audiobook-note" class="muted" hidden>
        Audiobook mode joins the whole disc into one MP3. Uncheck a track only to trim a publisher intro/outro.
      </p>

      <div class="actions">
        <span id="cd-rip-status" class="rip-status"></span>
        <button id="btn-cd-rip" type="button">Rip Disc</button>
      </div>
    </section>

    <template id="cd-track-row-tpl">
      <tr class="cd-track-row">
        <td class="col-pick"><input type="checkbox" class="cd-pick" checked /></td>
        <td class="cd-num"></td>
        <td><input type="text" class="cd-track-title" /></td>
        <td class="cd-dur"></td>
      </tr>
    </template>
```

- [ ] **Step 4: Verify the smoke test passes**

Run: `.venv/bin/pytest tests/test_api.py::test_index_includes_cd_panel_markup -q`
Expected: PASS.

- [ ] **Step 5: Branch the scan renderer in `app.js`**

In `scan()`, replace the empty-titles early return so CDs route to the CD panel. Change:

```javascript
    state.scan = data;
    state.selectedMeta = null;
    state.tvEpisodeMap.clear();
    state.tvEpisodeTitles.clear();
    if (!data.titles.length) {
      status.textContent = 'No titles found. Insert a DVD and click Rescan.';
      return;
    }
    renderDiscMeta(data);
    renderTitles(data);
    status.hidden = true;
    discSec.hidden = false;
    titlesSec.hidden = false;
```

to:

```javascript
    state.scan = data;
    state.selectedMeta = null;
    state.tvEpisodeMap.clear();
    state.tvEpisodeTitles.clear();
    $('#cd-section').hidden = true;
    if (data.disc_format === 'cd') {
      renderCDPanel(data);
      status.hidden = true;
      return;
    }
    if (!data.titles.length) {
      status.textContent = 'No titles found. Insert a disc and click Rescan.';
      return;
    }
    renderDiscMeta(data);
    renderTitles(data);
    status.hidden = true;
    discSec.hidden = false;
    titlesSec.hidden = false;
```

- [ ] **Step 6: Add the CD panel functions to `app.js`**

Add near the other render functions (e.g. after `renderTitles`'s block). This wires the mode toggle, MusicBrainz prefill, the track table, and the rip POST:

```javascript
// ---------- Audio CD ----------

function cdMode() {
  const el = document.querySelector('input[name="cd_mode"]:checked');
  return el ? el.value : 'music';
}

function renderCDPanel(scan) {
  $('#disc-meta-section').hidden = true;
  $('#titles-section').hidden = true;
  $('#cd-section').hidden = false;
  $('#cd-disc-label').textContent = 'Audio CD';

  // Release picker when MusicBrainz returned >1 candidate.
  const picker = $('#cd-release-picker');
  picker.innerHTML = '';
  const releases = scan.musicbrainz || [];
  picker.hidden = releases.length <= 1;
  if (releases.length > 1) {
    releases.forEach((rel, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'secondary';
      b.textContent = `${rel.artist} — ${rel.album}${rel.year ? ' (' + rel.year + ')' : ''}`;
      b.addEventListener('click', () => applyCDRelease(scan, i));
      picker.appendChild(b);
    });
  }
  applyCDRelease(scan, 0);
  applyCDModeLabels();
}

function applyCDRelease(scan, index) {
  const rel = (scan.musicbrainz || [])[index] || null;
  $('#cd-creator').value = rel ? rel.artist : '';
  $('#cd-title').value = rel ? rel.album : '';
  $('#cd-year').value = rel && rel.year ? rel.year : '';
  $('#cd-disc-number').value = rel && rel.disc_position ? rel.disc_position : 1;
  $('#cd-total-discs').value = rel && rel.total_discs ? rel.total_discs : '';

  // Titles come from the MB medium when present; else blank, keyed by track number.
  const mbTitles = new Map();
  if (rel) rel.tracks.forEach((t) => mbTitles.set(t.number, t.title || ''));

  const body = $('#cd-track-body');
  body.innerHTML = '';
  const tpl = $('#cd-track-row-tpl');
  scan.tracks.forEach((t) => {
    const row = tpl.content.firstElementChild.cloneNode(true);
    row.querySelector('.cd-num').textContent = t.number;
    row.querySelector('.cd-dur').textContent = fmtDuration(t.duration_seconds);
    const titleInput = row.querySelector('.cd-track-title');
    titleInput.value = mbTitles.get(t.number) || '';
    titleInput.dataset.number = t.number;
    body.appendChild(row);
  });
}

function applyCDModeLabels() {
  const audiobook = cdMode() === 'audiobook';
  $('#cd-creator-label').textContent = audiobook ? 'Author' : 'Artist';
  $('#cd-title-label').textContent = audiobook ? 'Book' : 'Album';
  $('#cd-creator').placeholder = audiobook ? 'Author' : 'Artist';
  $('#cd-title').placeholder = audiobook ? 'Book' : 'Album';
  $('#cd-disc-num-field').hidden = !audiobook;
  $('#cd-total-discs-field').hidden = !audiobook;
  $('#cd-audiobook-note').hidden = !audiobook;
  $('#btn-cd-rip').textContent = audiobook ? 'Rip Disc' : 'Rip Album';
}

function collectCDRequest() {
  const tracks = $$('#cd-track-body .cd-track-row').map((row) => ({
    number: parseInt(row.querySelector('.cd-track-title').dataset.number, 10),
    title: row.querySelector('.cd-track-title').value.trim(),
    include: row.querySelector('.cd-pick').checked,
  }));
  const total = $('#cd-total-discs').value;
  return {
    mode: cdMode(),
    disc_id: (state.scan && state.scan.disc_id) || null,
    creator: $('#cd-creator').value.trim(),
    title: $('#cd-title').value.trim(),
    year: $('#cd-year').value ? parseInt($('#cd-year').value, 10) : null,
    disc_number: parseInt($('#cd-disc-number').value || '1', 10),
    total_discs: total ? parseInt(total, 10) : null,
    tracks,
  };
}

async function startCDRip() {
  const statusEl = $('#cd-rip-status');
  const req = collectCDRequest();
  if (!req.creator || !req.title) {
    statusEl.textContent = 'Enter artist/author and album/book first.';
    statusEl.classList.add('error');
    return;
  }
  if (!req.tracks.some((t) => t.include)) {
    statusEl.textContent = 'Select at least one track.';
    statusEl.classList.add('error');
    return;
  }
  statusEl.classList.remove('error');
  statusEl.textContent = 'Queuing…';
  try {
    const res = await fetch('/api/cd/rip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    });
    if (!res.ok) throw new Error(await res.text());
    const { job_ids } = await res.json();
    statusEl.textContent = `Queued (${job_ids.length}).`;
    refreshJobs();
    job_ids.forEach(streamJob);
  } catch (err) {
    statusEl.textContent = `Rip failed: ${err.message || err}`;
    statusEl.classList.add('error');
  }
}
```

- [ ] **Step 7: Wire the CD event listeners in `DOMContentLoaded`**

In the `DOMContentLoaded` handler, add (near the other listeners):

```javascript
  $('#btn-cd-rip').addEventListener('click', startCDRip);
  $$('input[name="cd_mode"]').forEach((el) =>
    el.addEventListener('change', applyCDModeLabels));
  $('#cd-select-all').addEventListener('change', (e) => {
    $$('#cd-track-body .cd-pick').forEach((c) => { c.checked = e.target.checked; });
  });
```

- [ ] **Step 8: Add CD panel styles to `style.css`**

Append:

```css
.cd-release-picker { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
#cd-tracks .cd-track-title { width: 100%; }
#cd-tracks .cd-num, #cd-tracks .cd-dur { white-space: nowrap; }
#cd-audiobook-note { margin: 8px 0 0; font-size: 0.9em; }
```

- [ ] **Step 9: Run full suite**

Run: `.venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add app/templates/index.html app/static/app.js app/static/style.css tests/test_api.py
git commit -m "feat(cd): CD panel UI (music track table / audiobook whole-disc)"
```

---

### Task 11: End-to-end verification (real discs) + close issue

**Files:** none (verification + docs/bd).

> Per the spec's "Verification" section — requires the built image and the optical drive free.

- [ ] **Step 1: Build the image**

Run: `make build`
Expected: build succeeds with the new packages.

- [ ] **Step 2: Start and open the app**

Run: `make up-d && make logs`
Then browse to the app.

- [ ] **Step 3: Music CD**

Insert a music CD → the CD panel appears in Music mode with MusicBrainz-prefilled fields (if matched). Rip → confirm per-track FLACs land under `Music/<Artist>/<Album>/` with correct tags (`metaflac --list` or a tag viewer) and a library-ledger row.

- [ ] **Step 4: Audiobook CD (the 96-track archetype)**

Insert an audiobook CD, switch to Audiobook mode, set disc #/total → Rip → confirm ONE `Audiobooks/<Author>/<Book>/<Book>, Disc NN.mp3` is produced (not N files), tags include `TPOS`, and a library row appears.

- [ ] **Step 5: Cancel path**

Start an audiobook rip and cancel mid-disc → job shows CANCELLED, no output file remains, scratch has no leftover `.wav`.

- [ ] **Step 6: Close the issue**

```bash
bd close dvd-rip-ojo
```

---

## Self-Review

**Spec coverage** (each spec section → task):
- Detection & routing (`main.py`) → Task 8. ✅
- `cd.py` (is_audio_cd, scan, rip_disc, encoders, testability seam) → Tasks 4–5. ✅
- `musicbrainz.py` (exact-only, disc position, miss→[]) → Task 3. ✅
- `models.py` (ContentType, CDTrack, MBRelease, ScanResult widening, CDTrackSelection, CDRipRequest, RipRecord.artist) → Task 1. ✅
- `paths.py` (music_path, audiobook_path one-file-per-disc, sanitize `[untitled]`/Unicode) → Task 2. ✅
- API (`/api/scan`, `POST /api/cd/rip`) → Task 8. ✅
- Ripper (enqueue_cd, dispatch, _run_cd_job, cleanup/cancel) → Task 7. ✅
- Library (artist column + additive migration) → Task 6. ✅
- UI (CD panel, mode toggle, release picker, whole-disc audiobook) → Task 10. ✅
- Container & build (apt deps, discid) → Task 9. ✅
- Testing (both TOC archetypes, MB miss, join, sanitization, cancel) → Tasks 3–8. ✅
- Verification (real discs) → Task 11. ✅

**Deferred (correctly not built):** M4B/chapter markers; "split by MusicBrainz" per-title files; fuzzy-TOC; cover art; AccurateRip; music/audiobook backfill. Per Design decisions 8–9 / Non-goals.

**Type consistency:** `rip_disc` signature is identical in Task 5 (definition) and Task 7 (call). `enqueue_cd(*, request)` matches Task 8's call and Task 7's test. `MusicBrainzClient.lookup_disc` matches Task 4's `mb_client.lookup_disc` usage. `music_path`/`audiobook_path`/`clean_track_title` signatures match between Task 2 and Task 5. `ContentType(request.mode)` relies on the string enum values from Task 1 (`"music"`/`"audiobook"`).

**Known simplifications (intentional):** `_read_disc` runs libdiscid synchronously inside the async `scan` (~1s, drive access is already serialized by the `is_busy()` guard) — acceptable for v1; wrap in `run_in_executor` if it ever blocks noticeably. Library `output_path` for CD rows is the album/book folder (not a single file), matching the "one disc = one row" design.
