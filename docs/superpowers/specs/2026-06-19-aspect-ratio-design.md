# Aspect Ratio (Full screen / Widescreen) — Design

**Date:** 2026-06-19
**Status:** Approved (brainstorming) — pending implementation plan

## Context & Goal

Double-sided DVDs often carry a full-screen (4:3) side and a widescreen (16:9) side.
The user generally wants the widescreen rip and needs to (a) pick the correct title at
scan time and (b) verify afterward in the library. Today neither is possible: the scan
records only frame **width/height**, and **DVDs store both 4:3 and 16:9 at 720×480** —
the real signal is the **pixel aspect ratio (PAR)**, which we don't capture. So the rip
page's "Frame: 720×480" is useless for distinguishing aspect, and the library has no
aspect data at all.

This feature detects the true display aspect, classifies it as **Widescreen** or **Full
screen**, shows it per-title on the **rip page** (to pick correctly), and records +
displays + filters it in the **library** (to verify afterward).

### In scope
- Server-side aspect detection from HandBrake's `Geometry.PAR` (DVD) / frame dims (Blu-ray).
- Aspect shown on the rip page (the "Frame" column becomes "Aspect").
- Aspect captured at rip time, persisted in the ledger, shown + filterable in the library.

### Out of scope / decided
- **No backfill** of existing rows — they show "—" and are excluded when filtering by a
  specific aspect. Aspect is captured going forward only.
- No exact-ratio display (e.g. 2.35:1) — two buckets only (Widescreen / Full screen).
- Library aspect filter **defaults to All** (not Widescreen), so existing "—" rows and
  everything else stay visible.

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Where shown | Both rip page (pick correctly) **and** library (verify) |
| Detection source | HandBrake `Geometry.PAR` for DVD; frame dims (PAR 1:1) for Blu-ray |
| Buckets | `widescreen` (display aspect ≥ 1.56) / `fullscreen` (< 1.56) / `None` |
| Stored value | the **label string** (`"widescreen"`/`"fullscreen"`/null), not a float |
| Existing rows | left as "—" (no backfill) |
| Library filter default | All |
| Rip page column | rename "Frame" → "Aspect"; label shown, resolution as hover tooltip |

## Architecture

```
scan (dvd.py / bluray.py)
  └─ classify_aspect(width, height, par_num, par_den) -> "widescreen"|"fullscreen"|None
       └─ Title.aspect
            ├─ rip page (app.js): show label in the Aspect column; include in rip payload
            └─ /api/rip -> TitleSelection.aspect
                 └─ ripper.enqueue -> RipRecord.aspect
                      └─ library.record_rip -> rips.aspect (TEXT)
                           └─ library page (library.js): Aspect column + Aspect filter
```

One classifier, one threshold, used by both scanners and stored verbatim, so the library
filter is a plain string match like `type`/`format`.

## Components

### 1. `classify_aspect` — `app/dvd.py`
Pure helper (the single source of the threshold):

```python
def classify_aspect(width: int, height: int, par_num: int = 1, par_den: int = 1) -> Optional[str]:
    if not width or not height or not par_num or not par_den:
        return None
    dar = (width * par_num) / (height * par_den)
    return "widescreen" if dar >= 1.56 else "fullscreen"
```

`1.56` is the midpoint of 4:3 (1.333) and 16:9 (1.778). Letterboxed ≤2.35 films still
read widescreen (the stored frame is 16:9). PAR absent → defaults to 1:1 (square pixels);
only missing/zero dimensions yield `None`.

### 2. Scan wiring
- **`app/dvd.py`** (`parse_handbrake_scan`, ~line 121): read `par = geometry.get("PAR") or {}`,
  pass `par.get("Num", 1)` / `par.get("Den", 1)` to `classify_aspect`, set `Title(aspect=…)`.
- **`app/bluray.py`** (Title construction, ~line 149): `from .dvd import classify_aspect`
  (no import cycle — `dvd.py` does not import `bluray.py`); call with the title's
  width/height and default PAR 1:1, set `aspect`.

### 3. Models — `app/models.py`
Add `aspect: Optional[str] = None` to **`Title`**, **`TitleSelection`**, and **`RipRecord`**.

### 4. Persistence — `app/library.py`
- `_SCHEMA`: add `aspect TEXT` to the `rips` table.
- `init_db()`: migrate older DBs — `ALTER TABLE rips ADD COLUMN aspect TEXT` when the
  column is absent (same pattern as the `disc_format` migration).
- `record_rip()`: include `aspect` in the INSERT column list and values.
- `_row_to_record()`: read `row["aspect"]`.

### 5. Capture — `app/ripper.py`
In `enqueue()`'s `RipRecord(...)` construction (~line 135), add `aspect=selection.aspect`.
`main.py`'s `/api/rip` already forwards the whole `TitleSelection` to `enqueue`, so it
needs no change. `/api/scan` returns `ScanResult` whose `Title`s now include `aspect`
automatically (Pydantic response model).

### 6. Rip page — `app/templates/index.html`, `app/static/app.js`
- Template: rename the titles-table header `<th>Frame</th>` (line 88) to `<th>Aspect</th>`.
  The row template keeps `<td class="col-frame"></td>` (class unchanged; only the header
  label and cell content change).
- `app.js` (~line 340, currently `…textContent = `${title.frame_width}×${title.frame_height}``):
  render the aspect label — `Widescreen` / `Full screen` / `—` — and set the cell's
  `title` attribute to the raw `"{w}×{h}"` resolution as a tooltip.
- `app.js` rip-payload builder: include `aspect: title.aspect` in each `TitleSelection`
  object POSTed to `/api/rip` (so the ripper records it).

### 7. Library — `app/templates/library.html`, `app/static/library.js`, `app/static/style.css`
- Table gains an **Aspect** column. Update the `<thead>` (now 7 columns), `rowEl()` to
  append an aspect cell `{ v: aspectLabel(r.aspect), label: 'Aspect' }`, and `emptyRow()`'s
  `colSpan` from 6 → 7.
- A small `aspectLabel(a)` helper in `library.js`: `'widescreen' → 'Widescreen'`,
  `'fullscreen' → 'Full screen'`, else `'—'`.
- Filter bar gains `#lib-aspect` `<select>`: All / Widescreen / Full screen (values
  `all|widescreen|fullscreen`), **default `all`**. Add to `readFilters()`, `matches()`
  (`f.aspect === 'all' || r.aspect === f.aspect`), and the `#lib-clear` reset.
- `style.css`: no new rules needed beyond the existing `.lib-field`/table styling; the new
  column reflows on mobile via the existing `#library` card rules (the new cell carries a
  `data-label`, so it stacks like the rest).

## Edge cases & error handling
- Missing/zero dimensions, or PAR with a zero denominator → `aspect = None` → "—".
- Backfilled/legacy rows (no `aspect` in DB) → `None` → "—"; excluded by a specific aspect
  filter, shown under "All".
- Blu-ray titles always have dims → classify from dims (1920×1080 → widescreen).
- The aspect value is a closed set (`widescreen`/`fullscreen`/null); the library filter and
  display treat any other/absent value as "—".

## Testing
- **Backend (pytest):**
  - `classify_aspect`: 720×480 PAR 32:27 → `widescreen`; 720×480 PAR 8:9 → `fullscreen`;
    720×480 PAR 1:1 → `fullscreen` (1.5 < 1.56); 1920×1080 → `widescreen`; 0×0 → `None`;
    zero PAR denominator → `None`.
  - `dvd.py`: parsing `tests/fixtures/scan_normal.txt` sets `Title.aspect` (the PAR 32:27
    title → `widescreen`; PAR 1:1 titles → `fullscreen`); `scan_no_main.txt` (no PAR) →
    `fullscreen`.
  - `library.py`: `aspect` round-trips through `record_rip`/`list_rips`; the
    `ALTER TABLE … ADD COLUMN aspect` migration runs on a pre-existing `aspect`-less DB.
  - rip path: a `TitleSelection` with `aspect="widescreen"` lands in the recorded
    `RipRecord` (extend the existing ripper record test).
- **Frontend (structural + manual):** assert `library.html` serves the `#lib-aspect`
  control and an "Aspect" header; assert `app.js`/`library.js` contain the aspect wiring
  markers. Manual: scan a disc → rip page shows Widescreen/Full screen per title with the
  resolution tooltip; rip → library row shows the aspect; the library Aspect filter
  narrows correctly and defaults to All.

## Files
| File | Change |
|---|---|
| `app/dvd.py` | `classify_aspect` helper; set `Title.aspect` from PAR in scan parsing. |
| `app/bluray.py` | import `classify_aspect`; set `Title.aspect` from dims. |
| `app/models.py` | `aspect` field on `Title`, `TitleSelection`, `RipRecord`. |
| `app/library.py` | `aspect` column + migration + record/read. |
| `app/ripper.py` | store `selection.aspect` in the `RipRecord`. |
| `app/templates/index.html` | rename Frame → Aspect header. |
| `app/static/app.js` | render aspect label (+ resolution tooltip); send `aspect` in rip payload. |
| `app/templates/library.html` | Aspect column + `#lib-aspect` filter. |
| `app/static/library.js` | `aspectLabel`, Aspect cell, filter wiring, clear reset, colspan. |
| `tests/test_dvd.py`, `test_library.py`, `test_ripper.py`, `test_pwa.py` | coverage above. |

`app/main.py` needs no change (scan/rip endpoints forward the augmented models as-is).
