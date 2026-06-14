'use strict';

const state = {
  scan: null,
  jobStreams: new Map(),     // job_id -> EventSource
  contentType: 'movie',      // 'movie' | 'tv'
  selectedMeta: null,        // {type:'movie', title, year, tmdb_id?} or {type:'tv', show, season, tmdb_id?}
  tmdbEnabled: document.body.dataset.tmdbEnabled === 'true',
  tvEpisodeMap: new Map(),   // title_index -> episode_number (user override)
  tvEpisodeTitles: new Map(),// title_index -> episode_title (TMDB-fetched, optional)
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function fmtDuration(seconds) {
  if (!seconds) return '00:00';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

function fmtEta(seconds) {
  if (seconds == null) return '';
  return `ETA ${fmtDuration(seconds)}`;
}

// Parse a DVD disc label into a clean search term + parsed disc/season metadata.
// Example: "PTELLER_S4_D1"      → { search: "Pteller",       season: 4, disc: 1, isLikelyTV: true }
//          "DEADWOOD_SEASON_1_DISC_2" → { search: "Deadwood", season: 1, disc: 2, isLikelyTV: true }
//          "THE_MATRIX"          → { search: "The Matrix",    season: null, disc: null, isLikelyTV: false }
//          "LOTR_DISC_2"         → { search: "Lotr",          season: null, disc: 2, isLikelyTV: false }
function parseDiscLabel(label) {
  if (!label || label === 'UNKNOWN_DVD') {
    return { search: '', season: null, disc: null, isLikelyTV: false };
  }

  let working = label.toUpperCase();
  let season = null;
  let disc = null;

  // Season indicators (order matters: longest first so SEASON_4 beats S4)
  const seasonRe = /(?:^|_)(?:SEASON|SEAS|SE|S)_?(\d{1,2})(?:_|$)/;
  const sm = working.match(seasonRe);
  if (sm) {
    season = parseInt(sm[1], 10);
    working = working.replace(seasonRe, '_');
  }

  // Disc indicators
  const discRe = /(?:^|_)(?:DISC|DISK|D)_?(\d{1,2})(?:_|$)/;
  const dm = working.match(discRe);
  if (dm) {
    disc = parseInt(dm[1], 10);
    working = working.replace(discRe, '_');
  }

  // Volume / part / side — strip but don't store
  working = working.replace(/(?:^|_)(?:VOLUME|VOL|V)_?\d{1,2}(?:_|$)/, '_');
  working = working.replace(/(?:^|_)(?:PART|PT)_?\d{1,2}(?:_|$)/, '_');
  working = working.replace(/(?:^|_)SIDE_?[AB](?:_|$)/, '_');

  // Format markers
  working = working.replace(/(?:^|_)(?:NTSC|PAL|WIDESCREEN|FULLSCREEN|WS|FS|HD|DVD|BD)(?:_|$)/g, '_');

  // Trailing/leading separators after stripping
  working = working.replace(/^_+|_+$/g, '').replace(/_{2,}/g, '_');

  const search = working
    .replace(/_/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());

  return { search, season, disc, isLikelyTV: season !== null };
}

// ---------- Scan ----------

async function scan() {
  const status = $('#scan-status');
  const discSec = $('#disc-meta-section');
  const titlesSec = $('#titles-section');
  status.textContent = 'Scanning…';
  status.classList.remove('error');
  status.hidden = false;
  discSec.hidden = true;
  titlesSec.hidden = true;
  try {
    const res = await fetch('/api/scan');
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    state.scan = data;
    state.selectedMeta = null;
    state.tvEpisodeMap.clear();
    state.tvEpisodeTitles.clear();
    if (!data.titles.length) {
      status.textContent = 'No titles found. Insert a DVD and click Rescan.';
      return;
    }
    renderDiscMeta(data);
    renderTitles(data);
    status.hidden = true;
    discSec.hidden = false;
    titlesSec.hidden = false;
  } catch (err) {
    status.textContent = `Scan failed: ${err.message || err}`;
    status.classList.add('error');
  }
}

// ---------- Disc metadata panel ----------

function renderDiscMeta(scan) {
  $('#disc-label').textContent = scan.dvd_label;

  // Parse the label, set search prefill + season + auto-toggle content type.
  const parsed = parseDiscLabel(scan.dvd_label);
  $('#meta-search').value = parsed.search;
  $('#tv-season').value = parsed.season || 1;

  if (parsed.isLikelyTV && state.contentType !== 'tv') {
    state.contentType = 'tv';
    document.querySelector('input[name="content_type"][value="tv"]').checked = true;
  }

  $('#search-results').hidden = true;
  $('#meta-manual').hidden = true;
  $('#meta-selected').hidden = true;
  $('#tv-disc-info').hidden = (state.contentType !== 'tv');
  $('#manual-movie').hidden = (state.contentType !== 'movie');
  $('#manual-tv').hidden = (state.contentType !== 'tv');

  if (!state.tmdbEnabled) {
    $('#btn-search').disabled = true;
    $('#btn-search').title = 'TMDB_API_KEY not set; using manual entry only';
    $('#meta-manual').hidden = false;
  }
}

async function runSearch() {
  const q = $('#meta-search').value.trim();
  if (!q) return;
  $('#meta-manual').hidden = true;
  const res = await fetch(`/api/search?type=${state.contentType}&query=${encodeURIComponent(q)}`);
  if (!res.ok) {
    showRipStatus('Search failed', true);
    return;
  }
  const data = await res.json();
  renderSearchResults(data.results);
}

function renderSearchResults(results) {
  const list = $('#search-results');
  list.replaceChildren();
  if (!results.length) {
    const li = document.createElement('li');
    li.className = 'muted';
    li.textContent = 'No results. Click "Enter manually" to type it in.';
    list.appendChild(li);
    list.hidden = false;
    return;
  }
  const tpl = $('#search-result-tpl');
  for (const r of results) {
    const li = tpl.content.firstElementChild.cloneNode(true);
    const poster = $('.poster', li);
    if (r.poster_url) {
      poster.src = r.poster_url;
    } else {
      poster.alt = '(no poster)';
    }
    const titleText = (state.contentType === 'movie') ? r.title : r.name;
    $('.search-title', li).textContent = titleText;
    $('.search-year', li).textContent = r.year ? `(${r.year})` : '';
    $('.search-overview', li).textContent = r.overview || '';
    $('.search-pick', li).addEventListener('click', () => {
      pickResult(r);
    });
    list.appendChild(li);
  }
  list.hidden = false;
}

async function pickResult(r) {
  if (state.contentType === 'movie') {
    state.selectedMeta = { type: 'movie', title: r.title, year: r.year, tmdb_id: r.id };
  } else {
    const season = Number($('#tv-season').value) || 1;
    state.selectedMeta = { type: 'tv', show: r.name, season, tmdb_id: r.id };
    await loadTVEpisodes(r.id, season);
  }
  paintSelectedMeta();
  renderTitleAssignments();
  updateRipButton();
}

// Reveal the manual-entry section so the user can type in title/show/etc.
// The actual save happens when they click the Apply button.
function showManualEntry() {
  $('#search-results').hidden = true;
  $('#meta-manual').hidden = false;
  $('#manual-movie').hidden = (state.contentType !== 'movie');
  $('#manual-tv').hidden = (state.contentType !== 'tv');
  const firstInput = (state.contentType === 'movie')
    ? $('#movie-title')
    : $('#tv-show');
  firstInput.focus();
  showRipStatus('', false);
}

function applyManual() {
  if (state.contentType === 'movie') {
    const title = $('#movie-title').value.trim();
    if (!title) {
      showRipStatus('Title is required', true);
      $('#movie-title').focus();
      return;
    }
    const year = Number($('#movie-year').value) || null;
    state.selectedMeta = { type: 'movie', title, year };
  } else {
    const show = $('#tv-show').value.trim();
    if (!show) {
      showRipStatus('Show is required', true);
      $('#tv-show').focus();
      return;
    }
    const season = Number($('#tv-season').value) || 1;
    state.selectedMeta = { type: 'tv', show, season };
  }
  paintSelectedMeta();
  renderTitleAssignments();
  updateRipButton();
}

function paintSelectedMeta() {
  const el = $('#meta-selected');
  if (!state.selectedMeta) {
    el.hidden = true;
    return;
  }
  el.replaceChildren();
  const text = document.createElement('span');
  if (state.selectedMeta.type === 'movie') {
    const y = state.selectedMeta.year ? ` (${state.selectedMeta.year})` : '';
    text.textContent = `Movie: ${state.selectedMeta.title}${y}`;
  } else {
    text.textContent = `TV: ${state.selectedMeta.show} — Season ${state.selectedMeta.season}`;
  }
  const change = document.createElement('button');
  change.type = 'button';
  change.className = 'change-meta';
  change.textContent = 'change';
  change.addEventListener('click', () => {
    state.selectedMeta = null;
    el.hidden = true;
    $('#search-results').hidden = true;
    $('#meta-manual').hidden = false;
    renderTitleAssignments();
    updateRipButton();
  });
  el.appendChild(text);
  el.appendChild(change);
  el.hidden = false;
  $('#search-results').hidden = true;
  $('#meta-manual').hidden = true;
}

async function loadTVEpisodes(tvId, season) {
  state.tvEpisodeTitles.clear();
  try {
    const res = await fetch(`/api/tv/${tvId}/seasons/${season}/episodes`);
    if (!res.ok) return;
    const eps = await res.json();
    for (const ep of eps) {
      if (ep.name) state.tvEpisodeTitles.set(ep.episode_number, ep.name);
    }
  } catch (_) { /* ignore */ }
}

// ---------- Titles table ----------

function renderTitles(scan) {
  const body = $('#titles-body');
  body.replaceChildren();
  const tpl = $('#title-row-tpl');
  for (const title of scan.titles) {
    const row = tpl.content.firstElementChild.cloneNode(true);
    row.dataset.titleIndex = title.index;
    const pick = $('.pick', row);
    pick.checked = !!title.is_main_feature;
    pick.addEventListener('change', () => {
      renderTitleAssignments();
      updateRipButton();
    });

    $('.title-num', row).textContent = `#${title.index}`;
    if (title.is_main_feature) {
      const badge = document.createElement('span');
      badge.className = 'main-feature-badge';
      badge.textContent = 'Main feature';
      // Insert AFTER the title number, before the inline assignment elements.
      $('.title-num', row).after(badge);
    }
    $('.col-duration', row).textContent = fmtDuration(title.duration_seconds);
    $('.col-chapters', row).textContent = title.chapter_count;
    $('.col-frame', row).textContent = `${title.frame_width}×${title.frame_height}`;

    const epInput = $('.ep-num', row);
    epInput.addEventListener('change', () => {
      const v = Number(epInput.value) || 1;
      state.tvEpisodeMap.set(title.index, v);
      renderTitleAssignments();
    });

    const audioList = $('.audio-list', row);
    title.audio_tracks.forEach((t, i) => {
      const li = document.createElement('li');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.value = t.index;
      cb.checked = i === 0;
      cb.dataset.role = 'audio';
      const label = document.createElement('label');
      const desc = [t.language, t.codec, t.channels].filter(Boolean).join(' / ');
      label.append(cb, document.createTextNode(` #${t.index} `));
      const sub = document.createElement('span');
      sub.className = 'muted';
      sub.textContent = desc || '(track)';
      label.appendChild(sub);
      li.appendChild(label);
      audioList.appendChild(li);
    });

    const subsList = $('.subs-list', row);
    if (!title.subtitle_tracks.length) {
      const li = document.createElement('li');
      li.className = 'muted';
      li.textContent = 'None (forced subs auto-detected)';
      subsList.appendChild(li);
    } else {
      title.subtitle_tracks.forEach((s) => {
        const li = document.createElement('li');
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.value = s.index;
        cb.dataset.role = 'subtitle';
        // Auto-select English tracks so a full English subtitle track is embedded
        // by default (user can still toggle others). Matches "I want English subs".
        const lang = (s.language || '').toLowerCase();
        cb.checked = lang.startsWith('eng');
        const label = document.createElement('label');
        label.append(cb, document.createTextNode(` #${s.index} `));
        const sub = document.createElement('span');
        sub.className = 'muted';
        sub.textContent = s.language || s.description || '(subtitle)';
        label.appendChild(sub);
        li.appendChild(label);
        subsList.appendChild(li);
      });
    }
    body.appendChild(row);
  }
  renderTitleAssignments();
  updateRipButton();
}

// Show inline assignment widgets (episode input for TV, cd-N for Movie) only
// on picked rows. Default episode number = the title's own index, so #3 → E[3];
// user edits to the real episode number per row.
function renderTitleAssignments() {
  const rows = $$('#titles-body tr.title-row');
  const isTV = state.contentType === 'tv';
  let movieCounter = 0;

  for (const row of rows) {
    const movieEl = $('.assignment-movie', row);
    const tvEl = $('.assignment-tv', row);
    movieEl.hidden = true;
    tvEl.hidden = true;

    const picked = $('.pick', row).checked;
    if (!picked) continue;

    const idx = Number(row.dataset.titleIndex);
    if (isTV) {
      tvEl.hidden = false;
      const epNum = state.tvEpisodeMap.has(idx)
        ? state.tvEpisodeMap.get(idx)
        : idx;  // default = title index
      $('.ep-num', row).value = epNum;
    } else {
      movieEl.hidden = false;
      movieCounter += 1;
      movieEl.textContent = movieCounter === 1 ? 'cd1' : `cd${movieCounter}`;
    }
  }
}

function updateRipButton() {
  const anyPicked = $$('.pick:checked').length > 0;
  const metaOk = !!state.selectedMeta;
  $('#btn-rip').disabled = !(anyPicked && metaOk);
  if (!metaOk && anyPicked) {
    showRipStatus('Pick a movie or TV show above before ripping', false);
  } else {
    showRipStatus('', false);
  }
}

function showRipStatus(msg, isError) {
  const el = $('#rip-status');
  el.textContent = msg;
  el.classList.toggle('error', !!isError);
}

// ---------- Rip ----------

function collectSelections() {
  const out = [];
  let movieCounter = 0;
  const rows = $$('#titles-body tr.title-row');
  const isTV = state.contentType === 'tv';

  for (const row of rows) {
    if (!$('.pick', row).checked) continue;
    const index = Number(row.dataset.titleIndex);
    const audio_tracks = $$('input[data-role="audio"]:checked', row).map((cb) => Number(cb.value));
    const subtitle_tracks = $$('input[data-role="subtitle"]:checked', row).map((cb) => Number(cb.value));
    if (isTV) {
      // Read directly from the inline input; map keeps any manual override.
      const epNum = state.tvEpisodeMap.has(index)
        ? state.tvEpisodeMap.get(index)
        : Number($('.ep-num', row).value) || index;
      out.push({
        index,
        audio_tracks,
        subtitle_tracks,
        episode_number: epNum,
        episode_title: state.tvEpisodeTitles.get(epNum) || null,
      });
    } else {
      movieCounter += 1;
      out.push({
        index,
        audio_tracks,
        subtitle_tracks,
        part_index: movieCounter,
      });
    }
  }
  return out;
}

async function startRip() {
  if (!state.scan || !state.selectedMeta) return;
  const selections = collectSelections();
  if (!selections.length) return;
  const encoderMode = $('input[name="encoder_mode"]:checked').value;
  const body = {
    dvd_label: state.scan.dvd_label,
    encoder_mode: encoderMode,
    burn_forced_subtitles: $('#burn-forced').checked,
    content_type: state.selectedMeta.type,
    titles: selections,
  };
  if (state.selectedMeta.type === 'movie') {
    body.movie = {
      title: state.selectedMeta.title,
      year: state.selectedMeta.year || null,
      tmdb_id: state.selectedMeta.tmdb_id || null,
    };
  } else {
    body.tv = {
      show: state.selectedMeta.show,
      season: state.selectedMeta.season,
      tmdb_id: state.selectedMeta.tmdb_id || null,
    };
  }
  const res = await fetch('/api/rip', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const txt = await res.text();
    showRipStatus(`Failed to start rip: ${txt}`, true);
    return;
  }
  const { job_ids } = await res.json();
  await refreshJobs();
  job_ids.forEach(streamJob);
  showRipStatus(`Queued ${job_ids.length} job(s)`, false);
}

// ---------- Jobs ----------

async function refreshJobs() {
  const res = await fetch('/api/jobs');
  if (!res.ok) return;
  const jobs = await res.json();
  const list = $('#jobs-list');
  list.replaceChildren();
  for (const job of jobs) {
    list.appendChild(renderJob(job));
    streamJob(job.id);
  }
}

function renderJob(job) {
  const tpl = $('#job-item-tpl');
  const li = tpl.content.firstElementChild.cloneNode(true);
  li.dataset.id = job.id;
  updateJob(li, job);
  $('.job-cancel', li).addEventListener('click', async () => {
    await fetch(`/api/jobs/${job.id}/cancel`, { method: 'POST' });
  });
  return li;
}

function updateJob(li, job) {
  li.className = `job ${job.status}`;
  const path = job.output_path || '';
  const display = path.split('/').slice(-2).join('/'); // last two path segments
  $('.job-title', li).textContent = `Title ${job.title_index} → ${display}`;
  const statusEl = $('.job-status', li);
  statusEl.className = `job-status ${job.status}`;
  statusEl.textContent = job.status;
  $('.progress-bar', li).style.width = `${Math.max(0, Math.min(100, job.percent || 0))}%`;
  const meta = [];
  if (job.percent != null) meta.push(`${(job.percent).toFixed(1)}%`);
  if (job.fps != null) meta.push(`${job.fps.toFixed(1)} fps`);
  if (job.eta_seconds != null) meta.push(fmtEta(job.eta_seconds));
  $('.job-meta', li).textContent = meta.join(' · ');
  const errEl = $('.job-error', li);
  if (job.error) {
    errEl.textContent = job.error;
    errEl.hidden = false;
  } else {
    errEl.hidden = true;
  }
  const cancelBtn = $('.job-cancel', li);
  const terminal = ['done', 'failed', 'cancelled'].includes(job.status);
  cancelBtn.disabled = terminal;
  cancelBtn.hidden = terminal;
}

function streamJob(jobId) {
  if (state.jobStreams.has(jobId)) return;
  const es = new EventSource(`/api/jobs/${jobId}/progress`);
  state.jobStreams.set(jobId, es);
  es.addEventListener('progress', (e) => {
    const job = JSON.parse(e.data);
    let li = $(`#jobs-list .job[data-id="${jobId}"]`);
    if (!li) {
      li = renderJob(job);
      $('#jobs-list').appendChild(li);
    } else {
      updateJob(li, job);
    }
    if (['done', 'failed', 'cancelled'].includes(job.status)) {
      es.close();
      state.jobStreams.delete(jobId);
    }
  });
  es.onerror = () => {
    es.close();
    state.jobStreams.delete(jobId);
  };
}

// ---------- Init ----------

document.addEventListener('DOMContentLoaded', () => {
  $('#btn-rescan').addEventListener('click', scan);
  $('#btn-rip').addEventListener('click', startRip);
  $('#btn-search').addEventListener('click', runSearch);
  $('#btn-manual').addEventListener('click', showManualEntry);
  $('#btn-manual-apply').addEventListener('click', applyManual);
  $('#select-all').addEventListener('change', (e) => {
    $$('.pick').forEach((cb) => { cb.checked = e.target.checked; });
    renderTitleAssignments();
    updateRipButton();
  });
  $$('input[name="content_type"]').forEach((r) => {
    r.addEventListener('change', (e) => {
      state.contentType = e.target.value;
      state.selectedMeta = null;
      $('#meta-selected').hidden = true;
      $('#search-results').hidden = true;
      $('#manual-movie').hidden = (state.contentType !== 'movie');
      $('#manual-tv').hidden = (state.contentType !== 'tv');
      $('#tv-disc-info').hidden = (state.contentType !== 'tv');
      $('#meta-manual').hidden = false;
      renderTitleAssignments();
      updateRipButton();
    });
  });
  $('#tv-season').addEventListener('change', async () => {
    if (state.selectedMeta?.type === 'tv') {
      state.selectedMeta.season = Number($('#tv-season').value) || 1;
      if (state.selectedMeta.tmdb_id) {
        await loadTVEpisodes(state.selectedMeta.tmdb_id, state.selectedMeta.season);
      }
      paintSelectedMeta();
    }
  });
  $('#meta-search').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      runSearch();
    }
  });
  ['#movie-title', '#movie-year', '#tv-show', '#tv-season'].forEach((sel) => {
    $(sel).addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        applyManual();
      }
    });
  });
  scan();
  refreshJobs();
});
