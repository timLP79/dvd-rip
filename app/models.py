"""Pydantic models shared across the API and worker layers."""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field


class EncoderMode(str, Enum):
    QUALITY = "quality"  # x264 CPU, CRF 20, slow preset
    SPEED = "speed"      # nvenc_h264, CQ 22, slow preset


class ContentType(str, Enum):
    MOVIE = "movie"
    TV = "tv"
    MUSIC = "music"
    AUDIOBOOK = "audiobook"


class AudioTrack(BaseModel):
    index: int
    language: Optional[str] = None
    codec: Optional[str] = None
    channels: Optional[str] = None
    description: Optional[str] = None


class SubtitleTrack(BaseModel):
    index: int
    language: Optional[str] = None
    description: Optional[str] = None


class Title(BaseModel):
    index: int
    duration_seconds: int
    chapter_count: int
    frame_width: int
    frame_height: int
    aspect: Optional[str] = None   # "widescreen" | "fullscreen" | None
    audio_tracks: list[AudioTrack] = Field(default_factory=list)
    subtitle_tracks: list[SubtitleTrack] = Field(default_factory=list)
    is_main_feature: bool = False


class CDTrack(BaseModel):
    number: int
    duration_seconds: int
    title: Optional[str] = None


class MBRelease(BaseModel):
    """One MusicBrainz release candidate matched by exact disc ID."""
    artist: str
    album: str
    year: Optional[int] = None
    disc_position: Optional[int] = None   # which disc this is (1-based)
    total_discs: Optional[int] = None     # media count of the release
    is_audiobook: bool = False            # MB release-group secondary type = Audiobook/Spokenword
    tracks: list[CDTrack] = Field(default_factory=list)


class ScanResult(BaseModel):
    dvd_label: str
    titles: list[Title] = Field(default_factory=list)
    disc_format: Literal["dvd", "bluray", "cd"] = "dvd"
    # Audio-CD fields (empty for video discs):
    tracks: list[CDTrack] = Field(default_factory=list)
    disc_id: Optional[str] = None
    musicbrainz: list[MBRelease] = Field(default_factory=list)


class MovieMetadata(BaseModel):
    """Metadata for a movie disc. One movie per disc; multi-part = multiple files."""
    title: str
    year: Optional[int] = None
    tmdb_id: Optional[int] = None


class TVMetadata(BaseModel):
    """Metadata for a TV disc. One show, one season per disc; episodes per title."""
    show: str
    season: int
    tmdb_id: Optional[int] = None


class TitleSelection(BaseModel):
    """A title the user wants to rip, plus its track and metadata mapping.

    For Movies: `part_index` is 1-based; part 1 = main file, part 2+ = `- cd2` etc.
    For TV:     `episode_number` is required; `episode_title` is optional metadata.
    """
    index: int
    audio_tracks: list[int]
    subtitle_tracks: list[int]
    part_index: Optional[int] = None       # movie multi-part numbering
    episode_number: Optional[int] = None   # TV episode number
    episode_title: Optional[str] = None    # TV episode title (informational only;
                                           # not used in filename per user's convention)
    aspect: Optional[str] = None   # "widescreen" | "fullscreen" | None (from the scanned title)


class RipRequest(BaseModel):
    dvd_label: str
    encoder_mode: EncoderMode = EncoderMode.QUALITY
    disc_format: Literal["dvd", "bluray"] = "dvd"
    # Run Foreign Audio Search and burn forced/foreign-dialogue subtitles into the
    # picture. Default on for correctness; uncheck for plain-English discs to skip
    # the slow scan pre-pass (selected subtitle tracks are still embedded either way).
    burn_forced_subtitles: bool = True
    content_type: ContentType
    movie: Optional[MovieMetadata] = None
    tv: Optional[TVMetadata] = None
    titles: list[TitleSelection]


class CDTrackSelection(BaseModel):
    number: int
    title: str = ""
    include: bool = True
    duration_seconds: Optional[int] = None   # enables real-time (bytes-based) rip progress


class CDRipRequest(BaseModel):
    mode: Literal["music", "audiobook"]
    disc_id: Optional[str] = None
    creator: str                      # Artist (music) / Author (audiobook)
    title: str                        # Album (music) / Book (audiobook)
    year: Optional[int] = None
    disc_number: int = 1
    total_discs: Optional[int] = None
    split_tracks: bool = False        # audiobook: split into per-track MP3s (MusicBrainz titles)
    tracks: list[CDTrackSelection]


# --- TMDB search results -----------------------------------------------------

class TMDBMovieResult(BaseModel):
    id: int
    title: str
    year: Optional[int] = None
    poster_url: Optional[str] = None
    overview: Optional[str] = None


class TMDBTVResult(BaseModel):
    id: int
    name: str
    year: Optional[int] = None  # first air year
    poster_url: Optional[str] = None
    overview: Optional[str] = None


class TMDBEpisode(BaseModel):
    season_number: int
    episode_number: int
    name: Optional[str] = None
    air_date: Optional[str] = None
    runtime: Optional[int] = None


# --- Web Push subscriptions --------------------------------------------------

class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscription(BaseModel):
    """A browser PushManager subscription, as POSTed by the client."""
    endpoint: str
    keys: PushSubscriptionKeys


class PushUnsubscribe(BaseModel):
    endpoint: str


# --- Job tracking ------------------------------------------------------------

class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(BaseModel):
    id: str
    title_index: int
    output_path: str
    status: JobStatus = JobStatus.QUEUED
    percent: float = 0.0
    fps: Optional[float] = None
    eta_seconds: Optional[int] = None
    error: Optional[str] = None
    phase: Optional[str] = None


class RipRecord(BaseModel):
    """A persisted rip ledger entry. Mirrors the `rips` SQLite table."""
    id: Optional[int] = None
    created_at: Optional[str] = None          # ISO-8601; set at record time if None
    content_type: ContentType
    title: str                                 # movie title OR show name
    artist: Optional[str] = None               # Artist/Author for music/audiobook rows
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    tmdb_id: Optional[int] = None
    output_path: str
    encoder_mode: Optional[EncoderMode] = None
    burn_forced: bool = True
    file_size_bytes: Optional[int] = None
    rip_seconds: Optional[int] = None
    status: JobStatus = JobStatus.DONE
    error: Optional[str] = None
    source: str = "rip"                        # "rip" | "backfill"
    disc_format: str = "dvd"
    aspect: Optional[str] = None
