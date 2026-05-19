"""Thin async TMDB v3 client.

Used by /api/search and /api/tv/.../episodes to auto-fill disc metadata.

If TMDB_API_KEY is unset, all methods return empty results so the UI's manual
entry path keeps working. No hard dependency.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from .models import TMDBEpisode, TMDBMovieResult, TMDBTVResult

log = logging.getLogger(__name__)

_BASE_URL = "https://api.themoviedb.org/3"
_IMAGE_BASE = "https://image.tmdb.org/t/p/w185"
_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _year(date_str: Optional[str]) -> Optional[int]:
    if not date_str or len(date_str) < 4:
        return None
    try:
        return int(date_str[:4])
    except ValueError:
        return None


def _poster_url(path: Optional[str]) -> Optional[str]:
    return f"{_IMAGE_BASE}{path}" if path else None


class TMDBClient:
    """Reusable TMDB client. Shares a single httpx.AsyncClient instance."""

    def __init__(self, api_key: Optional[str] = None, http_client: Optional[httpx.AsyncClient] = None):
        self.api_key = api_key if api_key is not None else os.getenv("TMDB_API_KEY", "").strip()
        self._client = http_client or httpx.AsyncClient(timeout=_TIMEOUT)
        self._owns_client = http_client is None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search_movie(self, query: str, *, limit: int = 10) -> list[TMDBMovieResult]:
        if not self.enabled or not query.strip():
            return []
        try:
            resp = await self._client.get(
                f"{_BASE_URL}/search/movie",
                params={"api_key": self.api_key, "query": query, "include_adult": "false"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("TMDB movie search failed: %s", exc)
            return []
        return [
            TMDBMovieResult(
                id=item["id"],
                title=item.get("title") or item.get("original_title") or "Untitled",
                year=_year(item.get("release_date")),
                poster_url=_poster_url(item.get("poster_path")),
                overview=item.get("overview") or None,
            )
            for item in (resp.json().get("results") or [])[:limit]
        ]

    async def search_tv(self, query: str, *, limit: int = 10) -> list[TMDBTVResult]:
        if not self.enabled or not query.strip():
            return []
        try:
            resp = await self._client.get(
                f"{_BASE_URL}/search/tv",
                params={"api_key": self.api_key, "query": query, "include_adult": "false"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("TMDB tv search failed: %s", exc)
            return []
        return [
            TMDBTVResult(
                id=item["id"],
                name=item.get("name") or item.get("original_name") or "Untitled",
                year=_year(item.get("first_air_date")),
                poster_url=_poster_url(item.get("poster_path")),
                overview=item.get("overview") or None,
            )
            for item in (resp.json().get("results") or [])[:limit]
        ]

    async def get_tv_season_episodes(self, tv_id: int, season: int) -> list[TMDBEpisode]:
        if not self.enabled:
            return []
        try:
            resp = await self._client.get(
                f"{_BASE_URL}/tv/{tv_id}/season/{season}",
                params={"api_key": self.api_key},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("TMDB season fetch failed: %s", exc)
            return []
        episodes = resp.json().get("episodes") or []
        return [
            TMDBEpisode(
                season_number=ep.get("season_number", season),
                episode_number=ep.get("episode_number", 0),
                name=ep.get("name") or None,
                air_date=ep.get("air_date") or None,
                runtime=ep.get("runtime") or None,
            )
            for ep in episodes
        ]
