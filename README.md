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

Two supported hosts. Linux runs in Docker; macOS runs natively (see
[macOS](#macos-apple-silicon) for why Docker can't work there).

**Linux (the container path)**

- Linux host (tested on Ubuntu 24.04) with an attached DVD drive at `/dev/sr0`
  (override via the `DVD_DEVICE` env var)
- Docker + `docker compose`
- For Speed mode: Nvidia GPU + [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
  installed and configured for Docker

**macOS (the native path)**

- Apple Silicon or Intel Mac, macOS 11+
- A USB optical drive (Macs have had no internal one for years)
- Homebrew, and Python 3.11–3.13 (**not** 3.14 — see [macOS](#macos-apple-silicon))
- Quality mode only; there is no NVENC

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
year (Plex pulls year + episode titles from its own metadata sources). DVD rips are
`.mp4`; Blu-ray rips are `.mkv` (see [Blu-ray](#blu-ray) for why) — the naming is
otherwise identical.

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

## Blu-ray

Blu-ray discs are auto-detected at scan time: when a BD is inserted the UI shows a **Blu-ray** badge next to the disc label and the workflow is otherwise identical to DVD.

Ripping a Blu-ray requires two extra things:

1. **A MakeMKV beta key** — MakeMKV is the only tool with a working libre BD decryption
   stack. The free beta key rotates roughly monthly; get the current value from the sticky
   post at <https://forum.makemkv.com/forum/viewtopic.php?t=1053>, then add it to `.env`:

   ```bash
   MAKEMKV_APP_KEY=T-xxxx...     # paste the current key here
   ```

   Without it, Blu-ray rips fail immediately (DVD path is unaffected).

2. **~40 GB of transient scratch space** under `./data` — the rip runs in two phases:
   MakeMKV first decrypts the selected title to a temporary MKV in `./data/scratch`,
   then HandBrake encodes that MKV to the final H.264 file. The scratch MKV is deleted
   once the encode finishes (or is cancelled). Make sure the `./data` volume has enough
   headroom before starting a Blu-ray rip.

3. **Your drive id in `MAKEMKV_SDF_STOP`** — most non-LibreDrive USB Blu-ray drives on
   Linux otherwise make `makemkvcon` hang at 100% CPU during "SDF" (drive-firmware)
   processing. Setting `MAKEMKV_SDF_STOP` runs the drive in standard AACS mode, which
   reads 1080p Blu-ray fine (LibreDrive is only needed for UHD/4K). To find your id, do
   one scan attempt, then read the drive id from the log inside the container:

   ```bash
   docker compose exec dvd-rip grep "SDF auto" /home/app/MakeMKV_log.txt
   # -> SDF auto v0a5: PIONEER_BD-RW___BDR-XD08_1.00_BHDL000771UC
   ```

   Copy that id into `.env` and `make restart`:

   ```bash
   MAKEMKV_SDF_STOP=PIONEER_BD-RW___BDR-XD08_1.00_BHDL000771UC
   ```

### Blu-ray outputs MKV (not MP4)

Blu-ray rips are written as **`.mkv`**, while DVD rips stay `.mp4`. The reason is
subtitles: Blu-ray subtitles are **PGS** (bitmap), which an MP4 container can't carry as
a soft/selectable track — only MKV can. So your disc's English subtitle stays selectable
(and forced subtitles still burn in), matching the DVD behaviour. Plex Direct Plays MKV
on the Android app and Direct Streams it to Chromecast (a cheap container rewrap, no video
re-encode); turning bitmap subtitles **on** triggers a burn-transcode, exactly as it does
for DVD VOBSUB. Both formats coexist in one Plex library.

> **Note:** Blu-ray scans are slower than DVD — for discs with BD+ copy protection
> MakeMKV runs a decode pass that "may take up to a few minutes" before titles appear.
>
> **MakeMKV servers:** `makemkvcon` is configured to never phone home
> (`app_UpdateEnable=0` + `MAKEMKV_SDF_STOP`), so rips keep working even while
> makemkv.com is unreachable. The only effect is that disc-specific BD+ data can't be
> downloaded; MakeMKV falls back to a built-in generic table, which handles typical
> 1080p discs.

The DVD path is unchanged — no extra config needed for DVD-only use.

## macOS (Apple Silicon)

On macOS the app runs **natively**, not in Docker.

> **Docker cannot work here.** `docker-compose.yml` device-maps the optical drive
> (`/dev/sr0`, plus its SCSI-generic node for MakeMKV) into the container. On macOS
> Docker runs inside a Linux VM with no USB or optical passthrough, so that mapping
> can never resolve — a container would report "no disc" forever. `make up` detects
> macOS and says so rather than failing obscurely.

### Setup

```bash
brew install handbrake libdvdcss lsdvd flac lame libcdio-paranoia libdiscid
make doctor     # checks tools, drive, encoders, and library loading
make native     # creates .venv if needed, then serves on http://localhost:8000
```

`make doctor` is the fastest way to diagnose problems here, because almost every
failure on this platform is silent: a missing `libdvdcss` produces an unreadable
disc rather than an error, and a wrong libdiscid device makes an audio CD scan as
a DVD. It reports each dependency, whether a drive and disc are present, and
whether the raw device is readable.

For Blu-ray, install MakeMKV from <https://www.makemkv.com/download/> — the
Homebrew cask is deprecated because it fails macOS Gatekeeper. The app finds
`makemkvcon` inside `/Applications/MakeMKV.app` automatically; override with
`MAKEMKVCON_BIN`.

### What differs from Linux

| | Linux | macOS |
|---|---|---|
| Runs in | Docker container | Natively (`make native`) |
| Drive path | `/dev/sr0`, fixed | `/dev/rdiskN`, **auto-detected per scan** via `drutil` |
| Speed mode | NVENC H.264 | **Unavailable** — the toggle is hidden and the API rejects it |
| cdparanoia | `cdparanoia` | `cd-paranoia` (libcdio) |
| Rips land in | `${OUTPUT_DIR}` → `/output` | `~/Movies/dvd-rip` |
| Rip ledger | `./data/library.db` | `~/Library/Application Support/dvd-rip/library.db` |

**No Speed mode.** Apple Silicon has no NVENC. VideoToolbox (`vt_h264`) exists but
is deliberately not wired up: on an M-series CPU, x264 on SD DVD content already
runs many times faster than realtime, so a hardware encoder would trade quality
for a speedup you don't need. Requesting `speed` returns HTTP 400 rather than
silently falling back to x264 — a silent downgrade is indistinguishable from a hang.

**The drive is detected per scan**, not pinned at startup, so a drive plugged in
after the app is running is picked up on the next **Rescan** with no restart. Pin
it with `DVD_DEVICE=/dev/rdisk4` to skip detection.

**Python 3.14 does not work.** The pinned `pydantic-core` publishes no wheels for
3.14, and on the free-threaded `3.14t` build pip falls back to compiling Rust,
which fails against PyO3 0.22. The repo pins 3.12.13 via `.tool-versions`, and the
Makefile picks the newest interpreter that has wheels; override with
`make native PYTHON=python3.13`.

**Full Disk Access.** macOS may deny raw reads of `/dev/rdiskN` from a sandboxed
terminal. If `make doctor` reports the device is not readable, grant your terminal
Full Disk Access in System Settings → Privacy & Security.

## Configuration

| Env var       | Default       | Notes                                       |
| ------------- | ------------- | ------------------------------------------- |
| `DVD_DEVICE`  | `/dev/sr0`    | Path inside the container (mapped via compose). On macOS, unset means auto-detect per scan |
| `OUTPUT_ROOT` | `/output`     | Where the container writes; mounted as `${OUTPUT_DIR}`. macOS: `~/Movies/dvd-rip` |
| `PUID`        | `1000`        | UID to run the app as (auto-set by `make` to `id -u`) |
| `PGID`        | `1000`        | GID to run the app as (auto-set by `make` to `id -g`) |
| `CDROM_GID`   | `24`          | Host's cdrom group GID (auto-set by `make` from `getent group cdrom`) |
| `TMDB_API_KEY`| _(unset)_     | Optional: TMDB v3 API key for in-UI title/episode lookup |
| `LIBRARY_DB`  | `/data/library.db` | SQLite rip ledger; persisted via the `./data:/data` mount |
| `MAKEMKV_APP_KEY` | _(unset)_ | MakeMKV beta key — required for Blu-ray; rotates ~monthly |
| `MAKEMKV_SCRATCH` | `/data/scratch` | Scratch dir for transient decrypt MKVs (~40 GB needed) |
| `MAKEMKV_SDF_STOP` | _(unset)_ | Drive id to run in standard (non-LibreDrive) mode — avoids the 100%-CPU SDF hang on most USB BD drives |
| `LOG_LEVEL`   | `INFO`        | `DEBUG` for verbose HandBrake stdout        |
| `DISCID_DEVICE` | _(auto)_    | macOS only: libdiscid names drives by index (`1`), not path — override if the wrong drive is read |
| `MAKEMKVCON_BIN` | `makemkvcon` | Path to the binary; macOS auto-falls back to `/Applications/MakeMKV.app/…` |
| `MAKEMKV_SOURCE` | `dev:<device>` | Disc argument for makemkvcon. Set to `disc:N` if `dev:` isn't recognised (`makemkvcon -r info disc:9999` lists drives) |
| `CDPARANOIA_BIN` | `cdparanoia` | `cd-paranoia` on macOS (libcdio's build)   |

The compose file maps `${DVD_DEVICE:-/dev/sr0}` from host to `/dev/sr0` inside the container,
`${OUTPUT_DIR:-./output}` to `/output`, and `./data` to `/data` (the rip ledger).

## Library

The app keeps a persistent record of every rip in `./data/library.db` (SQLite):

- **Library page** (`/library`) — everything you've ripped, newest first, with a
  toggle to show failed/cancelled attempts.
- **Duplicate warning** — when you pick a movie you've already ripped, the rip page
  warns you. It never blocks — you can always re-rip (e.g. from a better disc).
- **Backfill** — on first start, existing files under `./output` are imported so the
  catalog and duplicate check work immediately.

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
- **macOS:** run `make doctor` — it checks the drive, the disc and every tool in one
  pass. If the header reads "no drive detected", `drutil status` sees no optical
  drive at all (unplugged, or asleep on a bus-powered hub).

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
make test          # creates .venv (from requirements-dev.txt) and runs pytest
make dev           # uvicorn with --reload
make native        # run against real tools, no reload (the macOS run path)
```

Or by hand:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

Use `requirements-dev.txt`, not `requirements.txt` + a guess: the suite needs
`pytest-asyncio` (pytest.ini sets `asyncio_mode=auto`) and `respx` (mocks TMDB and
MusicBrainz), and without them every async test errors at collection.

The tests never touch an optical drive: DVD scans replay recorded fixtures, CD
tests inject fake disc readers and subprocess spawners, and `conftest.py` pins
`DVD_DEVICE` so device auto-detection never shells out during a test run.

Note: running outside Docker won't decrypt commercial DVDs unless `libdvdcss` is
installed on the host (`brew install libdvdcss` on macOS, where HandBrake links it
directly).

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

## Phone access (track rips from your phone)

The app is already LAN-reachable at `http://<host-ip>:8000`. To get a stable
HTTPS address (required for installable PWA + Web Push) and optional off-LAN
access, front it with Tailscale:

1. Install Tailscale on the host and your phone; sign both into the same tailnet.
   Enable MagicDNS + HTTPS certs in the Tailscale admin console.
2. On the host, publish the app over HTTPS:
   ```bash
   tailscale serve --bg 8000
   ```
   It is now at `https://<host>.<tailnet>.ts.net` with a trusted cert. The raw
   `:8000` port is never forwarded to the public internet.
3. Generate secrets and put them in `.env`, then restart the container:
   ```bash
   make secrets   # paste the printed lines into .env
   # also set APP_PASSWORD=<your password> in .env
   ```
4. On the phone, open the `https://…ts.net` URL, sign in, and tap **Enable
   notifications** in the Jobs panel (optionally "Add to Home Screen" for an app
   icon). You'll get a push when each rip finishes or fails.

Security: access is gated by your tailnet **and** the shared password. Tighten
further with Tailscale ACLs if you share your tailnet.
