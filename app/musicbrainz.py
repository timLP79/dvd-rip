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
    # An exact-hit release tagged Audiobook/Spokenword is a reliable "this is an
    # audiobook" signal — enough to default the UI mode (a MISS is not, hence manual).
    rg = release.get("release-group") or {}
    secondary = {s.lower() for s in (rg.get("secondary-types") or [])}
    return MBRelease(
        artist=_artist_name(release.get("artist-credit")),
        album=release.get("title") or "Unknown Album",
        year=_year(release.get("date")),
        disc_position=matched.get("position"),
        total_discs=len(media) or None,
        is_audiobook=bool(secondary & {"audiobook", "spokenword"}),
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
                params={"inc": "recordings+artist-credits+release-groups", "fmt": "json"},
                headers={"User-Agent": self.user_agent},
            )
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.info("MusicBrainz disc lookup failed for %s: %s", disc_id, exc)
            return []
        return [_release_to_mbrelease(disc_id, r) for r in (data.get("releases") or [])]
