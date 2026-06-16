from pathlib import Path

from app.library import Library
from app.models import ContentType, JobStatus, RipRecord


def _lib(tmp_path) -> Library:
    lib = Library(str(tmp_path / "library.db"))
    lib.init_db()
    return lib


def _movie(title="Batman Begins", year=2005, **kw) -> RipRecord:
    base = dict(
        content_type=ContentType.MOVIE,
        title=title,
        year=year,
        output_path=f"/output/Movies/{title} ({year})/{title} ({year}).mp4",
        status=JobStatus.DONE,
    )
    base.update(kw)
    return RipRecord(**base)


def test_record_and_list_roundtrip(tmp_path):
    lib = _lib(tmp_path)
    lib.record_rip(_movie(file_size_bytes=1234, rip_seconds=600))
    rows = lib.list_rips()
    assert len(rows) == 1
    assert rows[0].title == "Batman Begins"
    assert rows[0].year == 2005
    assert rows[0].file_size_bytes == 1234
    assert rows[0].created_at is not None


def test_list_newest_first(tmp_path):
    lib = _lib(tmp_path)
    lib.record_rip(_movie(title="A", created_at="2026-01-01T00:00:00+00:00"))
    lib.record_rip(_movie(title="B", created_at="2026-02-01T00:00:00+00:00"))
    titles = [r.title for r in lib.list_rips()]
    assert titles == ["B", "A"]


def test_list_excludes_unsuccessful_by_default(tmp_path):
    lib = _lib(tmp_path)
    lib.record_rip(_movie(title="Good", status=JobStatus.DONE))
    lib.record_rip(_movie(title="Bad", status=JobStatus.FAILED, error="boom"))
    assert [r.title for r in lib.list_rips()] == ["Good"]
    all_titles = {r.title for r in lib.list_rips(include_unsuccessful=True)}
    assert all_titles == {"Good", "Bad"}


def _tv(show="Deadwood", season=1, episode=1, **kw) -> RipRecord:
    base = dict(
        content_type=ContentType.TV,
        title=show,
        season=season,
        episode=episode,
        output_path=f"/output/TV Shows/{show}/Season {season:02d}/{show} S{season:02d}E{episode:02d}.mp4",
        status=JobStatus.DONE,
    )
    base.update(kw)
    return RipRecord(**base)


def test_find_existing_by_tmdb_id(tmp_path):
    lib = _lib(tmp_path)
    lib.record_rip(_movie(title="Whatever", year=1999, tmdb_id=603))
    hits = lib.find_existing(content_type=ContentType.MOVIE, tmdb_id=603,
                             title="different name", year=2050)
    assert len(hits) == 1 and hits[0].tmdb_id == 603


def test_find_existing_movie_by_title_year_case_insensitive(tmp_path):
    lib = _lib(tmp_path)
    lib.record_rip(_movie(title="Top Gun", year=1986))
    hits = lib.find_existing(content_type=ContentType.MOVIE, title="top gun", year=1986)
    assert len(hits) == 1
    miss = lib.find_existing(content_type=ContentType.MOVIE, title="Top Gun", year=2022)
    assert miss == []


def test_find_existing_tv_tuple(tmp_path):
    lib = _lib(tmp_path)
    lib.record_rip(_tv(show="Deadwood", season=1, episode=2))
    hit = lib.find_existing(content_type=ContentType.TV, title="deadwood", season=1, episode=2)
    assert len(hit) == 1
    miss = lib.find_existing(content_type=ContentType.TV, title="Deadwood", season=1, episode=3)
    assert miss == []


def test_find_existing_ignores_failed_and_other_type(tmp_path):
    lib = _lib(tmp_path)
    lib.record_rip(_movie(title="Dune", year=2021, status=JobStatus.FAILED))
    assert lib.find_existing(content_type=ContentType.MOVIE, title="Dune", year=2021) == []
