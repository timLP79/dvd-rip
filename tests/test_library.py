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
