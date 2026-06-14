from app.models import EncoderMode
from app.presets import build_handbrake_args


def _args_to_pairs(args: list[str]) -> dict[str, str]:
    """Collapse long-form --flag value pairs into a dict for easier assertions.

    Flags without a value (e.g. --optimize) get an empty string.
    """
    out: dict[str, str] = {}
    i = 0
    while i < len(args):
        token = args[i]
        if token.startswith("--"):
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                out[token] = args[i + 1]
                i += 2
                continue
            out[token] = ""
            i += 1
            continue
        i += 1
    return out


def test_quality_mode_uses_x264():
    args = build_handbrake_args(
        device="/dev/sr0",
        title_index=1,
        output_path="/output/movie/title_01.mp4",
        audio_tracks=[1],
        subtitle_tracks=[],
        encoder_mode=EncoderMode.QUALITY,
    )
    pairs = _args_to_pairs(args)
    assert pairs["--encoder"] == "x264"
    assert pairs["--quality"] == "20"
    assert pairs["--encoder-preset"] == "slow"
    assert "--encoder-tune" not in pairs  # grain tune dropped for H.264
    assert pairs["--format"] == "av_mp4"
    assert "--optimize" in args
    assert "--markers" in args
    assert pairs["--decomb"] == "default"
    assert pairs["--crop-mode"] == "auto"
    assert "--auto-anamorphic" in args
    assert pairs["--input"] == "/dev/sr0"
    assert pairs["--output"] == "/output/movie/title_01.mp4"
    assert pairs["--title"] == "1"
    # One selected source track -> two output tracks: stereo AAC + passthrough copy.
    assert pairs["--audio"] == "1,1"
    assert pairs["--aencoder"] == "ca_aac,copy"
    assert pairs["--mixdown"] == "stereo,none"
    assert pairs["--audio-fallback"] == "ac3"


def test_speed_mode_uses_nvenc():
    args = build_handbrake_args(
        device="/dev/sr0",
        title_index=1,
        output_path="/output/movie/title_01.mp4",
        audio_tracks=[1],
        subtitle_tracks=[],
        encoder_mode=EncoderMode.SPEED,
    )
    pairs = _args_to_pairs(args)
    assert pairs["--encoder"] == "nvenc_h264"
    assert pairs["--quality"] == "22"
    assert pairs["--encoder-preset"] == "slow"
    assert "--encoder-tune" not in pairs


def test_subtitle_scan_default_when_no_subs_selected():
    args = build_handbrake_args(
        device="/dev/sr0",
        title_index=1,
        output_path="/o.mp4",
        audio_tracks=[1],
        subtitle_tracks=[],
        encoder_mode=EncoderMode.QUALITY,
    )
    pairs = _args_to_pairs(args)
    assert pairs["--subtitle"] == "scan"
    assert pairs["--subtitle-burned"] == "scan"
    assert pairs["--subtitle-default"] == "scan"


def test_explicit_subtitles_use_comma_list():
    args = build_handbrake_args(
        device="/dev/sr0",
        title_index=2,
        output_path="/o.mp4",
        audio_tracks=[1, 2],
        subtitle_tracks=[1, 3],
        encoder_mode=EncoderMode.QUALITY,
    )
    pairs = _args_to_pairs(args)
    assert pairs["--subtitle"] == "1,3"
    # Two source tracks -> four output tracks: each gets stereo AAC + passthrough.
    assert pairs["--audio"] == "1,1,2,2"
    assert pairs["--aencoder"] == "ca_aac,copy,ca_aac,copy"
    assert pairs["--mixdown"] == "stereo,none,stereo,none"


def test_empty_audio_defaults_to_track_one():
    args = build_handbrake_args(
        device="/dev/sr0",
        title_index=1,
        output_path="/o.mp4",
        audio_tracks=[],
        subtitle_tracks=[],
        encoder_mode=EncoderMode.QUALITY,
    )
    pairs = _args_to_pairs(args)
    # Defaults to track 1, still doubled into stereo + passthrough.
    assert pairs["--audio"] == "1,1"
    assert pairs["--aencoder"] == "ca_aac,copy"
