import httpx
import pytest
import respx

from app.tmdb import TMDBClient


@pytest.mark.asyncio
async def test_disabled_when_no_api_key():
    client = TMDBClient(api_key="")
    assert client.enabled is False
    assert await client.search_movie("anything") == []
    assert await client.search_tv("anything") == []
    assert await client.get_tv_season_episodes(1, 1) == []
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_search_movie_parses_results():
    respx.get("https://api.themoviedb.org/3/search/movie").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 603,
                        "title": "The Matrix",
                        "release_date": "1999-03-30",
                        "poster_path": "/abc.jpg",
                        "overview": "A hacker discovers...",
                    },
                    {
                        "id": 604,
                        "title": "The Matrix Reloaded",
                        "release_date": "2003-05-15",
                        "poster_path": None,
                        "overview": None,
                    },
                ]
            },
        )
    )
    client = TMDBClient(api_key="fake")
    results = await client.search_movie("matrix")
    await client.aclose()

    assert len(results) == 2
    assert results[0].id == 603
    assert results[0].title == "The Matrix"
    assert results[0].year == 1999
    assert results[0].poster_url == "https://image.tmdb.org/t/p/w185/abc.jpg"
    assert results[1].poster_url is None
    assert results[1].overview is None


@pytest.mark.asyncio
@respx.mock
async def test_search_tv_parses_results():
    respx.get("https://api.themoviedb.org/3/search/tv").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": 1437,
                        "name": "Deadwood",
                        "first_air_date": "2004-03-21",
                        "poster_path": "/dead.jpg",
                    }
                ]
            },
        )
    )
    client = TMDBClient(api_key="fake")
    results = await client.search_tv("deadwood")
    await client.aclose()

    assert len(results) == 1
    assert results[0].name == "Deadwood"
    assert results[0].year == 2004
    assert results[0].id == 1437


@pytest.mark.asyncio
@respx.mock
async def test_get_tv_season_episodes_parses():
    respx.get("https://api.themoviedb.org/3/tv/1437/season/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "episodes": [
                    {"season_number": 1, "episode_number": 1, "name": "Deadwood", "air_date": "2004-03-21"},
                    {"season_number": 1, "episode_number": 2, "name": "Deep Water", "air_date": "2004-03-28"},
                ]
            },
        )
    )
    client = TMDBClient(api_key="fake")
    eps = await client.get_tv_season_episodes(1437, 1)
    await client.aclose()

    assert [e.episode_number for e in eps] == [1, 2]
    assert eps[0].name == "Deadwood"
    assert eps[1].name == "Deep Water"


@pytest.mark.asyncio
@respx.mock
async def test_search_returns_empty_on_http_error():
    respx.get("https://api.themoviedb.org/3/search/movie").mock(
        return_value=httpx.Response(500)
    )
    client = TMDBClient(api_key="fake")
    assert await client.search_movie("x") == []
    await client.aclose()


@pytest.mark.asyncio
async def test_empty_query_short_circuits():
    client = TMDBClient(api_key="fake")
    assert await client.search_movie("   ") == []
    assert await client.search_tv("") == []
    await client.aclose()
