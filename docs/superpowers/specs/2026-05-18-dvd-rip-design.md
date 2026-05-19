# DVD Rip — Dockerized DVD-to-Plex Pipeline

## Goal

A single Docker container that, when run on the user's Ubuntu 24.04 desktop, reads the currently-inserted DVD, exposes a local web UI to pick which titles/audio/subtitle tracks to rip, decrypts via `libdvdcss`, encodes with HandBrake to Plex-optimized MP4 files, and writes them to a mounted output directory.

## Constraints

- **Host**: Ubuntu 24.04, Nvidia RTX 3060, nvidia-container-toolkit already installed
- **Use case**: Single-user, local-only (LAN), one DVD at a time
- **Quality target**: "Best compression and quality possible" — interpreted as x265 CPU encoding at CRF 20 with `slow` preset by default; NVENC HEVC available as a faster (lower-quality-per-byte) option
- **Output target**: Plex Media Server playback — H.265 MP4 with faststart, AAC + AC3 passthrough, soft-included subs

## Architecture

```
┌────────────────────── Docker container ─────────────────────────┐
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐   │
│  │  FastAPI     │───▶│  ripper.py   │───▶│  HandBrakeCLI    │   │
│  │  + HTMX UI   │    │  (job queue) │    │  + libdvdcss2    │   │
│  │  :8000       │◀───│  SSE progress│◀───│                  │   │
│  └──────────────┘    └──────────────┘    └────────┬─────────┘   │
│         ▲                                         │             │
│         │                                         ▼             │
│  ┌──────┴───────┐                         ┌──────────────────┐  │
│  │   dvd.py     │────── --scan ─────────▶ │  /dev/sr0        │  │
│  │   scanner    │◀───── titles json ─────│  (DVD device)    │  │
│  └──────────────┘                         └──────────────────┘  │
│                                                  │              │
│                                                  ▼              │
│                                          ┌──────────────────┐   │
│                                          │  /output (vol)   │   │
│                                          └──────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
        │                       │                     │
   --device=/dev/sr0     --gpus all (opt)     -v /host:/output
```

## Components

### `app/dvd.py` — DVD scanning
- Runs `HandBrakeCLI --scan --title 0 -i /dev/sr0 --json` and parses the JSON title list
- Falls back to `lsdvd` for the disc volume label (used as output folder name)
- Returns a structured list: `[Title{index, duration, chapters, audio_tracks, subtitle_tracks, frame_size}]`
- Heuristic to flag the "main feature" (longest title >20 min)

### `app/ripper.py` — Job orchestration
- In-memory job queue, runs one HandBrake job at a time (single optical drive, single GPU)
- Spawns `HandBrakeCLI` as a subprocess, parses progress percentage from stdout
- Publishes progress to an `asyncio.Queue` per job, consumed by SSE endpoint
- Status states: `queued`, `running`, `done`, `failed`, `cancelled`
- On completion, writes the MP4 to `/output/<DVD_LABEL>/title_<NN>_<descriptor>.mp4`

### `app/presets.py` — HandBrake settings
Two encoder modes, both 2-pass disabled (CRF mode):

**Quality mode (default, CPU x265):**
```
--encoder x265 --quality 20 --encoder-preset slow --encoder-tune grain
--aencoder ca_aac,copy --ab 160 --mixdown stereo,5point1 --arate auto
--audio-fallback ac3
--decomb default --crop-mode auto
--auto-anamorphic
--markers
--format av_mp4 --optimize
--subtitle scan,all --subtitle-burned scan --subtitle-default scan
```

**Speed mode (NVENC HEVC):**
```
--encoder nvenc_h265 --quality 22 --encoder-preset slow
(remaining audio/sub/container options same as quality mode)
```

### `app/main.py` — FastAPI app
Routes:
- `GET /` — HTMX page; auto-triggers `GET /api/scan`
- `GET /api/scan` — returns scan results as JSON (or HTML partial for HTMX)
- `POST /api/rip` — accepts `{titles: [{index, audio_tracks, subtitle_tracks, encoder_mode}], dvd_label}` and enqueues jobs
- `GET /api/jobs` — list of current and recent jobs
- `GET /api/jobs/{id}/progress` — SSE endpoint streaming `{percent, eta, fps}` events
- `POST /api/jobs/{id}/cancel` — kill the underlying HandBrake process
- `POST /api/eject` — `eject /dev/sr0` so the user can swap discs

### `app/templates/index.html` — UI
Single page, HTMX-driven:
1. Header: DVD label + Rescan + Eject buttons
2. Title list table: checkbox, title #, duration, chapters, frame size, "Main feature" badge
3. Per-title expandable row: audio track checkboxes, subtitle track checkboxes
4. Footer: encoder mode toggle (Quality / Speed), Start Rip button
5. Jobs panel: live progress bars via SSE, cancel buttons

Styling: plain CSS, no framework. Goal is utilitarian, single-screen.

## Container

### Dockerfile (sketch)
```dockerfile
FROM nvidia/cuda:12.6.0-base-ubuntu24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      handbrake-cli libdvd-pkg libdvdread8 libdvdnav4 lsdvd eject \
      python3.12 python3-pip \
    && dpkg-reconfigure libdvd-pkg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt
COPY app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

Note on `libdvd-pkg`: this is the Debian/Ubuntu package that downloads and builds `libdvdcss2` at install time (since libdvdcss isn't redistributable in binary form on Ubuntu). The `dpkg-reconfigure` step accepts the EULA non-interactively.

### docker-compose.yml
```yaml
services:
  dvd-rip:
    build: .
    container_name: dvd-rip
    ports:
      - "8000:8000"
    devices:
      - "/dev/sr0:/dev/sr0"
    volumes:
      - "${OUTPUT_DIR:-./output}:/output"
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu, video]
    environment:
      - DVD_DEVICE=/dev/sr0
      - OUTPUT_ROOT=/output
```

User runs `OUTPUT_DIR=/path/to/plex/movies docker compose up`.

## Data flow

1. Container starts → uvicorn serves :8000
2. User opens `http://localhost:8000`
3. Page loads → HTMX hits `/api/scan` → backend runs HandBrakeCLI scan → returns title list
4. User picks titles + tracks → clicks Start Rip → `POST /api/rip` → ripper enqueues
5. Job runs → SSE streams progress → UI updates progress bars
6. On completion, MP4 appears in `/output/<DVD_LABEL>/` (= host's `${OUTPUT_DIR}/<DVD_LABEL>/`)
7. User clicks Eject → DVD pops out → insert next disc → click Rescan

## Error handling

- **No disc inserted**: scan returns empty title list; UI shows "Insert a DVD and click Rescan"
- **Encrypted disc with libdvdcss missing**: HandBrake stderr includes a CSS error; surface in UI as "Decryption failed — check libdvdcss install"
- **HandBrake crash mid-rip**: job marked failed, partial MP4 deleted, stderr exposed in UI
- **GPU mode requested but Nvidia unavailable in container**: fall back to CPU mode with a UI warning
- **Output directory full / unwritable**: HandBrake fails on open; surface error

## Testing

- **Unit tests** (`tests/test_dvd.py`): feed `dvd.py` recorded `HandBrakeCLI --scan --json` fixtures, assert parsed structure. Cover normal disc, single-title disc, multi-angle disc, no-disc.
- **Unit tests** (`tests/test_ripper.py`): mock the subprocess; feed canned HandBrake progress lines; assert progress events and final state transitions.
- **Unit tests** (`tests/test_presets.py`): assert preset arg lists for both modes are well-formed.
- **Integration test** (`tests/test_api.py`): FastAPI TestClient hits `/api/scan` with a mocked scanner; asserts JSON shape and SSE handshake.
- **Manual smoke test**: documented in README — `docker compose up`, browse to localhost:8000, rip a real DVD.

No tests require a real DVD drive in CI.

## Out of scope (YAGNI)

- Auth / multi-user
- Multiple concurrent rips (single drive, single GPU — no point)
- Blu-ray (different decryption story; user said DVD)
- Direct Plex API integration / library refresh
- Cloud storage destinations
- Persistent job history across restarts
- Mobile-responsive UI
- Multi-disc box-set automation

## File layout

```
dvd-rip/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── dvd.py
│   ├── ripper.py
│   ├── presets.py
│   ├── models.py
│   ├── templates/
│   │   └── index.html
│   └── static/
│       └── style.css
└── tests/
    ├── test_dvd.py
    ├── test_ripper.py
    ├── test_presets.py
    ├── test_api.py
    └── fixtures/
        ├── scan_normal.json
        ├── scan_single_title.json
        └── scan_empty.json
```
