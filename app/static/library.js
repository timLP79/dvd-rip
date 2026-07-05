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
  if (r.content_type === 'music' || r.content_type === 'audiobook') {
    // r.artist = Artist/Author, r.title = Album/Book.
    const base = r.artist ? `${r.artist} — ${r.title}` : r.title;
    return r.year ? `${base} (${r.year})` : base;
  }
  return r.year ? `${r.title} (${r.year})` : r.title;
}

function aspectLabel(a) {
  if (a === 'widescreen') return 'Widescreen';
  if (a === 'fullscreen') return 'Full screen';
  return '—';
}

function readFilters() {
  return {
    search: $('#lib-search').value.trim().toLowerCase(),
    type: $('#lib-type').value,
    format: $('#lib-format').value,
    aspect: $('#lib-aspect').value,
    status: $('#lib-status').value,
    dateFrom: $('#lib-date-from').value,
    dateTo: $('#lib-date-to').value,
  };
}

function matches(r, f) {
  if (f.search && !label(r).toLowerCase().includes(f.search)) return false;
  if (f.type !== 'all' && r.content_type !== f.type) return false;
  if (f.format !== 'all' && r.disc_format !== f.format) return false;
  if (f.aspect !== 'all' && r.aspect !== f.aspect) return false;
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
    { v: aspectLabel(r.aspect), label: 'Aspect' },
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
  td.colSpan = 7;
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
  for (const id of ['#lib-type', '#lib-format', '#lib-aspect', '#lib-status', '#lib-date-from', '#lib-date-to']) {
    $(id).addEventListener('change', resetToFirstPageAndRender);
  }
  $('#lib-prev').addEventListener('click', () => { if (page > 1) { page--; render(); } });
  $('#lib-next').addEventListener('click', () => { page++; render(); });
  $('#lib-clear').addEventListener('click', () => {
    $('#lib-search').value = '';
    $('#lib-type').value = 'all';
    $('#lib-format').value = 'all';
    $('#lib-aspect').value = 'all';
    $('#lib-status').value = 'done';
    $('#lib-date-from').value = '';
    $('#lib-date-to').value = '';
    resetToFirstPageAndRender();
  });
  load();
});
