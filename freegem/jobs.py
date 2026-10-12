"""Asynchronous job management, artifact storage, and execution queue.

Handles REST job submission, status tracking, result persistence, and artifact
management.
"""

from __future__ import annotations

import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Job:
    """A background job record."""

    id: str
    type: str  # "scrape", "research", "playwright", "document"
    status: str  # "queued", "processing", "completed", "failed"
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    artifacts: list[str] = field(default_factory=list)  # list of artifact IDs
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    retry_traces: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "status": self.status,
            "params": self.params,
            "result": self.result,
            "error": self.error,
            "artifacts": self.artifacts,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "retry_traces": self.retry_traces,
        }


@dataclass
class Artifact:
    """A file artifact (e.g. screenshot, PDF, trace, exported report)."""

    id: str
    job_id: str
    filename: str
    content_type: str
    file_path: str
    created_at: float = field(default_factory=time.time)


class JobManager:
    """In-memory job and artifact manager with background worker thread execution."""

    def __init__(self, artifact_dir: str | Path | None = None) -> None:
        self.jobs: dict[str, Job] = {}
        self.artifacts: dict[str, Artifact] = {}
        self._lock = threading.Lock()
        if artifact_dir is None:
            artifact_dir = Path(tempfile.gettempdir()) / "freegem_artifacts"
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)

    def create_job(self, job_type: str, params: dict[str, Any]) -> Job:
        """Create and queue a new job."""
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        job = Job(id=job_id, type=job_type, status="queued", params=params)
        with self._lock:
            self.jobs[job_id] = job
        return job

    def get_job(self, job_id: str) -> Job | None:
        """Retrieve job by ID."""
        with self._lock:
            return self.jobs.get(job_id)

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        """List recently submitted jobs."""
        with self._lock:
            sorted_jobs = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [j.to_dict() for j in sorted_jobs[:limit]]

    def update_job_status(
        self,
        job_id: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        retry_trace: dict[str, Any] | None = None,
    ) -> None:
        """Update status and record results or retry trace for a job."""
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job.status = status
            now = time.time()
            if status == "processing" and job.started_at is None:
                job.started_at = now
            elif status in ("completed", "failed"):
                job.completed_at = now
            if result is not None:
                job.result = result
            if error is not None:
                job.error = error
            if retry_trace is not None:
                job.retry_traces.append(retry_trace)

    def save_artifact(
        self, job_id: str, filename: str, content_type: str, file_path_or_bytes: str | bytes
    ) -> Artifact:
        """Save an artifact file and associate it with a job."""
        art_id = f"art-{uuid.uuid4().hex[:12]}"
        dest_path = self.artifact_dir / f"{art_id}_{filename}"

        if isinstance(file_path_or_bytes, bytes):
            with open(dest_path, "wb") as f:
                f.write(file_path_or_bytes)
        else:
            # Move or copy file
            import shutil

            shutil.copyfile(file_path_or_bytes, dest_path)

        art = Artifact(
            id=art_id,
            job_id=job_id,
            filename=filename,
            content_type=content_type,
            file_path=str(dest_path),
        )
        with self._lock:
            self.artifacts[art_id] = art
            job = self.jobs.get(job_id)
            if job and art_id not in job.artifacts:
                job.artifacts.append(art_id)
        return art

    def get_artifact(self, art_id: str) -> Artifact | None:
        """Retrieve artifact metadata and file path."""
        with self._lock:
            return self.artifacts.get(art_id)


# Global singleton job manager instance
GLOBAL_JOB_MANAGER = JobManager()
