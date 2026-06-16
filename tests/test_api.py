from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.models import AudioTrack, ScanResult, SubtitleTrack, Title


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("DVD_DEVICE", "/dev/null")
    monkeypatch.setenv("TMDB_API_KEY", "")
    monkeypatch.setenv("LIBRARY_DB", str(tmp_path / "library.db"))
    from importlib import reload
    from app import main as main_module
    reload(main_module)
    with TestClient(main_module.app) as c:
        yield c


def test_index_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "DVD Rip" in res.text
    assert "Start Rip" in res.text


def test_scan_endpoint_returns_titles(client):
    fake = ScanResult(
        dvd_label="TEST_DISC",
        titles=[
            Title(
                index=1,
                duration_seconds=5400,
                chapter_count=10,
                frame_width=720,
                frame_height=480,
                audio_tracks=[AudioTrack(index=1, language="English")],
                subtitle_tracks=[SubtitleTrack(index=1, language="English")],
                is_main_feature=True,
            ),
        ],
    )

    async def fake_scan(device):
        return fake

    with patch("app.main.scan", side_effect=fake_scan):
        res = client.get("/api/scan")
    assert res.status_code == 200
    body = res.json()
    assert body["dvd_label"] == "TEST_DISC"
    assert body["titles"][0]["index"] == 1
    assert body["titles"][0]["is_main_feature"] is True


def test_rip_rejects_empty_titles(client):
    res = client.post(
        "/api/rip",
        json={
            "dvd_label": "X",
            "encoder_mode": "quality",
            "content_type": "movie",
            "movie": {"title": "Foo", "year": 2020},
            "titles": [],
        },
    )
    assert res.status_code == 400


def test_rip_rejects_movie_without_metadata(client):
    res = client.post(
        "/api/rip",
        json={
            "dvd_label": "X",
            "encoder_mode": "quality",
            "content_type": "movie",
            "titles": [{"index": 1, "audio_tracks": [1], "subtitle_tracks": []}],
        },
    )
    assert res.status_code == 400


def test_rip_rejects_tv_without_episode_number(client):
    res = client.post(
        "/api/rip",
        json={
            "dvd_label": "X",
            "encoder_mode": "quality",
            "content_type": "tv",
            "tv": {"show": "Deadwood", "season": 1},
            "titles": [{"index": 1, "audio_tracks": [1], "subtitle_tracks": []}],
        },
    )
    assert res.status_code == 400


def test_rip_accepts_valid_movie(client):
    res = client.post(
        "/api/rip",
        json={
            "dvd_label": "THE_MATRIX",
            "encoder_mode": "quality",
            "content_type": "movie",
            "movie": {"title": "The Matrix", "year": 1999},
            "titles": [
                {"index": 1, "audio_tracks": [1], "subtitle_tracks": [], "part_index": 1}
            ],
        },
    )
    assert res.status_code == 200
    assert len(res.json()["job_ids"]) == 1


def test_rip_accepts_valid_tv(client):
    res = client.post(
        "/api/rip",
        json={
            "dvd_label": "DEADWOOD_S1D1",
            "encoder_mode": "quality",
            "content_type": "tv",
            "tv": {"show": "Deadwood", "season": 1},
            "titles": [
                {"index": 1, "audio_tracks": [1], "subtitle_tracks": [], "episode_number": 1},
                {"index": 2, "audio_tracks": [1], "subtitle_tracks": [], "episode_number": 2},
            ],
        },
    )
    assert res.status_code == 200
    assert len(res.json()["job_ids"]) == 2


def test_search_returns_empty_when_tmdb_disabled(client):
    res = client.get("/api/search?type=movie&query=matrix")
    assert res.status_code == 200
    body = res.json()
    assert body["tmdb_enabled"] is False
    assert body["results"] == []


def test_search_requires_query(client):
    res = client.get("/api/search?type=movie")
    assert res.status_code == 422


def test_jobs_endpoint_empty_initially(client):
    res = client.get("/api/jobs")
    assert res.status_code == 200
    assert res.json() == []


def test_cancel_unknown_job_returns_409(client):
    res = client.post("/api/jobs/nonexistent/cancel")
    assert res.status_code == 409


def test_progress_unknown_job_returns_404(client):
    res = client.get("/api/jobs/nonexistent/progress")
    assert res.status_code == 404


def test_library_empty_initially(client):
    res = client.get("/api/library")
    assert res.status_code == 200
    assert res.json() == []


def test_library_check_no_match_on_empty(client):
    res = client.get("/api/library/check", params={
        "content_type": "movie", "title": "Nothing", "year": 1999})
    assert res.status_code == 200
    assert res.json()["matches"] == []


def test_recorded_rip_appears_via_api(client):
    """Write through the Library (same DB the app opened), read back via the API."""
    import os
    from app.library import Library
    from app.models import ContentType, JobStatus, RipRecord

    lib = Library(os.environ["LIBRARY_DB"])
    lib.record_rip(RipRecord(
        content_type=ContentType.MOVIE, title="Top Gun", year=1986,
        output_path="/output/Movies/Top Gun (1986)/Top Gun (1986).mp4",
        status=JobStatus.DONE, file_size_bytes=123,
    ))

    rows = client.get("/api/library").json()
    assert any(r["title"] == "Top Gun" for r in rows)

    chk = client.get("/api/library/check",
                     params={"content_type": "movie", "title": "Top Gun", "year": 1986})
    assert len(chk.json()["matches"]) == 1
