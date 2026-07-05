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

function aspectLabel(a) {
  if (a === 'widescreen') return 'Widescreen';
  if (a === 'fullscreen') return 'Full screen';
  return '—';
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
    if (res.status === 409) {
      // A rip is using the drive — don't rescan it. Progress shows in the Jobs section.
      status.textContent = 'Rip in progress — disc scan paused. Watch progress in Jobs below.';
      status.classList.remove('error');
      status.hidden = false;
      return;
    }
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    state.scan = data;
    state.selectedMeta = null;
    state.tvEpisodeMap.clear();
    state.tvEpisodeTitles.clear();
    $('#cd-section').hidden = true;
    if (data.disc_format === 'cd') {
      renderCDPanel(data);
      status.hidden = true;
      return;
    }
    if (!data.titles.length) {
      status.textContent = 'No titles found. Insert a disc and click Rescan.';
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
  const badge = $('#disc-format-badge');
  badge.textContent = scan.disc_format === 'bluray' ? 'Blu-ray' : 'DVD';
  badge.hidden = false;

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

async function checkDuplicate() {
  const el = $('#dup-warning');
  el.hidden = true;
  if (!state.selectedMeta || state.selectedMeta.type !== 'movie') return;
  const m = state.selectedMeta;
  const params = new URLSearchParams({ content_type: 'movie' });
  if (m.tmdb_id) params.set('tmdb_id', m.tmdb_id);
  params.set('title', m.title);
  if (m.year) params.set('year', m.year);
  try {
    const res = await fetch(`/api/library/check?${params.toString()}`);
    if (!res.ok) return;
    const { matches } = await res.json();
    if (matches && matches.length) {
      const when = (matches[0].created_at || '').slice(0, 10);
      const y = m.year ? ` (${m.year})` : '';
      el.textContent = `⚠ Already ripped: ${m.title}${y} — ${when}. You can still rip it again.`;
      el.hidden = false;
    }
  } catch (_) { /* advisory only */ }
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
    $('#dup-warning').hidden = true;
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
  checkDuplicate();
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
    const frameCell = $('.col-frame', row);
    frameCell.textContent = aspectLabel(title.aspect);
    frameCell.title = `${title.frame_width}×${title.frame_height}`;
    row.dataset.aspect = title.aspect || '';

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

// ---------- Audio CD ----------

function cdMode() {
  const el = document.querySelector('input[name="cd_mode"]:checked');
  return el ? el.value : 'music';
}

function renderCDPanel(scan) {
  $('#disc-meta-section').hidden = true;
  $('#titles-section').hidden = true;
  $('#cd-section').hidden = false;
  $('#cd-disc-label').textContent = 'Audio CD';

  // Release picker when MusicBrainz returned >1 candidate.
  const picker = $('#cd-release-picker');
  picker.innerHTML = '';
  const releases = scan.musicbrainz || [];
  picker.hidden = releases.length <= 1;
  if (releases.length > 1) {
    releases.forEach((rel, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'secondary';
      b.textContent = `${rel.artist} — ${rel.album}${rel.year ? ' (' + rel.year + ')' : ''}`;
      b.addEventListener('click', () => applyCDRelease(scan, i));
      picker.appendChild(b);
    });
  }
  // Default to Audiobook when MusicBrainz tags the release as one (secondary type),
  // so a matched audiobook disc doesn't open in Music mode.
  const wantAudiobook = releases.length > 0 && !!releases[0].is_audiobook;
  const modeEl = document.querySelector(
    `input[name="cd_mode"][value="${wantAudiobook ? 'audiobook' : 'music'}"]`);
  if (modeEl) modeEl.checked = true;

  applyCDRelease(scan, 0);
  applyCDModeLabels();
}

function applyCDRelease(scan, index) {
  const rel = (scan.musicbrainz || [])[index] || null;
  $('#cd-creator').value = rel ? rel.artist : '';
  $('#cd-title').value = rel ? rel.album : '';
  $('#cd-year').value = rel && rel.year ? rel.year : '';
  $('#cd-disc-number').value = rel && rel.disc_position ? rel.disc_position : 1;
  $('#cd-total-discs').value = rel && rel.total_discs ? rel.total_discs : '';

  // Titles come from the MB medium when present; else blank, keyed by track number.
  const mbTitles = new Map();
  if (rel) rel.tracks.forEach((t) => mbTitles.set(t.number, t.title || ''));

  const body = $('#cd-track-body');
  body.innerHTML = '';
  const tpl = $('#cd-track-row-tpl');
  scan.tracks.forEach((t) => {
    const row = tpl.content.firstElementChild.cloneNode(true);
    row.querySelector('.cd-num').textContent = t.number;
    row.querySelector('.cd-dur').textContent = fmtDuration(t.duration_seconds);
    const titleInput = row.querySelector('.cd-track-title');
    titleInput.value = mbTitles.get(t.number) || '';
    titleInput.dataset.number = t.number;
    body.appendChild(row);
  });
}

function applyCDModeLabels() {
  const audiobook = cdMode() === 'audiobook';
  $('#cd-creator-label').textContent = audiobook ? 'Author' : 'Artist';
  $('#cd-title-label').textContent = audiobook ? 'Book' : 'Album';
  $('#cd-creator').placeholder = audiobook ? 'Author' : 'Artist';
  $('#cd-title').placeholder = audiobook ? 'Book' : 'Album';
  $('#cd-disc-num-field').hidden = !audiobook;
  $('#cd-total-discs-field').hidden = !audiobook;
  $('#cd-audiobook-note').hidden = !audiobook;
  $('#cd-tracks').hidden = audiobook;          // never render the (up to 99-row) table for audiobook
  $('#cd-audiobook-trim').hidden = !audiobook;
  $('#btn-cd-rip').textContent = audiobook ? 'Rip Disc' : 'Rip Album';
}

function collectCDRequest() {
  const tracks = $$('#cd-track-body .cd-track-row').map((row) => ({
    number: parseInt(row.querySelector('.cd-track-title').dataset.number, 10),
    title: row.querySelector('.cd-track-title').value.trim(),
    include: row.querySelector('.cd-pick').checked,
  }));
  if (cdMode() === 'audiobook') {
    const skipFirst = $('#cd-skip-first').checked;
    const skipLast = $('#cd-skip-last').checked;
    tracks.forEach((t, i) => {
      t.include = !((skipFirst && i === 0) || (skipLast && i === tracks.length - 1));
    });
  }
  const total = $('#cd-total-discs').value;
  return {
    mode: cdMode(),
    disc_id: (state.scan && state.scan.disc_id) || null,
    creator: $('#cd-creator').value.trim(),
    title: $('#cd-title').value.trim(),
    year: $('#cd-year').value ? parseInt($('#cd-year').value, 10) : null,
    disc_number: parseInt($('#cd-disc-number').value || '1', 10),
    total_discs: total ? parseInt(total, 10) : null,
    tracks,
  };
}

async function startCDRip() {
  const statusEl = $('#cd-rip-status');
  const req = collectCDRequest();
  if (!req.creator || !req.title) {
    statusEl.textContent = 'Enter artist/author and album/book first.';
    statusEl.classList.add('error');
    return;
  }
  if (!req.tracks.some((t) => t.include)) {
    statusEl.textContent = 'Select at least one track.';
    statusEl.classList.add('error');
    return;
  }
  statusEl.classList.remove('error');
  statusEl.textContent = 'Queuing…';
  try {
    const res = await fetch('/api/cd/rip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(req),
    });
    if (!res.ok) throw new Error(await res.text());
    const { job_ids } = await res.json();
    statusEl.textContent = `Queued (${job_ids.length}).`;
    refreshJobs();
    job_ids.forEach(streamJob);
  } catch (err) {
    statusEl.textContent = `Rip failed: ${err.message || err}`;
    statusEl.classList.add('error');
  }
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
        aspect: row.dataset.aspect || null,
      });
    } else {
      movieCounter += 1;
      out.push({
        index,
        audio_tracks,
        subtitle_tracks,
        part_index: movieCounter,
        aspect: row.dataset.aspect || null,
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
    disc_format: (state.scan && state.scan.disc_format) || 'dvd',
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
  if (job.phase) meta.push(job.phase);  // backend sends a human-readable stage label
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
  $('#btn-cd-rip').addEventListener('click', startCDRip);
  $$('input[name="cd_mode"]').forEach((el) =>
    el.addEventListener('change', applyCDModeLabels));
  $('#cd-select-all').addEventListener('change', (e) => {
    $$('#cd-track-body .cd-pick').forEach((c) => { c.checked = e.target.checked; });
  });
  scan();
  refreshJobs();
});

// ---- Web Push notify toggle ------------------------------------------------
function urlB64ToUint8Array(b64) {
  const pad = '='.repeat((4 - (b64.length % 4)) % 4);
  const base = (b64 + pad).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(base);
  return Uint8Array.from([...raw].map((c) => c.charCodeAt(0)));
}

async function initPushUI() {
  const btn = document.getElementById('btn-notify');
  if (!btn) return;
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) return; // unsupported: stay hidden

  let key = '';
  try {
    const info = await (await fetch('/api/push/key')).json();
    if (!info.enabled) return; // push disabled server-side: stay hidden
    key = info.key;
  } catch (e) { return; }

  btn.hidden = false;
  if (!window.isSecureContext) {
    btn.textContent = 'Open via HTTPS to enable';
    btn.disabled = true;
    return;
  }

  let reg;
  const render = (sub) => {
    btn.classList.toggle('active', !!sub);
    btn.textContent = sub ? 'Notifications on' : 'Enable notifications';
  };
  try {
    reg = await navigator.serviceWorker.register('/sw.js');
    render(await reg.pushManager.getSubscription());
  } catch (e) {
    console.error('Push UI init failed:', e);
    btn.textContent = 'Notifications unavailable';
    btn.disabled = true;
    return;
  }

  btn.addEventListener('click', async () => {
    try {
      const existing = await reg.pushManager.getSubscription();
      if (existing) {
        await existing.unsubscribe();
        const res = await fetch('/api/push/unsubscribe', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: existing.endpoint }),
        });
        if (!res.ok) {
          console.error('unsubscribe rejected by server', res.status);
        }
        render(null);
        return;
      }
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') { btn.textContent = 'Notifications blocked'; return; }
      const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlB64ToUint8Array(key),
      });
      const res = await fetch('/api/push/subscribe', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sub),
      });
      if (!res.ok) {
        await sub.unsubscribe();
        render(null);
        btn.textContent = 'Subscribe failed';
        console.error('subscribe rejected by server', res.status);
        return;
      }
      render(sub);
    } catch (e) {
      console.error('Notification toggle failed:', e);
      btn.textContent = 'Try again';
      render(await reg.pushManager.getSubscription());
    }
  });
}

initPushUI().catch(console.error);
