# Movie Library Database Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent SQLite ledger of every rip, powering a non-blocking duplicate warning, an audit log, and a separate in-app Library catalog page.

**Architecture:** A single `app/library.py` module owns all SQLite access (open→act→close per call). The ripper records a row when a job reaches a terminal state. FastAPI exposes `/api/library` and `/api/library/check`; a new `/library` page renders the catalog. On startup the DB is created and existing `/output` files are backfilled.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, `sqlite3` (stdlib), vanilla JS + Jinja2. Tests via pytest against a temp SQLite file (no real DVD drive or network).

**Spec:** `docs/superpowers/specs/2026-06-15-movie-library-database-design.md`

---

## File Structure

- `app/models.py` — **modify**: add `RipRecord`.
- `app/library.py` — **create**: `Library` class (init_db, record_rip, list_rips, find_existing, backfill) + path-parsing helpers.
- `app/ripper.py` — **modify**: hold a `Library`, store a per-job `RipRecord` template at enqueue, record it on terminal state.
- `app/main.py` — **modify**: construct `Library` in lifespan, init + backfill, add `/api/library`, `/api/library/check`, `/library` routes.
- `app/templates/library.html` — **create**: catalog page.
- `app/static/library.js` — **create**: fetch + render catalog.
- `app/static/app.js` — **modify**: dedup warning on metadata pick.
- `app/templates/index.html` — **modify**: add a Library link + a dup-warning element.
- `docker-compose.yml`, `Dockerfile`, `.gitignore` — **modify**: `./data:/data` mount, `LIBRARY_DB` env.
- `tests/test_library.py` — **create**.
- `tests/test_api.py` — **modify**: library endpoint tests.

---

## Task 1: Config & volume plumbing

**Files:**
- Modify: `Dockerfile`, `docker-compose.yml`, `.gitignore`

- [ ] **Step 1: Add `LIBRARY_DB` default to the Dockerfile env block**

In `Dockerfile`, find the `ENV` block that sets `DVD_DEVICE=/dev/sr0 ...` and add `LIBRARY_DB`:

```dockerfile
ENV DVD_DEVICE=/dev/sr0 \
    OUTPUT_ROOT=/output \
    HOST=0.0.0.0 \
    PORT=8000 \
    PUID=1000 \
    PGID=1000 \
    CDROM_GID=24 \
    TMDB_API_KEY="" \
    LIBRARY_DB=/data/library.db
```

- [ ] **Step 2: Add the data volume + env to docker-compose.yml**

In `docker-compose.yml`, under `volumes:` add the data mount, and under `environment:` add `LIBRARY_DB`:

```yaml
    volumes:
      - "${DVD_DEVICE:-/dev/sr0}:/dev/sr0"
      - "${OUTPUT_DIR:-./output}:/output"
      - "./data:/data"
    environment:
      DVD_DEVICE: /dev/sr0
      OUTPUT_ROOT: /output
      LIBRARY_DB: /data/library.db
      PUID: "${PUID:-1000}"
      PGID: "${PGID:-1000}"
      CDROM_GID: "${CDROM_GID:-24}"
      TMDB_API_KEY: "${TMDB_API_KEY:-}"
```

(The existing `volumes`/`environment` keys already have the other entries — add only the new lines shown.)

- [ ] **Step 3: Gitignore the data dir**

In `.gitignore`, add below the `output/` line:

```
data/
```

- [ ] **Step 4: Ensure the entrypoint chowns /data**

In `scripts/entrypoint.sh`, find the line `chown -R "$PUID:$PGID" "${OUTPUT_ROOT:-/output}" 2>/dev/null || true` and add a matching line for the DB dir right after it:

```bash
mkdir -p "$(dirname "${LIBRARY_DB:-/data/library.db}")"
chown -R "$PUID:$PGID" "$(dirname "${LIBRARY_DB:-/data/library.db}")" 2>/dev/null || true
```

- [ ] **Step 5: Commit**

```bash
git add Dockerfile docker-compose.yml .gitignore scripts/entrypoint.sh
git commit -m "feat(library): add /data volume and LIBRARY_DB config"
```

---

## Task 2: `RipRecord` model

**Files:**
- Modify: `app/models.py`
- Test: `tests/test_library.py`

- [ ] **Step 1: Add the model**

Append to `app/models.py` (after the existing `Job` model):

```python
class RipRecord(BaseModel):
    """A persisted rip ledger entry. Mirrors the `rips` SQLite table."""
    id: Optional[int] = None
    created_at: Optional[str] = None          # ISO-8601; set at record time if None
    content_type: ContentType
    title: str                                 # movie title OR show name
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
```

- [ ] **Step 2: Verify it imports**

Run: `.venv/bin/python -c "from app.models import RipRecord; print(RipRecord(content_type='movie', title='X', output_path='/x.mp4').status)"`
Expected: `JobStatus.DONE`

- [ ] **Step 3: Commit**

```bash
git add app/models.py
git commit -m "feat(library): add RipRecord model"
```

---

## Task 3: `Library` — init, record, list

**Files:**
- Create: `app/library.py`
- Test: `tests/test_library.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_library.py`:

```python
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
    assert rows[0].created_at is not None  # auto-stamped


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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_library.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.library'`

- [ ] **Step 3: Implement `app/library.py` (init/record/list)**

Create `app/library.py`:

```python
"""SQLite persistence for the rip ledger.

The only module that touches SQL. Each operation opens a fresh connection
(open -> act -> close) to avoid cross-thread sharing concerns; writes come only
from the single ripper worker and reads are light.
"""

from __future__ import annotations

import logging
import sqlite3
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


class Library:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

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
        if not include_unsuccessful:
            q += " WHERE status = 'done'"
        q += " ORDER BY datetime(created_at) DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(q).fetchall()
        return [_row_to_record(r) for r in rows]
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_library.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add app/library.py tests/test_library.py
git commit -m "feat(library): SQLite init, record_rip, list_rips"
```

---

## Task 4: `Library.find_existing` (dedup query)

**Files:**
- Modify: `app/library.py`, `tests/test_library.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_library.py`:

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_library.py -k find_existing -q`
Expected: FAIL — `AttributeError: 'Library' object has no attribute 'find_existing'`

- [ ] **Step 3: Implement `find_existing`**

Add this method to the `Library` class in `app/library.py` (below `list_rips`):

```python
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
        clauses = ["status = 'done'", "content_type = ?"]
        params: list = [ct]
        if tmdb_id is not None:
            clauses.append("tmdb_id = ?")
            params.append(tmdb_id)
        elif ct == "movie":
            clauses.append("LOWER(title) = LOWER(?)")
            params.append(title or "")
            if year is not None:
                clauses.append("year = ?")
                params.append(year)
        else:  # tv
            clauses.append("LOWER(title) = LOWER(?)")
            params.append(title or "")
            clauses.append("season = ?")
            params.append(season)
            clauses.append("episode = ?")
            params.append(episode)
        q = "SELECT * FROM rips WHERE " + " AND ".join(clauses) + \
            " ORDER BY datetime(created_at) DESC, id DESC"
        with self._connect() as conn:
            rows = conn.execute(q, params).fetchall()
        return [_row_to_record(r) for r in rows]
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_library.py -q`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
git add app/library.py tests/test_library.py
git commit -m "feat(library): find_existing dedup query"
```

---

## Task 5: Backfill from `/output`

**Files:**
- Modify: `app/library.py`, `tests/test_library.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_library.py`:

```python
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
    assert lib.backfill(str(out)) == 0          # second run adds nothing
    assert len(lib.list_rips()) == 1


def test_backfill_missing_dir_is_noop(tmp_path):
    lib = _lib(tmp_path)
    assert lib.backfill(str(tmp_path / "nope")) == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_library.py -k backfill -q`
Expected: FAIL — `AttributeError: 'Library' object has no attribute 'backfill'`

- [ ] **Step 3: Implement path parsing + backfill**

Add to the top level of `app/library.py` (after `_row_to_record`):

```python
import re

_MOVIE_DIR_RE = re.compile(r"^(?P<title>.+?)(?: \((?P<year>\d{4})\))?$")
_TV_FILE_RE = re.compile(r"^(?P<show>.+) S(?P<season>\d+)E(?P<episode>\d+)$", re.IGNORECASE)


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
    if len(parts) >= 2 and parts[0] == "TV Shows":
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
```

Then add this method to the `Library` class:

```python
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
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_library.py -q`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add app/library.py tests/test_library.py
git commit -m "feat(library): backfill existing output files"
```

---

## Task 6: Ripper records each terminal job

**Files:**
- Modify: `app/ripper.py`

- [ ] **Step 1: Import + accept a Library, add a records dict**

In `app/ripper.py`, update the imports to add `os`, `time`, and `RipRecord`/`Library`:

```python
import asyncio
import logging
import os
import re
import secrets
import shlex
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator, Deque, Optional
```

Add to the model import block (`from .models import (...)`) the name `RipRecord`. Add a new import line after the paths import:

```python
from .library import Library
```

In `Ripper.__init__`, add a `library` parameter and two new dict fields:

```python
    def __init__(self, *, device: str, output_root: Path, library: Optional[Library] = None):
        self.device = device
        self.output_root = output_root
        self.library = library
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._jobs: dict[str, Job] = {}
        self._job_order: list[str] = []
        self._selections: dict[str, tuple[TitleSelection, EncoderMode, str, bool]] = {}
        self._records: dict[str, RipRecord] = {}
        self._subscribers: dict[str, list[asyncio.Queue[Job]]] = {}
        self._current_process: Optional[asyncio.subprocess.Process] = None
        self._current_job_id: Optional[str] = None
        self._worker_task: Optional[asyncio.Task[None]] = None
```

- [ ] **Step 2: Build a RipRecord template at enqueue**

In `enqueue`, immediately before `self._queue.put_nowait(job_id)`, add:

```python
        self._records[job_id] = RipRecord(
            content_type=content_type,
            title=(movie.title if content_type is ContentType.MOVIE else tv.show),  # type: ignore[union-attr]
            year=(movie.year if movie else None),
            season=(tv.season if tv else None),
            episode=(selection.episode_number if content_type is ContentType.TV else None),
            tmdb_id=(movie.tmdb_id if movie else (tv.tmdb_id if tv else None)),
            output_path=str(output_path),
            encoder_mode=encoder_mode,
            burn_forced=burn_forced,
            status=JobStatus.QUEUED,
        )
```

- [ ] **Step 3: Record on terminal state**

In `_run_job`, capture a start time and write the record on completion. Replace the body from `self._update(job_id, status=JobStatus.RUNNING, percent=0.0)` through the terminal branches with:

```python
        self._update(job_id, status=JobStatus.RUNNING, percent=0.0)
        started = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._current_process = proc
        self._current_job_id = job_id
        tail: Deque[str] = deque(maxlen=25)
        try:
            await self._consume_progress(job_id, proc, tail)
            rc = await proc.wait()
        finally:
            self._current_process = None
            self._current_job_id = None

        elapsed = int(time.monotonic() - started)
        if rc == 0:
            self._update(job_id, status=JobStatus.DONE, percent=100.0)
            self._save_record(job_id, JobStatus.DONE, rip_seconds=elapsed)
        else:
            cancelled = rc < 0 or self._jobs[job_id].status == JobStatus.CANCELLED
            _cleanup_partial(output_path)
            if cancelled:
                self._update(job_id, status=JobStatus.CANCELLED)
                self._save_record(job_id, JobStatus.CANCELLED)
            else:
                detail = "\n".join(tail).strip()
                message = f"HandBrakeCLI exited with code {rc}"
                if detail:
                    message += f"\n{detail}"
                self._update(job_id, status=JobStatus.FAILED, error=message)
                self._save_record(job_id, JobStatus.FAILED, error=message)
```

- [ ] **Step 4: Add the `_save_record` helper**

Add this method to the `Ripper` class (next to `_update`). It reads the output
path from the stored record, so it works from both the terminal branches *and*
the worker's exception handler (where the local `output_path` isn't in scope):

```python
    def _save_record(
        self,
        job_id: str,
        status: JobStatus,
        *,
        rip_seconds: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Persist a finished job to the library. Best-effort: never crash the worker."""
        if self.library is None:
            return
        record = self._records.get(job_id)
        if record is None:
            return
        record.status = status
        record.error = error
        record.rip_seconds = rip_seconds
        if status is JobStatus.DONE:
            try:
                record.file_size_bytes = os.path.getsize(record.output_path)
            except OSError:
                record.file_size_bytes = None
        try:
            self.library.record_rip(record)
        except Exception:
            log.exception("failed to record rip %s to library", job_id)
```

- [ ] **Step 4b: Also record worker-level failures**

A failure *before* the terminal branches (e.g. `HandBrakeCLI` missing, or any
exception in `_run_job`) is caught by `_worker` and must also be recorded, or the
audit log would miss it. In `_worker`, update the `except` handler:

```python
            try:
                await self._run_job(job_id)
            except Exception as exc:
                log.exception("Unhandled error in rip job %s", job_id)
                self._update(job_id, status=JobStatus.FAILED, error=str(exc))
                self._save_record(job_id, JobStatus.FAILED, error=str(exc))
```

- [ ] **Step 5: Trim records alongside selections**

In `_trim_finished`, find the loop body that pops `self._selections.pop(jid, None)` and add a sibling line:

```python
            self._selections.pop(jid, None)
            self._records.pop(jid, None)
```

- [ ] **Step 6: Run the full suite (ripper still imports & passes)**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (existing 53 + new library tests)

- [ ] **Step 7: Commit**

```bash
git add app/ripper.py
git commit -m "feat(library): ripper records each terminal job"
```

---

## Task 7: API — lifespan init, /api/library, /api/library/check

**Files:**
- Modify: `app/main.py`, `tests/test_api.py`

- [ ] **Step 1: Add failing API tests**

Append to `tests/test_api.py` (the `client` fixture already sets `OUTPUT_ROOT`/`DVD_DEVICE`; the lifespan will create the DB under a temp `LIBRARY_DB` set here):

```python
def test_library_empty_initially(client):
    res = client.get("/api/library")
    assert res.status_code == 200
    assert res.json() == []


def test_library_check_no_match_on_empty(client):
    res = client.get("/api/library/check", params={
        "content_type": "movie", "title": "Nothing", "year": 1999})
    assert res.status_code == 200
    assert res.json()["matches"] == []


def test_recorded_rip_appears_via_api(client):
    """Write through the Library (same DB the app opened), read back via the API.

    Deterministic — avoids depending on a real HandBrake subprocess or worker timing.
    Both the test's connection and the app's point at the same LIBRARY_DB file.
    """
    import os
    from app.library import Library
    from app.models import ContentType, JobStatus, RipRecord

    lib = Library(os.environ["LIBRARY_DB"])
    lib.record_rip(RipRecord(
        content_type=ContentType.MOVIE, title="Top Gun", year=1986,
        output_path="/output/Movies/Top Gun (1986)/Top Gun (1986).mp4",
        status=JobStatus.DONE, file_size_bytes=123,
    ))

    rows = client.get("/api/library").json()
    assert any(r["title"] == "Top Gun" for r in rows)

    chk = client.get("/api/library/check",
                     params={"content_type": "movie", "title": "Top Gun", "year": 1986})
    assert len(chk.json()["matches"]) == 1
```

- [ ] **Step 2: Set `LIBRARY_DB` in the test fixture**

In `tests/test_api.py`, in the `client` fixture, add a `LIBRARY_DB` env var alongside the existing `monkeypatch.setenv` calls, before the module reload:

```python
    monkeypatch.setenv("OUTPUT_ROOT", str(tmp_path / "output"))
    monkeypatch.setenv("DVD_DEVICE", "/dev/null")
    monkeypatch.setenv("TMDB_API_KEY", "")
    monkeypatch.setenv("LIBRARY_DB", str(tmp_path / "library.db"))
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_api.py -k library -q`
Expected: FAIL — 404 (routes not defined yet)

- [ ] **Step 4: Wire the Library into main.py**

In `app/main.py`:

(a) Add imports near the others:

```python
from .library import Library
from .models import RipRecord
```

Add the config constant after `OUTPUT_ROOT`:

```python
LIBRARY_DB = os.getenv("LIBRARY_DB", "/data/library.db")
```

(b) In the `lifespan` function, construct + init + backfill the Library and pass it to the Ripper:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    library = Library(LIBRARY_DB)
    library.init_db()
    try:
        library.backfill(str(OUTPUT_ROOT))
    except Exception:
        logging.getLogger(__name__).exception("library backfill failed")
    ripper = Ripper(device=DVD_DEVICE, output_root=OUTPUT_ROOT, library=library)
    await ripper.start()
    tmdb = TMDBClient()
    app.state.ripper = ripper
    app.state.tmdb = tmdb
    app.state.library = library
    try:
        yield
    finally:
        await ripper.stop()
        await tmdb.aclose()
```

(c) Add the endpoints (place after `api_rip`):

```python
@app.get("/api/library", response_model=list[RipRecord])
async def api_library(request: Request, include_unsuccessful: bool = False) -> list[RipRecord]:
    library: Library = request.app.state.library
    return library.list_rips(include_unsuccessful=include_unsuccessful)


@app.get("/api/library/check")
async def api_library_check(
    request: Request,
    content_type: Literal["movie", "tv"] = Query(...),
    tmdb_id: Optional[int] = None,
    title: Optional[str] = None,
    year: Optional[int] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> dict:
    library: Library = request.app.state.library
    matches = library.find_existing(
        content_type=content_type, tmdb_id=tmdb_id, title=title,
        year=year, season=season, episode=episode,
    )
    return {"matches": [m.model_dump() for m in matches]}
```

Add `from typing import Optional` to the imports if not present (it isn't currently — `Literal` is imported; add `Optional`):

```python
from typing import Literal, Optional
```

- [ ] **Step 5: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_api.py -q`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (all)

- [ ] **Step 7: Commit**

```bash
git add app/main.py tests/test_api.py
git commit -m "feat(library): lifespan init/backfill + /api/library endpoints"
```

---

## Task 8: Library catalog page

**Files:**
- Create: `app/templates/library.html`, `app/static/library.js`
- Modify: `app/main.py` (route), `app/templates/index.html` (nav link), `app/static/style.css` (reuse existing classes; add minimal table styles if needed)

- [ ] **Step 1: Add the `/library` route to main.py**

After the `index` route in `app/main.py`:

```python
@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "library.html", {"asset_version": ASSET_VERSION}
    )
```

- [ ] **Step 2: Create `app/templates/library.html`**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>DVD Rip — Library</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/static/style.css?v={{ asset_version }}" />
</head>
<body>
  <header class="topbar">
    <h1 class="brand-title">DVD Rip</h1>
    <nav class="topbar-meta">
      <a class="nav-link" href="/">Rip</a>
      <a class="nav-link active" href="/library">Library</a>
    </nav>
    <div class="topbar-actions">
      <label class="opt"><input type="checkbox" id="show-failed" /> <span>Show failed/cancelled</span></label>
    </div>
  </header>

  <main>
    <section class="panel">
      <div class="section-head"><h2>Library</h2><span id="lib-count" class="rip-status"></span></div>
      <table class="titles">
        <thead>
          <tr><th>Title</th><th>Ripped</th><th>Size</th><th>Encoder</th><th>Status</th></tr>
        </thead>
        <tbody id="library-body"></tbody>
      </table>
    </section>
  </main>

  <script src="/static/library.js?v={{ asset_version }}"></script>
</body>
</html>
```

- [ ] **Step 3: Create `app/static/library.js`**

```javascript
'use strict';

const $ = (s, r = document) => r.querySelector(s);

function fmtSize(bytes) {
  if (!bytes) return '—';
  const gb = bytes / 1024 / 1024 / 1024;
  return gb >= 1 ? `${gb.toFixed(2)} GB` : `${(bytes / 1024 / 1024).toFixed(0)} MB`;
}

function fmtDate(iso) {
  if (!iso) return '—';
  return iso.slice(0, 10);
}

function label(r) {
  if (r.content_type === 'tv') {
    const s = String(r.season ?? 0).padStart(2, '0');
    const e = String(r.episode ?? 0).padStart(2, '0');
    return `${r.title} S${s}E${e}`;
  }
  return r.year ? `${r.title} (${r.year})` : r.title;
}

async function load() {
  const includeFailed = $('#show-failed').checked;
  const res = await fetch(`/api/library?include_unsuccessful=${includeFailed}`);
  const rows = res.ok ? await res.json() : [];
  const body = $('#library-body');
  body.replaceChildren();
  for (const r of rows) {
    const tr = document.createElement('tr');
    const cells = [label(r), fmtDate(r.created_at), fmtSize(r.file_size_bytes),
                   r.encoder_mode || '—', r.status];
    for (const c of cells) {
      const td = document.createElement('td');
      td.textContent = c;
      tr.appendChild(td);
    }
    if (r.status !== 'done') tr.classList.add('row-muted');
    body.appendChild(tr);
  }
  $('#lib-count').textContent = `${rows.length} title(s)`;
}

document.addEventListener('DOMContentLoaded', () => {
  $('#show-failed').addEventListener('change', load);
  load();
});
```

- [ ] **Step 4: Add nav-link + row-muted styles**

Append to `app/static/style.css`:

```css
.nav-link {
  color: var(--text-muted);
  text-decoration: none;
  font-size: 13px;
  padding: 4px 2px;
  border-bottom: 2px solid transparent;
}
.nav-link:hover { color: var(--text); }
.nav-link.active { color: var(--text-strong); border-bottom-color: var(--accent); }
.row-muted td { color: var(--text-faint); }
```

- [ ] **Step 5: Add a Library link to the rip page header**

In `app/templates/index.html`, replace the `topbar-meta` block's opening so the nav links appear. Find:

```html
    <div class="topbar-meta">
      <span class="meta-cell"><span class="meta-label">Device</span> <code>{{ device }}</code></span>
```

and insert the nav links just inside, before the first `meta-cell`:

```html
    <div class="topbar-meta">
      <a class="nav-link active" href="/">Rip</a>
      <a class="nav-link" href="/library">Library</a>
      <span class="meta-cell"><span class="meta-label">Device</span> <code>{{ device }}</code></span>
```

- [ ] **Step 6: Manual smoke test**

Run a local dev server with a temp DB and confirm the page renders:

```bash
LIBRARY_DB=/tmp/lib-smoke.db OUTPUT_ROOT=./output DVD_DEVICE=/dev/null \
  .venv/bin/python -c "
from fastapi.testclient import TestClient
import importlib, app.main as m; importlib.reload(m)
with TestClient(m.app) as c:
    assert c.get('/library').status_code == 200
    assert 'Library' in c.get('/library').text
    print('library page OK; api rows:', len(c.get('/api/library').json()))
"
```
Expected: `library page OK; api rows: ...`

- [ ] **Step 7: Commit**

```bash
git add app/main.py app/templates/library.html app/static/library.js app/static/style.css app/templates/index.html
git commit -m "feat(library): catalog page + nav"
```

---

## Task 9: Dedup warning on the rip page

**Files:**
- Modify: `app/templates/index.html`, `app/static/app.js`

- [ ] **Step 1: Add a warning element to the rip options area**

In `app/templates/index.html`, find the `<div class="rip-options">` block and add a warning span right before it:

```html
      <div id="dup-warning" class="dup-warning" hidden></div>

      <div class="rip-options">
```

- [ ] **Step 2: Style it**

Append to `app/static/style.css`:

```css
.dup-warning {
  margin-top: 14px;
  padding: 10px 14px;
  background: rgba(217, 156, 58, 0.10);
  border: 1px solid rgba(217, 156, 58, 0.4);
  border-left: 2px solid var(--warning);
  border-radius: var(--radius);
  color: var(--text);
  font-size: 13px;
}
```

- [ ] **Step 3: Call the check when metadata is chosen**

In `app/static/app.js`, add a function and call it from `paintSelectedMeta` (which runs whenever a movie/show is picked). Add near the other fetch helpers:

```javascript
async function checkDuplicate() {
  const el = $('#dup-warning');
  el.hidden = true;
  if (!state.selectedMeta) return;
  const m = state.selectedMeta;
  const params = new URLSearchParams({ content_type: m.type });
  if (m.tmdb_id) params.set('tmdb_id', m.tmdb_id);
  if (m.type === 'movie') {
    params.set('title', m.title);
    if (m.year) params.set('year', m.year);
  } else {
    params.set('title', m.show);
    params.set('season', m.season);
  }
  // For TV, dedup is per-episode; check happens per selected title at rip time is
  // overkill — here we surface a show/season-level note only when a movie matches.
  try {
    const res = await fetch(`/api/library/check?${params.toString()}`);
    if (!res.ok) return;
    const { matches } = await res.json();
    if (matches && matches.length) {
      const when = (matches[0].created_at || '').slice(0, 10);
      el.textContent = `⚠ Already ripped: ${m.type === 'movie' ? (m.title + (m.year ? ` (${m.year})` : '')) : m.show} — ${when}. You can still rip it again.`;
      el.hidden = false;
    }
  } catch (_) { /* advisory only */ }
}
```

At the end of `paintSelectedMeta()` (after `el.hidden = false;` and the surrounding logic), add a call:

```javascript
  checkDuplicate();
```

Also, in the existing "change" handler inside `paintSelectedMeta` (the `change.addEventListener('click', ...)` that sets `state.selectedMeta = null`), hide the warning so a stale notice doesn't linger after clearing the pick — add this line inside that click handler:

```javascript
    $('#dup-warning').hidden = true;
```

- [ ] **Step 4: Manual smoke test (movie dedup)**

```bash
LIBRARY_DB=/tmp/lib-dup.db OUTPUT_ROOT=./output DVD_DEVICE=/dev/null \
  .venv/bin/python -c "
from fastapi.testclient import TestClient
import importlib, app.main as m; importlib.reload(m)
with TestClient(m.app) as c:
    # backfill imported ./output movies at startup; check one of them:
    r = c.get('/api/library/check', params={'content_type':'movie','title':'Top Gun','year':1986})
    print('matches:', len(r.json()['matches']))
"
```
Expected: `matches: 1` (Top Gun was ripped earlier; backfill caught it). If your `./output` differs, substitute a title you have.

- [ ] **Step 5: Commit**

```bash
git add app/templates/index.html app/static/app.js app/static/style.css
git commit -m "feat(library): non-blocking duplicate warning on rip page"
```

---

## Task 10: Docs + full verification

**Files:**
- Modify: `README.md`, `CLAUDE.md`

- [ ] **Step 1: Update CLAUDE.md module map**

In `CLAUDE.md`, add a row to the module map table (after the `ripper.py` row):

```markdown
| `library.py` | SQLite rip ledger — `record_rip`/`list_rips`/`find_existing` + `/output` backfill |
```

And add to the container plumbing notes that a `./data:/data` volume holds `library.db`.

- [ ] **Step 2: Update README**

Add a short "Library" section to `README.md` describing: the catalog page at `/library`, the non-blocking duplicate warning, that history persists in `./data/library.db`, and that existing files are backfilled on first start.

```markdown
## Library

The app keeps a persistent record of every rip in `./data/library.db` (SQLite):

- **Library page** (`/library`) — everything you've ripped, newest first, with a
  toggle to show failed/cancelled attempts.
- **Duplicate warning** — when you pick a title you've already ripped, the rip page
  warns you. It never blocks — you can always re-rip (e.g. from a better disc).
- **Backfill** — on first start, existing files under `./output` are imported so the
  catalog and duplicate check work immediately.
```

- [ ] **Step 3: Full suite green**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (all)

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs(library): document the rip ledger, catalog page, and dedup"
```

- [ ] **Step 5: Build + deploy (only if no rip is running)**

Check first: `curl -fsS http://localhost:8000/api/jobs` — confirm no `running`/`queued` jobs. Then:

```bash
make build && make restart
```

Verify live: open `http://localhost:8000/library`, confirm your existing movies are listed; on the Rip page, pick an already-ripped movie and confirm the warning appears.

---

## Notes for the implementer

- **Run as a single worker.** Do not add uvicorn `--workers`; the Ripper and Library
  hold in-process state.
- **Best-effort recording.** A library write failure must never fail a rip — the file
  is the product, the ledger is secondary (already wrapped in `_save_record`).
- **Backfill matches the Plex paths** produced by `app/paths.py`: `Movies/Title (Year)/...`
  and `TV Shows/Show/Season NN/Show SNNENN.mp4`. If those conventions change, update
  `parse_output_path`.
- **TV dedup** is per-episode in `find_existing`; the rip-page warning only surfaces the
  movie case (per the spec's "warn when I pick the movie"). Episode-level warnings can be
  added later if wanted.
