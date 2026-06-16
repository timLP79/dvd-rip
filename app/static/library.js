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
