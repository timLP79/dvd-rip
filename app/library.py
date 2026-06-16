"""SQLite persistence for the rip ledger.

The only module that touches SQL. Each operation opens a fresh connection
(open -> act -> close) to avoid cross-thread sharing concerns; writes come only
from the single ripper worker and reads are light.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import ContentType, JobStatus, RipRecord

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    content_type TEXT NOT NULL,
    title TEXT NOT NULL,
    year INTEGER,
    season INTEGER,
    episode INTEGER,
    tmdb_id INTEGER,
    output_path TEXT NOT NULL,
    encoder_mode TEXT,
    burn_forced INTEGER NOT NULL DEFAULT 1,
    file_size_bytes INTEGER,
    rip_seconds INTEGER,
    status TEXT NOT NULL,
    error TEXT,
    source TEXT NOT NULL DEFAULT 'rip'
);
"""

_MOVIE_DIR_RE = re.compile(r"^(?P<title>.+?)(?: \((?P<year>\d{4})\))?$")
_TV_FILE_RE = re.compile(r"^(?P<show>.+) S(?P<season>\d+)E(?P<episode>\d+)$", re.IGNORECASE)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_record(row: sqlite3.Row) -> RipRecord:
    return RipRecord(
        id=row["id"],
        created_at=row["created_at"],
        content_type=row["content_type"],
        title=row["title"],
        year=row["year"],
        season=row["season"],
        episode=row["episode"],
        tmdb_id=row["tmdb_id"],
        output_path=row["output_path"],
        encoder_mode=row["encoder_mode"],
        burn_forced=bool(row["burn_forced"]),
        file_size_bytes=row["file_size_bytes"],
        rip_seconds=row["rip_seconds"],
        status=row["status"],
        error=row["error"],
        source=row["source"],
    )


def parse_output_path(path: Path, output_root: Path) -> Optional[RipRecord]:
    """Build a backfill RipRecord from a Plex-layout mp4 path, or None if unparseable."""
    try:
        rel = path.relative_to(output_root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) >= 2 and parts[0] == "Movies":
        m = _MOVIE_DIR_RE.match(parts[1])
        if not m:
            return None
        year = int(m.group("year")) if m.group("year") else None
        return RipRecord(
            content_type=ContentType.MOVIE,
            title=m.group("title").strip(),
            year=year,
            output_path=str(path),
            status=JobStatus.DONE,
            source="backfill",
        )
    if len(parts) >= 4 and parts[0] == "TV Shows":
        m = _TV_FILE_RE.match(path.stem)
        if not m:
            return None
        return RipRecord(
            content_type=ContentType.TV,
            title=m.group("show").strip(),
            season=int(m.group("season")),
            episode=int(m.group("episode")),
            output_path=str(path),
            status=JobStatus.DONE,
            source="backfill",
        )
    return None


class Library:
    def __init__(self, db_path: str):
        self.db_path = db_path

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    def record_rip(self, record: RipRecord) -> None:
        ct = record.content_type.value if isinstance(record.content_type, ContentType) else record.content_type
        enc = record.encoder_mode.value if record.encoder_mode is not None and hasattr(record.encoder_mode, "value") else record.encoder_mode
        status = record.status.value if isinstance(record.status, JobStatus) else record.status
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO rips (created_at, content_type, title, year, season,
                       episode, tmdb_id, output_path, encoder_mode, burn_forced,
                       file_size_bytes, rip_seconds, status, error, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    record.created_at or _now_iso(),
                    ct, record.title, record.year, record.season, record.episode,
                    record.tmdb_id, record.output_path, enc,
                    1 if record.burn_forced else 0,
                    record.file_size_bytes, record.rip_seconds, status,
                    record.error, record.source,
                ),
            )

    def list_rips(self, *, include_unsuccessful: bool = False) -> list[RipRecord]:
        q = "SELECT * FROM rips"
        params: list = []
        if not include_unsuccessful:
            q += " WHERE status = ?"
            params.append(JobStatus.DONE.value)
        q += " ORDER BY datetime(created_at) DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [_row_to_record(r) for r in rows]

    def find_existing(
        self,
        *,
        content_type,
        tmdb_id: Optional[int] = None,
        title: Optional[str] = None,
        year: Optional[int] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> list[RipRecord]:
        ct = content_type.value if isinstance(content_type, ContentType) else content_type
        clauses = ["status = ?", "content_type = ?"]
        params: list = [JobStatus.DONE.value, ct]
        if tmdb_id is not None:
            clauses.append("tmdb_id = ?")
            params.append(tmdb_id)
        elif ct == "movie":
            if not title:
                return []
            clauses.append("LOWER(title) = LOWER(?)")
            params.append(title)
            if year is not None:
                clauses.append("year = ?")
                params.append(year)
        elif ct == "tv":
            if not title or season is None or episode is None:
                return []
            clauses.append("LOWER(title) = LOWER(?)")
            params.append(title)
            clauses.append("season = ?")
            params.append(season)
            clauses.append("episode = ?")
            params.append(episode)
        else:
            return []
        q = "SELECT * FROM rips WHERE " + " AND ".join(clauses) + \
            " ORDER BY datetime(created_at) DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [_row_to_record(r) for r in rows]

    def _existing_paths(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT output_path FROM rips").fetchall()
        return {r["output_path"] for r in rows}

    def backfill(self, output_root: str) -> int:
        root = Path(output_root)
        if not root.exists():
            return 0
        existing = self._existing_paths()
        added = 0
        for mp4 in sorted(root.rglob("*.mp4")):
            if str(mp4) in existing:
                continue
            record = parse_output_path(mp4, root)
            if record is None:
                log.debug("backfill: could not parse %s", mp4)
                continue
            try:
                record.file_size_bytes = mp4.stat().st_size
            except OSError:
                pass
            self.record_rip(record)
            added += 1
        if added:
            log.info("backfill: added %d existing file(s) to the library", added)
        return added
