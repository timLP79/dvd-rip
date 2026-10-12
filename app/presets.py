"""HandBrakeCLI argument construction for Plex-optimized DVD rips.

Two encoder modes, both H.264 for universal Plex direct-play (the user's clients
are an Android phone + Chromecasts, which transcode HEVC):

- QUALITY: x264 CPU, CRF 20, slow preset. Transparent for SD; fast on a modern CPU.
- SPEED:   nvenc_h264 (Nvidia GPU), CQ 22, slow preset. Near-instant; fine for TVs.
           Only on hosts where HandBrake reports usable NVENC; otherwise Speed
           mode is rejected rather than silently downgraded (see hostenv).

H.264 is chosen over H.265 deliberately: at DVD resolution (480p) HEVC's
compression edge shrinks to ~20-30%, while H.264 8-bit is the one format every
Plex client direct-plays without a server-side transcode.

Common settings target Plex playback:
- AAC stereo + AC3 5.1 passthrough (Plex direct-plays both on virtually every client)
- Decomb deinterlace (DVD content is frequently interlaced)
- Auto crop + automatic anamorphic to preserve correct aspect
- Chapter markers; MP4 with faststart for DVD, MKV for Blu-ray (so PGS subtitles
  can ride along as soft/selectable tracks — MP4 can't carry them). The container
  is chosen from the output path's extension.
- Burn-in forced subtitles, soft-include the rest
"""

from __future__ import annotations

from typing import Iterable, Optional

from . import hostenv
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
    burn_forced: bool,
    encoder_mode: EncoderMode,
    input_file: Optional[str] = None,
) -> list[str]:
    """Build the HandBrakeCLI argv for a single rip job.

    Audio/subtitle indices are 1-based per HandBrake's convention.

    `subtitle_tracks` are soft-muxed (selectable, off by default). When
    `burn_forced` is set, HandBrake's Foreign Audio Search ("scan") is prepended
    and the forced track it finds is burned into the picture.
    """
    if not audio_tracks:
        # HandBrake requires at least one audio track selection; default to track 1.
        audio_tracks = [1]

    # For each selected source track we emit TWO output tracks so Plex has both a
    # universally-compatible stereo track and the original surround mix:
    #   1. ca_aac / stereo   — plays on every client (phones, browsers, TVs)
    #   2. copy / none       — passthrough of the original AC3/DTS surround,
    #                          falling back to AC3 encode if the source codec
    #                          can't live in an MP4 (--audio-fallback ac3)
    # The stereo track is listed first so it's the default; clients (or Plex's
    # per-device "preferred audio" setting) can select the surround track.
    audio_out: list[int] = []      # source track per output track (duplicated)
    aencoders: list[str] = []
    mixdowns: list[str] = []
    bitrates: list[str] = []
    arates: list[str] = []
    for t in audio_tracks:
        audio_out += [t, t]
        aencoders += ["ca_aac", "copy"]
        mixdowns += ["stereo", "none"]
        bitrates += ["160", "160"]   # bitrate is ignored for the copy/passthrough track
        arates += ["auto", "auto"]

    # Container follows the output extension: Blu-ray rips are .mkv so the disc's
    # PGS (bitmap) subtitles can ride along as soft/selectable tracks — MP4 can't
    # carry PGS. --optimize (faststart) is MP4-only; MKV needs neither.
    is_mkv = output_path.lower().endswith(".mkv")
    container = "av_mkv" if is_mkv else "av_mp4"

    args: list[str] = [
        "HandBrakeCLI",
        *( ["--input", input_file, "--title", "1"]
           if input_file is not None
           else ["--input", device, "--title", str(title_index)] ),
        "--output", output_path,
        "--format", container,
        *(["--optimize"] if not is_mkv else []),  # MP4 faststart
        "--markers",                  # chapter markers
        "--decomb", "default",        # adaptive deinterlace
        "--crop-mode", "auto",
        "--auto-anamorphic",
        "--audio", _comma_join(audio_out),
        "--aencoder", ",".join(aencoders),
        "--ab", ",".join(bitrates),
        "--mixdown", ",".join(mixdowns),
        "--arate", ",".join(arates),
        "--audio-fallback", "ac3",
    ]

    # Subtitle handling:
    #   - burn_forced: prepend Foreign Audio Search ("scan") and burn the forced
    #     foreign-dialogue track into the picture so it always shows (and
    #     direct-plays on clients that can't render image subtitles).
    #   - subtitle_tracks: soft-mux the selected disc tracks (e.g. full English)
    #     as selectable, off-by-default tracks.
    sub_spec: list[str] = []
    if burn_forced:
        sub_spec.append("scan")
    sub_spec += [str(s) for s in subtitle_tracks]
    if sub_spec:
        args += ["--subtitle", ",".join(sub_spec)]
        if burn_forced:
            args += ["--subtitle-burned", "scan"]

    if encoder_mode is EncoderMode.QUALITY:
        args += [
            "--encoder", "x264",
            "--encoder-preset", "slow",
            "--quality", "20",
        ]
    elif encoder_mode is EncoderMode.SPEED:
        # No usable NVENC on this host — see hostenv.initialize_encoders.
        # Fail loudly instead of silently substituting x264: the user picked Speed
        # expecting a fast GPU rip, and a silent swap would just look like a hang.
        if hostenv.SPEED_ENCODER is None:
            raise ValueError(
                "Speed mode is unavailable on this host (no supported hardware "
                "encoder); use Quality mode."
            )
        args += [
            "--encoder", hostenv.SPEED_ENCODER,
            "--encoder-preset", "slow",
            "--quality", "22",
        ]
    else:  # pragma: no cover - exhaustive via Enum
        raise ValueError(f"Unknown encoder mode: {encoder_mode}")

    return args
