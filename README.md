# DVD Rip

A local web UI that turns an inserted DVD into Plex-ready MP4s. Runs in Docker on
Linux or natively on macOS, with CPU encoding by default.

**Start here:** [Run on a Mac](#run-it-on-macos) · [Run on Linux](#run-it-on-linux)

- Decrypts CSS with `libdvdcss`
- Encodes with HandBrake — **Quality mode** (x264 CPU, CRF 20, `slow`) by default,
  **Speed mode** (NVENC H.264) available when HandBrake detects usable NVIDIA support
- Plex-friendly defaults: H.264 MP4 with faststart, AAC stereo + AC3/DTS passthrough,
  decomb deinterlace, auto crop, anamorphic, chapter markers, forced-sub burn-in
- H.264 (not H.265) for universal Plex direct-play — at DVD resolution HEVC's size
  win is small, and H.264 8-bit never triggers a server transcode on phones/Chromecasts
- One job at a time for the optical drive, live progress over Server-Sent Events

## Requirements

Two supported hosts. Linux runs in Docker; macOS runs natively (see
[Mac setup](#run-it-on-macos) for setup and why Docker can't work there).

**Linux (the container path)**

- Linux host (tested on Ubuntu 24.04) with an attached DVD drive at `/dev/sr0`
  (override via the `DVD_DEVICE` env var)
- Docker + `docker compose`
- Intel and AMD CPUs work in Quality mode; no GPU or NVIDIA software is required
- For Speed mode: Nvidia GPU + [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
  installed and configured for Docker, plus the NVIDIA override below

**macOS (the native path)**

- Apple Silicon or Intel Mac with the required command-line tools; check
  [Homebrew installation requirements](https://docs.brew.sh/Installation) for your macOS version
- A USB optical drive (Macs have had no internal one for years)
- Homebrew, and Python 3.11–3.13 (**not** 3.14 — see [Mac setup](#run-it-on-macos))
- Quality mode only; there is no NVENC

<a id="macos-apple-silicon"></a>

<a id="macos-apple-silicon-and-intel"></a>

## Run it on macOS

These instructions apply to **Intel and Apple Silicon Macs**. The app runs
natively using `make native`; no Docker or NVIDIA GPU is needed.

> **Docker cannot work here.** `docker-compose.yml` device-maps the optical drive
> (`/dev/sr0`, plus its SCSI-generic node for MakeMKV) into the container. On macOS
> Docker runs inside a Linux VM with no USB or optical passthrough, so that mapping
> can never resolve — a container would report "no disc" forever. `make up` detects
> macOS and says so rather than failing obscurely.

### First-time Mac setup

1. Install [Homebrew](https://brew.sh/) if needed, and follow its instructions to
   add `brew` to your Terminal's PATH. Homebrew also guides you through installing
   Apple's Command Line Tools, which provide `make` and `git`.
2. Download or clone the project branch containing these instructions. Open
   Terminal in the `dvd-rip` folder (the folder containing this README and the
   `Makefile`). Run the following commands there:

   ```bash
   # Install the command-line tools and a supported Python version.
   HOMEBREW_NO_AUTO_UPDATE=1 brew install python@3.13 handbrake libdvdcss lsdvd \
     flac lame libcdio-paranoia libdiscid

   # Create the app's Python environment and install its dependencies.
   make venv PYTHON="$(brew --prefix)/bin/python3.13"
   ```

   The explicit Python path works with either Homebrew installation location and
   avoids selecting an incompatible system Python. Python 3.11–3.13 is required
   by the project's pinned dependencies; this example installs
   [Python 3.13](https://formulae.brew.sh/formula/python@3.13).
3. Connect your USB optical drive and insert a disc, then check the installation:

   ```bash
   make doctor
   ```

   Resolve any required-tool or drive-read errors it reports. For DVD ripping,
   missing MakeMKV and TMDB keys are optional warnings; you can enter a movie
   title manually. Blu-ray needs the additional setup below.
4. Start the app:

   ```bash
   make native
   ```

5. Open **<http://localhost:8000>** in your browser. Keep the Terminal window open
   while the app is running. Choose the title to rip and use **Quality** mode.
   Completed files go under **`~/Movies/dvd-rip`** by default.

### Starting and stopping on a Mac

For subsequent runs, open Terminal in the project folder and run `make native`.
Stop with **Ctrl+C** in that Terminal after rips finish. To prevent idle sleep
while the app runs, launch it with:

```bash
caffeinate -i make native
```

DVD ripping needs no `.env` file. For optional metadata lookup or Blu-ray
settings, create `.env` from `.env.example` if you do not already have one, then
edit the relevant keys. Keep existing settings when updating the app.

### Mac diagnostics and Blu-ray setup

`make doctor` is the fastest way to diagnose problems here, because almost every
failure on this platform is silent: a missing `libdvdcss` produces an unreadable
disc rather than an error, and a wrong libdiscid device makes an audio CD scan as
a DVD. It reports each dependency, whether a drive and disc are present, and
whether the raw device is readable.

For Blu-ray, install MakeMKV from <https://www.makemkv.com/download/> — download the
`.dmg` and drag `MakeMKV.app` to `/Applications` (the Homebrew cask is deprecated
because it fails macOS Gatekeeper). The app finds `makemkvcon` inside the bundle
automatically; override with `MAKEMKVCON_BIN`.

Then set `MAKEMKV_APP_KEY` in `.env` (the free beta key, which rotates roughly
monthly). On its first run `make native` writes `~/Library/MakeMKV/settings.conf`
with that key and `app_UpdateEnable = "0"` — the update check hangs scans at high
CPU whenever makemkv.com is unreachable, and it is on by default. An existing
settings.conf is never overwritten, since the MakeMKV GUI owns that file too.

### What differs from Linux

| | Linux | macOS |
|---|---|---|
| Runs in | Docker container | Natively (`make native`) |
| Drive path | `/dev/sr0`, fixed | `/dev/rdiskN`, **auto-detected per scan** via `drutil` |
| Speed mode | NVENC H.264, if detected (NVIDIA override required in Docker) | **Unavailable** — the toggle is hidden and the API rejects it |
| cdparanoia | `cdparanoia` | `cd-paranoia` (libcdio) |
| Rips land in | `${OUTPUT_DIR}` → `/output` | `~/Movies/dvd-rip` |
| Rip ledger | `./data/library.db` | `~/Library/Application Support/dvd-rip/library.db` |
| MakeMKV config | written by the entrypoint | `~/Library/MakeMKV/settings.conf`, written by `make native` |

Verified on macOS against an external USB Pioneer BDR-XD08: **DVD** (scan +
CSS-decrypted rip) and **Blu-ray** (~16 s to scan 18 titles, then AACS decrypt →
encode). **Audio CD** has only been exercised negatively — DVDs and Blu-rays are
correctly *not* routed to the CD scanner — so the libdiscid drive-index path is
still unconfirmed against a real CD.

**Quality mode on both Intel and Apple Silicon.** The app uses x264 CPU encoding
on all Macs. VideoToolbox and Intel Quick Sync acceleration are not implemented.
Encoding speed depends on the CPU and source resolution; the Apple Silicon
performance observations do not establish performance on an Intel Mac. The
Speed toggle is hidden, and requesting `speed` through the API returns HTTP 400.

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

## Run it on Linux

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

The default configuration uses **CPU encoding** and requests no GPU. To enable
NVIDIA acceleration on Linux, add this to `.env` before starting:

```dotenv
COMPOSE_FILE=docker-compose.yml:docker-compose.nvidia.yml
```

Then use the same `make up` / `make up-d` commands. The override exposes the GPU
and its video driver libraries. For a one-time invocation without changing `.env`:

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.nvidia.yml make up
```

To return to CPU-only operation, remove that setting from `.env` and unset it in
your shell if exported, then run `make up-d` to recreate the container. Wait for
running and queued rips to finish before recreating it. A plain
`docker compose restart` does not apply changes to GPU access.

At startup, the app asks HandBrake for its available encoders. **Speed** appears
only when HandBrake reports `nvenc_h264` and successfully encodes a tiny generated
test clip (no disc needed). Otherwise Quality remains available and the API
rejects Speed requests. Restart the app after changing GPU access or
drivers. Intel Quick Sync acceleration is not implemented; Intel users can use
Quality mode on the CPU.

The encoder-list check has a 10-second timeout and the test encode has a
20-second timeout. A failed check disables Speed for that app session; Quality
remains available. The check runs once at startup, without reading the disc.

CPU-only and optional NVIDIA configurations have regression coverage. A full
hardware smoke test of these changes on Intel Linux and an NVIDIA host is still
tracked in `dvd-rip-c0b`; Intel Mac hardware validation is also outstanding.

Open <http://localhost:8000>. The page will scan the disc automatically — if there's no disc
in the drive, insert one and click **Rescan**.

## Ripping a disc (Mac or Linux)

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
6. Use **Quality** (CPU), or choose **Speed** (NVENC) if the host supports it and the option appears
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

## Configuration

| Env var       | Default       | Notes                                       |
| ------------- | ------------- | ------------------------------------------- |
| `COMPOSE_FILE` | _(unset)_ | Linux Docker only: default uses CPU encoding. Set `docker-compose.yml:docker-compose.nvidia.yml` to expose NVIDIA hardware; no effect on `make native` |
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
| `HANDBRAKE_SCAN_TIMEOUT` | `600` | Seconds HandBrake gets to scan disc structure. Measured: ~26 s for an ordinary DVD, but a copy-protected dual-layer disc exceeded 120 s |
| `DISCID_DEVICE` | _(auto)_    | macOS only: libdiscid names drives by index (`1`), not path — override if the wrong drive is read |
| `MAKEMKVCON_BIN` | `makemkvcon` | Path to the binary; macOS auto-falls back to `/Applications/MakeMKV.app/…` |
| `MAKEMKV_SOURCE` | `dev:<device>` | Disc argument for makemkvcon. The default is verified working on macOS; override with `disc:N` if your build disagrees (`makemkvcon -r info disc:9999` lists drives) |
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
  pass. "no drive detected" means `drutil status` sees no optical drive at all
  (unplugged, or asleep on a bus-powered hub); "drive ready — no disc" means the
  drive is fine and the tray is empty. macOS creates no `/dev/diskN` node until
  media is loaded, so the device path only appears with a disc in.

### Scan is slow, or reports "Disc scan timed out"

Some discs use structural copy protection (ARccOS and friends) that pads the disc
with a dozen or more decoy two-hour titles. HandBrake walks every one of them, and
over a USB optical bus that can take many minutes.

Measured on an external USB Blu-ray drive: an ordinary DVD scans in **~26 s**; a
protected dual-layer disc blew past 120 s. That is why the budget defaults to 600 s.
If you hit the timeout, raise it and rescan:

```bash
make native HANDBRAKE_SCAN_TIMEOUT=1800
```

A timed-out scan now says so explicitly. It used to render as "No titles found",
which is indistinguishable from a blank disc and sends you to swap a disc that was
fine.

### CSS decryption errors

- Confirm `libdvdcss2` got built during the image build: the `dpkg-reconfigure libdvd-pkg`
  step should print "Building libdvdcss2..." and finish with success
- Some commercial discs use Sony ARccOS / Macrovision RipGuard bad sectors — HandBrake will
  log the affected sectors and continue; the resulting MP4 may have a few glitches

### NVENC mode fails / "encoder not found"

- Confirm `nvidia-container-toolkit` is installed on the host and Docker is configured to
  use it: `docker run --rm --gpus all nvidia/cuda:12.6.0-base-ubuntu24.04 nvidia-smi`
- Enable `docker-compose.nvidia.yml` using `COMPOSE_FILE` as shown above. Existing
  NVIDIA installations must opt in after updating; the default now requests no GPU.
- Check `docker compose exec dvd-rip HandBrakeCLI --help`: the video encoder list
  must include `nvenc_h264`. The app checks this and runs a small NVENC test encode
  at startup, hiding Speed if either check fails or times out. Restart after fixing the
  driver/container configuration. Quality mode (CPU x264) needs no NVIDIA support.

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
