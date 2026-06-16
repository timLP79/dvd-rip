# Blu-ray Support — Design

## Goal

Add the ability to rip **1080p Blu-ray** discs, alongside the existing DVD pipeline.
When a Blu-ray is inserted, the app detects it, lists its titles, and rips the
selected title(s) to the same Plex-compliant H.264 MP4s the DVD path produces — so
the rest of the app (UI, TMDB metadata, Plex naming, library/dedup) is unchanged.

## Decisions (settled during brainstorming)

- **Scope: 1080p Blu-ray only.** No 4K UHD (needs AACS 2.0 + a UHD-friendly drive;
  out of scope, likely infeasible on this hardware).
- **Decryption: MakeMKV, free beta key.** Reliable on virtually all commercial BDs
  (AACS + BD+). The beta key rotates ~monthly and is supplied via env var.
- **Two-stage pipeline:** MakeMKV decrypts/remuxes → HandBrake encodes. MakeMKV is
  *only* a decrypt front-end; HandBrake does all encoding with the existing presets.
- **Auto-detect disc type;** DVD path stays byte-for-byte unchanged.
- **Codec unchanged:** H.264 (Chromecast direct-play) — applies at 1080p too.
- Hardware confirmed: the drive at `/dev/sr0` reports `ID_CDROM_BD=1`.

## Architecture

```
Insert disc → scan → detect type
   ├─ DVD  → existing pipeline (HandBrakeCLI scan + lsdvd; HandBrake rips /dev/sr0) — UNCHANGED
   └─ BD   → makemkvcon info (title list)
                 user picks title(s) + tracks (same UI)
                 rip job, two phases:
                   phase 1 "decrypting": makemkvcon mkv dev:<device> <title> <scratch>/  → temp .mkv
                   phase 2 "encoding":   HandBrakeCLI -i <temp.mkv> ... → /output/<Plex path>.mp4
                 delete temp .mkv
```

The disc is addressed as `dev:<DVD_DEVICE>` (the configurable device path), not the
`disc:0` index form, so it honors a non-default `DVD_DEVICE`.

The key simplifying choice: **MakeMKV extracts the full title (all streams) to an
MKV; HandBrake then does ALL the work** — H.264 encode, AAC+AC3 audio, decomb, crop,
forced-subtitle scan/burn, faststart — treating the MKV as a normal input file. This
means `presets.py` logic is reused as-is; only the *input* changes from
`device + --title N` to `-i <mkv>`.

## Disc-type detection

At scan time (`/api/scan`):

1. Run `makemkvcon -r info dev:<DVD_DEVICE>`. MakeMKV identifies the disc
   type and, for a Blu-ray, returns the title list.
2. If it reports a **Blu-ray** → use that title list (Blu-ray pipeline).
3. Otherwise → fall back to the **existing DVD scanner** (HandBrakeCLI `--scan` +
   `lsdvd`), unchanged.

The probe spins the disc (~10–30 s); acceptable at scan time. `ScanResult` gains a
`disc_format` field (`"dvd"` | `"bluray"`) so the UI can show a small badge and the
rest of the flow knows which pipeline a subsequent rip uses.

## Components

### `app/bluray.py` (new) — MakeMKV front-end

The only module that touches `makemkvcon`. Pure functions + small async helpers:

- `detect_and_scan(device) -> Optional[ScanResult]` — run `makemkvcon -r info`,
  return a `ScanResult(disc_format="bluray", titles=[...])` if it's a Blu-ray, else
  `None` (signal to fall back to DVD scan).
- `parse_makemkv_info(robot_output) -> list[Title]` — parse MakeMKV robot-mode
  `TINFO`/`SINFO` lines into the existing `Title` model (index, duration, chapters,
  frame size best-effort, audio/subtitle tracks). Pure; unit-tested with fixtures.
- `decrypt_title(device, title_id, scratch_dir, on_progress) -> Path` — run
  `makemkvcon -r --progress=-same mkv dev:<device> <title_id> <scratch_dir>`, parse
  `PRGV:` progress lines, return the produced `.mkv` path. Async, cancellable.
- `parse_makemkv_progress(line) -> Optional[float]` — parse `PRGV:cur,total,max`
  into a 0–100 percent. Pure; unit-tested.

MakeMKV's title numbering and stream layout are its own; the produced MKV carries all
selected streams. We extract the **whole title** (default profile) so HandBrake can
pick audio/subtitle tracks downstream exactly as it does for DVD.

**Track-index mapping (known assumption + risk).** The user picks audio/subtitle
tracks by index from the *makemkvcon* scan, but HandBrake later selects by index from
the *extracted MKV*. This relies on MakeMKV preserving source stream order so that
"audio track N" and "subtitle track N" line up between the two tools. This generally
holds (MakeMKV keeps the source ordering), but it is an assumption to **verify during
implementation** with a real disc. Fallback if it doesn't hold: after decrypt, have
the ripper re-scan the MKV with `HandBrakeCLI --scan` and map the user's chosen
positions onto HandBrake's reported track order before building the encode args. The
plan should include this verification step before relying on direct index pass-through.

### `app/ripper.py` (modify) — two-phase Blu-ray jobs

- The stored job context gains `disc_format`. The worker branches: DVD jobs run the
  existing single HandBrake subprocess; Blu-ray jobs run **decrypt then encode**.
- Phase 1: call `bluray.decrypt_title(...)`, mapping its progress to the job
  (`phase="decrypting"`, percent 0–100 within the phase).
- Phase 2: build HandBrake args with the temp MKV as input, run as today
  (`phase="encoding"`).
- `finally`: delete the temp MKV (and on cancel/fail, delete temp MKV + partial MP4).
- Cancellation must kill whichever subprocess is current (reuse `_current_process`).
- Recording to the library happens on terminal state as today, with `disc_format` set.

### `app/presets.py` (modify) — MKV input

`build_handbrake_args` gains an optional `input_file: Optional[str]`. When set, use
`--input <input_file>` and `--title 1` (an MKV has a single title) instead of
`--input <device> --title <n>`. All other args (encoder, audio, subtitle, crop, etc.)
are identical. DVD callers are unchanged.

### `app/models.py` (modify)

- `ScanResult.disc_format: Literal["dvd","bluray"] = "dvd"`.
- `Job.phase: Optional[str] = None` (`"decrypting"` | `"encoding"`).
- `RipRecord.disc_format: str = "dvd"`.
- `RipRequest` gains `disc_format` (so `/api/rip` knows which pipeline to use; the
  frontend echoes back what `/api/scan` reported).

### `app/main.py` (modify)

- `/api/scan`: try `bluray.detect_and_scan`; if `None`, existing DVD scan.
- `/api/rip`: pass `disc_format` through to the ripper.
- No new routes.

### Frontend (modify, minimal)

- Show a `DVD` / `Blu-ray` badge near the disc label (from `scan.disc_format`).
- Show the job `phase` in the jobs panel during a Blu-ray rip ("Decrypting… / Encoding…").
- Per the project's clean-UI preference: no other UI changes; the title table, TMDB
  flow, encoder toggle, and forced-subtitle checkbox all stay the same.

## MakeMKV in the container

- **Build from source** in the `Dockerfile`: download the pinned `makemkv-oss` and
  `makemkv-bin` tarballs, install build deps (build-essential, pkg-config, libc6-dev,
  libssl-dev, zlib1g-dev, libexpat1-dev, and ffmpeg dev libs MakeMKV needs), compile,
  install `makemkvcon` (CLI only — no Qt GUI). Pin the version for reproducible builds.
- **Beta key via env**: `MAKEMKV_APP_KEY`. The entrypoint writes
  `~/.MakeMKV/settings.conf` containing `app_Key = "<key>"` for the `app` user before
  dropping privileges. Supplied through `.env` (gitignored); `.env.example` documents it.
- **Scratch dir**: `MAKEMKV_SCRATCH` (default `/data/scratch`, on the existing `/data`
  volume). Created + chowned by the entrypoint; temp MKVs live here transiently and are
  deleted after each rip.

## Data flow — a Blu-ray rip

1. Insert BD → `/api/scan` → `makemkvcon info` → `ScanResult(disc_format="bluray")`.
2. User picks title(s) + metadata (TMDB / manual) exactly as for DVD.
3. `POST /api/rip` with `disc_format="bluray"` → ripper enqueues; job records
   `disc_format`.
4. Worker, Blu-ray branch:
   - phase `decrypting`: `makemkvcon mkv` → `<scratch>/title_NN.mkv`, progress 0–100.
   - phase `encoding`: HandBrake `-i <mkv>` → `/output/<Plex path>.mp4`, progress 0–100.
   - delete the temp MKV.
5. Library records the rip with `disc_format="bluray"`.

## Error handling

- **Missing/expired beta key**: `makemkvcon` fails with an auth error. Detect and
  surface a clear job error: "MakeMKV beta key missing or expired — update
  `MAKEMKV_APP_KEY` in `.env` and restart." DVD rips are unaffected.
- **Disc won't decrypt** (rare with MakeMKV): surface MakeMKV's stderr tail, like the
  HandBrake error-surfacing already does.
- **Scratch disk full**: MakeMKV fails on write; surface the error; clean up partial MKV.
- **Detection probe fails / empty drive**: `detect_and_scan` returns `None`; DVD scan
  then also finds nothing → existing "insert a disc" UI.
- **Cancel mid-decrypt or mid-encode**: terminate the current subprocess; delete temp
  MKV and any partial MP4; mark cancelled.
- MakeMKV recording/cleanup failures must never crash the worker (best-effort, logged).

## Testing

- `tests/test_bluray.py` — `parse_makemkv_info` against recorded `makemkvcon -r info`
  robot-output fixtures (a movie BD; a multi-title BD); `parse_makemkv_progress`
  against `PRGV:` lines; detection returns `None` on DVD/empty output.
- `tests/test_presets.py` — add cases for the `input_file` path (MKV input → `--input
  <file> --title 1`, no device).
- `tests/test_ripper.py` — two-phase orchestration with mocked subprocesses (decrypt
  then encode), temp-MKV cleanup, cancel during each phase.
- `tests/test_api.py` — `/api/scan` returns `disc_format`; `/api/rip` routes by
  `disc_format`.
- No real Blu-ray drive, disc, or MakeMKV key required in tests (all subprocess
  interactions mocked / fixture-driven), consistent with the existing suite.

## Out of scope (YAGNI)

- 4K UHD Blu-ray (AACS 2.0, UHD-friendly drive, HDR tone-mapping).
- Paid MakeMKV license automation (manual `.env` update on beta-key rotation).
- Auto-refreshing the rotating beta key (against MakeMKV ToS; manual is fine).
- Selecting individual streams *in MakeMKV* (we extract the full title; HandBrake
  selects tracks downstream).
- Multi-disc / playlist-obfuscation handling beyond what MakeMKV does automatically.

## File layout

```
dvd-rip/
├── app/
│   ├── bluray.py          # NEW — makemkvcon front-end (detect/scan/decrypt/progress)
│   ├── dvd.py             # unchanged
│   ├── ripper.py          # two-phase Blu-ray jobs; DVD path unchanged
│   ├── presets.py         # + optional MKV input_file
│   ├── models.py          # + disc_format / Job.phase
│   └── main.py            # scan detection + rip routing
├── scripts/entrypoint.sh  # write MakeMKV beta key + scratch dir
├── Dockerfile             # build makemkvcon; MAKEMKV_* env defaults
├── docker-compose.yml     # MAKEMKV_APP_KEY / MAKEMKV_SCRATCH passthrough
├── .env.example           # document MAKEMKV_APP_KEY
└── tests/
    └── test_bluray.py     # NEW
```
