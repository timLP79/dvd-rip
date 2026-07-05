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


import wave
from pathlib import Path

from app.cd import flac_args, lame_args, lame_track_args, rip_disc
from app.models import CDTrackSelection


def test_flac_args_carry_tags(tmp_path):
    args = flac_args(tmp_path / "in.wav", tmp_path / "out.flac", artist="Radiohead",
                     album="OK Computer", title="Airbag", track_number=1,
                     track_total=12, date=1997)
    assert args[0] == "flac"
    assert "--tag=ARTIST=Radiohead" in args
    assert "--tag=ALBUMARTIST=Radiohead" in args
    assert "--tag=TITLE=Airbag" in args
    assert "--tag=TRACKNUMBER=1" in args
    assert "--tag=TRACKTOTAL=12" in args
    assert "--tag=DATE=1997" in args
    assert args[-1] == str(tmp_path / "in.wav")


def test_lame_args_carry_disc_tags(tmp_path):
    args = lame_args(tmp_path / "j.wav", tmp_path / "out.mp3", artist="Neil Gaiman",
                     title="Fragile Things", album="Fragile Things",
                     disc_number=5, total_discs=9, year=2006)
    assert args[0] == "lame"
    assert "--ta" in args and "Neil Gaiman" in args
    assert "--tl" in args and "Fragile Things" in args
    i = args.index("--tv")
    assert args[i + 1] == "TPOS=5/9"
    assert args[-2:] == [str(tmp_path / "j.wav"), str(tmp_path / "out.mp3")]


def _fake_wav(path, seconds=1):
    """A minimal valid CDDA-format WAV so _concat_wavs (real) can join them."""
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * (44100 * seconds))


def _out_path(cmd):
    """The file a fake tool 'produces': cdparanoia/lame write the LAST arg;
    flac writes the arg after -o (its last arg is the INPUT wav)."""
    if cmd[0] == "flac":
        return cmd[cmd.index("-o") + 1]
    return cmd[-1]


def _spawn_factory(recorder):
    """Fake spawn: records the argv, fabricates the file the tool would write."""
    async def fake_spawn(cmd, *, register_proc=None):
        recorder.append(cmd)
        if cmd[0] == "cdparanoia":
            _fake_wav(Path(_out_path(cmd)))     # valid WAV so the real join works
        else:                                    # flac/lame -> create the output file
            Path(_out_path(cmd)).write_bytes(b"encoded")
        return 0, ""
    return fake_spawn


def test_lame_track_args_carry_track_tags(tmp_path):
    args = lame_track_args(tmp_path / "in.wav", tmp_path / "out.mp3",
                           artist="Neil Gaiman", title="The Mapmaker",
                           album="Fragile Things", track_number=2, disc_number=1,
                           total_discs=9, year=2006)
    assert args[0] == "lame"
    assert args[args.index("--tt") + 1] == "The Mapmaker"     # per-track TITLE
    assert args[args.index("--tl") + 1] == "Fragile Things"   # ALBUM = book
    assert args[args.index("--tn") + 1] == "2"                # track number
    assert args[args.index("--tv") + 1] == "TPOS=1/9"         # disc position
    assert args[-2:] == [str(tmp_path / "in.wav"), str(tmp_path / "out.mp3")]


@pytest.mark.asyncio
async def test_rip_disc_audiobook_split_one_file_per_track(tmp_path):
    cmds = []
    tracks = [CDTrackSelection(number=1, title="The Mapmaker"),
              CDTrackSelection(number=2, title="A Study in Emerald")]
    written = await rip_disc(
        "/dev/sr0", mode="audiobook", output_root=tmp_path, creator="Neil Gaiman",
        title="Fragile Things", year=2006, disc_number=1, total_discs=9,
        tracks=tracks, scratch_dir=str(tmp_path / "scratch"), split=True,
        spawn=_spawn_factory(cmds))
    assert len(written) == 2
    assert (tmp_path / "Audiobooks" / "Neil Gaiman" / "Fragile Things"
            / "1-01 - The Mapmaker.mp3").exists()
    # per-track lame, NOT a whole-disc join
    assert [c[0] for c in cmds] == ["cdparanoia", "lame", "cdparanoia", "lame"]
    assert not list((tmp_path / "scratch").glob("*.wav"))


@pytest.mark.asyncio
async def test_rip_disc_audiobook_split_removes_written_on_failure(tmp_path):
    calls = {"n": 0}
    async def flaky_spawn(cmd, *, register_proc=None):
        if cmd[0] == "cdparanoia":
            _fake_wav(Path(_out_path(cmd)))
            return 0, ""
        calls["n"] += 1
        if calls["n"] == 2:                      # second track's lame encode fails
            return 1, "lame: disk full"
        Path(_out_path(cmd)).write_bytes(b"x")
        return 0, ""
    tracks = [CDTrackSelection(number=1, title="A"), CDTrackSelection(number=2, title="B")]
    with pytest.raises(Exception):
        await rip_disc("/dev/sr0", mode="audiobook", output_root=tmp_path,
                       creator="Auth", title="Book", year=None, disc_number=1,
                       total_discs=None, tracks=tracks, split=True,
                       scratch_dir=str(tmp_path / "scratch"), spawn=flaky_spawn)
    # No partial garbage: the first (successful) MP3 is rolled back; scratch swept.
    assert not list((tmp_path / "Audiobooks").rglob("*.mp3"))
    assert not list((tmp_path / "scratch").glob("*.wav"))


@pytest.mark.asyncio
async def test_rip_disc_music_writes_one_file_per_track(tmp_path):
    cmds = []
    tracks = [CDTrackSelection(number=1, title="Airbag"),
              CDTrackSelection(number=2, title="Paranoid Android"),
              CDTrackSelection(number=3, title="", include=False)]
    written = await rip_disc(
        "/dev/sr0", mode="music", output_root=tmp_path, creator="Radiohead",
        title="OK Computer", year=1997, disc_number=1, total_discs=None,
        tracks=tracks, scratch_dir=str(tmp_path / "scratch"),
        spawn=_spawn_factory(cmds))
    assert len(written) == 2                                   # track 3 excluded
    assert (tmp_path / "Music" / "Radiohead" / "OK Computer" / "01 - Airbag.flac").exists()
    assert not list((tmp_path / "scratch").glob("*.wav"))      # wavs cleaned up
    assert [c[0] for c in cmds] == ["cdparanoia", "flac", "cdparanoia", "flac"]


@pytest.mark.asyncio
async def test_rip_disc_audiobook_joins_to_one_file(tmp_path):
    cmds = []
    tracks = [CDTrackSelection(number=n) for n in range(1, 5)]
    progress = []
    written = await rip_disc(
        "/dev/sr0", mode="audiobook", output_root=tmp_path, creator="Neil Gaiman",
        title="Fragile Things", year=2006, disc_number=5, total_discs=9,
        tracks=tracks, scratch_dir=str(tmp_path / "scratch"),
        on_progress=progress.append, spawn=_spawn_factory(cmds))
    assert len(written) == 1
    assert written[0].name == "Fragile Things, Disc 05.mp3"
    assert written[0].exists()
    # 4 cdparanoia extracts + exactly one lame encode
    assert [c[0] for c in cmds].count("cdparanoia") == 4
    assert [c[0] for c in cmds].count("lame") == 1
    assert progress[-1] == 100
    assert not list((tmp_path / "scratch").glob("*.wav"))


@pytest.mark.asyncio
async def test_rip_disc_removes_written_files_on_failure(tmp_path):
    calls = {"n": 0}
    async def flaky_spawn(cmd, *, register_proc=None):
        if cmd[0] == "cdparanoia":
            _fake_wav(Path(_out_path(cmd)))
            return 0, ""
        calls["n"] += 1
        if calls["n"] == 2:                      # second flac encode fails
            return 1, "flac: disk full"
        Path(_out_path(cmd)).write_bytes(b"x")   # first flac writes its real output
        return 0, ""
    tracks = [CDTrackSelection(number=1, title="A"), CDTrackSelection(number=2, title="B")]
    with pytest.raises(Exception):
        await rip_disc("/dev/sr0", mode="music", output_root=tmp_path,
                       creator="Art", title="Alb", year=None, disc_number=1,
                       total_discs=None, tracks=tracks,
                       scratch_dir=str(tmp_path / "scratch"), spawn=flaky_spawn)
    # First (successful) track file was rolled back — no partial garbage.
    assert not list((tmp_path / "Music").rglob("*.flac"))
    # Scratch WAVs are swept even on the failure path (both modes).
    assert not list((tmp_path / "scratch").glob("*.wav"))
