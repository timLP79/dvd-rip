# DVD Rip

A single Docker container that turns an inserted DVD into Plex-ready MP4s with a local web UI.

- Decrypts CSS with `libdvdcss`
- Encodes with HandBrake — **Quality mode** (x264 CPU, CRF 20, `slow`) by default,
  **Speed mode** (NVENC H.264) available as a toggle
- Plex-friendly defaults: H.264 MP4 with faststart, AAC stereo + AC3/DTS passthrough,
  decomb deinterlace, auto crop, anamorphic, chapter markers, forced-sub burn-in
- H.264 (not H.265) for universal Plex direct-play — at DVD resolution HEVC's size
  win is small, and H.264 8-bit never triggers a server transcode on phones/Chromecasts
- One job at a time (single drive, single GPU), live progress over Server-Sent Events

## Requirements

- Linux host (tested on Ubuntu 24.04) with an attached DVD drive at `/dev/sr0`
  (override via the `DVD_DEVICE` env var)
- Docker + `docker compose`
- For Speed mode: Nvidia GPU + [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
  installed and configured for Docker

## Run it

```bash
# See available make targets
make help

# Build (first time only; ~5–10 min)
make build

# Start in the foreground; OUTPUT_DIR and DVD_DEVICE are overridable
make up OUTPUT_DIR=/mnt/plex/movies

# Or detached:
make up-d
make logs
make down
```

Underneath, `make` just wraps `docker compose`; bare `docker compose build` / `up` work too.

Open <http://localhost:8000>. The page will scan the disc automatically — if there's no disc
in the drive, insert one and click **Rescan**.

### Workflow

1. Insert a DVD
2. Open the web UI; it auto-scans the disc
3. In the **What is this disc?** panel:
   - Toggle **Movie** or **TV Show**
   - Type the title (pre-filled from disc label) and click **Search TMDB**, or click
     **Enter manually** to type it in directly
   - For TV: set the **season** and the **starting episode** for this disc
4. Pick which title(s) to rip in the table below
5. (Optional) expand a title row to choose specific audio tracks / subtitles, or override
   the auto-assigned episode/part number
6. Choose **Quality** (best compression, slow) or **Speed** (NVENC, faster, slightly larger files)
7. Click **Start Rip** — progress streams live
8. When done, MP4s land in the Plex-compliant paths described below
9. Press the drive's physical eject button to swap discs

## Output layout (Plex-compliant)

```
${OUTPUT_DIR}/
├── Movies/
│   └── The Matrix (1999)/
│       └── The Matrix (1999).mp4
│       └── The Matrix (1999) - cd2.mp4          # only for multi-part movies
└── TV Shows/
    └── Deadwood/
        └── Season 01/
            ├── Deadwood S01E01.mp4
            └── Deadwood S01E02.mp4
```

Plex auto-stacks `- cd2`, `- cd3`, … into a single movie entry. Show folders omit the
year (Plex pulls year + episode titles from its own metadata sources).

## TMDB metadata lookup

The UI can search [TMDB](https://www.themoviedb.org/) to fill in titles/years/episode info.
Without an API key, the search button is disabled and you type metadata in manually.

To enable:

1. Make a free account at <https://www.themoviedb.org/signup>
2. Request an API key: <https://www.themoviedb.org/settings/api> (pick "Developer", any
   non-commercial app description works)
3. Put it in a `.env` file in this repo:

   ```bash
   cp .env.example .env
   $EDITOR .env       # set TMDB_API_KEY=...
   make restart       # pick up the new value
   ```

`docker compose` reads `.env` automatically. The file is gitignored.

(You can also `export TMDB_API_KEY=...` from your shell — that takes precedence over
`.env` if both are set.)

## Configuration

| Env var       | Default       | Notes                                       |
| ------------- | ------------- | ------------------------------------------- |
| `DVD_DEVICE`  | `/dev/sr0`    | Path inside the container (mapped via compose) |
| `OUTPUT_ROOT` | `/output`     | Where the container writes; mounted as `${OUTPUT_DIR}` |
| `PUID`        | `1000`        | UID to run the app as (auto-set by `make` to `id -u`) |
| `PGID`        | `1000`        | GID to run the app as (auto-set by `make` to `id -g`) |
| `CDROM_GID`   | `24`          | Host's cdrom group GID (auto-set by `make` from `getent group cdrom`) |
| `TMDB_API_KEY`| _(unset)_     | Optional: TMDB v3 API key for in-UI title/episode lookup |
| `LOG_LEVEL`   | `INFO`        | `DEBUG` for verbose HandBrake stdout        |

The compose file maps `${DVD_DEVICE:-/dev/sr0}` from host to `/dev/sr0` inside the container,
and `${OUTPUT_DIR:-./output}` to `/output`.

## User and file permissions

The container starts as root, but the entrypoint drops privileges via `gosu` before launching
uvicorn. The dropped user (`app` inside the container) is created at runtime with the UID/GID
you supply via `PUID`/`PGID`, so MP4s written to `${OUTPUT_DIR}` are owned by your host user —
no `sudo` needed to manage them, and Plex (if it runs as the same user) can read them
directly.

`make` auto-detects sensible defaults from your shell: `PUID=$(id -u)`, `PGID=$(id -g)`, and
`CDROM_GID` from `getent group cdrom`. Override only if you want the files owned by a
different user (e.g. a dedicated `plex` account):

```bash
make up PUID=$(id -u plex) PGID=$(id -g plex)
```

The container also joins the host's cdrom group (via `group_add` in `docker-compose.yml`) so
`/dev/sr0` is readable without `--privileged`.

If you bind-mount an `${OUTPUT_DIR}` that already contains files owned by a different user,
the entrypoint will `chown` it on startup. To skip that (e.g. on an NFS share where chown
fails), pre-create the directory owned by your `PUID:PGID` and the chown is a no-op.

## Troubleshooting

### "Scan failed" / no titles found

- Confirm the host has a disc: `ls /dev/sr0` and `lsdvd /dev/sr0`
- Verify the container can read the device: `docker compose exec dvd-rip lsdvd /dev/sr0`
- If your DVD drive isn't `/dev/sr0`, set `DVD_DEVICE=/dev/sr1` (or whichever) before
  `docker compose up`

### CSS decryption errors

- Confirm `libdvdcss2` got built during the image build: the `dpkg-reconfigure libdvd-pkg`
  step should print "Building libdvdcss2..." and finish with success
- Some commercial discs use Sony ARccOS / Macrovision RipGuard bad sectors — HandBrake will
  log the affected sectors and continue; the resulting MP4 may have a few glitches

### NVENC mode fails / "encoder not found"

- Confirm `nvidia-container-toolkit` is installed on the host and Docker is configured to
  use it: `docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi`
- The Speed mode is best-effort. Fall back to Quality mode (CPU x264) if NVENC isn't available.

### Permissions on the DVD device

The compose file passes the host's cdrom GID into the container as a supplementary group, so
`/dev/sr0` is readable without `--privileged`. If `lsdvd /dev/sr0` works on the host but the
container scan fails with a permission error:

1. Confirm the host group: `getent group cdrom` (or `disk` on some distros).
2. Set `CDROM_GID` to that GID and rebuild: `make up CDROM_GID=24`.
3. On a fresh disc, the kernel sometimes re-creates the device with a brief permissions gap.
   Eject and re-insert and try again.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest httpx
.venv/bin/python -m pytest tests/ -v
.venv/bin/uvicorn app.main:app --reload
```

Note: running outside Docker won't decrypt commercial DVDs unless `libdvdcss2` is installed
on the host.

## Why these defaults?

DVDs are interlaced 720×480 (NTSC) or 720×576 (PAL). At those resolutions:

- **H.264, not H.265.** HEVC's compression edge is biggest at 4K; at DVD's 480p it
  shrinks to ~20–30%, while H.264 8-bit is the one codec every Plex client direct-plays.
  With an Android phone and Chromecasts, H.264 means playback never triggers a server-side
  transcode — the small storage saving from HEVC isn't worth the transcode tax.
- **x264 at CRF 20 with `slow` preset** produces near-source quality. A typical 90-minute
  movie lands around 1–1.5 GB. x264 at SD is very fast on a modern CPU (hundreds of fps).
- **NVENC H.264 at CQ 22** is far faster (GPU) but a bit larger at similar quality.
  Worth it for batch rips of bonus features where time matters more than bytes.
- **AAC stereo + AC3/DTS passthrough** (two audio tracks per source track): the stereo
  AAC plays anywhere; the passthrough preserves the original 5.1 surround for AV receivers.
- MP4 + faststart so Plex doesn't have to read the entire file before playback starts.
