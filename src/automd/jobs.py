from __future__ import annotations

import secrets
import shutil
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import (
    Artifact,
    JobStatus,
    ProcessingOptions,
    ProgressEvent,
    SourceSpec,
)
from .pipeline import JobCancelled, Pipeline, PipelineResult


class BusyError(RuntimeError):
    pass


@dataclass(slots=True)
class Job:
    id: str
    workspace: Path
    sources: list[SourceSpec]
    options: ProcessingOptions
    status: JobStatus = JobStatus.QUEUED
    message: str = "Queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    events: list[ProgressEvent] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    document_count: int = 0
    skipped_count: int = 0
    warning_count: int = 0
    error: str | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    future: Future[None] | None = None
    delete_requested: bool = False
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def emit(
        self,
        event: str,
        phase: str,
        completed: int,
        total: int,
        message: str,
        source_id: str | None,
    ) -> None:
        with self._lock:
            sequence = self.events[-1].sequence + 1 if self.events else 1
            self.events.append(
                ProgressEvent(sequence, event, phase, completed, total, message, source_id)
            )
            if len(self.events) > 2_000:
                self.events = self.events[-1_000:]
            self.message = message
            self.updated_at = time.time()

    def events_after(self, sequence: int) -> list[ProgressEvent]:
        with self._lock:
            return [event for event in self.events if event.sequence > sequence]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "id": self.id,
                "status": self.status,
                "message": self.message,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "document_count": self.document_count,
                "skipped_count": self.skipped_count,
                "warning_count": self.warning_count,
                "error": self.error,
                "artifacts": [artifact.public() for artifact in self.artifacts],
            }


class JobManager:
    def __init__(self) -> None:
        self.session_root = Path(tempfile.gettempdir()) / f"automd-{secrets.token_hex(8)}"
        self.session_root.mkdir(parents=True, exist_ok=False)
        _clear_stale_sessions(self.session_root.parent, exclude=self.session_root)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="automd-worker")

    def create(self, sources: list[SourceSpec], options: ProcessingOptions) -> Job:
        with self._lock:
            pending = sum(
                job.status in {JobStatus.QUEUED, JobStatus.RUNNING} for job in self._jobs.values()
            )
            if pending >= 2:
                raise BusyError("Auto-MD already has two active or queued jobs")
            job_id = secrets.token_urlsafe(18)
            workspace = self.session_root / job_id
            workspace.mkdir(parents=True, exist_ok=False)
            job = Job(job_id, workspace, sources, options)
            self._jobs[job_id] = job
            return job

    def start(self, job: Job) -> None:
        job.future = self._executor.submit(self._run, job)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job:
            job.cancel_event.set()
            job.emit("progress", "cancelling", 0, 1, "Cancelling…", None)
        return job

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status in {JobStatus.QUEUED, JobStatus.RUNNING} and job.future is not None:
                job.delete_requested = True
                job.cancel_event.set()
                return True
            self._jobs.pop(job_id, None)
        shutil.rmtree(job.workspace, ignore_errors=True)
        return True

    def shutdown(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel_event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)
        shutil.rmtree(self.session_root, ignore_errors=True)

    def _run(self, job: Job) -> None:
        try:
            job.status = JobStatus.RUNNING
            job.emit("progress", "starting", 0, 1, "Starting conversion", None)
            result = Pipeline(job.workspace, job.cancel_event, job.emit).run(
                job.sources, job.options
            )
            self._apply_result(job, result)
        except JobCancelled:
            job.status = JobStatus.CANCELLED
            job.emit("cancelled", "cancelled", 1, 1, "Conversion cancelled", None)
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
            job.emit("failed", "failed", 1, 1, f"Conversion failed: {exc}", None)
        finally:
            job.updated_at = time.time()
            if job.delete_requested:
                with self._lock:
                    self._jobs.pop(job.id, None)
                shutil.rmtree(job.workspace, ignore_errors=True)

    def _apply_result(self, job: Job, result: PipelineResult) -> None:
        job.artifacts = result.artifacts
        job.document_count = len(result.documents)
        job.skipped_count = len(result.skipped)
        job.warning_count = len(result.warnings)
        job.status = (
            JobStatus.COMPLETED_WITH_WARNINGS
            if result.skipped or result.warnings
            else JobStatus.COMPLETED
        )
        job.emit(
            "complete",
            "complete",
            1,
            1,
            f"Ready — {job.document_count} documents processed",
            None,
        )


def _clear_stale_sessions(temp_root: Path, *, exclude: Path) -> None:
    cutoff = time.time() - 24 * 60 * 60
    for candidate in temp_root.glob("automd-*"):
        try:
            if candidate == exclude or not candidate.is_dir() or candidate.is_symlink():
                continue
            if candidate.stat().st_mtime < cutoff:
                shutil.rmtree(candidate, ignore_errors=True)
        except OSError:
            continue
