# Scan Concurrency Guard + Cancelable Scan — Design

- **Date:** 2026-07-05
- **Status:** 📐 Design approved — not yet implemented
- **Bead:** `dvd-rip-opz` (feature). Follow-up: `dvd-rip-r6n` (wrap blocking `discid.read`).
- **Related:** hardens the existing `/api/scan` path; reuses the `register_proc` cancellation pattern from `bluray.decrypt_title` and `cd._spawn`.

## Summary

Prevent overlapping disc scans and let an in-flight scan be canceled.

Today `/api/scan` only refuses when a **rip** is running (`ripper.is_busy()`, `app/main.py:203`); nothing guards against a second **scan** starting while one is already in progress, and the frontend Rescan button is never disabled. Clicking Rescan a few times on a slow disc piled up **five concurrent `makemkvcon` processes** on the single optical drive, all thrashing it. Because a scan on a bad disc can hang for up to ~10 min (`bluray._INFO_TIMEOUT = 600s`), a guard alone would leave the UI locked with no escape — so cancel is part of this change.

## Goals

- A scan cannot start while another scan is already running (backend-authoritative, not just a UI nicety).
- An in-flight scan can be canceled, which actually **kills the drive subprocess** (not merely the asyncio task).
- The Rescan button cannot be re-fired mid-scan; a separate Cancel control is available while scanning.
- A rip cannot be enqueued while a scan is running.
- No regression to the existing rip→scan guard. No real optical disc required in tests.

## Non-goals

- Queuing scans (single drive → reject overlap, don't queue).
- Canceling the in-process libdiscid TOC read (see Known limitation).
- Changing rip concurrency or the job queue.

## Current behavior

- `api_scan` (`app/main.py:197`): rejects with 409 only when `ripper.is_busy()`; otherwise runs the probe sequence inline: `cd.is_audio_cd` → `is_dvd_video` (lsdvd) → `dvd.scan` (HandBrake+lsdvd) **or** `bluray.detect_and_scan` (makemkvcon).
- `ripper.is_busy()` (`app/ripper.py:190`): `self._current_job_id is not None` — rip-only.
- Frontend `scan()` (`app/static/app.js:91`), wired to `#btn-rescan` (line 800) and called once on load (line 854). Button never disabled → repeat clicks fan out concurrent `/api/scan`.
- The scan probes spawn subprocesses privately; no handle is exposed for cancellation (unlike `bluray.decrypt_title`, which threads `register_proc`).

## Design

### 1. `ScanCoordinator` (new module `app/scan_coordinator.py`)

A single-purpose object stored on `app.state`, owning scan-in-progress state and the current scan subprocess.

State: `_busy` (bool), `_proc` (current subprocess handle), `_cancelled` (bool).

Interface:
- `busy: bool` — is a scan in progress (checked synchronously by endpoints).
- `cancelled: bool` — set by `cancel()`; read by the probe sequence to short-circuit between subprocesses.
- `async run(factory) -> ScanResult` — raise `ScanBusyError` if already `busy`; else set `_busy`, reset `_cancelled`/`_proc`, run `await factory(register_proc)`, and **always clear state in a `finally`** so an exception never wedges the flag.
- `register_proc(proc)` — record the current subprocess; probes run sequentially so only one is live at a time.
- `async cancel() -> bool` — set `_cancelled`; if a subprocess is live (`returncode is None`), `kill()` it; return whether a scan was actually running. **Does not** cancel the request task — killing the subprocess lets the scan return naturally, keeping the HTTP response clean.

Cancel is deterministic across the whole sequence: mid-probe, `kill()` on the registered subprocess ends it; in the tiny window *between* probes, the sequence (§2) checks `coordinator.cancelled` before starting the next stage and returns early.

### 2. Backend wiring (`app/main.py`)

- `api_scan`: keep the `ripper.is_busy()` → 409 check. Wrap the probe sequence in `scan_coordinator.run(...)`. On `ScanBusyError` → **409 "Scan already in progress"**. Thread `register_proc` into `is_dvd_video`, `dvd.scan`, and `bluray.detect_and_scan` (each forwards it to its `_spawn`, exactly as `cd._spawn` already accepts it). The sequence checks `scan_coordinator.cancelled` between probe stages (CD → DVD → Blu-ray) and returns an empty `ScanResult` early if a cancel landed between subprocesses.
- New **`POST /api/scan/cancel`** → `scan_coordinator.cancel()`, returns `{"cancelled": bool}` (200 either way; `false` when nothing was running).
- `api_rip` and `api_cd_rip`: add a cheap `if scan_coordinator.busy: raise HTTPException(409, "Scan in progress")` — the scan→rip protection chosen instead of a unified drive-owner refactor.

Thread the `register_proc` param through the scan probes:
- `dvd.is_dvd_video(device, *, register_proc=None)` → its `_spawn` calls `register_proc(proc)` after spawn.
- `dvd.scan(...)` — forward to both the HandBrake and lsdvd `_spawn` calls.
- `bluray.detect_and_scan(...)` — forward to its `_spawn`.
- `dvd._spawn` / `bluray._spawn` gain an optional `register_proc` (mirroring `cd._spawn`).

### 3. Frontend UX (`app/static/app.js`, `app/templates/index.html`) — separate cancel button

- New `#btn-scan-cancel` button in the scan-status area, `hidden` by default.
- `scan()`: on entry disable `#btn-rescan` (greyed) and unhide `#btn-scan-cancel`; in the `finally`, re-enable Rescan and hide Cancel.
- `#btn-scan-cancel` click → `cancelScan()`: set `state.scanCancelled = true`, `POST /api/scan/cancel`. The in-flight `GET /api/scan` then resolves (killed subprocess → benign empty/None result); `scan()` sees `state.scanCancelled` and renders **"Scan canceled — insert a disc and Rescan"** instead of "No titles found", then clears the flag.
- New concurrent-scan 409 (another tab, or the page-load auto-scan racing a manual one) → status **"Scan already in progress."** (not styled as an error).

### 4. Error handling

- Killed-mid-scan: the probes already tolerate non-zero subprocess exit (`dvd.scan` returns empty titles, `bluray.detect_and_scan` returns `None`), so the endpoint returns a clean `ScanResult`; the frontend `scanCancelled` flag supplies the user-facing wording.
- `finally` in `ScanCoordinator.run` guarantees the busy flag is released on success, exception, or cancel.
- `cancel()` on an idle coordinator is a no-op returning `false`.

### 5. Known limitation

`cd.is_audio_cd` reads the TOC via `discid.read(device)` — a **blocking in-process libdiscid call** (`app/cd.py:47`, no executor). It is not a subprocess, so `cancel()` cannot interrupt it, and a disc with an unreadable TOC could block the event loop. This step is fast (~1s, lead-in TOC only) and is not the multi-minute hang the feature targets; cancel governs the subprocess probes (`lsdvd`/`makemkvcon`/HandBrake). Tracked as follow-up **`dvd-rip-r6n`** (wrap in `asyncio.to_thread`); out of scope here.

## Testing (pytest; fake-spawner convention — no real drive)

- **`ScanCoordinator` unit:** `busy` toggles across `run`; `run` raises `ScanBusyError` when already busy; `cancel` kills the registered fake proc and sets `_cancelled`; `run` releases state when the factory raises; `cancel` on idle returns `false`.
- **Endpoint:** two concurrent `/api/scan` (fake probe blocks on an event) → second returns 409; `POST /api/scan/cancel` kills the fake long-running proc and leaves the coordinator idle; `/api/rip` and `/api/cd/rip` during an active scan → 409.
- **Frontend:** button enable/disable + cancel wiring verified manually/smoke (no JS test harness in this repo — no build step).

## Out of scope / follow-ups

- `dvd-rip-r6n` — move the blocking `discid.read` off the event loop.
- Unifying scan + rip under one "drive owner" (Approach 2, considered and declined) — the cheap rip-enqueue guard covers the collision this change needs.
