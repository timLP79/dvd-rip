from collections import deque
from pathlib import Path

from app.ripper import Ripper, _PROGRESS_RE


def test_progress_regex_with_eta():
    line = "Encoding: task 1 of 1, 42.50 % (35.10 fps, avg 32.45 fps, ETA 00h08m12s)"
    m = _PROGRESS_RE.search(line)
    assert m is not None
    assert m.group("pct") == "42.50"
    assert m.group("fps") == "35.10"
    assert m.group("h") == "00"
    assert m.group("m") == "08"
    assert m.group("s") == "12"


def test_progress_regex_without_eta():
    line = "Encoding: task 1 of 1, 12.34 %"
    m = _PROGRESS_RE.search(line)
    assert m is not None
    assert m.group("pct") == "12.34"
    assert m.group("fps") is None


def test_progress_regex_ignores_other_output():
    line = "Scanning title 1 of 5"
    assert _PROGRESS_RE.search(line) is None


def test_handle_line_collects_non_progress_into_tail():
    """Non-progress output (errors, warnings) is retained for failure diagnostics."""
    ripper = Ripper(device="/dev/null", output_root=Path("/tmp"))
    tail: deque[str] = deque(maxlen=25)

    # A progress line does NOT go into the tail.
    ripper._handle_line("nojob", "Encoding: task 1 of 1, 5.00 %", tail)
    assert len(tail) == 0

    # Error/warning lines DO accumulate (stripped, non-empty only).
    ripper._handle_line("nojob", "libdvdcss: could not get title key", tail)
    ripper._handle_line("nojob", "   ", tail)  # blank -> skipped
    ripper._handle_line("nojob", "No space left on device", tail)
    assert list(tail) == [
        "libdvdcss: could not get title key",
        "No space left on device",
    ]


def test_handle_line_tail_is_bounded():
    ripper = Ripper(device="/dev/null", output_root=Path("/tmp"))
    tail: deque[str] = deque(maxlen=25)
    for i in range(100):
        ripper._handle_line("nojob", f"log line {i}", tail)
    assert len(tail) == 25
    assert list(tail)[-1] == "log line 99"


def test_save_record_persists_to_library(tmp_path):
    from app.library import Library
    from app.models import ContentType, JobStatus, RipRecord
    from app.ripper import Ripper

    lib = Library(str(tmp_path / "lib.db"))
    lib.init_db()
    r = Ripper(device="/dev/null", output_root=tmp_path, library=lib)
    r._records["job1"] = RipRecord(
        content_type=ContentType.MOVIE, title="Heat", year=1995,
        output_path=str(tmp_path / "Heat.mp4"), status=JobStatus.QUEUED,
    )
    (tmp_path / "Heat.mp4").write_bytes(b"x" * 42)
    r._save_record("job1", JobStatus.DONE, rip_seconds=120)

    rows = lib.list_rips()
    assert len(rows) == 1
    assert rows[0].title == "Heat"
    assert rows[0].status == JobStatus.DONE
    assert rows[0].file_size_bytes == 42
    assert rows[0].rip_seconds == 120


def test_save_record_noop_without_library(tmp_path):
    from app.models import JobStatus
    from app.ripper import Ripper
    r = Ripper(device="/dev/null", output_root=tmp_path, library=None)
    # Should not raise even though there's no library and no record.
    r._save_record("missing", JobStatus.FAILED, error="x")


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
