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

    async def fake_scan(device, register_proc=None):
        return fake

    async def fake_is_dvd(device, register_proc=None):
        return True  # deterministic: take the DVD path without spawning lsdvd

    with patch("app.main.is_dvd_video", side_effect=fake_is_dvd), \
         patch("app.main.scan", side_effect=fake_scan):
        res = client.get("/api/scan")
    assert res.status_code == 200
    body = res.json()
    assert body["dvd_label"] == "TEST_DISC"
    assert body["titles"][0]["index"] == 1
    assert body["titles"][0]["is_main_feature"] is True


def test_scan_dvd_skips_makemkv(client):
    """A DVD-Video disc routes to the DVD scanner and never invokes makemkvcon."""
    fake = ScanResult(dvd_label="DVD_DISC", titles=[])
    bluray_called = {"hit": False}

    async def fake_is_dvd(device, register_proc=None):
        return True

    async def fake_scan(device, register_proc=None):
        return fake

    async def fake_bluray(device, register_proc=None):
        bluray_called["hit"] = True
        return None

    with patch("app.main.is_dvd_video", side_effect=fake_is_dvd), \
         patch("app.main.scan", side_effect=fake_scan), \
         patch("app.main.bluray_detect_and_scan", side_effect=fake_bluray):
        res = client.get("/api/scan")
    assert res.status_code == 200
    assert res.json()["dvd_label"] == "DVD_DISC"
    assert bluray_called["hit"] is False  # makemkvcon must NOT run for a DVD


def test_scan_uses_bluray_when_not_dvd(client):
    """A non-DVD disc falls through to makemkvcon-based Blu-ray detection."""
    bd = ScanResult(dvd_label="BLURAY", titles=[], disc_format="bluray")

    async def fake_is_dvd(device, register_proc=None):
        return False

    async def fake_bluray(device, register_proc=None):
        return bd

    with patch("app.main.is_dvd_video", side_effect=fake_is_dvd), \
         patch("app.main.bluray_detect_and_scan", side_effect=fake_bluray):
        res = client.get("/api/scan")
    assert res.status_code == 200
    assert res.json()["disc_format"] == "bluray"


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


def test_scan_reports_bluray_when_detected(client):
    from app.models import ScanResult, Title
    bd = ScanResult(dvd_label="BLURAY", disc_format="bluray", titles=[
        Title(index=0, duration_seconds=6000, chapter_count=12,
              frame_width=1920, frame_height=1080)])

    async def fake_detect(device, **kw):
        return bd

    with patch("app.main.bluray_detect_and_scan", side_effect=fake_detect):
        res = client.get("/api/scan")
    assert res.status_code == 200
    assert res.json()["disc_format"] == "bluray"


def test_scan_falls_back_to_dvd(client):
    async def fake_detect(device, **kw):
        return None
    from app.models import ScanResult

    async def fake_dvd(device, register_proc=None):
        return ScanResult(dvd_label="A_DVD", titles=[])

    with patch("app.main.bluray_detect_and_scan", side_effect=fake_detect), \
         patch("app.main.scan", side_effect=fake_dvd):
        res = client.get("/api/scan")
    assert res.status_code == 200
    assert res.json()["disc_format"] == "dvd"
    assert res.json()["dvd_label"] == "A_DVD"


def test_scan_blocked_while_rip_running(client):
    """Opening the page (auto-scan) or hitting Rescan during an active rip must NOT
    touch the drive — /api/scan returns 409 before any scan subprocess runs."""
    from app import main as main_module
    ripper = main_module.app.state.ripper
    ripper._current_job_id = "job-x"
    try:
        res = client.get("/api/scan")
        assert res.status_code == 409
    finally:
        ripper._current_job_id = None


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


def test_index_includes_cd_panel_markup(client):
    res = client.get("/")
    assert res.status_code == 200
    assert 'id="cd-section"' in res.text
    assert 'id="cd-track-body"' in res.text
    assert 'id="btn-cd-rip"' in res.text


def test_library_page_has_cd_filters(client):
    res = client.get("/library")
    assert res.status_code == 200
    # Type filter gains Music/Audiobook; format filter gains CD.
    assert '<option value="music">' in res.text
    assert '<option value="audiobook">' in res.text
    assert '<option value="cd">' in res.text


def test_second_scan_returns_409_while_scanning(client):
    from app import main as main_module
    coord = main_module.app.state.scan_coordinator
    coord._busy = True  # simulate a scan already in progress
    try:
        res = client.get("/api/scan")
        assert res.status_code == 409
        assert "already in progress" in res.json()["detail"].lower()
    finally:
        coord._busy = False


def test_scan_cancel_kills_live_proc(client):
    from app import main as main_module
    coord = main_module.app.state.scan_coordinator

    class FakeProc:
        returncode = None
        def __init__(self):
            self.killed = False
        def kill(self):
            self.killed = True

    proc = FakeProc()
    coord._busy = True
    coord._proc = proc
    try:
        res = client.post("/api/scan/cancel")
        assert res.status_code == 200
        assert res.json()["cancelled"] is True
        assert proc.killed is True
    finally:
        coord._busy = False
        coord._proc = None


def test_scan_cancel_when_idle_returns_false(client):
    res = client.post("/api/scan/cancel")
    assert res.status_code == 200
    assert res.json()["cancelled"] is False


def test_rip_blocked_while_scanning(client):
    from app import main as main_module
    coord = main_module.app.state.scan_coordinator
    coord._busy = True
    try:
        res = client.post("/api/rip", json={"titles": []})
        assert res.status_code == 409
    finally:
        coord._busy = False


def test_cd_rip_blocked_while_scanning(client):
    from app import main as main_module
    coord = main_module.app.state.scan_coordinator
    coord._busy = True
    try:
        res = client.post("/api/cd/rip", json={"tracks": []})
        assert res.status_code == 409
    finally:
        coord._busy = False
