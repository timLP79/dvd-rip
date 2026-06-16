"""Job queue and HandBrake subprocess orchestration.

A single background worker processes one rip at a time. We only have one optical drive and
one GPU; running rips in parallel just thrashes both, so serializing is the right default.

Progress is parsed from HandBrakeCLI's stdout (the plain-text 'Encoding: task ... %' lines)
and published to per-job asyncio.Queues that the SSE endpoint subscribes to.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shlex
import shutil
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator, Deque, Optional

from .models import (
    ContentType,
    EncoderMode,
    Job,
    JobStatus,
    MovieMetadata,
    RipRecord,
    TitleSelection,
    TVMetadata,
)
from .library import Library
from .paths import movie_path, tv_episode_path, unique_path
from .presets import build_handbrake_args
from . import bluray

log = logging.getLogger(__name__)

# Matches:  Encoding: task 1 of 1, 12.34 % (45.67 fps, avg 50.00 fps, ETA 00h05m23s)
_PROGRESS_RE = re.compile(
    r"Encoding:\s*task\s+\d+\s+of\s+\d+,\s+(?P<pct>\d+\.\d+)\s*%"
    r"(?:\s+\((?P<fps>\d+\.\d+)\s*fps,\s*avg\s*[\d.]+\s*fps,"
    r"\s*ETA\s*(?P<h>\d+)h(?P<m>\d+)m(?P<s>\d+)s\))?"
)

_MAX_FINISHED_JOBS = 50


class Ripper:
    """Owns the job queue, the worker task, and progress fanout."""

    def __init__(self, *, device: str, output_root: Path, library: Optional[Library] = None,
                 scratch_dir: str = "/data/scratch"):
        self.device = device
        self.output_root = output_root
        self.library = library
        self.scratch_dir = scratch_dir
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._jobs: dict[str, Job] = {}
        self._job_order: list[str] = []
        self._selections: dict[str, tuple[TitleSelection, EncoderMode, str, bool, str]] = {}
        self._records: dict[str, RipRecord] = {}
        self._subscribers: dict[str, list[asyncio.Queue[Job]]] = {}
        self._current_process: Optional[asyncio.subprocess.Process] = None
        self._current_job_id: Optional[str] = None
        self._worker_task: Optional[asyncio.Task[None]] = None
        self._cancelling: set[str] = set()

    async def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="ripper-worker")

    async def stop(self) -> None:
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    def enqueue(
        self,
        *,
        selection: TitleSelection,
        encoder_mode: EncoderMode,
        content_type: ContentType,
        burn_forced: bool = True,
        disc_format: str = "dvd",
        movie: Optional[MovieMetadata] = None,
        tv: Optional[TVMetadata] = None,
    ) -> Job:
        if content_type is ContentType.MOVIE:
            if movie is None:
                raise ValueError("movie metadata required for content_type=movie")
            output_path = movie_path(
                self.output_root,
                title=movie.title,
                year=movie.year,
                part_index=selection.part_index,
            )
        else:
            if tv is None:
                raise ValueError("tv metadata required for content_type=tv")
            if selection.episode_number is None:
                raise ValueError("episode_number required for TV titles")
            output_path = tv_episode_path(
                self.output_root,
                show=tv.show,
                season=tv.season,
                episode=selection.episode_number,
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path = unique_path(output_path)

        job_id = secrets.token_hex(6)
        job = Job(
            id=job_id,
            title_index=selection.index,
            output_path=str(output_path),
            status=JobStatus.QUEUED,
        )
        self._jobs[job_id] = job
        self._job_order.append(job_id)
        self._selections[job_id] = (selection, encoder_mode, str(output_path), burn_forced, disc_format)
        self._subscribers[job_id] = []
        self._records[job_id] = RipRecord(
            content_type=content_type,
            title=(movie.title if content_type is ContentType.MOVIE else tv.show),  # type: ignore[union-attr]
            year=(movie.year if movie else None),
            season=(tv.season if tv else None),
            episode=(selection.episode_number if content_type is ContentType.TV else None),
            tmdb_id=(movie.tmdb_id if movie else (tv.tmdb_id if tv else None)),
            output_path=str(output_path),
            encoder_mode=encoder_mode,
            burn_forced=burn_forced,
            disc_format=disc_format,
            status=JobStatus.QUEUED,
        )
        self._queue.put_nowait(job_id)
        self._trim_finished()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        return [self._jobs[jid] for jid in self._job_order if jid in self._jobs]

    async def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        if job.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
            return False
        if job_id == self._current_job_id and self._current_process is not None:
            self._cancelling.add(job_id)
            try:
                self._current_process.terminate()
            except ProcessLookupError:
                pass
            return True
        self._update(job_id, status=JobStatus.CANCELLED)
        return True

    async def subscribe(self, job_id: str) -> AsyncIterator[Job]:
        job = self._jobs.get(job_id)
        if not job:
            return
        queue: asyncio.Queue[Job] = asyncio.Queue()
        self._subscribers[job_id].append(queue)
        try:
            yield job.model_copy()
            if job.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
                return
            while True:
                update = await queue.get()
                yield update
                if update.status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED):
                    return
        finally:
            try:
                self._subscribers[job_id].remove(queue)
            except ValueError:
                pass

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if not job or job.status == JobStatus.CANCELLED:
                continue
            try:
                await self._run_job(job_id)
            except Exception as exc:
                log.exception("Unhandled error in rip job %s", job_id)
                self._update(job_id, status=JobStatus.FAILED, error=str(exc))
                self._save_record(job_id, JobStatus.FAILED, error=str(exc))

    async def _run_job(self, job_id: str) -> None:
        selection, encoder_mode, output_path, burn_forced, disc_format = self._selections[job_id]
        started = time.monotonic()
        if disc_format == "bluray":
            await self._run_bluray_job(job_id, selection, encoder_mode, output_path,
                                       burn_forced, started)
        else:
            await self._run_dvd_job(job_id, selection, encoder_mode, output_path,
                                    burn_forced, started)

    async def _run_dvd_job(self, job_id: str, selection: TitleSelection, encoder_mode: EncoderMode,
                           output_path: str, burn_forced: bool, started: float) -> None:
        log.info("starting DVD rip job %s", job_id)
        self._update(job_id, status=JobStatus.RUNNING, percent=0.0)
        # Keep the last N non-progress output lines so a failure can report WHY
        # (CSS decryption error, disk full, no audio track) instead of a bare exit code.
        tail: Deque[str] = deque(maxlen=25)
        rc = await self._run_handbrake(
            job_id, input_file=None, output_path=output_path,
            selection=selection, encoder_mode=encoder_mode, burn_forced=burn_forced, tail=tail)
        elapsed = int(time.monotonic() - started)
        if rc == 0:
            self._update(job_id, status=JobStatus.DONE, percent=100.0)
            self._save_record(job_id, JobStatus.DONE, rip_seconds=elapsed)
        else:
            cancelled = rc < 0 or job_id in self._cancelling or self._jobs[job_id].status == JobStatus.CANCELLED
            _cleanup_partial(output_path)
            if cancelled:
                self._update(job_id, status=JobStatus.CANCELLED)
                self._save_record(job_id, JobStatus.CANCELLED)
            else:
                detail = "\n".join(tail).strip()
                message = f"HandBrakeCLI exited with code {rc}"
                if detail:
                    message += f"\n{detail}"
                self._update(job_id, status=JobStatus.FAILED, error=message)
                self._save_record(job_id, JobStatus.FAILED, error=message)
        self._cancelling.discard(job_id)

    async def _run_handbrake(self, job_id: str, *, input_file: Optional[str], output_path: str,
                             selection: TitleSelection, encoder_mode: EncoderMode,
                             burn_forced: bool, tail: Deque[str]) -> int:
        args = build_handbrake_args(
            device=self.device,
            title_index=selection.index,
            output_path=output_path,
            audio_tracks=selection.audio_tracks,
            subtitle_tracks=selection.subtitle_tracks,
            burn_forced=burn_forced,
            encoder_mode=encoder_mode,
            input_file=input_file,
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

    async def _run_bluray_job(self, job_id: str, selection: TitleSelection,
                              encoder_mode: EncoderMode, output_path: str,
                              burn_forced: bool, started: float) -> None:
        tail: Deque[str] = deque(maxlen=25)
        mkv_path = None
        try:
            # Phase 1: decrypt (makemkvcon). A cancel kills the process, which makes
            # decrypt_title raise — classify cancel vs genuine failure explicitly.
            self._update(job_id, status=JobStatus.RUNNING, phase="decrypting", percent=0.0)
            def _prog(p: float) -> None:
                self._update(job_id, percent=p)
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

    async def _consume_progress(
        self,
        job_id: str,
        proc: asyncio.subprocess.Process,
        tail: Deque[str],
    ) -> None:
        # HandBrakeCLI emits "Encoding: task X of Y, P.PP %" lines terminated
        # with \r so they overwrite each other in a terminal. asyncio's
        # readline() blocks until \n — so we must read raw chunks and split
        # on either \r or \n ourselves, otherwise the UI sits at 0% the entire
        # rip even though encoding is happening.
        assert proc.stdout is not None
        buffer = bytearray()
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                if buffer:
                    self._handle_line(job_id, buffer.decode(errors="replace"), tail)
                return
            buffer.extend(chunk)
            while True:
                # Find the earliest \r or \n in the buffer.
                idx = -1
                for i, b in enumerate(buffer):
                    if b == 0x0a or b == 0x0d:
                        idx = i
                        break
                if idx < 0:
                    break
                line = bytes(buffer[:idx])
                del buffer[: idx + 1]
                if line:
                    self._handle_line(job_id, line.decode(errors="replace"), tail)

    def _handle_line(self, job_id: str, line: str, tail: Deque[str]) -> None:
        match = _PROGRESS_RE.search(line)
        if not match:
            # Not a progress tick — keep it for failure diagnostics. HandBrake's
            # actual error (decryption, disk full, no audio) lands here.
            stripped = line.strip()
            if stripped:
                tail.append(stripped)
            return
        pct = float(match.group("pct"))
        fps = float(match.group("fps")) if match.group("fps") else None
        eta: Optional[int] = None
        if match.group("h") is not None:
            eta = (
                int(match.group("h")) * 3600
                + int(match.group("m")) * 60
                + int(match.group("s"))
            )
        self._update(job_id, percent=pct, fps=fps, eta_seconds=eta)

    def _save_record(
        self,
        job_id: str,
        status: JobStatus,
        *,
        rip_seconds: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        """Persist a finished job to the library. Best-effort: never crash the worker."""
        if self.library is None:
            return
        record = self._records.get(job_id)
        if record is None:
            return
        record.status = status
        record.error = error
        record.rip_seconds = rip_seconds
        if status is JobStatus.DONE:
            try:
                record.file_size_bytes = os.path.getsize(record.output_path)
            except OSError:
                record.file_size_bytes = None
        try:
            self.library.record_rip(record)
        except Exception:
            log.exception("failed to record rip %s to library", job_id)

    def _update(self, job_id: str, **fields: object) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        for key, value in fields.items():
            setattr(job, key, value)
        snapshot = job.model_copy()
        for queue in self._subscribers.get(job_id, []):
            queue.put_nowait(snapshot)

    def _trim_finished(self) -> None:
        finished = [
            jid for jid in self._job_order
            if self._jobs.get(jid)
            and self._jobs[jid].status in (JobStatus.DONE, JobStatus.FAILED, JobStatus.CANCELLED)
        ]
        excess = len(finished) - _MAX_FINISHED_JOBS
        if excess <= 0:
            return
        for jid in finished[:excess]:
            self._jobs.pop(jid, None)
            self._selections.pop(jid, None)
            self._records.pop(jid, None)
            self._subscribers.pop(jid, None)
            try:
                self._job_order.remove(jid)
            except ValueError:
                pass


def _cleanup_partial(path_str: str) -> None:
    """Remove a partial output file from a cancelled or failed job.

    HandBrake writes the MP4 progressively, but only finalizes the moov atom
    (and applies faststart) on clean exit. A non-zero exit leaves an
    unplayable file regardless of size, so we delete it unconditionally
    rather than letting hundreds of MB of garbage accumulate in the library.
    """
    try:
        Path(path_str).unlink(missing_ok=True)
    except OSError:
        pass


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
