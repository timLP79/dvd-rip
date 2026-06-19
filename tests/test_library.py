from pathlib import Path

from app.library import Library
from app.models import ContentType, JobStatus, RipRecord, PushSubscription


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


def test_disc_format_roundtrips(tmp_path):
    lib = _lib(tmp_path)
    lib.record_rip(_movie(title="A DVD", disc_format="dvd"))
    lib.record_rip(_movie(title="A BD", disc_format="bluray"))
    by_title = {r.title: r.disc_format for r in lib.list_rips()}
    assert by_title["A DVD"] == "dvd"
    assert by_title["A BD"] == "bluray"


def test_disc_format_column_migrates_on_existing_db(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    # Simulate a pre-disc_format DB: create the table without the column.
    conn = sqlite3.connect(db)
    conn.executescript(
        """CREATE TABLE rips (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            content_type TEXT NOT NULL, title TEXT NOT NULL, year INTEGER,
            season INTEGER, episode INTEGER, tmdb_id INTEGER, output_path TEXT NOT NULL,
            encoder_mode TEXT, burn_forced INTEGER NOT NULL DEFAULT 1,
            file_size_bytes INTEGER, rip_seconds INTEGER, status TEXT NOT NULL,
            error TEXT, source TEXT NOT NULL DEFAULT 'rip');
        INSERT INTO rips (created_at, content_type, title, output_path, status)
            VALUES ('2026-01-01T00:00:00+00:00', 'movie', 'Old Movie',
                    '/output/Movies/Old Movie/Old Movie.mp4', 'done');"""
    )
    conn.commit()
    conn.close()

    lib = Library(str(db))
    lib.init_db()  # must ALTER the table to add disc_format, defaulting old rows to 'dvd'
    lib.record_rip(_movie(title="New BD", disc_format="bluray"))
    by_title = {r.title: r.disc_format for r in lib.list_rips()}
    assert by_title["Old Movie"] == "dvd"
    assert by_title["New BD"] == "bluray"


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


def test_backfill_parses_movie_and_tv(tmp_path):
    out = tmp_path / "output"
    movie = out / "Movies" / "Open Range (2003)" / "Open Range (2003).mp4"
    tv = out / "TV Shows" / "Deadwood" / "Season 01" / "Deadwood S01E03.mp4"
    for f in (movie, tv):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * 10)

    lib = _lib(tmp_path)
    added = lib.backfill(str(out))
    assert added == 2

    rows = {r.title: r for r in lib.list_rips()}
    assert rows["Open Range"].year == 2003
    assert rows["Open Range"].source == "backfill"
    assert rows["Open Range"].file_size_bytes == 10
    assert rows["Deadwood"].season == 1 and rows["Deadwood"].episode == 3


def test_backfill_is_idempotent(tmp_path):
    out = tmp_path / "output"
    movie = out / "Movies" / "Top Gun (1986)" / "Top Gun (1986).mp4"
    movie.parent.mkdir(parents=True, exist_ok=True)
    movie.write_bytes(b"x")
    lib = _lib(tmp_path)
    assert lib.backfill(str(out)) == 1
    assert lib.backfill(str(out)) == 0
    assert len(lib.list_rips()) == 1


def test_backfill_missing_dir_is_noop(tmp_path):
    lib = _lib(tmp_path)
    assert lib.backfill(str(tmp_path / "nope")) == 0


def test_find_existing_returns_empty_on_insufficient_identity(tmp_path):
    lib = _lib(tmp_path)
    lib.record_rip(_movie(title="Heat", year=1995))
    lib.record_rip(_tv(show="Deadwood", season=1, episode=1))
    # movie with no title and no tmdb_id -> can't claim a dup
    assert lib.find_existing(content_type=ContentType.MOVIE) == []
    # tv missing episode -> can't claim a dup (no silent NULL match)
    assert lib.find_existing(content_type=ContentType.TV, title="Deadwood", season=1) == []


def test_push_subscription_crud(tmp_path):
    lib = _lib(tmp_path)
    lib.add_subscription("https://push.example/abc", "p256-key", "auth-key")
    subs = lib.list_subscriptions()
    assert len(subs) == 1
    assert isinstance(subs[0], PushSubscription)
    assert subs[0].endpoint == "https://push.example/abc"
    assert subs[0].keys.p256dh == "p256-key"
    assert subs[0].keys.auth == "auth-key"


def test_push_subscription_upsert_is_idempotent(tmp_path):
    lib = _lib(tmp_path)
    lib.add_subscription("https://push.example/abc", "k1", "a1")
    lib.add_subscription("https://push.example/abc", "k2", "a2")  # same endpoint
    subs = lib.list_subscriptions()
    assert len(subs) == 1
    assert subs[0].keys.p256dh == "k2"


def test_push_subscription_remove(tmp_path):
    lib = _lib(tmp_path)
    lib.add_subscription("https://push.example/abc", "k", "a")
    lib.remove_subscription("https://push.example/abc")
    assert lib.list_subscriptions() == []


def test_aspect_roundtrips(tmp_path):
    lib = _lib(tmp_path)
    lib.record_rip(_movie(title="Wide", aspect="widescreen"))
    lib.record_rip(_movie(title="Full", aspect="fullscreen"))
    by_title = {r.title: r.aspect for r in lib.list_rips()}
    assert by_title["Wide"] == "widescreen"
    assert by_title["Full"] == "fullscreen"


def test_aspect_defaults_none(tmp_path):
    lib = _lib(tmp_path)
    lib.record_rip(_movie(title="Legacy"))
    assert lib.list_rips()[0].aspect is None


def test_aspect_migration_on_preexisting_db(tmp_path):
    import sqlite3
    db = str(tmp_path / "library.db")
    # The rips schema AS IT IS TODAY (before this task), i.e. no `aspect` column.
    conn = sqlite3.connect(db)
    conn.executescript(
        """CREATE TABLE rips (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
            content_type TEXT NOT NULL, title TEXT NOT NULL, year INTEGER, season INTEGER,
            episode INTEGER, tmdb_id INTEGER, output_path TEXT NOT NULL, encoder_mode TEXT,
            burn_forced INTEGER NOT NULL DEFAULT 1, file_size_bytes INTEGER, rip_seconds INTEGER,
            status TEXT NOT NULL, error TEXT, source TEXT NOT NULL DEFAULT 'rip',
            disc_format TEXT NOT NULL DEFAULT 'dvd');"""
    )
    conn.commit(); conn.close()
    lib = Library(db); lib.init_db()                 # must ALTER ADD COLUMN aspect
    lib.record_rip(_movie(title="Migrated", aspect="widescreen"))
    assert lib.list_rips()[0].aspect == "widescreen"
