# Blu-ray Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 1080p Blu-ray ripping — auto-detect BD vs DVD, decrypt the chosen title with MakeMKV, then encode it to the existing H.264 MP4 with HandBrake.

**Architecture:** A new `app/bluray.py` isolates all `makemkvcon` interaction (detect/scan/decrypt/progress). The ripper runs Blu-ray jobs in two phases — MakeMKV decrypt → HandBrake encode of the temp MKV — reusing the existing presets unchanged. DVD jobs are untouched. MakeMKV is compiled into the image; its rotating beta key comes from `MAKEMKV_APP_KEY`.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, `sqlite3`; `makemkvcon` (built from source) + HandBrakeCLI; pytest with constructed fixtures (no real disc/drive/MakeMKV key in CI).

**Spec:** `docs/superpowers/specs/2026-06-16-bluray-support-design.md`

---

## Verification points (call these out to the human during execution)

These three pieces are written against documentation and MUST be validated against real hardware after the image builds — they may need iteration:

1. **`makemkvcon -r info` exact output** — the parser uses MakeMKV's documented robot format and attribute codes. Capture real output (`makemkvcon -r info dev:/dev/sr0` on a real BD) and confirm the fixtures match; adjust `parse_makemkv_info` if codes differ.
2. **MakeMKV build recipe + version** — `MAKEMKV_VERSION` must be set to the current release from <https://www.makemkv.com/download/>. The Docker build is the highest-risk task; expect to iterate.
3. **Track-index mapping** — confirm MakeMKV preserves source audio/subtitle order in the MKV so the user's picked indices line up for HandBrake. Fallback (re-scan the MKV) documented in the spec.

---

## File Structure

- `app/models.py` — **modify**: `ScanResult.disc_format`, `Job.phase`, `RipRecord.disc_format`, `RipRequest.disc_format`.
- `app/presets.py` — **modify**: optional `input_file` (MKV input).
- `app/bluray.py` — **create**: MakeMKV robot-output parsers + detect/scan/decrypt.
- `app/ripper.py` — **modify**: two-phase Blu-ray jobs, `disc_format` routing, temp-MKV cleanup.
- `app/main.py` — **modify**: scan detection + rip routing.
- `app/dvd.py` — **unchanged**.
- `Dockerfile`, `scripts/entrypoint.sh`, `docker-compose.yml`, `.env.example` — **modify**: build MakeMKV, beta key, scratch dir.
- `app/templates/index.html`, `app/static/app.js` — **modify**: disc badge + phase label.
- `tests/test_bluray.py` — **create**; `tests/test_presets.py`, `tests/test_ripper.py`, `tests/test_api.py` — **modify**.

---

## Task 1: Models

**Files:** Modify `app/models.py`; Test `tests/test_bluray.py` (new, import smoke).

- [ ] **Step 1: Add fields**

In `app/models.py`:
- Add `from typing import Literal` if not present (it isn't — add it).
- In `ScanResult`, add: `disc_format: Literal["dvd", "bluray"] = "dvd"`.
- In `Job`, add: `phase: Optional[str] = None`.
- In `RipRequest`, add: `disc_format: Literal["dvd", "bluray"] = "dvd"`.
- In `RipRecord`, add: `disc_format: str = "dvd"`.

- [ ] **Step 2: Verify import + defaults**

Run: `.venv/bin/python -c "from app.models import ScanResult, Job, RipRequest, RipRecord; print(ScanResult(dvd_label='x', titles=[]).disc_format, Job(id='1', title_index=1, output_path='/x').phase)"`
Expected: `dvd None`

- [ ] **Step 3: Commit**

```bash
git add app/models.py
git commit -m "feat(bluray): add disc_format + Job.phase to models"
```

---

## Task 2: HandBrake MKV input in presets

**Files:** Modify `app/presets.py`, `tests/test_presets.py`.

- [ ] **Step 1: Add failing test**

Append to `tests/test_presets.py`:

```python
def test_mkv_input_uses_file_not_device():
    args = build_handbrake_args(
        device="/dev/sr0",
        title_index=7,
        output_path="/o.mp4",
        audio_tracks=[1],
        subtitle_tracks=[],
        burn_forced=True,
        encoder_mode=EncoderMode.QUALITY,
        input_file="/data/scratch/title.mkv",
    )
    pairs = _args_to_pairs(args)
    # MKV input overrides the device + title number (MKV has a single title).
    assert pairs["--input"] == "/data/scratch/title.mkv"
    assert pairs["--title"] == "1"
    assert "/dev/sr0" not in args
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_presets.py::test_mkv_input_uses_file_not_device -q`
Expected: FAIL (`build_handbrake_args() got an unexpected keyword argument 'input_file'`)

- [ ] **Step 3: Implement**

In `app/presets.py`, change the signature of `build_handbrake_args` to add `input_file: Optional[str] = None` (add `from typing import Iterable, Optional` — `Iterable` is already imported; add `Optional`). Then replace the two argv entries that set `--input` and `--title`:

Find:
```python
        "--input", device,
        "--title", str(title_index),
```
Replace with:
```python
        *( ["--input", input_file, "--title", "1"]
           if input_file is not None
           else ["--input", device, "--title", str(title_index)] ),
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_presets.py -q`
Expected: PASS (all preset tests).

- [ ] **Step 5: Commit**

```bash
git add app/presets.py tests/test_presets.py
git commit -m "feat(bluray): presets accept an MKV input file"
```

---

## Task 3: `bluray.py` parsers (pure functions)

**Files:** Create `app/bluray.py`; create `tests/test_bluray.py`.

- [ ] **Step 1: Write failing parser tests**

Create `tests/test_bluray.py`:

```python
from app.bluray import (
    is_bluray_output,
    parse_makemkv_info,
    parse_makemkv_progress,
)

# Constructed from MakeMKV's documented robot (-r) format. VERIFY against real
# `makemkvcon -r info` output during execution.
SAMPLE_INFO = '\n'.join([
    'MSG:1005,0,1,"MakeMKV v1.17.7 started","%1","x"',
    'DRV:0,2,999,12,"BD-RE HL-DT-ST","BIG_MOVIE","/dev/sr0"',
    'CINFO:1,6209,"Blu-ray disc"',
    'CINFO:2,0,"BIG_MOVIE"',
    'TCOUNT:2',
    'TINFO:0,8,0,"24"',
    'TINFO:0,9,0,"1:52:30"',
    'TINFO:0,10,0,"30.1 GB"',
    'TINFO:0,11,0,"32319012864"',
    'SINFO:0,0,1,6201,"Video"',
    'SINFO:0,0,19,0,"1920x1080"',
    'SINFO:0,1,1,6202,"Audio"',
    'SINFO:0,1,3,0,"eng"',
    'SINFO:0,1,4,0,"English"',
    'SINFO:0,1,7,0,"DTS-HD Master Audio"',
    'SINFO:0,2,1,6202,"Audio"',
    'SINFO:0,2,3,0,"fra"',
    'SINFO:0,2,4,0,"French"',
    'SINFO:0,3,1,6203,"Subtitles"',
    'SINFO:0,3,3,0,"eng"',
    'SINFO:0,3,4,0,"English"',
    'TINFO:1,8,0,"2"',
    'TINFO:1,9,0,"0:03:10"',
    'SINFO:1,0,1,6201,"Video"',
    'SINFO:1,0,19,0,"1920x1080"',
])


def test_is_bluray_output_true_for_bd():
    assert is_bluray_output(SAMPLE_INFO) is True


def test_is_bluray_output_false_for_dvd_or_empty():
    dvd = 'DRV:0,2,999,1,"DVD HL-DT-ST","SOME_DVD","/dev/sr0"\nCINFO:1,6206,"DVD disc"\nTCOUNT:1'
    assert is_bluray_output(dvd) is False
    assert is_bluray_output("") is False


def test_parse_makemkv_info_titles():
    titles = parse_makemkv_info(SAMPLE_INFO)
    assert len(titles) == 2
    main = titles[0]
    assert main.index == 0
    assert main.duration_seconds == 1 * 3600 + 52 * 60 + 30
    assert main.chapter_count == 24
    assert main.frame_width == 1920 and main.frame_height == 1080
    assert [a.language for a in main.audio_tracks] == ["English", "French"]
    assert [s.language for s in main.subtitle_tracks] == ["English"]
    # main feature = longest title flagged
    assert main.is_main_feature is True
    assert titles[1].is_main_feature is False


def test_parse_makemkv_progress():
    assert parse_makemkv_progress("PRGV:16384,32768,65536") == 50.0
    assert parse_makemkv_progress("PRGV:0,0,65536") == 0.0
    assert parse_makemkv_progress("not a progress line") is None
    # guard against divide-by-zero
    assert parse_makemkv_progress("PRGV:5,5,0") == 0.0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_bluray.py -q`
Expected: FAIL (`No module named 'app.bluray'`)

- [ ] **Step 3: Implement the parsers**

Create `app/bluray.py`:

```python
"""MakeMKV front-end: detect Blu-ray discs, list titles, decrypt a title to MKV.

The only module that touches `makemkvcon`. Robot-mode (-r) output is parsed into
the same `Title`/`ScanResult` models the DVD path uses, so the UI is uniform.

NOTE: the robot output format + attribute codes here follow MakeMKV's documented
`-r` protocol. Validate against real `makemkvcon -r info` output on a real disc.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .models import AudioTrack, ScanResult, SubtitleTrack, Title

log = logging.getLogger(__name__)

# MakeMKV ap_ItemAttributeId codes used here (from apdefs.h):
_ATTR_NAME = 2
_ATTR_LANG_CODE = 3
_ATTR_LANG_NAME = 4
_ATTR_CODEC_LONG = 7
_ATTR_CHAPTER_COUNT = 8
_ATTR_DURATION = 9
_ATTR_VIDEO_SIZE = 19

_MAIN_FEATURE_MIN_SECONDS = 20 * 60


def is_bluray_output(robot_output: str) -> bool:
    """True if `makemkvcon info` output describes a Blu-ray disc."""
    return bool(re.search(r"blu-?ray", robot_output, re.IGNORECASE))


def _duration_to_seconds(value: str) -> int:
    parts = value.strip().split(":")
    if not all(p.isdigit() for p in parts) or not parts:
        return 0
    parts = [int(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts[-3], parts[-2], parts[-1]
    return h * 3600 + m * 60 + s


def parse_makemkv_progress(line: str) -> Optional[float]:
    """Parse a `PRGV:current,total,max` line into 0-100 percent (uses `total`)."""
    m = re.match(r"PRGV:(\d+),(\d+),(\d+)", line.strip())
    if not m:
        return None
    _current, total, pmax = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if pmax <= 0:
        return 0.0
    return round(total / pmax * 100.0, 1)


@dataclass
class _TitleAcc:
    index: int
    duration_seconds: int = 0
    chapter_count: int = 0
    frame_width: int = 0
    frame_height: int = 0
    audio: list = None
    subs: list = None
    def __post_init__(self):
        self.audio = []
        self.subs = []


def parse_makemkv_info(robot_output: str) -> list[Title]:
    """Parse robot `info` output (TINFO/SINFO lines) into Title models."""
    titles: dict[int, _TitleAcc] = {}
    # stream type accumulator: (title, stream) -> {"kind","lang_code","lang_name","codec"}
    streams: dict[tuple, dict] = {}

    tinfo_re = re.compile(r'^TINFO:(\d+),(\d+),\d+,"(.*)"$')
    sinfo_re = re.compile(r'^SINFO:(\d+),(\d+),(\d+),\d+,"(.*)"$')

    for line in robot_output.splitlines():
        tm = tinfo_re.match(line)
        if tm:
            tid, code, value = int(tm.group(1)), int(tm.group(2)), tm.group(3)
            acc = titles.setdefault(tid, _TitleAcc(index=tid))
            if code == _ATTR_DURATION:
                acc.duration_seconds = _duration_to_seconds(value)
            elif code == _ATTR_CHAPTER_COUNT and value.isdigit():
                acc.chapter_count = int(value)
            continue
        sm = sinfo_re.match(line)
        if sm:
            tid, sid, code, value = (
                int(sm.group(1)), int(sm.group(2)), int(sm.group(3)), sm.group(4),
            )
            titles.setdefault(tid, _TitleAcc(index=tid))
            st = streams.setdefault((tid, sid), {})
            if code == 1:  # Type
                st["kind"] = value  # "Video" / "Audio" / "Subtitles"
            elif code == _ATTR_LANG_CODE:
                st["lang_code"] = value
            elif code == _ATTR_LANG_NAME:
                st["lang_name"] = value
            elif code == _ATTR_CODEC_LONG:
                st["codec"] = value
            elif code == _ATTR_VIDEO_SIZE:
                mm = re.match(r"(\d+)x(\d+)", value)
                if mm:
                    acc = titles[tid]
                    acc.frame_width, acc.frame_height = int(mm.group(1)), int(mm.group(2))

    # assemble audio/subtitle tracks per title in stream order
    for (tid, sid), st in sorted(streams.items()):
        acc = titles[tid]
        kind = (st.get("kind") or "").lower()
        lang = st.get("lang_name") or st.get("lang_code")
        if kind == "audio":
            acc.audio.append(AudioTrack(index=len(acc.audio) + 1, language=lang,
                                        codec=st.get("codec")))
        elif kind in ("subtitles", "subtitle"):
            acc.subs.append(SubtitleTrack(index=len(acc.subs) + 1, language=lang))

    out: list[Title] = []
    for tid in sorted(titles):
        acc = titles[tid]
        out.append(Title(
            index=acc.index,
            duration_seconds=acc.duration_seconds,
            chapter_count=acc.chapter_count,
            frame_width=acc.frame_width,
            frame_height=acc.frame_height,
            audio_tracks=acc.audio,
            subtitle_tracks=acc.subs,
            is_main_feature=False,
        ))
    # flag the longest title over the threshold as the main feature
    if out:
        longest = max(out, key=lambda t: t.duration_seconds)
        if longest.duration_seconds >= _MAIN_FEATURE_MIN_SECONDS:
            longest.is_main_feature = True
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_bluray.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/bluray.py tests/test_bluray.py
git commit -m "feat(bluray): makemkvcon robot-output parsers"
```

---

## Task 4: `bluray.py` detect/scan + decrypt (subprocess)

**Files:** Modify `app/bluray.py`, `tests/test_bluray.py`.

- [ ] **Step 1: Add failing tests (injected fake runner)**

Append to `tests/test_bluray.py`:

```python
import asyncio
from app.bluray import detect_and_scan, decrypt_title


def test_detect_and_scan_returns_scanresult_for_bd():
    async def fake_run(cmd, timeout=0):
        return 0, SAMPLE_INFO, ""
    result = asyncio.run(detect_and_scan("/dev/sr0", _run=fake_run))
    assert result is not None
    assert result.disc_format == "bluray"
    assert len(result.titles) == 2


def test_detect_and_scan_returns_none_for_dvd():
    async def fake_run(cmd, timeout=0):
        return 0, 'CINFO:1,6206,"DVD disc"\nTCOUNT:1', ""
    assert asyncio.run(detect_and_scan("/dev/sr0", _run=fake_run)) is None


def test_detect_and_scan_returns_none_on_makemkv_error():
    async def fake_run(cmd, timeout=0):
        return 1, "", "key expired"
    assert asyncio.run(detect_and_scan("/dev/sr0", _run=fake_run)) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_bluray.py -k detect -q`
Expected: FAIL (`cannot import name 'detect_and_scan'`)

- [ ] **Step 3: Implement detect/scan + decrypt**

Append to `app/bluray.py`:

```python
async def _spawn(cmd: list[str], timeout: float = 180.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def detect_and_scan(
    device: str,
    *,
    _run: Callable = _spawn,
) -> Optional[ScanResult]:
    """Run `makemkvcon info`; return a Blu-ray ScanResult, or None to fall back to DVD."""
    cmd = ["makemkvcon", "-r", "--cache=1", "info", f"dev:{device}"]
    try:
        rc, out, err = await _run(cmd, timeout=180.0)
    except FileNotFoundError:
        log.error("makemkvcon not found on PATH")
        return None
    except asyncio.TimeoutError:
        log.error("makemkvcon info timed out for %s", device)
        return None
    if rc != 0:
        log.info("makemkvcon info rc=%s (not a Blu-ray or key issue): %s", rc, err.strip()[:300])
        return None
    if not is_bluray_output(out):
        return None
    titles = parse_makemkv_info(out)
    return ScanResult(dvd_label="BLURAY", titles=titles, disc_format="bluray")


async def decrypt_title(
    device: str,
    title_id: int,
    scratch_dir: str,
    *,
    on_progress: Optional[Callable[[float], None]] = None,
    register_proc: Optional[Callable] = None,
) -> Path:
    """Decrypt one title to an MKV in scratch_dir. Returns the produced .mkv path.

    register_proc(proc) lets the caller (ripper) hold the process for cancellation.
    Raises RuntimeError on failure (with a tail of makemkvcon output).
    """
    Path(scratch_dir).mkdir(parents=True, exist_ok=True)
    cmd = ["makemkvcon", "-r", "--cache=1", "--progress=-same",
           "mkv", f"dev:{device}", str(title_id), scratch_dir]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    if register_proc:
        register_proc(proc)
    tail: list[str] = []
    assert proc.stdout is not None
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            break
        line = raw.decode(errors="replace").rstrip("\r\n")
        pct = parse_makemkv_progress(line)
        if pct is not None:
            if on_progress:
                on_progress(pct)
        elif line:
            tail.append(line)
            if len(tail) > 25:
                tail.pop(0)
    rc = await proc.wait()
    if rc != 0:
        detail = "\n".join(tail).strip()
        raise RuntimeError(f"makemkvcon exited with code {rc}\n{detail}")
    mkvs = sorted(Path(scratch_dir).glob("*.mkv"), key=lambda p: p.stat().st_mtime)
    if not mkvs:
        raise RuntimeError("makemkvcon produced no .mkv file")
    return mkvs[-1]
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_bluray.py -q`
Expected: PASS (all bluray tests).

- [ ] **Step 5: Commit**

```bash
git add app/bluray.py tests/test_bluray.py
git commit -m "feat(bluray): detect_and_scan + decrypt_title"
```

---

## Task 5: Ripper — two-phase Blu-ray jobs

**Files:** Modify `app/ripper.py`, `tests/test_ripper.py`. Read `ripper.py` first.

- [ ] **Step 1: Thread disc_format + scratch into enqueue/state**

In `Ripper.__init__`, add a `scratch_dir` parameter and store it:
```python
    def __init__(self, *, device: str, output_root: Path, library: Optional[Library] = None,
                 scratch_dir: str = "/data/scratch"):
```
and in the body: `self.scratch_dir = scratch_dir`.

Add `disc_format` to `enqueue` (keyword, default `"dvd"`), and store it in the selections tuple. Change the tuple type annotation and the assignment:
```python
        self._selections: dict[str, tuple[TitleSelection, EncoderMode, str, bool, str]] = {}
```
```python
        self._selections[job_id] = (selection, encoder_mode, str(output_path), burn_forced, disc_format)
```
Add `disc_format: str = "dvd"` to the `enqueue` signature, and set it on the RipRecord template (`disc_format=disc_format`).

Also add a cancellation-tracking set in `__init__` (used to classify cancel vs failure in the Blu-ray decrypt phase, where the process dying raises rather than returning an exit code):
```python
        self._cancelling: set[str] = set()
```
And in `cancel()`, in the branch that terminates the running process (`if job_id == self._current_job_id and self._current_process is not None:`), record the intent **before** terminating:
```python
            self._cancelling.add(job_id)
            try:
                self._current_process.terminate()
            except ProcessLookupError:
                pass
            return True
```

- [ ] **Step 2: Add failing orchestration test**

Append to `tests/test_ripper.py`:

```python
def test_bluray_job_runs_two_phases(tmp_path, monkeypatch):
    """Blu-ray job: decrypt (makemkv) then encode (handbrake). Both mocked."""
    import asyncio
    from app import bluray
    from app.library import Library
    from app.models import ContentType, EncoderMode, JobStatus, TitleSelection
    from app.ripper import Ripper

    calls = []

    async def fake_decrypt(device, title_id, scratch_dir, *, on_progress=None, register_proc=None):
        calls.append(("decrypt", title_id))
        if on_progress:
            on_progress(100.0)
        mkv = tmp_path / "title.mkv"
        mkv.write_bytes(b"x" * 10)
        return mkv

    monkeypatch.setattr(bluray, "decrypt_title", fake_decrypt)

    lib = Library(str(tmp_path / "lib.db")); lib.init_db()
    r = Ripper(device="/dev/sr0", output_root=tmp_path, library=lib,
               scratch_dir=str(tmp_path / "scratch"))

    # Stub the HandBrake phase so no real encoder runs. Signature accepts the
    # kwargs the real _run_handbrake takes (input_file, output_path, selection, ...).
    async def fake_encode(job_id, **kw):
        calls.append(("encode", kw.get("input_file")))
        (tmp_path / "out.mp4").write_bytes(b"y" * 10)
        return 0
    monkeypatch.setattr(r, "_run_handbrake", fake_encode, raising=False)

    from app.models import MovieMetadata
    job = r.enqueue(
        selection=TitleSelection(index=0, audio_tracks=[1], subtitle_tracks=[], part_index=1),
        encoder_mode=EncoderMode.QUALITY, content_type=ContentType.MOVIE,
        disc_format="bluray", movie=MovieMetadata(title="X", year=2020),
    )
    asyncio.run(r._run_job(job.id))
    assert ("decrypt", 0) in calls
    assert any(c[0] == "encode" for c in calls)
    assert r.get(job.id).status == JobStatus.DONE
```

(NOTE: this test pins the intended internal shape — a `_run_handbrake(job_id, input_file, output_path)` helper and a Blu-ray branch in `_run_job`. Implement to match.)

- [ ] **Step 2b: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_ripper.py -k two_phases -q`
Expected: FAIL.

- [ ] **Step 3: Refactor `_run_job` into a DVD path + a Blu-ray path**

In `app/ripper.py`, add `import shutil` to the imports and `from . import bluray`. Unpack `disc_format` in `_run_job`:
```python
        selection, encoder_mode, output_path, burn_forced, disc_format = self._selections[job_id]
```
Extract the existing HandBrake run into a helper `_run_handbrake(job_id, input_file, output_path)` that builds args (passing `input_file` for BD, else device+title), spawns HandBrakeCLI, consumes progress, returns rc. Then make `_run_job` branch:

```python
    async def _run_job(self, job_id: str) -> None:
        selection, encoder_mode, output_path, burn_forced, disc_format = self._selections[job_id]
        started = time.monotonic()
        if disc_format == "bluray":
            await self._run_bluray_job(job_id, selection, encoder_mode, output_path,
                                       burn_forced, started)
        else:
            await self._run_dvd_job(job_id, selection, encoder_mode, output_path,
                                    burn_forced, started)
```

Move the current `_run_job` body (HandBrake scan/encode of the device) into `_run_dvd_job(...)`, unchanged except it calls the shared `_run_handbrake` for the encode and keeps the terminal-status + `_save_record` logic. `_run_handbrake` signature:

```python
    async def _run_handbrake(self, job_id, *, input_file, output_path, selection,
                             encoder_mode, burn_forced, tail):
        args = build_handbrake_args(
            device=self.device, title_index=selection.index, output_path=output_path,
            audio_tracks=selection.audio_tracks, subtitle_tracks=selection.subtitle_tracks,
            burn_forced=burn_forced, encoder_mode=encoder_mode, input_file=input_file,
        )
        log.info("handbrake job %s: %s", job_id, shlex.join(args))
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self._current_process = proc
        self._current_job_id = job_id
        try:
            await self._consume_progress(job_id, proc, tail)
            return await proc.wait()
        finally:
            self._current_process = None
            self._current_job_id = None
```

And add the Blu-ray two-phase method:

```python
    async def _run_bluray_job(self, job_id, selection, encoder_mode, output_path,
                              burn_forced, started):
        tail: Deque[str] = deque(maxlen=25)
        mkv_path = None
        try:
            # Phase 1: decrypt (makemkvcon). A cancel kills the process, which makes
            # decrypt_title raise — classify cancel vs genuine failure explicitly.
            self._update(job_id, status=JobStatus.RUNNING, phase="decrypting", percent=0.0)
            def _prog(p): self._update(job_id, percent=p)
            try:
                mkv_path = await bluray.decrypt_title(
                    self.device, selection.index, self.scratch_dir,
                    on_progress=_prog,
                    register_proc=lambda pr: setattr(self, "_current_process", pr),
                )
            except Exception as exc:
                self._current_process = None
                self._current_job_id = None
                _sweep_scratch(self.scratch_dir)
                if job_id in self._cancelling or self._jobs[job_id].status == JobStatus.CANCELLED:
                    self._update(job_id, status=JobStatus.CANCELLED, phase=None)
                    self._save_record(job_id, JobStatus.CANCELLED)
                else:
                    msg = _friendly_makemkv_error(str(exc))
                    self._update(job_id, status=JobStatus.FAILED, phase=None, error=msg)
                    self._save_record(job_id, JobStatus.FAILED, error=msg)
                return
            # Phase 2: encode (HandBrake on the temp MKV)
            self._update(job_id, phase="encoding", percent=0.0)
            rc = await self._run_handbrake(
                job_id, input_file=str(mkv_path), output_path=output_path,
                selection=selection, encoder_mode=encoder_mode, burn_forced=burn_forced,
                tail=tail)
            elapsed = int(time.monotonic() - started)
            if rc == 0:
                self._update(job_id, status=JobStatus.DONE, phase=None, percent=100.0)
                self._save_record(job_id, JobStatus.DONE, rip_seconds=elapsed)
            else:
                cancelled = (rc < 0 or job_id in self._cancelling
                             or self._jobs[job_id].status == JobStatus.CANCELLED)
                _cleanup_partial(output_path)
                if cancelled:
                    self._update(job_id, status=JobStatus.CANCELLED, phase=None)
                    self._save_record(job_id, JobStatus.CANCELLED)
                else:
                    msg = f"HandBrakeCLI exited with code {rc}\n" + "\n".join(tail).strip()
                    self._update(job_id, status=JobStatus.FAILED, phase=None, error=msg)
                    self._save_record(job_id, JobStatus.FAILED, error=msg)
        finally:
            self._cancelling.discard(job_id)
            if mkv_path is not None:
                try:
                    Path(mkv_path).unlink(missing_ok=True)
                except OSError:
                    pass
```

`_run_dvd_job` is the previous `_run_job` body (it calls `_run_handbrake` with
`input_file=None` for the encode and keeps the existing terminal-status + `_save_record`
+ `_cleanup_partial` logic). DVD never sets `phase`. Its cancel branch should also include
`job_id in self._cancelling` in its `cancelled` check, and the `finally` should
`self._cancelling.discard(job_id)` — keeping cancel classification consistent across
both paths.

Add these module-level helpers near `_cleanup_partial`:

```python
def _sweep_scratch(scratch_dir: str) -> None:
    """Remove any leftover .mkv files from a failed/cancelled decrypt (single worker,
    so scratch only ever holds the current job's output)."""
    try:
        for mkv in Path(scratch_dir).glob("*.mkv"):
            mkv.unlink(missing_ok=True)
    except OSError:
        pass


def _friendly_makemkv_error(raw: str) -> str:
    """Map known makemkvcon failures to actionable messages."""
    low = raw.lower()
    if any(k in low for k in ("registration", "evaluation", "expired", "app_key", "key ")):
        return ("MakeMKV beta key missing or expired — update MAKEMKV_APP_KEY in .env "
                "(get the current free beta key from the MakeMKV forum) and restart.\n\n" + raw)
    return raw
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (all existing + new two-phase test).

- [ ] **Step 5: Commit**

```bash
git add app/ripper.py tests/test_ripper.py
git commit -m "feat(bluray): two-phase Blu-ray rip jobs (decrypt + encode)"
```

---

## Task 6: API — scan detection + rip routing

**Files:** Modify `app/main.py`, `tests/test_api.py`.

- [ ] **Step 1: Add failing tests**

Append to `tests/test_api.py`:

```python
def test_scan_reports_bluray_when_detected(client):
    from app.models import ScanResult, Title
    bd = ScanResult(dvd_label="BLURAY", disc_format="bluray", titles=[
        Title(index=0, duration_seconds=6000, chapter_count=12,
              frame_width=1920, frame_height=1080)])

    async def fake_detect(device, **kw):
        return bd

    with patch("app.main.bluray_detect_and_scan", side_effect=fake_detect):
        res = client.get("/api/scan")
    assert res.status_code == 200
    assert res.json()["disc_format"] == "bluray"


def test_scan_falls_back_to_dvd(client):
    async def fake_detect(device, **kw):
        return None
    from app.models import ScanResult

    async def fake_dvd(device):
        return ScanResult(dvd_label="A_DVD", titles=[])

    with patch("app.main.bluray_detect_and_scan", side_effect=fake_detect), \
         patch("app.main.scan", side_effect=fake_dvd):
        res = client.get("/api/scan")
    assert res.status_code == 200
    assert res.json()["disc_format"] == "dvd"
    assert res.json()["dvd_label"] == "A_DVD"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_api.py -k "bluray or falls_back" -q`
Expected: FAIL (`bluray_detect_and_scan` not defined).

- [ ] **Step 3: Implement**

In `app/main.py`:
- Import the detector under a patchable name: `from .bluray import detect_and_scan as bluray_detect_and_scan`.
- Add scratch config: `MAKEMKV_SCRATCH = os.getenv("MAKEMKV_SCRATCH", "/data/scratch")`.
- Pass scratch to the Ripper in lifespan: `Ripper(device=DVD_DEVICE, output_root=OUTPUT_ROOT, library=library, scratch_dir=MAKEMKV_SCRATCH)`.
- Replace `api_scan`:
```python
@app.get("/api/scan", response_model=ScanResult)
async def api_scan() -> ScanResult:
    bd = await bluray_detect_and_scan(DVD_DEVICE)
    if bd is not None:
        return bd
    return await scan(DVD_DEVICE)
```
- In `api_rip`, pass `disc_format` to enqueue:
```python
        job = ripper.enqueue(
            selection=selection, encoder_mode=payload.encoder_mode,
            content_type=payload.content_type, burn_forced=payload.burn_forced_subtitles,
            disc_format=payload.disc_format, movie=payload.movie, tv=payload.tv,
        )
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_api.py
git commit -m "feat(bluray): scan detection + rip routing by disc_format"
```

---

## Task 7: Container — build MakeMKV, beta key, scratch dir

**HIGH RISK / needs real build iteration.** Verified by building the image, not unit tests.

**Files:** Modify `Dockerfile`, `scripts/entrypoint.sh`, `docker-compose.yml`, `.env.example`.

- [ ] **Step 1: Add the MakeMKV build to the Dockerfile**

Set `MAKEMKV_VERSION` to the current release from <https://www.makemkv.com/download/> (check the page; the tarballs are `makemkv-oss-<V>.tar.gz` and `makemkv-bin-<V>.tar.gz`). Add a build stage after the existing apt install block (before `WORKDIR /app`):

```dockerfile
# --- MakeMKV (built from source; CLI only, no Qt GUI) ---
ARG MAKEMKV_VERSION=1.17.9
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential pkg-config wget ca-certificates \
        libc6-dev libssl-dev libexpat1-dev zlib1g-dev \
        libavcodec-dev libavutil-dev libavformat-dev \
    && cd /tmp \
    && wget -q "https://www.makemkv.com/download/makemkv-oss-${MAKEMKV_VERSION}.tar.gz" \
    && wget -q "https://www.makemkv.com/download/makemkv-bin-${MAKEMKV_VERSION}.tar.gz" \
    && tar xzf makemkv-oss-${MAKEMKV_VERSION}.tar.gz \
    && tar xzf makemkv-bin-${MAKEMKV_VERSION}.tar.gz \
    && cd makemkv-oss-${MAKEMKV_VERSION} \
    && ./configure --disable-gui >/dev/null \
    && make -j"$(nproc)" >/dev/null && make install \
    && cd ../makemkv-bin-${MAKEMKV_VERSION} \
    && mkdir -p tmp && echo accepted > tmp/eula_accepted \
    && make -j"$(nproc)" >/dev/null && make install \
    && cd / && rm -rf /tmp/makemkv-* \
    && apt-get purge -y build-essential pkg-config wget && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
```
Confirm `makemkvcon` is on PATH (`/usr/bin/makemkvcon`). If the build needs more dev libs, add them. (This is the iterate-until-it-builds step.)

Add MakeMKV env defaults to the existing `ENV` block:
```dockerfile
    MAKEMKV_APP_KEY="" \
    MAKEMKV_SCRATCH=/data/scratch
```

- [ ] **Step 2: Write the beta key + scratch dir in the entrypoint**

In `scripts/entrypoint.sh`, after the `/data` chown lines, add (writes the key into the `app` user's MakeMKV config when provided):

```bash
SCRATCH="${MAKEMKV_SCRATCH:-/data/scratch}"
mkdir -p "$SCRATCH" && chown -R "$PUID:$PGID" "$SCRATCH" 2>/dev/null || true
if [ -n "${MAKEMKV_APP_KEY:-}" ]; then
    APP_HOME="$(getent passwd "$PUID" | cut -d: -f6)"
    APP_HOME="${APP_HOME:-/home/app}"
    mkdir -p "$APP_HOME/.MakeMKV"
    printf 'app_Key = "%s"\n' "$MAKEMKV_APP_KEY" > "$APP_HOME/.MakeMKV/settings.conf"
    chown -R "$PUID:$PGID" "$APP_HOME/.MakeMKV" 2>/dev/null || true
fi
```

- [ ] **Step 3: Pass the env through compose**

In `docker-compose.yml`, add under `environment:`:
```yaml
      MAKEMKV_APP_KEY: "${MAKEMKV_APP_KEY:-}"
      MAKEMKV_SCRATCH: /data/scratch
```

- [ ] **Step 4: Document in `.env.example`**

Add under the TMDB key block:
```
# MakeMKV beta key for Blu-ray decryption (rotates ~monthly; get the current one at
# https://forum.makemkv.com/forum/viewtopic.php?t=1053 ). Without it, Blu-ray rips fail
# (DVD still works).
MAKEMKV_APP_KEY=
```

- [ ] **Step 5: Build to verify (the real test for this task)**

```bash
make build
```
Expected: image builds; then `docker run --rm dvd-rip-dvd-rip makemkvcon -r --version` prints a version line (rc 0). Iterate on deps/version until it builds and `makemkvcon` runs.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile scripts/entrypoint.sh docker-compose.yml .env.example
git commit -m "feat(bluray): build makemkvcon into image; beta key + scratch config"
```

---

## Task 8: Frontend — disc badge + phase label

**Files:** Modify `app/templates/index.html`, `app/static/app.js`, `app/static/style.css`. Read each first.

- [ ] **Step 1: Show the disc-format badge**

In `app/static/app.js`, `state` already holds `scan`. In `renderDiscMeta(scan)`, after setting `#disc-label`, add a badge. First add an element next to the disc label in `index.html` — find `<h2 id="disc-label">Disc</h2>` and change to:
```html
        <h2 id="disc-label">Disc</h2><span id="disc-format-badge" class="disc-badge" hidden></span>
```
Then in `renderDiscMeta`, after `$('#disc-label').textContent = scan.dvd_label;` add:
```javascript
  const badge = $('#disc-format-badge');
  badge.textContent = scan.disc_format === 'bluray' ? 'Blu-ray' : 'DVD';
  badge.hidden = false;
```
Send `disc_format` in the rip payload — in `startRip`, add to `body`:
```javascript
    disc_format: (state.scan && state.scan.disc_format) || 'dvd',
```

- [ ] **Step 2: Show the job phase**

In `app/static/app.js` `updateJob(li, job)`, where the meta string is built, prepend the phase when present. Find the `const meta = [];` block and add at the top of it:
```javascript
  if (job.phase) meta.push(job.phase === 'decrypting' ? 'Decrypting' : 'Encoding');
```

- [ ] **Step 3: Style the badge**

Append to `app/static/style.css`:
```css
.disc-badge {
  margin-left: 10px;
  font-size: 10px;
  font-weight: 500;
  color: var(--accent-bright);
  border: 1px solid var(--accent);
  padding: 1px 8px;
  border-radius: 999px;
  vertical-align: 2px;
}
```

- [ ] **Step 4: Smoke test (renders, no JS error)**

```bash
LIBRARY_DB=/tmp/bd-smoke.db OUTPUT_ROOT=./output DVD_DEVICE=/dev/null \
  .venv/bin/python -c "
from fastapi.testclient import TestClient
import importlib, app.main as m; importlib.reload(m)
with TestClient(m.app) as c:
    assert 'disc-format-badge' in c.get('/').text
print('OK')
"
rm -f /tmp/bd-smoke.db
```
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add app/templates/index.html app/static/app.js app/static/style.css
git commit -m "feat(bluray): disc-format badge + decrypt/encode phase label"
```

---

## Task 9: Docs + full verification

**Files:** Modify `README.md`, `CLAUDE.md`.

- [ ] **Step 1: CLAUDE.md** — add a `bluray.py` row to the module map (after `dvd.py`):
```markdown
| `bluray.py` | MakeMKV front-end — detect BD, scan titles, decrypt a title to a temp MKV (the only module that touches `makemkvcon`) |
```
And add to container plumbing: a note that `makemkvcon` is built into the image and `MAKEMKV_APP_KEY` (beta key, rotates) + `MAKEMKV_SCRATCH` configure Blu-ray.

- [ ] **Step 2: README** — add a "Blu-ray" section: auto-detected; needs `MAKEMKV_APP_KEY` in `.env` (free beta key, rotates ~monthly, link to the forum); two-stage (decrypt→encode) so rips are slower and need ~40 GB scratch transiently under `./data`; DVD path unchanged.

- [ ] **Step 3: Full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (all).

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs(bluray): document Blu-ray support, MakeMKV key, scratch dir"
```

- [ ] **Step 5: Real-hardware verification (human, after merge/deploy)**

With a real Blu-ray inserted and a current `MAKEMKV_APP_KEY` in `.env`:
1. `make build && make up-d` (ensure no rip running first).
2. Open the UI → confirm the disc shows a **Blu-ray** badge and titles list.
3. Confirm the three verification points: makemkvcon output parses correctly, the build works, and audio/subtitle track selection lines up in the output. Adjust `parse_makemkv_info` / track mapping if needed.
4. Rip the main title → confirm decrypt phase, then encode phase, then a playable MP4 in `/output/Movies/...` and a library row with `disc_format=bluray`.

---

## Notes for the implementer

- **Single worker still.** Blu-ray jobs are serialized like DVD; the two phases run sequentially within one job.
- **Cancellation** must kill whichever subprocess is current — `decrypt_title` registers its process via `register_proc`, and `_run_handbrake` sets `_current_process`; the existing `cancel()` terminates `_current_process`. Verify cancel works in both phases.
- **Scratch cleanup** is in the `finally` of `_run_bluray_job` — the temp MKV is always removed, success or failure.
- **Don't touch the DVD path** beyond extracting the shared `_run_handbrake` helper; `_run_dvd_job` must behave exactly as the old `_run_job` did.
