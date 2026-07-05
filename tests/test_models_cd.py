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
    assert req.split_tracks is False


def test_mbrelease_and_riprecord_artist():
    rel = MBRelease(artist="a", album="b", disc_position=5, total_discs=9,
                    tracks=[CDTrack(number=1, duration_seconds=1, title="t")])
    assert rel.disc_position == 5
    rec = RipRecord(content_type=ContentType.AUDIOBOOK, title="b", artist="a",
                    output_path="/x")
    assert rec.artist == "a"
