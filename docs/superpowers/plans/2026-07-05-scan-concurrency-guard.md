# Scan Concurrency Guard + Cancel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent overlapping disc scans (409 on overlap) and let an in-flight scan be canceled by killing the live drive subprocess.

**Architecture:** A single-purpose `ScanCoordinator` on `app.state` owns scan-in-progress state and the current scan subprocess handle. `/api/scan` runs its probe sequence through the coordinator (reject a second scan with 409); a new `POST /api/scan/cancel` kills the live subprocess. The scan probes gain an optional `register_proc` hook (mirroring `bluray.decrypt_title`/`cd._spawn`) so cancel can reach the process. The frontend disables Rescan during a scan and shows a separate Cancel button.

**Tech Stack:** Python 3.12, FastAPI, asyncio subprocesses, pytest (fake-spawner convention — no real optical drive), vanilla JS (no build step).

## Global Constraints

- **Beads:** `dvd-rip-opz` (this feature). Follow-up `dvd-rip-r6n` (blocking `discid.read`) is OUT OF SCOPE here.
- **Design spec:** `docs/superpowers/specs/2026-07-05-scan-concurrency-guard-design.md` — every task implements part of it.
- **Subprocesses:** argv-based `asyncio.create_subprocess_exec` only (never shell strings).
- **Tests never touch a real optical drive** — inject fake spawners / set `app.state` directly. Real subprocesses are allowed only for trivial system tools (`/bin/echo`).
- **No frontend build step** — plain `<button>`/`<script>`; no JS test harness exists, so frontend changes are verified by the Python suite staying green + manual smoke.
- **cancel() kills the subprocess but NOT the request task** — a killed probe returns naturally, keeping a clean HTTP response.
- Run single tests with `python -m pytest tests/<file>::<test> -v`; run the full suite with `make test` (sets up `.venv`).

---

### Task 1: `ScanCoordinator` class

**Files:**
- Create: `app/scan_coordinator.py`
- Test: `tests/test_scan_coordinator.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces:
  - `class ScanBusyError(Exception)`
  - `class ScanCoordinator` with:
    - `busy: bool` (property)
    - `cancelled: bool` (property)
    - `register_proc(proc) -> None`
    - `async run(factory: Callable[[Callable], Awaitable]) -> Any` — raises `ScanBusyError` if busy
    - `async cancel() -> bool`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scan_coordinator.py`:

```python
import asyncio

import pytest

from app.scan_coordinator import ScanBusyError, ScanCoordinator


class FakeProc:
    """Stand-in for asyncio subprocess: has returncode + kill()."""
    def __init__(self, returncode=None):
        self.returncode = returncode
        self.killed = False

    def kill(self):
        self.killed = True


def test_busy_true_during_run_false_after():
    c = ScanCoordinator()
    observed = {}

    async def factory(register_proc):
        observed["during"] = c.busy
        return "ok"

    result = asyncio.run(c.run(factory))
    assert observed["during"] is True
    assert c.busy is False
    assert result == "ok"


def test_run_raises_when_already_busy():
    c = ScanCoordinator()
    c._busy = True  # simulate a scan already running

    async def factory(register_proc):
        return "result"

    with pytest.raises(ScanBusyError):
        asyncio.run(c.run(factory))


def test_run_releases_busy_on_exception():
    c = ScanCoordinator()

    async def boom(register_proc):
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        asyncio.run(c.run(boom))
    assert c.busy is False


def test_cancel_kills_registered_proc_and_sets_flag():
    c = ScanCoordinator()
    proc = FakeProc(returncode=None)

    async def scenario():
        async def factory(register_proc):
            register_proc(proc)
            return await c.cancel()  # cancel from within the running scan
        return await c.run(factory)

    cancelled = asyncio.run(scenario())
    assert cancelled is True
    assert proc.killed is True
    assert c.cancelled is True


def test_cancel_when_idle_returns_false():
    c = ScanCoordinator()
    assert asyncio.run(c.cancel()) is False


def test_cancel_does_not_kill_finished_proc():
    c = ScanCoordinator()
    proc = FakeProc(returncode=0)  # already exited

    async def scenario():
        async def factory(register_proc):
            register_proc(proc)
            return await c.cancel()
        return await c.run(factory)

    cancelled = asyncio.run(scenario())
    assert cancelled is True          # a scan WAS running
    assert proc.killed is False       # but the proc had already exited
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scan_coordinator.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.scan_coordinator'`

- [ ] **Step 3: Implement `app/scan_coordinator.py`**

```python
"""Serializes /api/scan and makes an in-flight scan cancelable.

One scan touches the optical drive at a time. cancel() kills the live scan
subprocess (lsdvd/makemkvcon/HandBrake) so a hung scan on a bad disc can be
aborted instead of grinding the drive for minutes. It does NOT cancel the
request task — killing the subprocess lets the probe return naturally so the
HTTP response stays clean.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

log = logging.getLogger(__name__)


class ScanBusyError(Exception):
    """Raised by ScanCoordinator.run when a scan is already in progress."""


class ScanCoordinator:
    def __init__(self) -> None:
        self._busy: bool = False
        self._proc: Optional[Any] = None  # asyncio subprocess of the live probe
        self._cancelled: bool = False

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def register_proc(self, proc: Any) -> None:
        """Record the current scan subprocess so cancel() can kill it.

        Probes run sequentially, so only one process is live at a time; each
        call replaces the previous handle.
        """
        self._proc = proc

    async def run(self, factory: Callable[[Callable], Awaitable[Any]]) -> Any:
        """Run one scan under the single-scan guard.

        `factory(register_proc)` performs the probe sequence. Raises
        ScanBusyError if a scan is already running. State is always cleared,
        even on exception, so a crash never wedges the busy flag.
        """
        if self._busy:
            raise ScanBusyError()
        self._busy = True
        self._cancelled = False
        self._proc = None
        try:
            return await factory(self.register_proc)
        finally:
            self._busy = False
            self._proc = None

    async def cancel(self) -> bool:
        """Kill the live scan subprocess, if any. Returns True if a scan was running."""
        if not self._busy:
            return False
        self._cancelled = True
        proc = self._proc
        if proc is not None and getattr(proc, "returncode", None) is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_scan_coordinator.py -v`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add app/scan_coordinator.py tests/test_scan_coordinator.py
git commit -m "feat(scan): add ScanCoordinator (single-scan guard + cancel)

dvd-rip-opz"
```

---

### Task 2: Thread `register_proc` through the DVD + Blu-ray scan probes

**Files:**
- Modify: `app/dvd.py` (`_spawn`, `is_dvd_video`, `scan`)
- Modify: `app/bluray.py` (`_spawn`, `detect_and_scan`)
- Test: `tests/test_dvd.py`, `tests/test_bluray.py`

**Interfaces:**
- Consumes: nothing from Task 1 (independent).
- Produces (new optional kwargs, default `None` → existing callers/fakes unaffected):
  - `dvd._spawn(cmd, timeout=120.0, *, register_proc=None)`
  - `dvd.is_dvd_video(device, *, _run=_spawn, register_proc=None)`
  - `dvd.scan(device, *, commands=None, register_proc=None)`
  - `bluray._spawn(cmd, timeout=_INFO_TIMEOUT, *, register_proc=None)`
  - `bluray.detect_and_scan(device, *, _run=_spawn, register_proc=None)`

**Threading rule (keeps existing fakes green):** a probe forwards `register_proc` to its `_run`/`_spawn` **only when it is not None**, so old fakes with signature `(cmd, timeout=...)` are still called without the new kwarg.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_dvd.py`:

```python
def test_spawn_calls_register_proc_with_process():
    from app.dvd import _spawn
    captured = {}

    def reg(proc):
        captured["proc"] = proc

    rc, out, err = asyncio.run(_spawn(["/bin/echo", "hi"], register_proc=reg))
    assert rc == 0
    assert "hi" in out
    assert captured["proc"] is not None
    assert hasattr(captured["proc"], "kill")


def test_is_dvd_video_forwards_register_proc():
    captured = {}

    async def fake(cmd, timeout=15.0, register_proc=None):
        captured["rp"] = register_proc
        return (0, "Disc Title: X\n", "")

    def reg(proc):
        pass

    result = asyncio.run(is_dvd_video("/dev/sr0", _run=fake, register_proc=reg))
    assert result is True
    assert captured["rp"] is reg


def test_is_dvd_video_default_no_register_proc_kwarg():
    """Old-style fake without register_proc must still work (None path)."""
    async def fake(cmd, timeout=15.0):
        return (0, "Disc Title: X\n", "")

    assert asyncio.run(is_dvd_video("/dev/sr0", _run=fake)) is True
```

Append to `tests/test_bluray.py`:

```python
def test_detect_and_scan_forwards_register_proc():
    captured = {}

    async def fake_run(cmd, timeout=0, register_proc=None):
        captured["rp"] = register_proc
        return (0, "Blu-ray disc detected", "")

    def reg(proc):
        pass

    asyncio.run(detect_and_scan("/dev/sr0", _run=fake_run, register_proc=reg))
    assert captured["rp"] is reg
```

(`tests/test_bluray.py` already imports `asyncio` and `detect_and_scan`; if not, add `import asyncio`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_dvd.py::test_spawn_calls_register_proc_with_process tests/test_dvd.py::test_is_dvd_video_forwards_register_proc tests/test_bluray.py::test_detect_and_scan_forwards_register_proc -v`
Expected: FAIL (`_spawn()`/`is_dvd_video()`/`detect_and_scan()` got an unexpected keyword argument `register_proc`)

- [ ] **Step 3: Implement the threading in `app/dvd.py`**

Change `_spawn` (currently at `app/dvd.py:44`):

```python
async def _spawn(cmd: list[str], timeout: float = 120.0, *, register_proc=None) -> tuple[int, str, str]:
    """Run a command via argv (no shell) and capture output."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if register_proc is not None:
        register_proc(proc)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")
```

Change `is_dvd_video` (currently at `app/dvd.py:167`) signature + the `_run` call:

```python
async def is_dvd_video(device: str, *, _run: Callable = _spawn, register_proc=None) -> bool:
    # ... docstring unchanged ...
    kwargs = {} if register_proc is None else {"register_proc": register_proc}
    try:
        rc, stdout, _ = await _run(["lsdvd", device], timeout=15.0, **kwargs)
    except (FileNotFoundError, asyncio.TimeoutError):
        return False
    return rc == 0 and "Disc Title:" in stdout
```

Change `scan` (currently at `app/dvd.py:182`) signature + both `_spawn` calls:

```python
async def scan(
    device: str,
    *,
    commands: Optional[ScannerCommands] = None,
    register_proc=None,
) -> ScanResult:
    # ... docstring + cmds setup unchanged ...
    titles: list[Title] = []
    try:
        rc, stdout, stderr = await _spawn(cmds.handbrake, register_proc=register_proc)
        if rc != 0:
            log.warning("HandBrakeCLI scan returned %s: %s", rc, stderr.strip()[:500])
        titles = parse_handbrake_scan(stdout)
    except FileNotFoundError:
        log.error("HandBrakeCLI not found on PATH")
    except asyncio.TimeoutError:
        log.error("HandBrakeCLI scan timed out for %s", device)

    label = "UNKNOWN_DVD"
    try:
        rc, stdout, _ = await _spawn(cmds.lsdvd, timeout=15.0, register_proc=register_proc)
        if rc == 0:
            label = parse_lsdvd_label(stdout)
    except (FileNotFoundError, asyncio.TimeoutError):
        pass

    return ScanResult(dvd_label=label, titles=titles)
```

- [ ] **Step 4: Implement the threading in `app/bluray.py`**

Change `_spawn` (currently at `app/bluray.py:174`):

```python
async def _spawn(cmd: list[str], timeout: float = _INFO_TIMEOUT, *, register_proc=None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    if register_proc is not None:
        register_proc(proc)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
```

Change `detect_and_scan` (currently at `app/bluray.py:187`) signature + `_run` call:

```python
async def detect_and_scan(device: str, *, _run: Callable = _spawn, register_proc=None) -> Optional[ScanResult]:
    """Run `makemkvcon info`; return a Blu-ray ScanResult, or None to fall back to DVD."""
    cmd = ["makemkvcon", "-r", "--cache=1", "info", f"dev:{device}"]
    kwargs = {} if register_proc is None else {"register_proc": register_proc}
    try:
        rc, out, err = await _run(cmd, timeout=_INFO_TIMEOUT, **kwargs)
    except FileNotFoundError:
        log.error("makemkvcon not found on PATH")
        return None
    except asyncio.TimeoutError:
        log.error("makemkvcon info timed out for %s", device)
        return None
    # ... rest unchanged ...
```

- [ ] **Step 5: Run tests to verify they pass (new + existing regression)**

Run: `python -m pytest tests/test_dvd.py tests/test_bluray.py -v`
Expected: PASS (all existing DVD/Blu-ray tests still green + the 4 new forwarding tests)

- [ ] **Step 6: Commit**

```bash
git add app/dvd.py app/bluray.py tests/test_dvd.py tests/test_bluray.py
git commit -m "feat(scan): thread optional register_proc through DVD/BD scan probes

dvd-rip-opz"
```

---

### Task 3: Wire the coordinator into the app (guard, cancel endpoint, rip guards)

**Files:**
- Modify: `app/main.py` (imports, lifespan, `api_scan`, new `api_scan_cancel`, `api_rip` + `api_cd_rip` guards)
- Modify: `tests/test_api.py` (update existing scan fakes; add new tests)
- Modify: `CLAUDE.md` (module map row)

**Interfaces:**
- Consumes: `ScanCoordinator`, `ScanBusyError` (Task 1); `register_proc` kwargs on `is_dvd_video`/`scan`/`detect_and_scan` (Task 2).
- Produces: `app.state.scan_coordinator`; `POST /api/scan/cancel` → `{"cancelled": bool}`; 409 on overlapping scan; 409 on rip/cd-rip while scanning.

- [ ] **Step 1: Write the failing tests**

First, **update the existing scan fakes** in `tests/test_api.py` so they tolerate the new `register_proc` kwarg the endpoint now passes. In each of `test_scan_endpoint_returns_titles`, `test_scan_dvd_skips_makemkv`, `test_scan_uses_bluray_when_not_dvd`, and any other test whose fakes are `fake_is_dvd`/`fake_scan`/`fake_bluray`, change their signatures:

```python
    async def fake_is_dvd(device, register_proc=None):
        return True

    async def fake_scan(device, register_proc=None):
        return fake

    async def fake_bluray(device, register_proc=None):
        return None   # or `bd`, per that test
```

(The CD fakes `fake_is_audio(device, **kw)` / `fake_cd_scan(device, **kw)` already accept `**kw` — leave them.)

Then append new tests to `tests/test_api.py`:

```python
def test_second_scan_returns_409_while_scanning(client):
    from app import main as main_module
    coord = main_module.app.state.scan_coordinator
    coord._busy = True  # simulate a scan already in progress
    try:
        res = client.get("/api/scan")
        assert res.status_code == 409
        assert "already in progress" in res.json()["detail"].lower()
    finally:
        coord._busy = False


def test_scan_cancel_kills_live_proc(client):
    from app import main as main_module
    coord = main_module.app.state.scan_coordinator

    class FakeProc:
        returncode = None
        def __init__(self):
            self.killed = False
        def kill(self):
            self.killed = True

    proc = FakeProc()
    coord._busy = True
    coord._proc = proc
    try:
        res = client.post("/api/scan/cancel")
        assert res.status_code == 200
        assert res.json()["cancelled"] is True
        assert proc.killed is True
    finally:
        coord._busy = False
        coord._proc = None


def test_scan_cancel_when_idle_returns_false(client):
    res = client.post("/api/scan/cancel")
    assert res.status_code == 200
    assert res.json()["cancelled"] is False


def test_rip_blocked_while_scanning(client):
    from app import main as main_module
    coord = main_module.app.state.scan_coordinator
    coord._busy = True
    try:
        res = client.post("/api/rip", json={"titles": []})
        assert res.status_code == 409
    finally:
        coord._busy = False


def test_cd_rip_blocked_while_scanning(client):
    from app import main as main_module
    coord = main_module.app.state.scan_coordinator
    coord._busy = True
    try:
        res = client.post("/api/cd/rip", json={"tracks": []})
        assert res.status_code == 409
    finally:
        coord._busy = False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_api.py -v -k "409 or cancel or blocked_while_scanning"`
Expected: FAIL (`app.state.scan_coordinator` does not exist; `/api/scan/cancel` returns 404)

- [ ] **Step 3: Add the import + lifespan wiring in `app/main.py`**

Add to the imports (near `from .ripper import Ripper`):

```python
from .scan_coordinator import ScanBusyError, ScanCoordinator
```

In `lifespan` (after `app.state.push = push`, around `app/main.py:110`), add:

```python
    app.state.scan_coordinator = ScanCoordinator()
```

- [ ] **Step 4: Rewrite `api_scan` to run through the coordinator**

Replace the body of `api_scan` (`app/main.py:196-217`) with:

```python
@app.get("/api/scan", response_model=ScanResult)
async def api_scan(request: Request) -> ScanResult:
    # A running rip owns the optical drive; refuse to touch it mid-rip.
    ripper: Ripper = request.app.state.ripper
    if ripper.is_busy():
        raise HTTPException(status_code=409, detail="Rip in progress; disc scan unavailable")
    coordinator: ScanCoordinator = request.app.state.scan_coordinator

    async def _probe(register_proc) -> ScanResult:
        # Audio CD is the cheapest, most specific probe (libdiscid, ~1s); it is
        # an in-process call, so it is not subprocess-cancelable (see dvd-rip-r6n).
        if await cd.is_audio_cd(DVD_DEVICE):
            return await cd.scan(DVD_DEVICE, mb_client=request.app.state.musicbrainz)
        if coordinator.cancelled:
            return ScanResult(dvd_label="UNKNOWN_DVD", titles=[])
        if await is_dvd_video(DVD_DEVICE, register_proc=register_proc):
            return await scan(DVD_DEVICE, register_proc=register_proc)
        if coordinator.cancelled:
            return ScanResult(dvd_label="UNKNOWN_DVD", titles=[])
        bd = await bluray_detect_and_scan(DVD_DEVICE, register_proc=register_proc)
        if bd is not None:
            return bd
        if coordinator.cancelled:
            return ScanResult(dvd_label="UNKNOWN_DVD", titles=[])
        return await scan(DVD_DEVICE, register_proc=register_proc)

    try:
        return await coordinator.run(_probe)
    except ScanBusyError:
        raise HTTPException(status_code=409, detail="Scan already in progress")


@app.post("/api/scan/cancel")
async def api_scan_cancel(request: Request) -> dict:
    coordinator: ScanCoordinator = request.app.state.scan_coordinator
    cancelled = await coordinator.cancel()
    return {"cancelled": cancelled}
```

- [ ] **Step 5: Add the scan-busy guard to `api_rip` and `api_cd_rip`**

In `api_rip` (`app/main.py:243`), make the FIRST line of the body:

```python
async def api_rip(payload: RipRequest, request: Request) -> dict:
    coordinator: ScanCoordinator = request.app.state.scan_coordinator
    if coordinator.busy:
        raise HTTPException(status_code=409, detail="Scan in progress; wait for it to finish or cancel it")
    if not payload.titles:
        raise HTTPException(status_code=400, detail="No titles selected")
    # ... rest unchanged ...
```

In `api_cd_rip` (`app/main.py:276`), make the FIRST line of the body:

```python
async def api_cd_rip(payload: CDRipRequest, request: Request) -> dict:
    coordinator: ScanCoordinator = request.app.state.scan_coordinator
    if coordinator.busy:
        raise HTTPException(status_code=409, detail="Scan in progress; wait for it to finish or cancel it")
    if not any(t.include for t in payload.tracks):
        raise HTTPException(status_code=400, detail="No tracks selected")
    # ... rest unchanged ...
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/test_api.py -v`
Expected: PASS (existing scan tests still green with updated fakes + 5 new tests)

- [ ] **Step 7: Update the CLAUDE.md module map**

In `CLAUDE.md`, add a row to the module-map table (after the `ripper.py` row):

```markdown
| `scan_coordinator.py` | Serializes `/api/scan` (one scan at a time → 409 on overlap) and cancels an in-flight scan by killing the live drive subprocess |
```

- [ ] **Step 8: Run the full backend suite**

Run: `make test`
Expected: entire suite PASS.

- [ ] **Step 9: Commit**

```bash
git add app/main.py tests/test_api.py CLAUDE.md
git commit -m "feat(scan): guard /api/scan against overlap + add cancel endpoint

409 on a second concurrent scan (ScanBusyError), POST /api/scan/cancel
kills the live probe, and rip/cd-rip reject while a scan runs. dvd-rip-opz"
```

---

### Task 4: Frontend — separate Cancel button + scan states

**Files:**
- Modify: `app/templates/index.html` (add `#btn-scan-cancel`)
- Modify: `app/static/app.js` (`state.scanCancelled`, `scan()` button states + cancel/409 handling, `cancelScan()`, button wiring)

**Interfaces:**
- Consumes: `POST /api/scan/cancel`, the "Scan already in progress" 409 detail (Task 3).
- Produces: user-facing cancel control (no JS test harness; verified by the Python suite staying green + manual smoke).

- [ ] **Step 1: Add the Cancel button to the template**

In `app/templates/index.html`, replace the `topbar-actions` block (lines 25-27):

```html
    <div class="topbar-actions">
      <button id="btn-rescan" type="button" class="ghost">Rescan</button>
      <button id="btn-scan-cancel" type="button" class="ghost" hidden>Cancel scan</button>
    </div>
```

- [ ] **Step 2: Add the `scanCancelled` flag to app state**

In `app/static/app.js`, in the `state` object (near line 4 `scan: null,`), add:

```javascript
  scanCancelled: false,
```

- [ ] **Step 3: Replace `scan()` with the guarded/cancelable version**

Replace the whole `scan()` function (`app/static/app.js:91-135`) with:

```javascript
async function scan() {
  const status = $('#scan-status');
  const discSec = $('#disc-meta-section');
  const titlesSec = $('#titles-section');
  const rescanBtn = $('#btn-rescan');
  const cancelBtn = $('#btn-scan-cancel');
  state.scanCancelled = false;
  status.textContent = 'Scanning…';
  status.classList.remove('error');
  status.hidden = false;
  discSec.hidden = true;
  titlesSec.hidden = true;
  rescanBtn.disabled = true;
  cancelBtn.hidden = false;
  cancelBtn.disabled = false;
  try {
    const res = await fetch('/api/scan');
    if (res.status === 409) {
      const detail = (await res.json().catch(() => ({}))).detail || '';
      status.textContent = detail.toLowerCase().includes('scan already')
        ? 'A scan is already in progress.'
        : 'Rip in progress — disc scan paused. Watch progress in Jobs below.';
      status.classList.remove('error');
      status.hidden = false;
      return;
    }
    if (!res.ok) throw new Error(await res.text());
    const data = await res.json();
    if (state.scanCancelled) {
      status.textContent = 'Scan canceled. Insert a disc and click Rescan.';
      status.classList.remove('error');
      return;
    }
    state.scan = data;
    state.selectedMeta = null;
    state.tvEpisodeMap.clear();
    state.tvEpisodeTitles.clear();
    $('#cd-section').hidden = true;
    if (data.disc_format === 'cd') {
      renderCDPanel(data);
      status.hidden = true;
      return;
    }
    if (!data.titles.length) {
      status.textContent = 'No titles found. Insert a disc and click Rescan.';
      return;
    }
    renderDiscMeta(data);
    renderTitles(data);
    status.hidden = true;
    discSec.hidden = false;
    titlesSec.hidden = false;
  } catch (err) {
    if (state.scanCancelled) {
      status.textContent = 'Scan canceled. Insert a disc and click Rescan.';
      status.classList.remove('error');
    } else {
      status.textContent = `Scan failed: ${err.message || err}`;
      status.classList.add('error');
    }
  } finally {
    rescanBtn.disabled = false;
    cancelBtn.hidden = true;
  }
}

async function cancelScan() {
  state.scanCancelled = true;
  const cancelBtn = $('#btn-scan-cancel');
  cancelBtn.disabled = true;
  try {
    await fetch('/api/scan/cancel', { method: 'POST' });
  } catch (_) {
    // The in-flight scan will settle and render the canceled state regardless.
  }
}
```

- [ ] **Step 4: Wire the Cancel button**

In `app/static/app.js`, in the `DOMContentLoaded` handler (right after `$('#btn-rescan').addEventListener('click', scan);`, line 800), add:

```javascript
  $('#btn-scan-cancel').addEventListener('click', cancelScan);
```

- [ ] **Step 5: Verify the page still renders + full suite stays green**

Run: `make test`
Expected: PASS (incl. `tests/test_pwa.py` static page checks).

Manual smoke (optional, needs a disc + running container): load the page → Rescan greys out and "Cancel scan" appears during a scan; clicking Cancel stops the drive and shows "Scan canceled"; a second Rescan while scanning is impossible (button disabled) and a direct second `/api/scan` returns 409.

- [ ] **Step 6: Commit**

```bash
git add app/templates/index.html app/static/app.js
git commit -m "feat(scan): frontend cancel button + disable Rescan during scan

dvd-rip-opz"
```

---

## Self-Review

**Spec coverage:**
- Goal "no overlapping scans" → Task 1 (`run` raises `ScanBusyError`) + Task 3 (`api_scan` → 409).
- Goal "cancel kills the subprocess" → Task 1 (`cancel`) + Task 2 (`register_proc`) + Task 3 (endpoint threads it, `POST /api/scan/cancel`).
- Goal "Rescan can't re-fire mid-scan; separate Cancel control" → Task 4.
- Goal "rip can't start during a scan" → Task 3 (`api_rip`/`api_cd_rip` guards).
- Spec §5 known limitation (`discid.read` in-process) → not cancelable by design; CD probe deliberately gets no `register_proc`; follow-up `dvd-rip-r6n` filed. Covered (as an explicit non-goal).
- Testing plan (coordinator unit / endpoint / rip-guard) → Tasks 1 & 3. Frontend manual → Task 4.

**Placeholder scan:** No TBD/TODO; every code step shows complete code.

**Type consistency:** `register_proc` optional kwarg (default `None`) is consistent across `_spawn`/`is_dvd_video`/`scan`/`detect_and_scan` (Task 2) and the endpoint calls (Task 3). `ScanCoordinator.busy`/`cancelled`/`run`/`cancel`/`register_proc` names match between Task 1 (definition), Task 3 (endpoint use), and the tests. `{"cancelled": bool}` response shape matches the cancel test assertions.
