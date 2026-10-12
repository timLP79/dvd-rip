# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:ca08a54f -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

## Session Completion

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **File issues for remaining work** - Create issues for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   bd dolt push
   git push
   git status  # MUST show "up to date with origin"
   ```
5. **Clean up** - Clear stashes, prune remote branches
6. **Verify** - All changes committed AND pushed
7. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
<!-- END BEADS INTEGRATION -->


## Build & Test

```bash
# macOS (native — the ONLY working path there; Docker cannot see an optical drive)
make doctor           # check toolchain, drive, encoders, library loading
make native           # create .venv if needed, then serve on :8000

# Container (production path — Linux only)
make build            # build the Docker image (~5–10 min first time)
make up-d             # start detached
make logs             # tail container logs
make down             # stop (refuses if a job is running unless FORCE=1)
make restart          # rebuild+restart (same guard)

# Local Python dev (no Docker, no real rip)
make test             # creates .venv, installs deps, runs pytest
make dev              # uvicorn with --reload on host port $PORT

# Manual / API smoke
make scan             # GET /api/scan (container must be up)
make jobs             # GET /api/jobs
make help             # show all targets and effective variable values
```

The Makefile auto-detects `PUID`/`PGID`/`CDROM_GID` from the invoking shell so
files in `${OUTPUT_DIR}` end up owned by you, not root.

**Never run `make restart` or `make down` while a HandBrake job is in progress** — the
container's worker dies with the container and leaves a partial MP4. The Makefile's
`check-no-active-jobs` target enforces this; override with `FORCE=1` if intended.

## Architecture Overview

FastAPI + vanilla-JS web UI that orchestrates HandBrakeCLI. Runs in one Docker
container on Linux or natively on macOS. One drive, one concurrent rip; GPU optional.

```
Browser (HTML/CSS/vanilla JS, SSE) ─HTTP─▶ FastAPI / Uvicorn ──▶ Ripper worker
                                                                  └─▶ HandBrakeCLI
                                                                       ├─▶ /dev/sr0 (libdvdcss)
                                                                       └─▶ /output/<Plex path>
```

Module map (all in `app/`):

| File | Responsibility |
|---|---|
| `hostenv.py` | The one seam for host-OS differences — optical-device detection/naming (macOS has no stable path, and libdiscid names drives differently from everything else), tool binary names (`cd-paranoia` vs `cdparanoia`, `makemkvcon` inside an .app), storage defaults, and whether a Speed-mode hardware encoder exists. Anything platform-conditional belongs here, not scattered across modules |
| `main.py` | FastAPI routes (`/api/scan` routes CD→DVD→Blu-ray, `/api/scan/cancel`, `/api/search`, `/api/rip`, `/api/cd/rip`, `/api/jobs`, `/api/jobs/{id}/progress` SSE), lifespan, template rendering |
| `dvd.py` | `HandBrakeCLI --json --scan` + `lsdvd` parsing into `ScanResult`; `is_dvd_video()` fast lsdvd probe so DVDs skip makemkvcon |
| `bluray.py` | MakeMKV front-end — detect BD, scan titles, decrypt a title to a temp MKV (the only module that touches `makemkvcon`) |
| `cd.py` | Audio-CD ripping (the only module touching `libdiscid`/`cdparanoia`/`flac`/`lame`) — detection + TOC scan, and the per-track rip loop (`cdparanoia`→WAV, then `flac`/`lame`). Music → per-track FLAC; audiobook → one joined MP3/disc, or split-by-MusicBrainz into per-title MP3s. Within-track progress via WAV-size polling |
| `presets.py` | HandBrake argv builders for Quality (x264 CPU) and Speed (NVENC H.264) modes |
| `paths.py` | Plex-compliant output paths (`Movies/…`, `TV Shows/…`, `Music/Artist/Album/…`, `Audiobooks/Author/Book/…`) |
| `tmdb.py` | Async TMDB client (search movie/tv, season-episode list) — optional, gated on `TMDB_API_KEY` |
| `musicbrainz.py` | Async MusicBrainz client — **exact disc-ID lookup only** (no fuzzy TOC); returns candidate releases + `is_audiobook` (release-group secondary type). No API key; parallels `tmdb.py` |
| `ripper.py` | Asyncio job queue + single-worker subprocess control + SSE progress fan-out; dispatches DVD / Blu-ray / CD jobs; records each terminal job to the library |
| `scan_coordinator.py` | Serializes `/api/scan` (one scan at a time → 409 on overlap) and cancels an in-flight scan by killing the live drive subprocess |
| `library.py` | SQLite rip ledger — `record_rip`/`list_rips`/`find_existing` + `/output` backfill; additive migrations (`artist` column for music/audiobook). The only module that touches SQL |
| `models.py` | Pydantic models shared across API and worker (incl. `CDTrack`/`MBRelease`/`CDRipRequest`) |
| `static/`, `templates/` | UI (no build step — Jinja2 + vanilla JS + IBM Plex Sans/Mono fonts); `index.html`/`app.js` include the CD panel, `library.html`/`library.js` the ledger view |

Container plumbing (`Dockerfile`, `docker-compose.yml`, `scripts/entrypoint.sh`):

- Base: `nvidia/cuda:12.6.0-base-ubuntu24.04` (NVENC support)
- Apt: `handbrake-cli`, `libdvd-pkg` (builds `libdvdcss2` at install time), `libdvdread8t64`, `libdvdnav4`, `lsdvd`, `cdparanoia`, `flac`, `lame`, `libdiscid0` (audio-CD ripping), `gosu`, `python3.12`. Python deps add `discid` (libdiscid binding) — see `requirements.txt`.
- Entrypoint creates an `app` user with the host-supplied PUID/PGID, joins the cdrom group, chowns `/output`, and `gosu`-drops to that user before launching uvicorn — so PID 1 inside the container runs as your host UID.
- `docker-compose.yml` device-mounts `/dev/sr0` and adds `group_add: ${CDROM_GID:-24}` for drive read access.
- A `./data:/data` volume holds `library.db` (the SQLite rip ledger), kept out of the `/output` media tree so Plex never scans it. Path via `LIBRARY_DB`.
- `makemkvcon` is built from source into the image (see `ARG MAKEMKV_VERSION` in the Dockerfile). Blu-ray rips require `MAKEMKV_APP_KEY` in `.env` — the free MakeMKV beta key, which rotates roughly monthly (get the current value from the MakeMKV forum). The entrypoint writes the key into the `app` user's `~/.MakeMKV/settings.conf` at startup. Transient MKV scratch files land under `MAKEMKV_SCRATCH` (default `/data/scratch`; needs ~40 GB free during a rip) and are cleaned up after each job.

## Conventions & Patterns

- **Subprocesses** use argv lists, never shell strings. Rip/scan workers use asyncio; the bounded startup encoder probe uses `subprocess.run` off the event loop.
- **HandBrake progress** is parsed from raw byte chunks split on `\r` or `\n` (HandBrake uses CR to overwrite progress lines; line-based reads would block forever).
- **One job at a time** — the worker serializes access to the optical drive and encoding resources, including on CPU-only hosts.
- **Output cleanup** — non-zero exit from HandBrake means no `moov` atom → unplayable file. The ripper unlinks the partial unconditionally on cancel/fail.
- **No build step on the frontend** — just `<link>`/`<script>` tags. `?v=<token>` query strings (per-container-start hex) bust the browser cache after rebuilds so stale JS never collides with new HTML.
- **Plex naming** matches the user's existing library convention exactly: TV shows omit the year (`TV Shows/Deadwood/Season 01/Deadwood S01E01.mp4`), movies use `Movies/Title (Year)/Title (Year).mp4` with `- cd2` etc. for multi-part. Sanitization strips OS-illegal chars but keeps spaces and `!`.
- **CD ripping** — audio CDs are Red Book (no filesystem); a CD track is never a chapter. Audiobook = **one joined MP3 per disc** by default (a 96-track seek-index disc must never become 96 files), with an optional **split-by-MusicBrainz** mode (per-title MP3s) when the exact disc-ID hit carries titles. MusicBrainz is **exact disc-ID only** (fuzzy TOC returns false positives — bird-song CDs, jingle libraries). Full design + field evaluation: `docs/superpowers/specs/2026-07-04-cd-rip-design.md`.
- **Tests** never depend on a real optical drive — DVD uses recorded `tests/fixtures/scan_*.txt`; CD injects fake disc readers + subprocess spawners (no `cdparanoia`/`libdiscid`/`flac`/`lame` needed); `respx` mocks TMDB **and** MusicBrainz HTTP.
- **Two hosts, one codebase.** Linux runs the container (production); macOS runs
  natively via `make native` because Docker's Linux VM on macOS has no optical/USB
  passthrough — the compose `devices:` mapping can never resolve, so a container
  would report "no disc" forever. Keep every platform difference inside `hostenv.py`;
  preserve CPU Quality mode and optional NVIDIA support on Linux.
- **The optical device is resolved per scan/job on macOS**, not pinned at startup, so
  a drive plugged in later works without a restart. Three consumers want three
  different names for the same drive: HandBrake/lsdvd/MakeMKV take `/dev/rdiskN`,
  libdiscid takes a drive *index*, cdparanoia takes the raw path.
- **No silent encoder substitution.** If startup cannot verify usable NVENC,
  Speed mode is hidden in the UI and rejected with HTTP 400. Quality remains x264.
- **Python 3.11–3.13 only.** The pinned pydantic-core has no 3.14 wheels and fails to
  compile on free-threaded 3.14t. Pinned via `.tool-versions`; the Makefile also picks
  a working interpreter. Test deps live in `requirements-dev.txt` (`pytest-asyncio` is
  mandatory — pytest.ini sets `asyncio_mode=auto`).
- **Secrets** (just `TMDB_API_KEY` today) live in `.env`, which is gitignored. `.env.example` is the template.

## Host and encoder compatibility

- Linux runs in Docker with CPU Quality mode by default; Intel/AMD CPUs need no NVIDIA GPU or host toolkit. The CUDA base image does not make GPU access mandatory.
- NVIDIA users opt in with `COMPOSE_FILE=docker-compose.yml:docker-compose.nvidia.yml` in `.env`. That override supplies GPU access and the video driver libraries.
- Intel and Apple Silicon Macs run natively with `make native`, using CPU Quality mode. Quick Sync and VideoToolbox acceleration are not implemented.
- At startup, `hostenv.initialize_encoders()` checks HandBrake's encoder list and runs a tiny synthetic NVENC encode off the event loop. Timeouts are 10 seconds for the list and 20 seconds for encoding. Failure leaves Speed hidden and rejected by the API and preset builder; there is no silent encoder substitution.
- GPU/driver changes require app restart or container recreation after queued/running jobs finish. Keep detection in `hostenv.py`; preserve CPU Quality and optional NVENC behavior. Run `tests/test_hostenv.py`, `tests/test_api.py`, `tests/test_presets.py`, and `tests/test_compose.py` for changes here. Do not describe mocked tests as hardware validation; the Linux hardware follow-up is `dvd-rip-c0b`.
