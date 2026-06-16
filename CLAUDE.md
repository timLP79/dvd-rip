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
# Container (production path)
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

Single Docker container running a FastAPI + vanilla-JS web UI that orchestrates
HandBrakeCLI. One drive, one GPU, one concurrent rip.

```
Browser (HTML/CSS/vanilla JS, SSE) ─HTTP─▶ FastAPI / Uvicorn ──▶ Ripper worker
                                                                  └─▶ HandBrakeCLI
                                                                       ├─▶ /dev/sr0 (libdvdcss)
                                                                       └─▶ /output/<Plex path>
```

Module map (all in `app/`):

| File | Responsibility |
|---|---|
| `main.py` | FastAPI routes (`/api/scan`, `/api/search`, `/api/rip`, `/api/jobs`, `/api/jobs/{id}/progress` SSE), lifespan, template rendering |
| `dvd.py` | `HandBrakeCLI --json --scan` + `lsdvd` parsing into `ScanResult` |
| `bluray.py` | MakeMKV front-end — detect BD, scan titles, decrypt a title to a temp MKV (the only module that touches `makemkvcon`) |
| `presets.py` | HandBrake argv builders for Quality (x264 CPU) and Speed (NVENC H.264) modes |
| `paths.py` | Plex-compliant output paths (`Movies/Title (Year)/…`, `TV Shows/Show/Season NN/Show SNNeNN.mp4`) |
| `tmdb.py` | Async TMDB client (search movie/tv, season-episode list) — optional, gated on `TMDB_API_KEY` |
| `ripper.py` | Asyncio job queue + single-worker subprocess control + SSE progress fan-out; records each terminal job to the library |
| `library.py` | SQLite rip ledger — `record_rip`/`list_rips`/`find_existing` + `/output` backfill (the only module that touches SQL) |
| `models.py` | Pydantic models shared across API and worker |
| `static/`, `templates/` | UI (no build step — Jinja2 + vanilla JS + IBM Plex Sans/Mono fonts) |

Container plumbing (`Dockerfile`, `docker-compose.yml`, `scripts/entrypoint.sh`):

- Base: `nvidia/cuda:12.6.0-base-ubuntu24.04` (NVENC support)
- Apt: `handbrake-cli`, `libdvd-pkg` (builds `libdvdcss2` at install time), `libdvdread8t64`, `libdvdnav4`, `lsdvd`, `gosu`, `python3.12`
- Entrypoint creates an `app` user with the host-supplied PUID/PGID, joins the cdrom group, chowns `/output`, and `gosu`-drops to that user before launching uvicorn — so PID 1 inside the container runs as your host UID.
- `docker-compose.yml` device-mounts `/dev/sr0` and adds `group_add: ${CDROM_GID:-24}` for drive read access.
- A `./data:/data` volume holds `library.db` (the SQLite rip ledger), kept out of the `/output` media tree so Plex never scans it. Path via `LIBRARY_DB`.
- `makemkvcon` is built from source into the image (see `ARG MAKEMKV_VERSION` in the Dockerfile). Blu-ray rips require `MAKEMKV_APP_KEY` in `.env` — the free MakeMKV beta key, which rotates roughly monthly (get the current value from the MakeMKV forum). The entrypoint writes the key into the `app` user's `~/.MakeMKV/settings.conf` at startup. Transient MKV scratch files land under `MAKEMKV_SCRATCH` (default `/data/scratch`; needs ~40 GB free during a rip) and are cleaned up after each job.

## Conventions & Patterns

- **Subprocesses** are launched with asyncio's argv-based spawner (never shell strings) — no command-injection surface even with user-provided titles/years.
- **HandBrake progress** is parsed from raw byte chunks split on `\r` or `\n` (HandBrake uses CR to overwrite progress lines; line-based reads would block forever).
- **One job at a time** — there's only one optical drive and one GPU, so parallel rips just thrash both.
- **Output cleanup** — non-zero exit from HandBrake means no `moov` atom → unplayable file. The ripper unlinks the partial unconditionally on cancel/fail.
- **No build step on the frontend** — just `<link>`/`<script>` tags. `?v=<token>` query strings (per-container-start hex) bust the browser cache after rebuilds so stale JS never collides with new HTML.
- **Plex naming** matches the user's existing library convention exactly: TV shows omit the year (`TV Shows/Deadwood/Season 01/Deadwood S01E01.mp4`), movies use `Movies/Title (Year)/Title (Year).mp4` with `- cd2` etc. for multi-part. Sanitization strips OS-illegal chars but keeps spaces and `!`.
- **Tests** never depend on a real DVD drive — `tests/fixtures/scan_*.txt` are recorded `HandBrakeCLI --json --scan` outputs; `respx` mocks TMDB HTTP.
- **Secrets** (just `TMDB_API_KEY` today) live in `.env`, which is gitignored. `.env.example` is the template.
