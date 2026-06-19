# Library Filtering + Pagination — Design

**Date:** 2026-06-19
**Status:** Approved (brainstorming) — pending implementation plan
**Related:** Follow-up — genre filtering (deferred; needs TMDB genre capture + schema + backfill, its own spec).

## Context & Goal

The Library page (`/library`) lists every rip in one ungrouped table with a single
"show failed/cancelled" checkbox. As the ledger grows this becomes unwieldy. Add
**filtering, text search, and pagination** so the user can find rips quickly.

The page already fetches all matching rows and renders them client-side. The
realistic dataset is a personal rip library (tens to low-hundreds of rows), so all
filtering/paging is done **client-side** over a single fetch — no backend changes.
If the library ever reaches thousands of rows, moving to server-side query params is
a contained later change.

### In scope
- Filter bar: text search, content type, disc format, status, date range.
- Pagination: 25 rows/page, Prev/Next with a page indicator.
- Replace the existing "show failed/cancelled" checkbox with a status filter.
- Responsive filter bar (mobile), reusing the card reflow already shipped.

### Out of scope (YAGNI / deferred)
- **Genre** filtering — deferred to its own spec (no genre column exists; needs
  TMDB genre capture at rip time + schema + backfill).
- Server-side filtering/pagination/SQL changes.
- Column sorting (sort stays newest-first as the API returns it).
- Persisting filters across reloads (URL/localStorage) — in-memory only.

## Decisions (from brainstorming)

| Question | Decision |
|---|---|
| Filters | text search, content type (Movie/TV), disc format (DVD/Blu-ray), status, date range |
| Genre | deferred to a follow-up spec |
| Where filtering/paging runs | **client-side** (fetch all once, filter/page in JS) |
| Default status filter | **Done** (preserves current behavior of hiding failures) |
| Page size | **25** |
| Sorting | unchanged — newest-first (API `ORDER BY created_at DESC`) |

## Architecture

All changes live on the Library page. No `main.py`, `library.py`, or SQL changes.

```
/library page load
  └─ fetch /api/library?include_unsuccessful=true   (ALL rows, once)
       └─ allRows[]  (kept in memory)
            └─ render(): applyFilters(allRows) -> filtered[]
                          -> paginate(filtered, page, 25) -> pageRows[]
                          -> draw cards/rows + count + pager
  filter/search/date change -> page = 1 -> render()
  Prev/Next                  -> page +/- 1 (clamped) -> render()
```

Fetching with `include_unsuccessful=true` pulls failed/cancelled too, so the status
filter can show them on demand; the default status filter (`done`) hides them, matching
today's behavior.

## Components

### 1. Filter bar — `app/templates/library.html`
A `<div class="lib-filters">` above the table, replacing the current `#show-failed`
checkbox in the topbar. Controls (each wrapped in a `<label>` for a11y):
- `#lib-search` — `<input type="search" placeholder="Search title…">`.
- `#lib-type` — `<select>`: All / Movie / TV (values `all|movie|tv`).
- `#lib-format` — `<select>`: All / DVD / Blu-ray (values `all|dvd|bluray`).
- `#lib-status` — `<select>`: All / Done / Failed / Cancelled (values `all|done|failed|cancelled`), **default `done`**. This fully replaces the removed checkbox and is a superset of it: `All` = the old "show failed/cancelled" (everything visible), `Done` = old unchecked, and `Failed`/`Cancelled` isolate just those (which the checkbox could not do). Failure visibility is preserved, not lost.
- `#lib-date-from`, `#lib-date-to` — `<input type="date">` (both optional).
- `#lib-clear` — a "Clear filters" button/link resetting all controls to defaults.

The table gets a sibling pager `<div class="lib-pager">`: `#lib-prev` button,
`#lib-page-info` span (`Page X of Y`), `#lib-next` button. The existing `#lib-count`
keeps showing the filtered result count.

### 2. Filter + pagination logic — `app/static/library.js`
- On load: `fetch('/api/library?include_unsuccessful=true')` → `allRows`.
- Module state: `const PAGE_SIZE = 25; let page = 1;` and a `filters` object read from
  the controls (`search, type, format, status, dateFrom, dateTo`).
- `matches(r, f)` predicate (pure, all conditions AND):
  - **search**: `f.search` empty OR `label(r).toLowerCase().includes(f.search.trim().toLowerCase())`
    (`label()` already yields `"Title (Year)"` or `"Show S01E03"`, so title, year, and
    episode code are all searchable).
  - **type**: `f.type === 'all'` OR `r.content_type === f.type`.
  - **format**: `f.format === 'all'` OR `r.disc_format === f.format`.
  - **status**: `f.status === 'all'` OR `r.status === f.status`.
  - **date** (null-safe — a row with no `created_at` is excluded only when a bound is
    set): let `d = r.created_at ? r.created_at.slice(0,10) : ''`;
    `(!f.dateFrom || (d && d >= f.dateFrom))` AND `(!f.dateTo || (d && d <= f.dateTo))`
    (ISO `YYYY-MM-DD` compares correctly as strings).
- `render()`: `filtered = allRows.filter(r => matches(r, readFilters()))`;
  `totalPages = max(1, ceil(filtered.length / PAGE_SIZE))`; clamp `page` to
  `[1, totalPages]`; `pageRows = filtered.slice((page-1)*PAGE_SIZE, page*PAGE_SIZE)`;
  rebuild tbody from `pageRows` (reuse the existing card/td builder with `data-label` +
  `lib-title`); update `#lib-count` (`N title(s)`); update pager (`Page X of Y`, disable
  Prev at page 1 / Next at last page, hide `.lib-pager` when `totalPages <= 1`).
- Events: every filter/search/date control → set `page = 1`, `render()` (search on the
  `input` event for as-you-type). `#lib-prev`/`#lib-next` → adjust `page`, `render()`.
  `#lib-clear` → reset controls to defaults, `page = 1`, `render()`.
- Empty states (rendered in the tbody area): `allRows.length === 0` →
  "No rips yet." ; non-empty but `filtered.length === 0` → "No rips match your filters."

### 3. Styles — `app/static/style.css`
- `.lib-filters` — flex row, `gap`, `flex-wrap: wrap`, aligned controls; reuse existing
  input styling; add restrained `<select>` styling consistent with text inputs
  (blue-gray palette, accent only on focus ring). `.lib-pager` — flex row, centered,
  Prev/Next + info.
- Under the existing `@media (max-width: 480px)` block: `.lib-filters` controls go
  full-width and stack; pager buttons meet the 40px tap-target rule.

## Edge cases & error handling
- Fetch failure → `allRows = []`, show "No rips yet." (current behavior already treats a
  non-OK response as empty).
- `created_at` missing on a row → date filter treats it as non-matching only when a
  date bound is set (guard: skip date comparison if `r.created_at` is falsy → excluded
  when a bound is active, included when no date filter).
- Page clamping prevents an out-of-range page after filtering shrinks the set.
- Status default `done` means failed/cancelled rows are fetched but hidden until the
  user selects them — matching the old checkbox-unchecked default.

## Testing
- **Automated (structural, via `tests/test_pwa.py` TestClient):** the project has no JS
  test harness, so assert the controls are served in `library.html`
  (`id="lib-search"`, the three selects, the date inputs, `id="lib-prev"`/`id="lib-next"`)
  and that `library.js` contains the filter/paginate logic markers (`PAGE_SIZE`,
  `include_unsuccessful=true`, `matches`, `lib-status`). This is shallow by necessity —
  it guards wiring/regressions, not the filter logic itself.
- **Manual verification checklist** (documented in the plan): load `/library`; default
  shows only Done; type/format/status selects narrow correctly and combine; search
  matches title, year, and `S01E03`; date from/to bounds work; pager shows 25/page with
  working Prev/Next and correct `Page X of Y`; changing a filter resets to page 1; pager
  hidden when ≤1 page; "Clear filters" resets; empty-filter message vs empty-library
  message; mobile (≤480px) stacks controls and keeps the card reflow.

## Files
| File | Change |
|---|---|
| `app/templates/library.html` | Filter bar + pager markup; remove `#show-failed` checkbox. |
| `app/static/library.js` | Fetch-all + client-side filter/search/paginate + events + empty states. |
| `app/static/style.css` | `.lib-filters` / `.lib-pager` styling + 480px stacking. |
| `tests/test_pwa.py` | Structural assertions for the new controls + JS markers. |

No backend (`main.py` / `library.py` / SQL) changes.
