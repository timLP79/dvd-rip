# CD Ripping (Music + Audiobooks) — Design

- **Date:** 2026-07-04
- **Status:** Approved design, pending implementation plan
- **Related:** extends the existing DVD/Blu-ray rip pipeline

## Summary

Add audio-CD ripping to the app. Two content modes share one extraction path:

- **Music** → error-corrected rip to **FLAC** (lossless), tagged, into `Music/<Artist>/<Album>/`.
- **Audiobook** → rip to **MP3** (spoken-word bitrate), tagged, into `Audiobooks/<Author>/<Title>/`.

Track metadata comes from a best-effort **MusicBrainz** disc-ID lookup with full **manual** fallback. A whole disc is **one queue job** that produces N tagged files. The feature reuses the existing single-worker queue, SSE progress, drive guard, and library ledger.

## Goals

- Detect an audio CD and list its tracks (with durations) fast, without a data filesystem.
- Auto-fill artist/album/track titles from MusicBrainz when the disc is known; allow manual entry otherwise.
- Rip a whole disc as one job to per-track FLAC (music) or MP3 (audiobook), correctly tagged.
- Write files into the existing output root under new `Music/` and `Audiobooks/` subtrees so the media server picks them up like everything else.
- Record each disc rip in the existing library ledger.
- Never require a real optical disc in tests.

## Non-goals (deferred to a later iteration)

- **M4B / chaptered audiobooks** (concatenate all discs + chapter markers into one file).
- **Cover-art embedding** (the media server fetches its own art).
- **AccurateRip / whipper-style** rip verification.
- **Free-text MusicBrainz search** (only disc-ID lookup + manual editing in v1; multiple disc-ID candidates *are* handled).
- **Music/audiobook library backfill + duplicate-check** (record new rips only; no scan of pre-existing files).
- **Per-second progress within a track** (per-track granularity only in v1).
- **Non-FLAC music / non-MP3 audiobook formats.**

## Context: what already exists and is reused

The app is a single FastAPI container that orchestrates CLI tools (HandBrake, MakeMKV) as argv subprocesses, parses their stdout for progress, and fans progress out over SSE. Relevant existing pieces the CD path reuses **unchanged**:

- `Ripper` single-worker `asyncio.Queue` + `_worker`, SSE subscriber fan-out (`subscribe`/`_update`), `is_busy()` drive guard, finished-job trimming, push notifications.
- `/api/scan` disc-format routing seam in `app/main.py`.
- `paths.sanitize()` and `paths.unique_path()`.
- `library.record_rip()` + the library page.

The design mirrors existing module conventions: `cd.py` parallels `bluray.py` (the only module touching its CLI tools); `musicbrainz.py` parallels `tmdb.py` (async HTTP metadata client).

## Design decisions (with rationale)

1. **Assemble CLI tools directly** (`libdiscid` + `cdparanoia` + `flac`/`lame`) rather than a turnkey tool (`abcde`/`whipper`). Fits the codebase's "Python orchestrates argv CLI tools and parses their output" pattern, gives full control over progress/paths/tagging, and keeps MusicBrainz as a separate confirm-then-rip step. `abcde`/`whipper` own metadata + paths and are hard to drive for SSE progress.
2. **Whole disc = one job → N files** (with per-track exclude checkboxes), not one job per track. Matches how albums/books work and keeps the queue readable.
3. **Dedicated `POST /api/cd/rip` endpoint + `CDRipRequest`** rather than overloading the video `RipRequest`. Keeps both request shapes clean; no optional-field soup.
4. **Per-track progress**, not per-sector. `cdparanoia`'s terminal progress bar is fiddly to parse; per-track updates (with the current track name in `phase`) are robust and granular enough at ~30 s/track.
5. **Content mode is user-selected** (Music default / Audiobook) on the CD panel. MusicBrainz failing is not a reliable "this is an audiobook" signal, so the app cannot auto-detect it.
6. **Scan always runs one MusicBrainz lookup regardless of mode.** The mode only changes encoder + output tree + tag mapping + field labels — never *whether* we look up. MusicBrainz has partial audiobook coverage (some discs present, many not), so the same "seed if found, else manual" flow serves both.
7. **`libdiscid` reads the disc ID and the TOC in one call** (`discid.read(device)`), doubling as the audio-CD detector (it raises on a non-audio disc). Avoids a separate `cdparanoia -Q` probe.

## Detailed design

### Detection & routing (`app/main.py`)

`/api/scan` gains an audio-CD probe first (cheapest, most specific):

```
1. cd.is_audio_cd(device)     → cd.scan(device)      # libdiscid, ~1s
2. dvd.is_dvd_video(device)   → dvd.scan(device)     # lsdvd, ~1–2s
3. bluray.detect_and_scan()   → its result           # makemkvcon, slow
4. → dvd.scan(device)  (last-resort fallback)
```

`cd.is_audio_cd` = "does `discid.read(device)` succeed?" A data DVD/BD raises `discid.DiscError` and falls through.

### `app/cd.py` (new — the only module touching `cdparanoia`/`libdiscid`)

Responsibilities:

- `is_audio_cd(device) -> bool` — try `discid.read`; True on success.
- `scan(device) -> ScanResult` — read TOC via `discid`, build `list[CDTrack]` (number, duration_seconds from sector length), compute `disc_id`, then best-effort `musicbrainz.lookup_disc(disc_id)` (short timeout) → attach candidate releases. Returns `ScanResult(disc_format="cd", tracks=[...], disc_id=..., musicbrainz=[...])`. TOC always returns even if MusicBrainz is down/empty.
- `rip_disc(...)` — the shared per-track loop (invoked by the ripper worker), parameterized by an **encoder strategy** and **path builder** chosen by mode:

```
for i, track in enumerate(selected_tracks):
    on_stage(f"Ripping track {track.number}: {track.title}")
    on_progress(i / total * 100)                 # per-track granularity
    wav = scratch/<n>.wav
    rc = cdparanoia <n> wav                       # error-corrected extract; register proc for cancel
    if rc != 0: raise RipError(tail)
    out_path = path_builder(...)                  # music_path or audiobook_path
    rc = encode(wav, out_path, tags)              # flac --best  OR  lame (VBR)
    wav.unlink()
    if rc != 0: raise RipError(tail)
on_progress(100)
```

- **Encoder strategy** — `encode_track(wav, out_path, tags, fmt)`:
  - `fmt="flac"` → `flac --best -o out_path --tag=ARTIST=… --tag=ALBUM=… --tag=ALBUMARTIST=… --tag=TITLE=… --tag=TRACKNUMBER=… --tag=TRACKTOTAL=… --tag=DATE=… [--tag=DISCNUMBER=…] wav`
  - `fmt="mp3"` → `lame` at a spoken-word VBR preset with ID3 tags (`--tt`/`--ta`/`--tl`/`--tn`/`--ty`/`--tv TPOS=…`).
- Testability seam: the subprocess spawner (and the disc reader) are injectable (like the scanners' `_run`/`commands` parameters), so the loop, progress, cleanup, and cancel are tested without a real drive.

### `app/musicbrainz.py` (new — async HTTP client, parallels `tmdb.py`)

- `lookup_disc(disc_id) -> list[MBRelease]` → `GET https://musicbrainz.org/ws/2/discid/{disc_id}?inc=recordings+artist-credits&fmt=json`. Returns 0..N candidate releases, each `{artist, album, year, tracks:[{number, title}]}`. A disc ID often matches several regional pressings → return all as candidates.
- **No API key** required — MusicBrainz needs only a descriptive `User-Agent` (set from a constant/env). Best-effort with a short timeout; failures are logged and yield `[]`.

### `app/models.py` (edits)

- `ContentType` gains `MUSIC = "music"` and `AUDIOBOOK = "audiobook"`.
- `CDTrack{ number:int, duration_seconds:int, title:Optional[str] }`.
- `MBRelease{ artist:str, album:str, year:Optional[int], tracks:list[CDTrack] }`.
- `ScanResult` gains `tracks: list[CDTrack] = []`, `disc_id: Optional[str] = None`, `musicbrainz: list[MBRelease] = []`, and `disc_format` widened to `Literal["dvd","bluray","cd"]`.
- `CDRipRequest{ mode: Literal["music","audiobook"], disc_id:Optional[str], creator:str, title:str, year:Optional[int], disc_number:int = 1, total_discs:Optional[int], tracks: list[CDTrackSelection] }` where `CDTrackSelection{ number:int, title:str, include:bool }`.
  - `creator` = Artist (music) / Author (audiobook); `title` = Album / Book. Mapped to tags + paths per mode in the ripper.
- `RipRecord` gains `artist: Optional[str] = None` (holds Artist/Author); music/audiobook rows use `title` for Album/Book.

### `app/paths.py` (edits)

- `music_path(root, *, artist, album, disc_number, total_discs, track_number, track_title) -> Path`
  `root/Music/<Artist>/<Album>/PREFIX - <Title>.flac`
- `audiobook_path(root, *, author, book, disc_number, total_discs, track_number, track_title) -> Path`
  `root/Audiobooks/<Author>/<Book>/PREFIX - <Title>.mp3`
- `PREFIX` = `f"{disc_number}-{track:02d}"` when `total_discs` and `total_discs > 1` (or `disc_number > 1`), else `f"{track:02d}"`. Multi-disc prefixes prevent cross-disc collisions so successive discs append into the same album/book folder. Reuses `sanitize()`.

### API (`app/main.py`)

- `/api/scan` — routing above; response is still `ScanResult` (now able to carry CD fields).
- `POST /api/cd/rip` (new) — validate `CDRipRequest` (≥1 included track; non-empty `creator`/`title`), call `ripper.enqueue_cd(...)`, return the created job id. Like the existing video `/api/rip`, it just enqueues; the single worker serializes access to the drive (no `is_busy()` guard — that guard is only on `/api/scan`, which touches the drive synchronously).

### Ripper (`app/ripper.py`)

- `enqueue_cd(*, request: CDRipRequest) -> Job` — build the output folder + first `output_path` (album/book dir), create one `Job`, stash the request in a CD-specific selection map, seed a `RipRecord` (`content_type` = music/audiobook, `artist` = creator, `title` = title), enqueue.
- `_run_job` dispatches on disc format: `cd` → `_run_cd_job`, else existing DVD/BD paths.
- `_run_cd_job(job_id)` — calls `cd.rip_disc(...)` with the mode-selected encoder + path builder, wiring `on_progress`/`on_stage` to `_update` (reusing the `phase` field for "Ripping track N: Title") and `register_proc` for cancellation (same pattern as `_run_bluray_job`). On success → `DONE`, record. On `cdparanoia`/`lame`/`flac` failure → `FAILED` with a tail of the tool's output. On cancel/fail → remove the files this job wrote (matches the video "no partial garbage" rule) and sweep the scratch WAV.

### Library (`app/library.py`)

- `record_rip` persists music/audiobook rows (new `artist` column; `content_type` in {music, audiobook}; `output_path` = album/book folder). One disc rip = one row.
- **Schema migration:** the `artist` column is added additively — `init_db` runs `ALTER TABLE rips ADD COLUMN artist TEXT` when the column is absent, so existing `library.db` files upgrade in place without data loss.
- Library page renders the new rows (Artist/Author + Album/Book + track count/size).
- No music/audiobook backfill or duplicate-check in v1 (deferred).

### UI (`app/templates/index.html`, `app/static/app.js`, `style.css`)

When `scan.disc_format === "cd"`, render a **CD panel** instead of the video panel:

- **Mode toggle:** Music (default) / Audiobook.
- **Music:** Artist / Album / Year fields (pre-filled from the best MusicBrainz candidate); a **release picker** when `musicbrainz.length > 1`; a track table (number · editable title · duration · include checkbox).
- **Audiobook:** Author / Title (/ optional Narrator) fields; **Disc number** (which disc this is) + optional **Total discs**; same track table (titles often generic and editable).
- **Rip Album / Rip Disc** button → `POST /api/cd/rip`. Job progress uses the existing SSE job UI (phase shows the current track). No encoder-mode / subtitle / TMDB controls. Existing video UI untouched.

### Container & build

- `Dockerfile` apt list adds `cdparanoia flac lame libdiscid0`; `requirements.txt` adds `discid` (python-libdiscid binding).
- `make build` produces a new image **without touching a running container** — safe to build during an in-progress rip; only `make restart`/`make down` interrupt a rip.
- No changes to the optical-device mapping (the `/dev/sr0` + `BD_SG_DEVICE` sg-node mapping already covers CD access).

## Data flow

```
Insert CD → GET /api/scan
  → cd.is_audio_cd (libdiscid) ✓
  → cd.scan: discid.read → TOC + disc_id → musicbrainz.lookup_disc (best-effort)
  → ScanResult{disc_format:"cd", tracks, disc_id, musicbrainz[]}
UI CD panel: pick mode, confirm/edit metadata + track selection
  → POST /api/cd/rip {mode, creator, title, disc_number, tracks[]}
  → ripper.enqueue_cd → one Job
Worker → _run_cd_job → cd.rip_disc:
  for each included track: cdparanoia → wav → (flac|lame) tag → out_path; unlink wav
  progress/phase over SSE per track
  → DONE, library.record_rip
Files appear under <output root>/Music/… or /Audiobooks/…
```

## Error handling

- **Not an audio CD** → `is_audio_cd` false; routing falls through (not an error).
- **MusicBrainz miss / down** → scan still returns the TOC; UI shows manual fields. Logged, best-effort.
- **`cdparanoia` read error** (scratched disc) → job `FAILED` with a tail of `cdparanoia` stderr; job-written files removed; scratch swept.
- **`flac`/`lame` encode error** → job `FAILED` with tool output tail.
- **Scratch disk full** → job `FAILED` (surfaced from the tool).
- **Cancel** → kill the live `cdparanoia`/encoder process; remove files this job wrote + the scratch WAV.

## Testing (no real drive, per repo convention)

- `cd.py` TOC parsing → recorded `libdiscid` TOC fixtures → assert `CDTrack` list (counts, durations).
- `musicbrainz.py` → `respx`-mocked JSON (single-release + multi-release fixtures), like the TMDB tests.
- `paths.music_path` / `paths.audiobook_path` → unit tests: single vs multi-disc prefixing, sanitization, "Various Artists", collision behavior.
- `_run_cd_job` / `cd.rip_disc` → injected fake `cdparanoia`/`flac`/`lame` runners → assert per-track loop, progress ticks, tag args, output paths, WAV cleanup, cancel/fail cleanup.
- No test reads a physical CD.

## Verification (post-implementation, real disc)

Build the image (`make build`), then with a music CD and an audiobook CD in the drive: scan → confirm the panel/metadata → rip → confirm tagged FLAC/MP3 files land under `Music/…` / `Audiobooks/…` with correct names/tags and a library-ledger row. (Requires the drive to be free; a video rip may be in progress.)

## Open questions

None blocking. Future iterations may add M4B/chapters, cover art, AccurateRip, free-text MusicBrainz search, and library backfill/duplicate-check.
