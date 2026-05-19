"""HandBrakeCLI argument construction for Plex-optimized DVD rips.

Two encoder modes:

- QUALITY: x265 CPU, CRF 20, slow preset, grain tune. Best quality-per-byte for DVD source.
- SPEED:   nvenc_h265 (Nvidia GPU), CRF 22, slow preset. Faster but larger files at the same
           perceptual quality.

Common settings target Plex playback:
- AAC stereo + AC3 5.1 passthrough (Plex direct-plays both on virtually every client)
- Decomb deinterlace (DVD content is frequently interlaced)
- Auto crop + automatic anamorphic to preserve correct aspect
- Chapter markers, MP4 with faststart for instant playback
- Burn-in forced subtitles, soft-include the rest
"""

from __future__ import annotations

from typing import Iterable

from .models import EncoderMode


def _comma_join(values: Iterable[int]) -> str:
    return ",".join(str(v) for v in values)


def build_handbrake_args(
    *,
    device: str,
    title_index: int,
    output_path: str,
    audio_tracks: list[int],
    subtitle_tracks: list[int],
    encoder_mode: EncoderMode,
) -> list[str]:
    """Build the HandBrakeCLI argv for a single rip job.

    Audio/subtitle indices are 1-based per HandBrake's convention.
    """
    if not audio_tracks:
        # HandBrake requires at least one audio track selection; default to track 1.
        audio_tracks = [1]

    args: list[str] = [
        "HandBrakeCLI",
        "--input", device,
        "--title", str(title_index),
        "--output", output_path,
        "--format", "av_mp4",
        "--optimize",                 # MP4 faststart
        "--markers",                  # chapter markers
        "--decomb", "default",        # adaptive deinterlace
        "--crop-mode", "auto",
        "--auto-anamorphic",
        "--audio", _comma_join(audio_tracks),
        # Per-track encoder list: stereo AAC tracks get encoded, multi-channel passes through.
        # Repeating ca_aac means: encode each selected track with AAC; HandBrake will fall back
        # to copying when the source is AC3/DTS and we list `copy:ac3,copy:dts` via fallback.
        "--aencoder", ",".join(["ca_aac"] * len(audio_tracks)),
        "--ab", ",".join(["160"] * len(audio_tracks)),
        "--mixdown", ",".join(["stereo"] * len(audio_tracks)),
        "--arate", ",".join(["auto"] * len(audio_tracks)),
        "--audio-fallback", "ac3",
    ]

    if subtitle_tracks:
        args += ["--subtitle", _comma_join(subtitle_tracks)]
    else:
        # 'scan' detects forced subtitles in the primary audio language and burns them in.
        args += [
            "--subtitle", "scan",
            "--subtitle-burned", "scan",
            "--subtitle-default", "scan",
        ]

    if encoder_mode is EncoderMode.QUALITY:
        args += [
            "--encoder", "x265",
            "--encoder-preset", "slow",
            "--encoder-tune", "grain",
            "--quality", "20",
        ]
    elif encoder_mode is EncoderMode.SPEED:
        args += [
            "--encoder", "nvenc_h265",
            "--encoder-preset", "slow",
            "--quality", "22",
        ]
    else:  # pragma: no cover - exhaustive via Enum
        raise ValueError(f"Unknown encoder mode: {encoder_mode}")

    return args
