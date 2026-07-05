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

    async def fake_decrypt(device, title_id, scratch_dir, *, on_progress=None, on_stage=None, register_proc=None):
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


def test_bluray_cancel_during_decrypt_marks_cancelled(tmp_path, monkeypatch):
    import asyncio
    from app import bluray
    from app.library import Library
    from app.models import ContentType, EncoderMode, JobStatus, MovieMetadata, TitleSelection
    from app.ripper import Ripper

    lib = Library(str(tmp_path / "lib.db")); lib.init_db()
    r = Ripper(device="/dev/sr0", output_root=tmp_path, library=lib,
               scratch_dir=str(tmp_path / "scratch"))

    async def fake_decrypt(device, title_id, scratch_dir, *, on_progress=None, on_stage=None, register_proc=None):
        # simulate cancel(): mark intent + raise as a killed process would
        r._cancelling.add(job_id_holder["id"])
        raise RuntimeError("makemkvcon exited with code -15")

    monkeypatch.setattr(bluray, "decrypt_title", fake_decrypt)
    job = r.enqueue(
        selection=TitleSelection(index=0, audio_tracks=[1], subtitle_tracks=[], part_index=1),
        encoder_mode=EncoderMode.QUALITY, content_type=ContentType.MOVIE,
        disc_format="bluray", movie=MovieMetadata(title="X", year=2020))
    job_id_holder = {"id": job.id}
    asyncio.run(r._run_job(job.id))
    assert r.get(job.id).status == JobStatus.CANCELLED


def test_save_record_fires_push(tmp_path):
    import asyncio
    from app.library import Library
    from app.models import (
        ContentType, EncoderMode, JobStatus, MovieMetadata, TitleSelection,
    )
    from app.ripper import Ripper

    class FakePush:
        enabled = True
        def __init__(self):
            self.records = []
        async def notify(self, record):
            self.records.append(record)
            return 1

    fake = FakePush()

    async def scenario():
        lib = Library(str(tmp_path / "library.db"))
        lib.init_db()
        r = Ripper(device="/dev/null", output_root=tmp_path, library=lib, push=fake)
        job = r.enqueue(
            selection=TitleSelection(index=1, audio_tracks=[1], subtitle_tracks=[]),
            encoder_mode=EncoderMode.QUALITY,
            content_type=ContentType.MOVIE,
            movie=MovieMetadata(title="Inception", year=2010),
        )
        r._save_record(job.id, JobStatus.DONE)
        await asyncio.sleep(0)  # let the fire-and-forget task run
        return fake.records

    records = asyncio.run(scenario())
    assert len(records) == 1
    assert records[0].title == "Inception"
    assert records[0].status == JobStatus.DONE


def test_is_busy_reflects_current_job():
    r = Ripper(device="/dev/null", output_root=Path("/tmp"), library=None)
    assert r.is_busy() is False
    r._current_job_id = "job-1"          # a rip is actively running -> drive in use
    assert r.is_busy() is True
    r._current_job_id = None
    assert r.is_busy() is False


def test_save_record_retains_notify_task_until_done(tmp_path):
    # The fire-and-forget notify must hold a STRONG reference so the task isn't
    # garbage-collected before its (network-bound) send completes — the documented
    # asyncio.create_task weak-ref footgun that was silently dropping rip
    # notifications. Retained immediately, discarded on completion.
    import asyncio
    import gc
    from app.library import Library
    from app.models import (
        ContentType, EncoderMode, JobStatus, MovieMetadata, TitleSelection,
    )
    from app.ripper import Ripper

    class SlowPush:
        enabled = True
        def __init__(self):
            self.done = False
        async def notify(self, record):
            await asyncio.sleep(0.05)   # yields, like the real executor-bound send
            self.done = True
            return 1

    push = SlowPush()

    async def scenario():
        lib = Library(str(tmp_path / "lib.db")); lib.init_db()
        r = Ripper(device="/dev/null", output_root=tmp_path, library=lib, push=push)
        job = r.enqueue(
            selection=TitleSelection(index=1, audio_tracks=[1], subtitle_tracks=[]),
            encoder_mode=EncoderMode.QUALITY, content_type=ContentType.MOVIE,
            movie=MovieMetadata(title="X", year=2020),
        )
        r._save_record(job.id, JobStatus.DONE)
        retained = len(r._notify_tasks)     # strong ref held before notify finishes
        gc.collect()                        # an un-retained task could die here
        await asyncio.sleep(0.1)            # let the slow notify complete
        return retained, push.done, len(r._notify_tasks)

    retained, done, remaining = asyncio.run(scenario())
    assert retained == 1      # task was kept alive
    assert done is True       # notify actually completed (not GC'd mid-flight)
    assert remaining == 0     # cleaned up via done-callback


def test_save_record_persists_aspect(tmp_path):
    from app.library import Library
    from app.models import (ContentType, EncoderMode, JobStatus, MovieMetadata, TitleSelection)
    from app.ripper import Ripper

    lib = Library(str(tmp_path / "library.db")); lib.init_db()
    r = Ripper(device="/dev/null", output_root=tmp_path, library=lib)
    job = r.enqueue(
        selection=TitleSelection(index=1, audio_tracks=[1], subtitle_tracks=[], aspect="widescreen"),
        encoder_mode=EncoderMode.QUALITY, content_type=ContentType.MOVIE,
        movie=MovieMetadata(title="Wide Movie", year=2020),
    )
    r._save_record(job.id, JobStatus.DONE)
    assert lib.list_rips()[0].aspect == "widescreen"


def test_enqueue_cd_seeds_record_and_job(tmp_path):
    from app.library import Library
    from app.models import CDRipRequest, CDTrackSelection, ContentType
    from app.ripper import Ripper
    lib = Library(str(tmp_path / "lib.db")); lib.init_db()
    r = Ripper(device="/dev/sr0", output_root=tmp_path, library=lib)
    job = r.enqueue_cd(request=CDRipRequest(
        mode="audiobook", creator="Neil Gaiman", title="Fragile Things",
        disc_number=5, total_discs=9, tracks=[CDTrackSelection(number=1)]))
    assert job.id in r._cd_requests
    rec = r._records[job.id]
    assert rec.content_type == ContentType.AUDIOBOOK
    assert rec.artist == "Neil Gaiman"
    assert rec.disc_format == "cd"
    assert "Fragile Things" in job.output_path


def test_run_cd_job_calls_rip_disc_and_records(tmp_path, monkeypatch):
    import asyncio
    from app import cd
    from app.library import Library
    from app.models import CDRipRequest, CDTrackSelection, JobStatus
    from app.ripper import Ripper

    async def fake_rip_disc(device, **kw):
        out = tmp_path / "Audiobooks" / "Neil Gaiman" / "Fragile Things" / "Fragile Things, Disc 05.mp3"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 99)
        if kw.get("on_progress"):
            kw["on_progress"](100)
        return [out]

    monkeypatch.setattr(cd, "rip_disc", fake_rip_disc)
    lib = Library(str(tmp_path / "lib.db")); lib.init_db()
    r = Ripper(device="/dev/sr0", output_root=tmp_path, library=lib,
               scratch_dir=str(tmp_path / "scratch"))
    job = r.enqueue_cd(request=CDRipRequest(
        mode="audiobook", creator="Neil Gaiman", title="Fragile Things",
        disc_number=5, total_discs=9, tracks=[CDTrackSelection(number=1)]))
    asyncio.run(r._run_job(job.id))
    assert r.get(job.id).status == JobStatus.DONE
    rows = lib.list_rips()
    assert rows[0].artist == "Neil Gaiman"
    assert rows[0].file_size_bytes == 99


def test_run_cd_job_failure_marks_failed_and_cleans(tmp_path, monkeypatch):
    import asyncio
    from app import cd
    from app.library import Library
    from app.models import CDRipRequest, CDTrackSelection, JobStatus
    from app.ripper import Ripper

    async def boom_rip(device, **kw):
        raise cd.RipError("cdparanoia failed on track 3\nscratched disc")

    monkeypatch.setattr(cd, "rip_disc", boom_rip)
    lib = Library(str(tmp_path / "lib.db")); lib.init_db()
    r = Ripper(device="/dev/sr0", output_root=tmp_path, library=lib,
               scratch_dir=str(tmp_path / "scratch"))
    job = r.enqueue_cd(request=CDRipRequest(
        mode="music", creator="Art", title="Alb",
        tracks=[CDTrackSelection(number=1)]))
    asyncio.run(r._run_job(job.id))
    assert r.get(job.id).status == JobStatus.FAILED
    assert "scratched disc" in (r.get(job.id).error or "")


def test_cd_cancel_during_rip_marks_cancelled(tmp_path, monkeypatch):
    import asyncio
    from app import cd
    from app.library import Library
    from app.models import CDRipRequest, CDTrackSelection, JobStatus
    from app.ripper import Ripper

    lib = Library(str(tmp_path / "lib.db")); lib.init_db()
    r = Ripper(device="/dev/sr0", output_root=tmp_path, library=lib,
               scratch_dir=str(tmp_path / "scratch"))

    async def fake_rip_disc(device, **kw):
        # simulate cancel(): mark intent + raise as a terminated cdparanoia would
        r._cancelling.add(job_id_holder["id"])
        raise cd.RipError("cdparanoia exited with code -15")

    monkeypatch.setattr(cd, "rip_disc", fake_rip_disc)
    job = r.enqueue_cd(request=CDRipRequest(
        mode="music", creator="Art", title="Alb",
        tracks=[CDTrackSelection(number=1)]))
    job_id_holder = {"id": job.id}
    asyncio.run(r._run_job(job.id))
    assert r.get(job.id).status == JobStatus.CANCELLED
