import pytest

from app import hostenv
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


def _build(**overrides):
    """build_handbrake_args with sensible test defaults; override per test."""
    kwargs = dict(
        device="/dev/sr0",
        title_index=1,
        output_path="/o.mp4",
        audio_tracks=[1],
        subtitle_tracks=[],
        burn_forced=True,
        encoder_mode=EncoderMode.QUALITY,
    )
    kwargs.update(overrides)
    return build_handbrake_args(**kwargs)


def test_quality_mode_uses_x264():
    args = _build(output_path="/output/movie/title_01.mp4")
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


def test_speed_mode_uses_nvenc(monkeypatch):
    # Pinned rather than read from the host: the suite must assert the same thing
    # on the Linux rip box and on a dev Mac, which has no NVENC.
    monkeypatch.setattr(hostenv, "SPEED_ENCODER", "nvenc_h264")
    args = _build(encoder_mode=EncoderMode.SPEED)
    pairs = _args_to_pairs(args)
    assert pairs["--encoder"] == "nvenc_h264"
    assert pairs["--quality"] == "22"
    assert pairs["--encoder-preset"] == "slow"
    assert "--encoder-tune" not in pairs


def test_speed_mode_rejected_when_host_has_no_hardware_encoder(monkeypatch):
    """No NVENC (macOS) -> raise, never silently fall back to x264.

    A silent downgrade would hand the user a slow CPU rip they didn't ask for,
    indistinguishable from a hang.
    """
    monkeypatch.setattr(hostenv, "SPEED_ENCODER", None)
    with pytest.raises(ValueError, match="Speed mode is unavailable"):
        _build(encoder_mode=EncoderMode.SPEED)


def test_quality_mode_unaffected_by_missing_hardware_encoder(monkeypatch):
    monkeypatch.setattr(hostenv, "SPEED_ENCODER", None)
    pairs = _args_to_pairs(_build(encoder_mode=EncoderMode.QUALITY))
    assert pairs["--encoder"] == "x264"


def test_burn_forced_with_no_soft_tracks():
    """burn_forced + no selected tracks -> Foreign Audio Search, burned, no default flag."""
    args = _build(subtitle_tracks=[], burn_forced=True)
    pairs = _args_to_pairs(args)
    assert pairs["--subtitle"] == "scan"
    assert pairs["--subtitle-burned"] == "scan"
    assert "--subtitle-default" not in pairs


def test_burn_forced_plus_soft_english():
    """The real default: burn forced AND soft-embed the English track."""
    args = _build(subtitle_tracks=[3], burn_forced=True)
    pairs = _args_to_pairs(args)
    # scan (forced) first, then the soft English track.
    assert pairs["--subtitle"] == "scan,3"
    assert pairs["--subtitle-burned"] == "scan"


def test_soft_subtitles_without_forced_scan():
    """burn_forced off -> selected tracks soft-muxed, NO scan pre-pass, nothing burned."""
    args = _build(subtitle_tracks=[1, 3], burn_forced=False)
    pairs = _args_to_pairs(args)
    assert pairs["--subtitle"] == "1,3"
    assert "--subtitle-burned" not in pairs
    assert "scan" not in pairs.get("--subtitle", "")


def test_no_subtitles_at_all():
    """No selected tracks and no forced burn -> no subtitle args."""
    args = _build(subtitle_tracks=[], burn_forced=False)
    assert "--subtitle" not in args
    assert "--subtitle-burned" not in args


def test_empty_audio_defaults_to_track_one():
    args = _build(audio_tracks=[])
    pairs = _args_to_pairs(args)
    # Defaults to track 1, still doubled into stereo + passthrough.
    assert pairs["--audio"] == "1,1"
    assert pairs["--aencoder"] == "ca_aac,copy"


def test_mkv_input_uses_file_not_device():
    args = build_handbrake_args(
        device="/dev/sr0",
        title_index=7,
        output_path="/o.mp4",
        audio_tracks=[1],
        subtitle_tracks=[],
        burn_forced=True,
        encoder_mode=EncoderMode.QUALITY,
        input_file="/data/scratch/title.mkv",
    )
    pairs = _args_to_pairs(args)
    # MKV input overrides the device + title number (MKV has a single title).
    assert pairs["--input"] == "/data/scratch/title.mkv"
    assert pairs["--title"] == "1"
    assert "/dev/sr0" not in args


def test_mp4_output_uses_av_mp4_with_faststart():
    args = _build(output_path="/out/Movie.mp4", subtitle_tracks=[1], burn_forced=False)
    assert _args_to_pairs(args)["--format"] == "av_mp4"
    assert "--optimize" in args


def test_mkv_output_uses_av_mkv_and_no_optimize():
    # Blu-ray path: .mkv output -> av_mkv container, faststart dropped, PGS subs
    # still soft-muxed so they ride along selectable.
    args = build_handbrake_args(
        device="/dev/sr0", title_index=1, output_path="/out/Movie.mkv",
        audio_tracks=[1], subtitle_tracks=[1], burn_forced=True,
        encoder_mode=EncoderMode.QUALITY, input_file="/data/scratch/t.mkv",
    )
    pairs = _args_to_pairs(args)
    assert pairs["--format"] == "av_mkv"
    assert "--optimize" not in args         # MP4-only
    assert pairs["--subtitle"] == "scan,1"  # forced burn + soft English embed
    assert pairs["--subtitle-burned"] == "scan"
