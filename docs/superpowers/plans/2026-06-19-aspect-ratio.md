# Aspect Ratio (Full screen / Widescreen) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect each title's display aspect (Full screen / Widescreen), show it per-title on the rip page, capture it into the library ledger, and show + filter it in the library.

**Architecture:** A shared `classify_aspect` helper turns HandBrake `Geometry.PAR` (DVD) or frame dims (Blu-ray) into a `"widescreen"`/`"fullscreen"`/`None` label. The label rides on the `Title` model from the scan, into the rip request (`TitleSelection`), into the persisted `RipRecord` (+ a new `rips.aspect` column), and onto both the rip page and library page.

**Tech Stack:** FastAPI + Pydantic, SQLite, Jinja2 + vanilla JS (no build step), pytest.

## Global Constraints

- Detection is server-side, one threshold in one place: `classify_aspect(width, height, par_num=1, par_den=1)` returns `"widescreen"` if display aspect `(width*par_num)/(height*par_den) >= 1.56`, else `"fullscreen"`; `None` if width/height/par_den is missing or zero. PAR absent ⇒ defaults to 1:1.
- Stored value is the **label string** (`"widescreen"`/`"fullscreen"`/null), so the library filter is a plain string match like `type`/`format`.
- **No backfill** — existing rows have `aspect = NULL`, render as "—", and are excluded when filtering to a specific aspect. Library aspect filter **defaults to All**.
- Rip page: the titles-table **"Frame" column header becomes "Aspect"**; the cell shows the label with the raw `WIDTH×HEIGHT` as a hover (`title`) tooltip.
- `app/library.py` is the only module that executes SQL. Schema changes use the existing `ALTER TABLE … ADD COLUMN` migration pattern (mirrors `disc_format`).
- No JS test harness — frontend coverage is structural (served HTML/JS markers) + manual; backend is unit-tested.
- Commit after each task with the project trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and the `Claude-Session:` line.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `app/dvd.py` | modify | `classify_aspect` helper; set `Title.aspect` from PAR in scan parsing. |
| `app/bluray.py` | modify | import `classify_aspect`; set `Title.aspect` from dims (PAR 1:1). |
| `app/models.py` | modify | `aspect` field on `Title`, `TitleSelection`, `RipRecord`. |
| `app/library.py` | modify | `aspect` column + migration + record/read. |
| `app/ripper.py` | modify | store `selection.aspect` in the `RipRecord`. |
| `app/templates/index.html` | modify | rename Frame → Aspect header. |
| `app/static/app.js` | modify | render aspect label + resolution tooltip; send `aspect` in rip payload. |
| `app/templates/library.html` | modify | Aspect column + `#lib-aspect` filter. |
| `app/static/library.js` | modify | `aspectLabel`, Aspect cell, filter wiring, clear reset, colspan 7. |
| `tests/test_dvd.py`, `test_library.py`, `test_ripper.py`, `test_pwa.py` | modify | coverage. |

Dependency order: **Task 1 → Task 2 → (Task 3, Task 4)**. Tasks 3 and 4 are independent of each other but both need 1 (scan aspect) and 2 (persisted aspect). `app/main.py` needs no change (scan/rip endpoints forward the augmented models as-is).

---

### Task 1: `classify_aspect` + scan detection

**Files:**
- Modify: `app/dvd.py`, `app/bluray.py`, `app/models.py`
- Test: `tests/test_dvd.py`

**Interfaces:**
- Produces: `app.dvd.classify_aspect(width: int, height: int, par_num: int = 1, par_den: int = 1) -> Optional[str]`; `Title.aspect: Optional[str]` populated by both scanners.

- [ ] **Step 1: Write failing tests** — append to `tests/test_dvd.py`:

```python
from app.dvd import classify_aspect


def test_classify_aspect_widescreen_anamorphic():
    assert classify_aspect(720, 480, 32, 27) == "widescreen"   # DAR 1.78

def test_classify_aspect_fullscreen_anamorphic():
    assert classify_aspect(720, 480, 8, 9) == "fullscreen"     # DAR 1.33

def test_classify_aspect_square_ntsc_is_fullscreen():
    assert classify_aspect(720, 480, 1, 1) == "fullscreen"     # DAR 1.5 < 1.56

def test_classify_aspect_bluray_dims():
    assert classify_aspect(1920, 1080) == "widescreen"          # DAR 1.78, default PAR 1:1

def test_classify_aspect_missing_dims_is_none():
    assert classify_aspect(0, 0) is None

def test_classify_aspect_zero_par_is_none():
    assert classify_aspect(720, 480, 1, 0) is None


def test_parse_normal_disc_sets_aspect():
    titles = parse_handbrake_scan(_read("scan_normal.txt"))
    labels = {t.aspect for t in titles}
    # scan_normal has a PAR 32:27 title (widescreen) and PAR 1:1 titles (fullscreen)
    assert "widescreen" in labels
    assert "fullscreen" in labels
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_dvd.py -k "classify or aspect" -v`
Expected: FAIL (`cannot import name 'classify_aspect'`).

- [ ] **Step 3: Add `classify_aspect` to `app/dvd.py`** (module-level, above `parse_handbrake_scan`; `Optional` is already imported):

```python
def classify_aspect(width: int, height: int, par_num: int = 1, par_den: int = 1) -> Optional[str]:
    """Classify a title's display aspect ratio into widescreen / fullscreen.

    Display aspect = (width * PAR.num) / (height * PAR.den). DVDs store both 4:3 and
    16:9 at 720x480 and differ only by PAR, so PAR is required for correctness; Blu-ray
    has square pixels (PAR 1:1). Threshold 1.56 is the midpoint of 4:3 (1.333) and
    16:9 (1.778). Returns None when dimensions/PAR are missing.
    """
    if not width or not height or not par_num or not par_den:
        return None
    dar = (width * par_num) / (height * par_den)
    return "widescreen" if dar >= 1.56 else "fullscreen"
```

- [ ] **Step 4: Populate `Title.aspect` in `app/dvd.py`.** In `parse_handbrake_scan`, inside the
`titles.append(Title( … ))` constructor (where `geometry` is already in scope), add this
keyword argument (e.g. right after `frame_height=geometry.get("Height", 0),`):

```python
                aspect=classify_aspect(
                    geometry.get("Width", 0), geometry.get("Height", 0),
                    (geometry.get("PAR") or {}).get("Num", 1),
                    (geometry.get("PAR") or {}).get("Den", 1),
                ),
```

- [ ] **Step 5: Add the `aspect` field to `Title` in `app/models.py`** (after `frame_height`):

```python
    frame_width: int
    frame_height: int
    aspect: Optional[str] = None   # "widescreen" | "fullscreen" | None
```

- [ ] **Step 6: Populate `Title.aspect` in `app/bluray.py`.** Add the import near the top
(`bluray.py` does not currently import `dvd`, and `dvd` does not import `bluray`, so there is
no cycle):

```python
from .dvd import classify_aspect
```

Then in the `out.append(Title( … ))` constructor, add (after `frame_height=acc.frame_height,`):

```python
            aspect=classify_aspect(acc.frame_width, acc.frame_height),
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_dvd.py -v`
Expected: PASS (new aspect tests + existing dvd tests).

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (whole suite — adding an optional field breaks nothing).

- [ ] **Step 8: Commit**

```bash
git add app/dvd.py app/bluray.py app/models.py tests/test_dvd.py
git commit -m "feat(scan): detect display aspect (widescreen/fullscreen) from PAR"
```

---

### Task 2: Persist + capture aspect

**Files:**
- Modify: `app/models.py`, `app/library.py`, `app/ripper.py`
- Test: `tests/test_library.py`, `tests/test_ripper.py`

**Interfaces:**
- Consumes: nothing from Task 1 at runtime (independent field).
- Produces: `TitleSelection.aspect: Optional[str]`, `RipRecord.aspect: Optional[str]`, `rips.aspect` column; `enqueue()` writes `selection.aspect` into the record.

- [ ] **Step 1: Write failing tests** — append to `tests/test_library.py`:

```python
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
```

Note: `_movie(...)` is the existing helper in `tests/test_library.py`; it forwards `**kw`
to `RipRecord`, so `aspect=` works once the model field exists.

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_library.py -k aspect -v`
Expected: FAIL (`RipRecord` has no `aspect`; column missing).

- [ ] **Step 3: Add `aspect` to `TitleSelection` and `RipRecord` in `app/models.py`.**

In `TitleSelection` (after `episode_title`):

```python
    aspect: Optional[str] = None   # "widescreen" | "fullscreen" | None (from the scanned title)
```

In `RipRecord` (after `disc_format`):

```python
    disc_format: str = "dvd"
    aspect: Optional[str] = None
```

- [ ] **Step 4: Add the column + migration + read/write in `app/library.py`.**

(a) In `_SCHEMA`, add `aspect` as the last column of the `rips` table (after `disc_format …`):

```python
    disc_format TEXT NOT NULL DEFAULT 'dvd',
    aspect TEXT
);
```

(b) In `init_db()`, after the existing `disc_format` migration block, add:

```python
            if "aspect" not in cols:
                conn.execute("ALTER TABLE rips ADD COLUMN aspect TEXT")
```

(c) In `_row_to_record()`, add (after `disc_format=row["disc_format"],`):

```python
        aspect=row["aspect"],
```

(d) In `record_rip()`, add `aspect` to the INSERT. Change the column list to end with
`…, disc_format, aspect)`, add one more `?` to the VALUES list, and append
`record.aspect` to the parameter tuple (after `record.disc_format,`).

- [ ] **Step 5: Store aspect in the ripper.** In `app/ripper.py`, `enqueue()`'s
`RipRecord( … )` construction (~line 135), add (after `disc_format=disc_format,`):

```python
            aspect=selection.aspect,
```

- [ ] **Step 6: Write + run a ripper test** — append to `tests/test_ripper.py`:

```python
def test_save_record_persists_aspect(tmp_path):
    from app.library import Library
    from app.models import (ContentType, EncoderMode, JobStatus, MovieMetadata, TitleSelection)
    from app.ripper import Ripper

    lib = Library(str(tmp_path / "library.db")); lib.init_db()
    r = Ripper(device="/dev/null", output_root=tmp_path, library=lib)
    job = r.enqueue(
        selection=TitleSelection(index=1, audio_tracks=[1], subtitle_tracks=[], aspect="widescreen"),
        encoder_mode=EncoderMode.QUALITY, content_type=ContentType.MOVIE,
        movie=MovieMetadata(title="Wide Movie", year=2020),
    )
    r._save_record(job.id, JobStatus.DONE)
    assert lib.list_rips()[0].aspect == "widescreen"
```

Run: `.venv/bin/python -m pytest tests/test_library.py tests/test_ripper.py -v`
Expected: PASS.

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (whole suite).

- [ ] **Step 7: Commit**

```bash
git add app/models.py app/library.py app/ripper.py tests/test_library.py tests/test_ripper.py
git commit -m "feat(library): persist rip aspect ratio (schema + capture)"
```

---

### Task 3: Rip page — Aspect column

**Files:**
- Modify: `app/templates/index.html`, `app/static/app.js`
- Test: `tests/test_pwa.py`

**Interfaces:**
- Consumes: `Title.aspect` (from `/api/scan`, Task 1); `TitleSelection.aspect` (accepted by `/api/rip`, Task 2).
- Produces: rip page shows the aspect label per title and sends `aspect` in the rip payload.

- [ ] **Step 1: Write failing tests** — append to `tests/test_pwa.py`:

```python
def test_rip_page_has_aspect_column(client):
    html = client.get("/").text
    assert "<th>Aspect</th>" in html
    assert "<th>Frame</th>" not in html


def test_app_js_renders_and_sends_aspect(client):
    js = client.get("/static/app.js").text
    assert "function aspectLabel" in js
    assert "dataset.aspect" in js
    assert "aspect: row.dataset.aspect" in js
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pwa.py -k aspect -v`
Expected: FAIL (`<th>Frame</th>` still present; markers absent).

- [ ] **Step 3: Rename the header in `app/templates/index.html`** (line ~88):

Change `<th>Frame</th>` to `<th>Aspect</th>`. (Leave the row template's
`<td class="col-frame"></td>` class unchanged.)

- [ ] **Step 4: Add an `aspectLabel` helper to `app/static/app.js`** (near the other small
formatters such as `fmtDuration`):

```javascript
function aspectLabel(a) {
  if (a === 'widescreen') return 'Widescreen';
  if (a === 'fullscreen') return 'Full screen';
  return '—';
}
```

- [ ] **Step 5: Render the aspect label + tooltip in `app/static/app.js`.** Replace the
`.col-frame` line (~line 340, currently
`$('.col-frame', row).textContent = `${title.frame_width}×${title.frame_height}`;`) with:

```javascript
    const frameCell = $('.col-frame', row);
    frameCell.textContent = aspectLabel(title.aspect);
    frameCell.title = `${title.frame_width}×${title.frame_height}`;
    row.dataset.aspect = title.aspect || '';
```

- [ ] **Step 6: Send aspect in the rip payload in `app/static/app.js`.** In
`collectSelections()` (~lines 468 and 477), add `aspect: row.dataset.aspect || null,` to
BOTH pushed objects (the TV branch and the movie branch):

```javascript
      out.push({
        index,
        audio_tracks,
        subtitle_tracks,
        episode_number: epNum,
        episode_title: state.tvEpisodeTitles.get(epNum) || null,
        aspect: row.dataset.aspect || null,
      });
```
```javascript
      out.push({
        index,
        audio_tracks,
        subtitle_tracks,
        part_index: movieCounter,
        aspect: row.dataset.aspect || null,
      });
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pwa.py -v`
Expected: PASS.

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (whole suite).

- [ ] **Step 8: Commit**

```bash
git add app/templates/index.html app/static/app.js tests/test_pwa.py
git commit -m "feat(rip): show per-title aspect (Widescreen/Full screen) on the rip page"
```

---

### Task 4: Library — Aspect column + filter

**Files:**
- Modify: `app/templates/library.html`, `app/static/library.js`
- Test: `tests/test_pwa.py`

**Interfaces:**
- Consumes: `RipRecord.aspect` (from `/api/library`, Task 2).
- Produces: library Aspect column + `#lib-aspect` filter (default All).

- [ ] **Step 1: Write failing tests** — append to `tests/test_pwa.py`:

```python
def test_library_has_aspect_column_and_filter(client):
    html = client.get("/library").text
    assert 'id="lib-aspect"' in html
    assert "<th>Aspect</th>" in html


def test_library_js_aspect_wiring(client):
    js = client.get("/static/library.js").text
    assert "function aspectLabel" in js
    assert "lib-aspect" in js
    assert "colSpan = 7" in js
```

- [ ] **Step 2: Run them to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pwa.py -k "library_has_aspect or library_js_aspect" -v`
Expected: FAIL.

- [ ] **Step 3: Add the Aspect column + filter to `app/templates/library.html`.**

(a) Add the filter control inside `.lib-filters`, before the `#lib-status` field:

```html
        <label class="lib-field"><span>Aspect</span>
          <select id="lib-aspect">
            <option value="all">All</option>
            <option value="widescreen">Widescreen</option>
            <option value="fullscreen">Full screen</option>
          </select></label>
```

(b) Add an `Aspect` header to the table — change the `<thead>` row to:

```html
          <tr><th>Title</th><th>Ripped</th><th>Size</th><th>Duration</th><th>Aspect</th><th>Encoder</th><th>Status</th></tr>
```

- [ ] **Step 4: Wire it up in `app/static/library.js`.**

(a) Add the helper (near `label`):

```javascript
function aspectLabel(a) {
  if (a === 'widescreen') return 'Widescreen';
  if (a === 'fullscreen') return 'Full screen';
  return '—';
}
```

(b) In `rowEl()`, add an Aspect cell between Duration and Encoder so the order matches the
header:

```javascript
    { v: fmtDuration(r.rip_seconds), label: 'Duration' },
    { v: aspectLabel(r.aspect), label: 'Aspect' },
    { v: r.encoder_mode || '—', label: 'Encoder' },
```

(c) In `emptyRow()`, change `td.colSpan = 6;` to `td.colSpan = 7;`.

(d) In `readFilters()`, add `aspect: $('#lib-aspect').value,`.

(e) In `matches()`, add (alongside the other equality checks):

```javascript
  if (f.aspect !== 'all' && r.aspect !== f.aspect) return false;
```

(f) In `DOMContentLoaded`, add `'#lib-aspect'` to the list of select IDs that get the
`change` → `resetToFirstPageAndRender` listener.

(g) In the `#lib-clear` handler, add `$('#lib-aspect').value = 'all';`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_pwa.py -v`
Expected: PASS.

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (whole suite).

- [ ] **Step 6: Manual verification** (run `make dev`, open `http://localhost:8000/library`):
the table shows an Aspect column; the Aspect filter narrows to Widescreen / Full screen and
defaults to All; existing rows show "—" and drop out when a specific aspect is selected;
"Clear filters" resets Aspect to All; on mobile (≤480px) the new cell stacks with its
"Aspect" label like the others.

- [ ] **Step 7: Commit**

```bash
git add app/templates/library.html app/static/library.js tests/test_pwa.py
git commit -m "feat(library): aspect column and Widescreen/Full screen filter"
```

---

## Self-Review

- **Spec coverage:** `classify_aspect` + threshold ✅ (T1); scan wiring DVD+BD ✅ (T1);
  `aspect` on Title/TitleSelection/RipRecord ✅ (T1/T2); schema column + migration +
  record/read ✅ (T2); ripper capture ✅ (T2); rip page Frame→Aspect + label + tooltip +
  payload ✅ (T3); library column + filter default-All + clear + colspan ✅ (T4); no
  backfill / "—" for legacy ✅ (T2 default None + T4 aspectLabel); backend unit tests +
  frontend structural ✅.
- **Placeholder scan:** none — full code in every step.
- **Type/name consistency:** `classify_aspect(width, height, par_num=1, par_den=1)` and the
  `aspect` field name are identical across dvd/bluray/models/library/ripper; the JS
  `aspectLabel` mapping (`widescreen`→`Widescreen`, `fullscreen`→`Full screen`, else `—`) is
  identical in `app.js` and `library.js`; library header column order (Title, Ripped, Size,
  Duration, **Aspect**, Encoder, Status = 7 cols) matches `rowEl` cell order and the
  `colSpan = 7` empty row; `#lib-aspect` values (`all`/`widescreen`/`fullscreen`) match the
  `matches()` comparison and the stored label values.
