# Movie Library Database — Design

## Goal

Add a persistent record of every rip the app performs, serving three user-facing
features that the current in-memory job list and the filesystem cannot:

1. **Duplicate warning** — when the user picks a title that's already been ripped
   successfully, warn them *before* starting. **Non-blocking**: it never prevents
   a rip; the user can always proceed (e.g. to re-rip from a better disc or with
   different settings).
2. **Rip history / audit log** — a durable record of every rip attempt, including
   failures and cancellations, surviving container restarts and rebuilds.
3. **In-app catalog** — a separate **Library** page listing everything ripped,
   without opening Plex.

These are three views over one thing: a persistent table of rip records.

## Constraints & context

- Single-user, single-container app. One rip at a time (one worker).
- No persistence exists today — jobs live in an in-memory dict, trimmed to 50, lost
  on restart.
- Plex already indexes the media files; this database is *not* a media server. It's
  a rip ledger the app owns.
- Data volume is modest: tens to low-hundreds of records over the life of a library.

## Approach: SQLite

SQLite (Python stdlib `sqlite3`, no new dependency) over a JSON file or a
filesystem-scan-only approach, because it's the only option that cleanly serves all
three features — queryable for dedup and catalog sorting/filtering, and able to
record history (dates, encoder, duration, failures) that the filesystem doesn't
retain. One-writer (the ripper worker) / many-reader (API) access fits SQLite well.

## Storage location

`./data/library.db`, exposed to the container via a new bind mount `./data:/data`
(env `LIBRARY_DB=/data/library.db`). Kept deliberately **out of the `/output` tree**
so Plex never attempts to scan a `.db` file as media. The `./data` directory is
gitignored (like `./output`). Survives rebuilds because it's host-mounted.

## Architecture

```
ripper worker ──record_rip()──▶ app/library.py ──▶ /data/library.db
                                       ▲
FastAPI  /api/library        ──────────┤  list_rips()
         /api/library/check  ──────────┘  find_existing()
              ▲
Browser: Library page (separate route) + dedup warning on the rip page
```

### `app/library.py` — persistence boundary

Owns the SQLite connection and is the *only* module that touches SQL. Interface:

- `init_db(path)` — create the table if absent; run startup backfill.
- `record_rip(record: RipRecord) -> None` — insert a terminal-state rip.
- `list_rips(*, include_unsuccessful: bool = False) -> list[RipRecord]` — newest first.
- `find_existing(*, content_type, tmdb_id, title, year, season, episode) -> list[RipRecord]`
  — successful rips matching the identity of a pick (see Dedup matching). For TV,
  `title` carries the show name (same as the `title` column).

Each operation opens a fresh `sqlite3` connection (open → act → close). This avoids
cross-thread sharing concerns entirely; at this data volume the per-call overhead is
negligible, and writes only come from the single worker.

### Data model — `rips` table

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | autoincrement |
| `created_at` | TEXT (ISO-8601) | when the rip reached a terminal state |
| `content_type` | TEXT | `movie` \| `tv` |
| `title` | TEXT | movie title or show name |
| `year` | INTEGER NULL | movie year |
| `season` | INTEGER NULL | tv |
| `episode` | INTEGER NULL | tv |
| `tmdb_id` | INTEGER NULL | when known |
| `output_path` | TEXT | absolute path written |
| `encoder_mode` | TEXT | `quality` \| `speed` |
| `burn_forced` | INTEGER | 0/1 |
| `file_size_bytes` | INTEGER NULL | set on success |
| `rip_seconds` | INTEGER NULL | wall-clock duration, set on success |
| `status` | TEXT | `done` \| `failed` \| `cancelled` |
| `error` | TEXT NULL | failure detail |
| `source` | TEXT | `rip` (normal) \| `backfill` (imported from disk) |

A `RipRecord` Pydantic model in `app/models.py` mirrors these fields for typed
transport between the ripper, library, and API.

## Data flow

### Write path

The ripper records a row when a job reaches a terminal state. On `done` it stamps
`file_size_bytes` (from the output file) and `rip_seconds` (worker wall-clock from
job start). On `failed`/`cancelled` it records the status and any `error`. The
ripper gains a dependency on `library.record_rip`; recording failures is best-effort
and must never crash the worker (wrapped, logged on error).

### Dedup (feature 1) — non-blocking

When the user selects metadata on the rip page (TMDB pick or manual entry), the
frontend calls `GET /api/library/check` with the identity of the selection. The
endpoint calls `find_existing()` and returns any matching **successful** rips.

**Matching precedence:**
- If `tmdb_id` is present on both → match on `tmdb_id`.
- Else movie → match on case-insensitive `title` + `year`.
- Else tv → match on case-insensitive `title` (show name) + `season` + `episode`.

If matches exist, the UI shows a non-blocking notice next to Start Rip:
"⚠ Already ripped: Batman Begins (2005) — 2026-06-10. Rip anyway?" The Start Rip
button remains fully enabled. No server-side enforcement; this is advisory only.

### Catalog (features 2 & 4)

`GET /api/library?include_unsuccessful=<bool>` returns records newest-first. A new
**Library** page (own route, linked from the header) renders a table: title /
year (or show SxxExx), date ripped, file size, encoder mode, and — when the audit
toggle is on — failed/cancelled rows with their error. Read-only; no edit/delete in
this iteration.

### Backfill

On startup, `init_db` walks `/output` for `*.mp4` files. For any whose `output_path`
isn't already in the table, it inserts a `status=done, source=backfill` record,
parsing title/year (movies) or show/season/episode (tv) from the Plex path and
reading `file_size_bytes` from disk. `tmdb_id`, `encoder_mode`, `rip_seconds` are
null for backfilled rows (unknown). This makes the existing library catalogued and
dedup-aware immediately. Idempotent: re-running skips paths already present.

## API additions (`app/main.py`)

- `GET /api/library?include_unsuccessful=false` → `list[RipRecord]`
- `GET /api/library/check?content_type=&tmdb_id=&title=&year=&season=&episode=`
  (for TV, `title` is the show name) → `{ "matches": [RipRecord, ...] }`
- `GET /library` → renders the Library page template.

## Error handling

- DB unavailable / write error: logged; the rip itself still succeeds (the ledger is
  secondary to producing the file). `record_rip` failures never propagate to the worker.
- Backfill parse failures on an oddly-named file: skip that file with a debug log;
  never abort startup.
- `/api/library/check` on an empty/missing DB: returns `{ "matches": [] }`.

## Testing

- `tests/test_library.py` — against a temp SQLite file:
  - `record_rip` + `list_rips` round-trip; ordering newest-first.
  - `list_rips(include_unsuccessful)` filters failures correctly.
  - `find_existing` matching precedence: tmdb_id, title+year, tv tuple; no false match
    across content types; case-insensitive title.
  - Backfill path parsing for movie and tv Plex layouts; idempotency.
- `tests/test_api.py` additions — `/api/library` and `/api/library/check` shapes,
  including the empty-DB case.
- No real DVD drive or network required, consistent with the existing suite.

## Out of scope (YAGNI)

- Editing or deleting records from the UI.
- Search / filter / pagination on the Library page (simple newest-first list for now).
- Physical-collection inventory (owned-but-not-ripped tracking).
- Re-rip automation or "rip the rest of the season" flows.
- Exporting the library / external sync.

## File layout

```
dvd-rip/
├── app/
│   ├── library.py        # NEW — SQLite persistence boundary
│   ├── models.py         # + RipRecord
│   ├── ripper.py         # records rips on terminal state
│   ├── main.py           # + /api/library, /api/library/check, /library
│   ├── templates/
│   │   └── library.html  # NEW — catalog page
│   └── static/
│       └── library.js    # NEW — fetch + render the catalog
├── data/                 # NEW — gitignored; holds library.db
├── docker-compose.yml    # + ./data:/data mount, LIBRARY_DB env
├── Dockerfile            # + LIBRARY_DB default
└── tests/
    └── test_library.py   # NEW
```
