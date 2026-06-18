import asyncio
from pathlib import Path

import pytest

from app.dvd import is_dvd_video, parse_handbrake_scan, parse_lsdvd_label, _safe_label

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
