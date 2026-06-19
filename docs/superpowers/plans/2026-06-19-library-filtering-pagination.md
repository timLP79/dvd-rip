# Library Filtering + Pagination Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add client-side search, content-type/format/status/date filtering, and 25-per-page pagination to the Library page.

**Architecture:** Pure frontend change. The page fetches all rows once (`/api/library?include_unsuccessful=true`) into memory, then filters/searches/paginates in vanilla JS. No `main.py`, `library.py`, or SQL changes.

**Tech Stack:** Jinja2 template + vanilla JS (no build step) + CSS; pytest TestClient for structural assertions.

## Global Constraints

- No backend changes: do not touch `app/main.py`, `app/library.py`, or any SQL. The `/api/library?include_unsuccessful=true` endpoint already returns every row.
- Filtering/search/pagination are **client-side**, over a single fetch.
- Status control is a `<select>` (All/Done/Failed/Cancelled, **default `done`**) that **replaces** the removed `#show-failed` checkbox; `All` = the old "show failed/cancelled". Failure visibility is preserved.
- Page size is **25**. Sort is unchanged (newest-first, as the API returns it).
- UI stays clean/utility, blue-gray palette, accent `#4b73b8` only on focus. Match the existing input styling (`background: var(--bg-2)`, `border: 1px solid var(--rule)`, `padding: 8px 12px`, `border-radius: var(--radius)`, focus → `border-color: var(--accent)`). IBM Plex only.
- No JS test harness exists; automated coverage is **structural** (served HTML/JS/CSS markers) plus a manual checklist. Do not claim the filter logic is unit-tested.
- Commit with the project trailer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` and the `Claude-Session:` line.

## File Structure

| File | Change | Responsibility |
|---|---|---|
| `app/templates/library.html` | modify | Add filter bar + pager markup; remove the `#show-failed` checkbox. |
| `app/static/library.js` | rewrite | Fetch-all + client-side filter/search/paginate + events + empty states. |
| `app/static/style.css` | modify | `.lib-filters` / `.lib-field` / `.lib-pager` / `.lib-empty` styling + 480px stacking. |
| `tests/test_pwa.py` | modify | Structural assertions for the new controls, CSS, and JS markers. |

**Why one task:** removing the `#show-failed` checkbox from the template breaks the current `library.js` (which reads `$('#show-failed').checked`) until the JS is rewritten. The markup, JS, and CSS are interdependent and must land together — splitting them would leave the page non-functional between tasks.

---

### Task 1: Library filtering + pagination (markup + JS + CSS)

**Files:**
- Modify: `app/templates/library.html`
- Rewrite: `app/static/library.js`
- Modify: `app/static/style.css`
- Test: `tests/test_pwa.py`

**Interfaces:**
- Consumes: `GET /api/library?include_unsuccessful=true` → JSON array of `RipRecord`
  objects with fields `content_type` (`"movie"|"tv"`), `title`, `year`, `season`,
  `episode`, `disc_format` (`"dvd"|"bluray"`), `status` (`"done"|"failed"|"cancelled"`),
  `created_at` (ISO string), `encoder_mode`, `file_size_bytes`, `rip_seconds`.
- Produces: control element IDs `#lib-search #lib-type #lib-format #lib-status
  #lib-date-from #lib-date-to #lib-clear`, pager `#lib-pager #lib-prev #lib-next
  #lib-page-info`, and the existing `#lib-count` / `#library-body`.

- [ ] **Step 1: Write the failing structural tests** — append to `tests/test_pwa.py`:

```python
def test_library_filter_controls_present(client):
    html = client.get("/library").text
    for marker in (
        'id="lib-search"', 'id="lib-type"', 'id="lib-format"', 'id="lib-status"',
        'id="lib-date-from"', 'id="lib-date-to"', 'id="lib-clear"',
        'id="lib-pager"', 'id="lib-prev"', 'id="lib-next"',
    ):
        assert marker in html, marker
    assert 'value="done" selected' in html      # status defaults to Done
    assert 'id="show-failed"' not in html        # old checkbox removed


def test_library_filter_styles_present(client):
    css = client.get("/static/style.css").text
    assert ".lib-filters" in css
    assert ".lib-pager" in css


def test_library_js_filter_paginate_logic(client):
    js = client.get("/static/library.js").text
    assert "PAGE_SIZE" in js
    assert "include_unsuccessful=true" in js
    assert "function matches" in js
    assert "No rips match your filters" in js
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_pwa.py -k "library_filter or library_js" -v`
Expected: FAIL (controls/CSS/JS markers not present yet; `id="show-failed"` still present).

- [ ] **Step 3: Edit `app/templates/library.html`** — (a) remove the checkbox from the
topbar, leaving the (now empty) actions container so the topbar grid is unaffected;
(b) add the filter bar after `.section-head` and before the table; (c) add the pager
after the table.

Replace the topbar-actions block:

```html
    <div class="topbar-actions"></div>
```

Replace the `<main>` body so the panel contains the filter bar, table, and pager:

```html
  <main>
    <section class="panel">
      <div class="section-head"><h2>Library</h2><span id="lib-count" class="rip-status"></span></div>

      <div class="lib-filters">
        <label class="lib-field"><span>Search</span>
          <input type="search" id="lib-search" placeholder="Title, year, S01E03…" /></label>
        <label class="lib-field"><span>Type</span>
          <select id="lib-type">
            <option value="all">All</option>
            <option value="movie">Movie</option>
            <option value="tv">TV</option>
          </select></label>
        <label class="lib-field"><span>Format</span>
          <select id="lib-format">
            <option value="all">All</option>
            <option value="dvd">DVD</option>
            <option value="bluray">Blu-ray</option>
          </select></label>
        <label class="lib-field"><span>Status</span>
          <select id="lib-status">
            <option value="all">All</option>
            <option value="done" selected>Done</option>
            <option value="failed">Failed</option>
            <option value="cancelled">Cancelled</option>
          </select></label>
        <label class="lib-field"><span>From</span>
          <input type="date" id="lib-date-from" /></label>
        <label class="lib-field"><span>To</span>
          <input type="date" id="lib-date-to" /></label>
        <button type="button" id="lib-clear" class="ghost">Clear filters</button>
      </div>

      <table id="library" class="titles">
        <thead>
          <tr><th>Title</th><th>Ripped</th><th>Size</th><th>Duration</th><th>Encoder</th><th>Status</th></tr>
        </thead>
        <tbody id="library-body"></tbody>
      </table>

      <div class="lib-pager" id="lib-pager" hidden>
        <button type="button" id="lib-prev" class="ghost">Prev</button>
        <span id="lib-page-info"></span>
        <button type="button" id="lib-next" class="ghost">Next</button>
      </div>
    </section>
  </main>
```

- [ ] **Step 4: Rewrite `app/static/library.js`** with the full file below:

```javascript
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const PAGE_SIZE = 25;

let allRows = [];
let page = 1;

function fmtSize(bytes) {
  if (!bytes) return '—';
  const gb = bytes / 1024 / 1024 / 1024;
  return gb >= 1 ? `${gb.toFixed(2)} GB` : `${(bytes / 1024 / 1024).toFixed(0)} MB`;
}

function fmtDate(iso) {
  return iso ? iso.slice(0, 10) : '—';
}

function fmtDuration(seconds) {
  if (seconds == null) return '—';
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}:${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

function label(r) {
  if (r.content_type === 'tv') {
    const s = String(r.season ?? 0).padStart(2, '0');
    const e = String(r.episode ?? 0).padStart(2, '0');
    return `${r.title} S${s}E${e}`;
  }
  return r.year ? `${r.title} (${r.year})` : r.title;
}

function readFilters() {
  return {
    search: $('#lib-search').value.trim().toLowerCase(),
    type: $('#lib-type').value,
    format: $('#lib-format').value,
    status: $('#lib-status').value,
    dateFrom: $('#lib-date-from').value,
    dateTo: $('#lib-date-to').value,
  };
}

function matches(r, f) {
  if (f.search && !label(r).toLowerCase().includes(f.search)) return false;
  if (f.type !== 'all' && r.content_type !== f.type) return false;
  if (f.format !== 'all' && r.disc_format !== f.format) return false;
  if (f.status !== 'all' && r.status !== f.status) return false;
  const d = r.created_at ? r.created_at.slice(0, 10) : '';
  if (f.dateFrom && !(d && d >= f.dateFrom)) return false;
  if (f.dateTo && !(d && d <= f.dateTo)) return false;
  return true;
}

function rowEl(r) {
  const tr = document.createElement('tr');
  // `label` is the per-row card heading on mobile (see #library reflow in style.css);
  // the rest carry data-label so each cell reads "Label  value" stacked.
  const cells = [
    { v: label(r), cls: 'lib-title' },
    { v: fmtDate(r.created_at), label: 'Ripped' },
    { v: fmtSize(r.file_size_bytes), label: 'Size' },
    { v: fmtDuration(r.rip_seconds), label: 'Duration' },
    { v: r.encoder_mode || '—', label: 'Encoder' },
    { v: r.status, label: 'Status' },
  ];
  for (const c of cells) {
    const td = document.createElement('td');
    td.textContent = c.v;
    if (c.cls) td.className = c.cls;
    if (c.label) td.dataset.label = c.label;
    tr.appendChild(td);
  }
  if (r.status !== 'done') tr.classList.add('row-muted');
  return tr;
}

function emptyRow(msg) {
  const tr = document.createElement('tr');
  const td = document.createElement('td');
  td.colSpan = 6;
  td.className = 'lib-empty';
  td.textContent = msg;
  tr.appendChild(td);
  return tr;
}

function render() {
  const f = readFilters();
  const filtered = allRows.filter((r) => matches(r, f));
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  if (page > totalPages) page = totalPages;
  if (page < 1) page = 1;
  const start = (page - 1) * PAGE_SIZE;
  const pageRows = filtered.slice(start, start + PAGE_SIZE);

  const body = $('#library-body');
  body.replaceChildren();
  if (allRows.length === 0) {
    body.appendChild(emptyRow('No rips yet.'));
  } else if (filtered.length === 0) {
    body.appendChild(emptyRow('No rips match your filters.'));
  } else {
    for (const r of pageRows) body.appendChild(rowEl(r));
  }

  $('#lib-count').textContent = `${filtered.length} title(s)`;

  const pager = $('#lib-pager');
  if (totalPages <= 1) {
    pager.hidden = true;
  } else {
    pager.hidden = false;
    $('#lib-page-info').textContent = `Page ${page} of ${totalPages}`;
    $('#lib-prev').disabled = page <= 1;
    $('#lib-next').disabled = page >= totalPages;
  }
}

function resetToFirstPageAndRender() {
  page = 1;
  render();
}

async function load() {
  let rows = [];
  try {
    const res = await fetch('/api/library?include_unsuccessful=true');
    rows = res.ok ? await res.json() : [];
  } catch (e) {
    rows = [];
  }
  allRows = rows;
  page = 1;
  render();
}

document.addEventListener('DOMContentLoaded', () => {
  $('#lib-search').addEventListener('input', resetToFirstPageAndRender);
  for (const id of ['#lib-type', '#lib-format', '#lib-status', '#lib-date-from', '#lib-date-to']) {
    $(id).addEventListener('change', resetToFirstPageAndRender);
  }
  $('#lib-prev').addEventListener('click', () => { if (page > 1) { page--; render(); } });
  $('#lib-next').addEventListener('click', () => { page++; render(); });
  $('#lib-clear').addEventListener('click', () => {
    $('#lib-search').value = '';
    $('#lib-type').value = 'all';
    $('#lib-format').value = 'all';
    $('#lib-status').value = 'done';
    $('#lib-date-from').value = '';
    $('#lib-date-to').value = '';
    resetToFirstPageAndRender();
  });
  load();
});
```

- [ ] **Step 5: Add CSS to `app/static/style.css`.** Append this block at the END of the file:

```css
/* ============================================================
   Library filters + pager
   ============================================================ */
.lib-filters {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 12px;
  margin-bottom: 16px;
}
.lib-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 11px;
  font-weight: 500;
  color: var(--text-faint);
}
.lib-field input,
.lib-field select {
  font-family: var(--font-sans);
  font-size: 14px;
  font-weight: 400;
  background: var(--bg-2);
  border: 1px solid var(--rule);
  color: var(--text);
  padding: 8px 12px;
  border-radius: var(--radius);
}
.lib-field input:focus,
.lib-field select:focus { outline: none; border-color: var(--accent); }
.lib-field input::placeholder { color: var(--text-faint); }
#lib-clear { align-self: flex-end; }

.lib-empty { text-align: center; color: var(--text-muted); padding: 24px 0; }

.lib-pager {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 16px;
  margin-top: 16px;
  font-size: 13px;
  color: var(--text-muted);
}
.lib-pager button[disabled] { opacity: 0.45; cursor: default; }
```

Then, INSIDE the existing `@media (max-width: 480px) { ... }` block (the one that
contains the `#library` reflow rules), add these lines before the closing `}`:

```css
  /* Library filters stack full-width on phones. */
  .lib-field { flex: 1 1 100%; }
  #lib-clear { width: 100%; }
  .lib-pager button { min-height: 40px; }
```

- [ ] **Step 6: Run the tests to verify they pass, then the full suite**

Run: `.venv/bin/python -m pytest tests/test_pwa.py -v`
Expected: PASS (the 3 new tests + the existing PWA tests).

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (whole suite green).

- [ ] **Step 7: Manual verification** — run `make dev` (uvicorn on the host serving
`app/` straight from disk; auth is off because `.env` isn't loaded in this path), open
`http://localhost:8000/library`, and confirm:

  - Default view shows only **Done** rows (failed/cancelled hidden).
  - **Status** select: `All` shows everything incl. failed/cancelled; `Failed` /
    `Cancelled` isolate those.
  - **Type** (Movie/TV) and **Format** (DVD/Blu-ray) narrow correctly and combine
    (AND) with other filters.
  - **Search** matches title, year, and the `S01E03` label as you type.
  - **From/To** date inputs bound by `created_at` (inclusive).
  - **Pager**: 25/page, `Page X of Y` correct, Prev disabled on page 1, Next disabled
    on the last page; the pager is hidden when results fit on one page; changing any
    filter jumps back to page 1.
  - **Clear filters** resets all controls (status back to Done) and the page.
  - Empty states: a filter that matches nothing shows "No rips match your filters.";
    an empty library shows "No rips yet."
  - Mobile (DevTools ≤480px): the filter controls stack full-width and the table keeps
    the card reflow.

  Note: to see it in the **container**, a rebuild is required (`make build && make up-d`)
  because static/templates are baked into the image — but `make dev` is the fast path
  for this verification.

- [ ] **Step 8: Commit**

```bash
git add app/templates/library.html app/static/library.js app/static/style.css tests/test_pwa.py
git commit -m "feat(library): client-side search, filters, and pagination"
```

---

## Self-Review

- **Spec coverage:** search ✅ (step 4 `matches`), content type ✅, disc format ✅,
  status select replacing checkbox ✅ (steps 3–4, default `done`), date range ✅,
  pagination 25/page with Prev/Next + indicator + hide-when-single-page ✅, reset-to-page-1
  on filter change ✅, empty states ✅, mobile stacking ✅, structural tests + manual
  checklist ✅, no backend changes ✅, genre out of scope ✅.
- **Placeholder scan:** none — full code given for HTML, JS, CSS, and tests.
- **Type/name consistency:** control IDs in the template (`#lib-search`, `#lib-type`,
  `#lib-format`, `#lib-status`, `#lib-date-from`, `#lib-date-to`, `#lib-clear`,
  `#lib-pager`, `#lib-prev`, `#lib-next`, `#lib-page-info`) match every selector used in
  `library.js`; `matches`/`render`/`readFilters`/`PAGE_SIZE` are internally consistent;
  CSS selectors (`.lib-filters .lib-field .lib-pager .lib-empty #lib-clear`) match the
  markup. The `#library` id and `lib-title`/`data-label` card structure match the
  already-shipped mobile reflow.
