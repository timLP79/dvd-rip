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

function fmtDuration(seconds) {
  // Rip wall-clock as H:mm:ss. Null for backfilled/failed rows (duration unknown).
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

async function load() {
  const includeFailed = $('#show-failed').checked;
  const res = await fetch(`/api/library?include_unsuccessful=${includeFailed}`);
  const rows = res.ok ? await res.json() : [];
  const body = $('#library-body');
  body.replaceChildren();
  for (const r of rows) {
    const tr = document.createElement('tr');
    // `label` doubles as the per-row card heading on mobile (see #library reflow in
    // style.css); the rest carry data-label so each cell reads "Label  value" stacked.
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
    body.appendChild(tr);
  }
  $('#lib-count').textContent = `${rows.length} title(s)`;
}

document.addEventListener('DOMContentLoaded', () => {
  $('#show-failed').addEventListener('change', load);
  load();
});
