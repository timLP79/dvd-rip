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
