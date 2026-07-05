# CD Ripping (Music + Audiobooks) — Design

- **Date:** 2026-07-04 (revised same day after probing two real audiobook discs — see "Field evaluation")
- **Status:** ✅ **SHIPPED** — implemented per `docs/superpowers/plans/2026-07-04-cd-rip-implementation.md`, merged to `main` (PR #2), hardware-verified with a real rip. Library-page support merged separately (PR #3).
- **Related:** extends the existing DVD/Blu-ray rip pipeline

> **Shipped beyond this original v1 design** (added during live testing; this doc predates them):
> - **Audiobook auto-detect** — an exact MusicBrainz hit tagged `Audiobook`/`Spokenword` (release-group secondary type) now defaults the UI to Audiobook mode. (Design decision 5 said this wasn't possible; it referred to MB *misses* — a tagged *hit* is a reliable signal.)
> - **Split-by-MusicBrainz** — the "deferred" Design decision 9 is now implemented: a gated "Split into tracks" toggle (shown only when the exact hit carries per-track titles) emits one titled MP3 per track instead of the whole-disc join.
> - **Real-time within-track progress** — CD progress now polls the growing WAV against the track's known length (bytes-based), so the bar moves like HandBrake/MakeMKV rather than jumping per finished track. (Supersedes Design decision 4's per-track-only stance.)
> - **Library page** — shows `Artist — Album (Year)` for music/audiobook rows and adds Music/Audiobook/CD filters (PR #3).
> - **Known gap (accepted):** whole-disc audiobook *join* mode holds the bar at 90% during the final single `lame` encode of the joined WAV (no encode-progress). Split mode and music don't have this. Tracked separately.

## Summary

Add audio-CD ripping to the app. Two content modes share one extraction path:

- **Music** → error-corrected rip to **FLAC** (lossless), tagged, **one file per track**, into `Music/<Artist>/<Album>/`.
- **Audiobook** → error-corrected rip to **MP3** (spoken-word bitrate), tagged, **joined into one file per disc**, into `Audiobooks/<Author>/<Book>/`.

Track metadata comes from a best-effort **exact MusicBrainz disc-ID** lookup with full **manual** fallback. A whole disc is **one queue job**; music emits N per-track files, audiobook emits one joined disc file. The feature reuses the existing single-worker queue, SSE progress, drive guard, and library ledger.

## Goals

- Detect an audio CD and list its tracks (with durations) fast, without a data filesystem.
- Auto-fill artist/album/track titles from MusicBrainz when the disc is known; allow manual entry otherwise.
- Rip a whole disc as one job: per-track FLAC for music, one joined MP3 per disc for audiobooks, correctly tagged.
- Write files into the existing output root under new `Music/` and `Audiobooks/` subtrees so the media server picks them up like everything else.
- Record each disc rip in the existing library ledger.
- Never require a real optical disc in tests.

## Non-goals (deferred to a later iteration)

- **M4B / in-file chapter markers.** Not planned, and never derived from CD track boundaries — they are not chapters (see the field evaluation and Design decision 9).
- **"Split by MusicBrainz" per-title audiobook files.** The sanctioned *future* alternative to one-file-per-disc, gated on an exact disc-ID hit that carries per-track titles (Design decision 9). v1 always does one file per disc.
- **Fuzzy-TOC / free-text MusicBrainz lookup.** Only the exact disc-ID lookup is used; fuzzy-TOC matches on track count and returns unrelated discs (field evaluation), so manual entry is the fallback. Multiple *exact* disc-ID candidates *are* handled.
- **Cover-art embedding** (the media server fetches its own art).
- **AccurateRip / whipper-style** rip verification.
- **Music/audiobook library backfill + duplicate-check** (record new rips only; no scan of pre-existing files).
- **Per-second progress within a track** (per-track granularity only in v1).
- **Non-FLAC music / non-MP3 audiobook formats.**

## Field evaluation — two real audiobook discs (2026-07-04)

Before finalizing the audiobook output model, two physical discs were probed directly (TOC via the kernel CD-ROM ioctl; MusicBrainz/freedb disc IDs computed and looked up; CD-Text read via SG_IO). They bracket the problem:

| | *Fragile Things* (Neil Gaiman), disc 5 | An unidentified novel, one disc |
|---|---|---|
| Tracks | 8 | **96** |
| Track length | 4–13 min, irregular | **~45 s, uniform** (median 45 s; 95% within 35–52 s) |
| What a track represents | a story, or a mid-story chunk | an **arbitrary ~45 s seek mark** |
| MusicBrainz | exact disc-ID **hit** — real per-track titles + `medium.position = 5 of 9` | exact **miss**; fuzzy-TOC returned only false positives (bird-song guides, jingle libraries); no CD-Text |
| Usable metadata | MusicBrainz | **none — manual entry only** |

Conclusions that shaped this design:

1. **A CD track is never a chapter.** Gaiman splits mid-story (`Harlequin Valentine` → `I caper ahead of her.`); the novel splits every ~45 s. Neither boundary is semantic, and a single story can span multiple tracks *and* discs (`A Study in Emerald` runs across 5 tracks on 2 discs).
2. **Per-track MP3 output is wrong for audiobooks.** The novel would yield ~96 unnamed 45-second files per disc (~800+ across the set). → audiobook mode joins to **one file per disc** (Design decision 8).
3. **Fuzzy-TOC lookup is noise, not a fallback.** It matches on track count, returning unrelated discs. Only the exact disc-ID lookup is trustworthy; everything else is manual.
4. **CD-Text is effectively absent** on audiobook discs (neither disc carried any) — not a viable metadata source.
5. **Exact disc-ID hits carry the disc position** (`5 of 9`) → auto-fill disc number / total discs, and only fall back to manual entry when the lookup misses.

## Context: what already exists and is reused

The app is a single FastAPI container that orchestrates CLI tools (HandBrake, MakeMKV) as argv subprocesses, parses their stdout for progress, and fans progress out over SSE. Relevant existing pieces the CD path reuses **unchanged**:

- `Ripper` single-worker `asyncio.Queue` + `_worker`, SSE subscriber fan-out (`subscribe`/`_update`), `is_busy()` drive guard, finished-job trimming, push notifications.
- `/api/scan` disc-format routing seam in `app/main.py`.
- `paths.sanitize()` and `paths.unique_path()`.
- `library.record_rip()` + the library page.

The design mirrors existing module conventions: `cd.py` parallels `bluray.py` (the only module touching its CLI tools); `musicbrainz.py` parallels `tmdb.py` (async HTTP metadata client).

## Design decisions (with rationale)

1. **Assemble CLI tools directly** (`libdiscid` + `cdparanoia` + `flac`/`lame`) rather than a turnkey tool (`abcde`/`whipper`). Fits the codebase's "Python orchestrates argv CLI tools and parses their output" pattern, gives full control over progress/paths/tagging, and keeps MusicBrainz as a separate confirm-then-rip step. `abcde`/`whipper` own metadata + paths and are hard to drive for SSE progress.
2. **Whole disc = one job**, not one job per track. Output granularity follows the aggregation strategy (Design decision 8): music emits N per-track files (with per-track exclude checkboxes); audiobook emits one joined file per disc (whole disc, optional intro/outro trim). Matches how albums/books are consumed and keeps the queue readable.
3. **Dedicated `POST /api/cd/rip` endpoint + `CDRipRequest`** rather than overloading the video `RipRequest`. Keeps both request shapes clean; no optional-field soup.
4. **Per-track progress**, not per-sector. `cdparanoia`'s terminal progress bar is fiddly to parse; per-track updates (with the current track name in `phase`) are robust and granular enough at ~30 s/track.
5. **Content mode is user-selected** (Music default / Audiobook) on the CD panel. MusicBrainz failing is not a reliable "this is an audiobook" signal, so the app cannot auto-detect it.
6. **Scan always runs one MusicBrainz lookup regardless of mode.** The mode changes aggregation + encoder + output tree + tag mapping + field labels — never *whether* we look up. MusicBrainz has partial audiobook coverage (some discs present, many not), so the same "seed if found, else manual" flow serves both.
7. **`libdiscid` reads the disc ID and the TOC in one call** (`discid.read(device)`), doubling as the audio-CD detector (it raises on a non-audio disc). Avoids a separate `cdparanoia -Q` probe.
8. **Aggregation granularity differs by mode.** Music → one file **per track** (a track is a song). Audiobook → **one file per disc**: rip every included track, concatenate, encode once. CD track marks on audiobooks are meaningless (~45 s seek points or mid-story cuts, per the field evaluation), so per-track files produce dozens of unnamed fragments. Music and audiobook therefore share the *extraction* step (cdparanoia → WAV) but diverge at an **aggregation strategy** (encode-each vs concat-then-encode-once), on top of the existing encoder/path/tag differences.
9. **No chapter markers / M4B in any near-term version — and never derived from CD track boundaries.** Chapters need real boundaries; a CD offers only track marks (proven meaningless) or MusicBrainz titles (present sometimes, and even then mixing real titles with continuation fragments). The only correct future path is an **optional "split by MusicBrainz" mode**, available *only* on an exact disc-ID hit that carries per-track titles: divide the disc audio at MB track offsets into per-title **files** (file boundaries, not embedded chapter atoms — simpler and better supported by media servers than M4B). Deferred; gated on that metadata existing; output quality tracks the catalogue (prose that spills across tracks yields fragment-named files until a later "merge continuations" refinement). This is the file-splitting form of the user's "divide it by the online breakdown" instinct.

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
- `rip_disc(...)` — invoked by the ripper worker, parameterized by an **aggregation strategy** + **encoder** + **path builder** chosen by mode. Both modes share track *extraction* (cdparanoia → WAV) and diverge on how the WAVs become output files.

  **Music — one file per track:**
```
for i, track in enumerate(selected_tracks):
    on_stage(f"Ripping track {track.number}: {track.title}")
    on_progress(i / total * 100)                 # per-track granularity
    wav = scratch/<n>.wav
    rc = cdparanoia <n> wav                       # error-corrected; register proc for cancel
    if rc != 0: raise RipError(tail)
    encode_flac(wav, music_path(...), tags)       # one FLAC per song
    wav.unlink()
on_progress(100)
```

  **Audiobook — one file per disc (join):**
```
wavs = []
for i, track in enumerate(selected_tracks):      # selected = whole disc, minus any trimmed intro/outro
    on_stage(f"Ripping track {track.number} of {total}")
    on_progress(i / total * 90)                  # ripping = first 90%
    wav = scratch/<n>.wav
    rc = cdparanoia <n> wav                       # register proc for cancel
    if rc != 0: raise RipError(tail)
    wavs.append(wav)
on_stage("Encoding disc")
encode_mp3(concat(wavs), audiobook_path(...), tags)   # ONE lame encode of the joined audio
on_progress(100)
for w in wavs: w.unlink()
```
  (Concatenation is a raw join of equal-format PCM WAVs — `cdparanoia` can also stream a track range to a single WAV; per-track extraction is kept for progress granularity, cancel points, and intro/outro trimming.)

- **Encoders:**
  - `encode_flac(wav, out_path, tags)` → `flac --best -o out_path --tag=ARTIST=… --tag=ALBUM=… --tag=ALBUMARTIST=… --tag=TITLE=… --tag=TRACKNUMBER=… --tag=TRACKTOTAL=… --tag=DATE=… [--tag=DISCNUMBER=…] wav` (per song).
  - `encode_mp3(joined_wav, out_path, tags)` → `lame` at a spoken-word VBR preset with ID3 tags for the **whole disc**: `--ta <author> --tl <book> --tt "<book>"` (or `"<book> — Disc N"`), `--tn <disc#>`, `--tv TPOS=<disc#>/<total>`, `--ty <year>`.
- Testability seam: the subprocess spawner, the WAV join, and the disc reader are injectable (like the scanners' `_run`/`commands` parameters), so the loop, join, progress, cleanup, and cancel are tested without a real drive.

### `app/musicbrainz.py` (new — async HTTP client, parallels `tmdb.py`)

- `lookup_disc(disc_id) -> list[MBRelease]` → `GET https://musicbrainz.org/ws/2/discid/{disc_id}?inc=recordings+artist-credits&fmt=json`. Returns 0..N candidate releases, each `{artist, album, year, disc_position, total_discs, tracks:[{number, title}]}`. A disc ID can match several regional pressings → return all as candidates. The matched **medium carries `position`, and the release its medium count** → surface as `disc_position` / `total_discs` so the UI pre-fills the disc-number fields (validated: the Gaiman disc reported `5 of 9`).
- **Exact disc-ID lookup only.** No fuzzy-TOC and no free-text search: the fuzzy-TOC endpoint matches on track count and returns unrelated discs (field evaluation), so it is worse than nothing. A miss → `[]` → manual entry.
- **No API key** required — MusicBrainz needs only a descriptive `User-Agent` (set from a constant/env). Best-effort with a short timeout; failures are logged and yield `[]`.

### `app/models.py` (edits)

- `ContentType` gains `MUSIC = "music"` and `AUDIOBOOK = "audiobook"`.
- `CDTrack{ number:int, duration_seconds:int, title:Optional[str] }`.
- `MBRelease{ artist:str, album:str, year:Optional[int], tracks:list[CDTrack] }`.
- `ScanResult` gains `tracks: list[CDTrack] = []`, `disc_id: Optional[str] = None`, `musicbrainz: list[MBRelease] = []`, and `disc_format` widened to `Literal["dvd","bluray","cd"]`.
- `CDRipRequest{ mode: Literal["music","audiobook"], disc_id:Optional[str], creator:str, title:str, year:Optional[int], disc_number:int = 1, total_discs:Optional[int], tracks: list[CDTrackSelection] }` where `CDTrackSelection{ number:int, title:str, include:bool }`.
  - `creator` = Artist (music) / Author (audiobook); `title` = Album / Book. Mapped to tags + paths per mode in the ripper.
  - **`mode` selects aggregation** (music = per-track files; audiobook = one joined file per disc), not just encoder/path/tags.
  - For **audiobook**, `tracks[].include` defaults to all-true and is only used to trim a leading/trailing non-content track (e.g. a publisher intro); the included tracks are joined into one file. For **music**, each included track becomes its own file. `disc_number`/`total_discs` are pre-filled from the MusicBrainz medium on an exact hit, else user-entered.
- `RipRecord` gains `artist: Optional[str] = None` (holds Artist/Author); music/audiobook rows use `title` for Album/Book.

### `app/paths.py` (edits)

- `music_path(root, *, artist, album, disc_number, total_discs, track_number, track_title) -> Path`
  `root/Music/<Artist>/<Album>/PREFIX - <Title>.flac` (one per track).
  `PREFIX` = `f"{disc_number}-{track:02d}"` when `total_discs > 1` (or `disc_number > 1`), else `f"{track:02d}"` — prevents cross-disc song collisions.
- `audiobook_path(root, *, author, book, disc_number, total_discs) -> Path` — **one file per disc** (no per-track args):
  `root/Audiobooks/<Author>/<Book>/<Book>, Disc NN.mp3` (multi-disc) or `root/Audiobooks/<Author>/<Book>/<Book>.mp3` (single disc). Successive discs append into the same book folder; `Disc NN` keeps them ordered.
- **Sanitization:** both reuse `sanitize()`, extended to (a) map empty / `"[untitled]"` titles to `f"Track {n:02d}"`, and (b) normalize Unicode punctuation from MusicBrainz (smart quotes `“ ”`, ellipsis `…`, em-dash) so names are Plex/SMB-safe, not just ASCII-legal.

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
- **Audiobook:** Author / Book (/ optional Narrator) fields; **Disc number** + **Total discs** (pre-filled from MusicBrainz on an exact hit, else manual). **No track table** — the whole disc becomes one file; at most an optional "trim first/last track" control for a publisher intro/outro. A 96-track seek-index disc must never render 96 checkboxes.
- **Rip** button → `POST /api/cd/rip`. Progress uses the existing SSE job UI (music: phase = current track; audiobook: "Ripping track N of M" then "Encoding disc"). No encoder-mode / subtitle / TMDB controls. Existing video UI untouched.

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
UI CD panel: pick mode, confirm/edit metadata (music: track table; audiobook: whole disc + disc#)
  → POST /api/cd/rip {mode, creator, title, disc_number, total_discs, tracks[]}
  → ripper.enqueue_cd → one Job
Worker → _run_cd_job → cd.rip_disc:
  music:     for each track: cdparanoia → wav → flac (tag) → one file per track; unlink wav
  audiobook: for each track: cdparanoia → wav; then concat → one lame encode (tag) → one file per disc; unlink wavs
  progress/phase over SSE per track (+ "Encoding disc" for audiobook)
  → DONE, library.record_rip
Files appear under <output root>/Music/…  or  /Audiobooks/<Author>/<Book>/<Book>, Disc NN.mp3
```

## Error handling

- **Not an audio CD** → `is_audio_cd` false; routing falls through (not an error).
- **MusicBrainz miss / down** → scan still returns the TOC; UI shows manual fields. Logged, best-effort.
- **`cdparanoia` read error** (scratched disc) → job `FAILED` with a tail of `cdparanoia` stderr; job-written files removed; scratch swept.
- **`flac`/`lame` encode error** → job `FAILED` with tool output tail.
- **Scratch disk full** → job `FAILED` (surfaced from the tool).
- **Cancel** → kill the live `cdparanoia`/encoder process; remove files this job wrote + the scratch WAV.

## Testing (no real drive, per repo convention)

- `cd.py` TOC parsing → recorded TOC fixtures for **both archetypes** (8-track content-aligned *and* 96-track ~45 s seek-index) → assert `CDTrack` counts/durations.
- `musicbrainz.py` → `respx`-mocked JSON: single-release, multi-release, disc-position (`5 of 9`), and a **miss** (→ `[]`); assert the fuzzy-TOC endpoint is never called.
- `paths.music_path` / `paths.audiobook_path` → single vs multi-disc naming, `[untitled]`/empty → `Track NN`, Unicode-punctuation sanitization, cross-disc collision (two "Sunbird"/"Locks").
- `cd.rip_disc` → injected fake `cdparanoia`/`flac`/`lame`: **music** = per-track FLAC loop; **audiobook** = per-track WAV extraction then a single joined `lame` call (assert one output file, join order, `TPOS` tag, WAV cleanup); progress ticks; cancel/fail cleanup.
- No test reads a physical CD.

## Verification (post-implementation, real disc)

Build the image (`make build`), then with a music CD and an audiobook CD in the drive: scan → confirm the panel/metadata → rip → confirm tagged FLAC/MP3 files land under `Music/…` / `Audiobooks/…` with correct names/tags and a library-ledger row. (Requires the drive to be free; a video rip may be in progress.)

## Open questions

None blocking. Sanctioned future work (all deferred): an optional **"split by MusicBrainz"** per-title audiobook mode on exact disc-ID hits (Design decision 9), with a later "merge continuation tracks" refinement; cover art; AccurateRip; and library backfill/duplicate-check. M4B / in-file chapter markers are explicitly **not** planned (Design decision 9).
