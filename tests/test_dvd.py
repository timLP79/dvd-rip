import asyncio
from pathlib import Path

import pytest

from app import dvd
from app.dvd import classify_aspect, is_dvd_video, parse_handbrake_scan, parse_lsdvd_label, _safe_label

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_parse_normal_disc():
    titles = parse_handbrake_scan(_read("scan_normal.txt"))
    assert len(titles) == 3

    main = titles[0]
    assert main.index == 1
    assert main.duration_seconds == 1 * 3600 + 42 * 60 + 18
    assert main.chapter_count == 12
    assert main.frame_width == 720
    assert main.frame_height == 480
    assert main.is_main_feature is True
    assert [a.index for a in main.audio_tracks] == [1, 2]
    assert main.audio_tracks[0].language == "English"
    assert main.audio_tracks[0].channels == "5.1 Surround"
    assert [s.index for s in main.subtitle_tracks] == [1, 2]

    short = titles[2]
    assert short.is_main_feature is False
    assert short.audio_tracks == []
    assert short.subtitle_tracks == []


def test_main_feature_fallback_picks_longest_over_threshold():
    titles = parse_handbrake_scan(_read("scan_no_main.txt"))
    assert len(titles) == 2
    main = [t for t in titles if t.is_main_feature]
    assert len(main) == 1
    assert main[0].index == 1  # 25 minutes, longer than 20-min threshold


def test_empty_scan_returns_no_titles():
    titles = parse_handbrake_scan(_read("scan_empty.txt"))
    assert titles == []


def test_is_dvd_video_true_for_dvd():
    async def fake(cmd, timeout=15.0):
        return 0, "Disc Title: DVD_VIDEO\nTitle: 01, Length: 01:30:00.000\n", ""
    assert asyncio.run(is_dvd_video("/dev/sr0", _run=fake)) is True


def test_is_dvd_video_false_for_non_dvd():
    # lsdvd exits non-zero (and prints no DVD structure) on a Blu-ray / data disc.
    async def fake(cmd, timeout=15.0):
        return 1, "", "libdvdread: Can't open /dev/sr0 for reading\n"
    assert asyncio.run(is_dvd_video("/dev/sr0", _run=fake)) is False


def test_is_dvd_video_false_when_lsdvd_missing():
    async def fake(cmd, timeout=15.0):
        raise FileNotFoundError("lsdvd")
    assert asyncio.run(is_dvd_video("/dev/sr0", _run=fake)) is False


def test_is_dvd_video_false_on_timeout():
    async def fake(cmd, timeout=15.0):
        raise asyncio.TimeoutError()
    assert asyncio.run(is_dvd_video("/dev/sr0", _run=fake)) is False


def test_parse_lsdvd_label():
    out = "Disc Title: THE_MATRIX\nTitle: 01, Length: 02:16:13\n"
    assert parse_lsdvd_label(out) == "THE_MATRIX"


def test_parse_lsdvd_label_missing():
    assert parse_lsdvd_label("garbage output") == "UNKNOWN_DVD"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("The Matrix", "The_Matrix"),
        ("  hello world  ", "hello_world"),
        ("...", "UNKNOWN_DVD"),
        ("DISC/1:OK", "DISC_1_OK"),
    ],
)
def test_safe_label(raw, expected):
    assert _safe_label(raw) == expected


def test_classify_aspect_widescreen_anamorphic():
    assert classify_aspect(720, 480, 32, 27) == "widescreen"   # DAR 1.78


def test_classify_aspect_fullscreen_anamorphic():
    assert classify_aspect(720, 480, 8, 9) == "fullscreen"     # DAR 1.33


def test_classify_aspect_square_ntsc_is_fullscreen():
    assert classify_aspect(720, 480, 1, 1) == "fullscreen"     # DAR 1.5 < 1.56


def test_classify_aspect_bluray_dims():
    assert classify_aspect(1920, 1080) == "widescreen"          # DAR 1.78, default PAR 1:1


def test_classify_aspect_missing_dims_is_none():
    assert classify_aspect(0, 0) is None


def test_classify_aspect_zero_par_is_none():
    assert classify_aspect(720, 480, 1, 0) is None


def test_parse_normal_disc_sets_aspect():
    titles = parse_handbrake_scan(_read("scan_normal.txt"))
    labels = {t.aspect for t in titles}
    # scan_normal has a PAR 32:27 title (widescreen) and PAR 1:1 titles (fullscreen)
    assert "widescreen" in labels
    assert "fullscreen" in labels


def test_spawn_calls_register_proc_with_process():
    from app.dvd import _spawn
    captured = {}

    def reg(proc):
        captured["proc"] = proc

    rc, out, err = asyncio.run(_spawn(["/bin/echo", "hi"], register_proc=reg))
    assert rc == 0
    assert "hi" in out
    assert captured["proc"] is not None
    assert hasattr(captured["proc"], "kill")


def test_is_dvd_video_forwards_register_proc():
    captured = {}

    async def fake(cmd, timeout=15.0, register_proc=None):
        captured["rp"] = register_proc
        return (0, "Disc Title: X\n", "")

    def reg(proc):
        pass

    result = asyncio.run(is_dvd_video("/dev/sr0", _run=fake, register_proc=reg))
    assert result is True
    assert captured["rp"] is reg


def test_is_dvd_video_default_no_register_proc_kwarg():
    """Old-style fake without register_proc must still work (None path)."""
    async def fake(cmd, timeout=15.0):
        return (0, "Disc Title: X\n", "")

    assert asyncio.run(is_dvd_video("/dev/sr0", _run=fake)) is True


def test_scan_timeout_reports_why_it_is_empty(monkeypatch):
    """A timed-out scan must not look like a blank disc.

    Both produce zero titles, but they need opposite responses from the user
    (rescan with a longer budget vs. try another disc). Regression test for a
    real failure: a dual-layer ARccOS-protected disc blew past the original
    120s ceiling on a USB drive and the UI silently showed "no titles".
    """
    async def fake_spawn(cmd, timeout=120.0, register_proc=None):
        if cmd[0] == "HandBrakeCLI":
            raise asyncio.TimeoutError()
        return (0, "Disc Title: SOME_DISC\n", "")

    monkeypatch.setattr(dvd, "_spawn", fake_spawn)
    result = asyncio.run(dvd.scan("/dev/sr0"))

    assert result.titles == []
    assert result.dvd_label == "SOME_DISC"      # label still recovered via lsdvd
    assert result.warning is not None
    assert "timed out" in result.warning.lower()
    assert "HANDBRAKE_SCAN_TIMEOUT" in result.warning


def test_scan_uses_configured_timeout(monkeypatch):
    """The HandBrake scan must get the generous budget, not _spawn's default."""
    seen = {}

    async def fake_spawn(cmd, timeout=120.0, register_proc=None):
        seen[cmd[0]] = timeout
        return (0, "Disc Title: X\n", "")

    monkeypatch.setattr(dvd, "_spawn", fake_spawn)
    monkeypatch.setattr(dvd, "SCAN_TIMEOUT", 600.0)
    asyncio.run(dvd.scan("/dev/sr0"))
    assert seen["HandBrakeCLI"] == 600.0


def test_successful_scan_has_no_warning(monkeypatch):
    async def fake_spawn(cmd, timeout=120.0, register_proc=None):
        if cmd[0] == "HandBrakeCLI":
            return (0, open("tests/fixtures/scan_normal.txt").read(), "")
        return (0, "Disc Title: X\n", "")

    monkeypatch.setattr(dvd, "_spawn", fake_spawn)
    result = asyncio.run(dvd.scan("/dev/sr0"))
    assert result.titles and result.warning is None
