"""Pydantic models shared across the API and worker layers."""

from __future__ import annotations

from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, Field


class EncoderMode(str, Enum):
    QUALITY = "quality"  # x265 CPU, CRF 20, slow preset
    SPEED = "speed"      # nvenc_h265, CRF 22, slow preset


class ContentType(str, Enum):
    MOVIE = "movie"
    TV = "tv"


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
    audio_tracks: list[AudioTrack] = Field(default_factory=list)
    subtitle_tracks: list[SubtitleTrack] = Field(default_factory=list)
    is_main_feature: bool = False


class ScanResult(BaseModel):
    dvd_label: str
    titles: list[Title]


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


class RipRequest(BaseModel):
    dvd_label: str
    encoder_mode: EncoderMode = EncoderMode.QUALITY
    content_type: ContentType
    movie: Optional[MovieMetadata] = None
    tv: Optional[TVMetadata] = None
    titles: list[TitleSelection]


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
