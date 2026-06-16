from app.bluray import (
    is_bluray_output,
    parse_makemkv_info,
    parse_makemkv_progress,
)

# Constructed from MakeMKV's documented robot (-r) format. VERIFY against real
# `makemkvcon -r info` output during execution.
SAMPLE_INFO = '\n'.join([
    'MSG:1005,0,1,"MakeMKV v1.17.7 started","%1","x"',
    'DRV:0,2,999,12,"BD-RE HL-DT-ST","BIG_MOVIE","/dev/sr0"',
    'CINFO:1,6209,"Blu-ray disc"',
    'CINFO:2,0,"BIG_MOVIE"',
    'TCOUNT:2',
    'TINFO:0,8,0,"24"',
    'TINFO:0,9,0,"1:52:30"',
    'TINFO:0,10,0,"30.1 GB"',
    'TINFO:0,11,0,"32319012864"',
    'SINFO:0,0,1,6201,"Video"',
    'SINFO:0,0,19,0,"1920x1080"',
    'SINFO:0,1,1,6202,"Audio"',
    'SINFO:0,1,3,0,"eng"',
    'SINFO:0,1,4,0,"English"',
    'SINFO:0,1,7,0,"DTS-HD Master Audio"',
    'SINFO:0,2,1,6202,"Audio"',
    'SINFO:0,2,3,0,"fra"',
    'SINFO:0,2,4,0,"French"',
    'SINFO:0,3,1,6203,"Subtitles"',
    'SINFO:0,3,3,0,"eng"',
    'SINFO:0,3,4,0,"English"',
    'TINFO:1,8,0,"2"',
    'TINFO:1,9,0,"0:03:10"',
    'SINFO:1,0,1,6201,"Video"',
    'SINFO:1,0,19,0,"1920x1080"',
])


def test_is_bluray_output_true_for_bd():
    assert is_bluray_output(SAMPLE_INFO) is True


def test_is_bluray_output_false_for_dvd_or_empty():
    dvd = 'DRV:0,2,999,1,"DVD HL-DT-ST","SOME_DVD","/dev/sr0"\nCINFO:1,6206,"DVD disc"\nTCOUNT:1'
    assert is_bluray_output(dvd) is False
    assert is_bluray_output("") is False


def test_parse_makemkv_info_titles():
    titles = parse_makemkv_info(SAMPLE_INFO)
    assert len(titles) == 2
    main = titles[0]
    assert main.index == 0
    assert main.duration_seconds == 1 * 3600 + 52 * 60 + 30
    assert main.chapter_count == 24
    assert main.frame_width == 1920 and main.frame_height == 1080
    assert [a.language for a in main.audio_tracks] == ["English", "French"]
    assert [s.language for s in main.subtitle_tracks] == ["English"]
    # main feature = longest title flagged
    assert main.is_main_feature is True
    assert titles[1].is_main_feature is False


def test_parse_makemkv_progress():
    assert parse_makemkv_progress("PRGV:16384,32768,65536") == 50.0
    assert parse_makemkv_progress("PRGV:0,0,65536") == 0.0
    assert parse_makemkv_progress("not a progress line") is None
    # guard against divide-by-zero
    assert parse_makemkv_progress("PRGV:5,5,0") == 0.0
