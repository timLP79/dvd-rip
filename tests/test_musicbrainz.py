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
