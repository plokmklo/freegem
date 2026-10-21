"""Asynchronous job management, artifact storage, and bounded execution.

The job subsystem has three parts:

- :class:`JobManager` — job + artifact records. Optionally write-through
  durable via a :class:`SqliteJobStore` (status survives restarts; jobs that
  were queued/processing when the process died come back ``interrupted``).
- :class:`JobRunner` — the EXECUTION engine. Replaces the original
  unbounded thread-per-job model: a fixed worker pool pulls from a BOUNDED
  queue (saturation raises :class:`JobQueueFull`, surfaced by the REST face
  as HTTP 429), jobs carry timeouts (watchdog marks ``timed_out``),
  cancellation is cooperative, failures retry with capped attempts and
  exponential backoff, terminal jobs are evicted past the retention window,
  and an optional external queue (``memory://``, ``sqlite://``,
  ``redis://``, ``amqp://`` via :func:`freegem.queues.open_queue`) decouples
  submission from execution.
- :func:`execute_job` — the body dispatcher shared by the REST face and the
  MCP face.

Execution limits are a promise, same as the crawler's ResourceLimits: the
server cannot spawn unbounded threads (fixed pool), cannot buffer an
unbounded backlog (bounded queue + 429), and cannot lose track of what it
already did (durable store).
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

#: Terminal and non-terminal job statuses. ``cancelled`` (worker or queue
#: honored a cancel request), ``timed_out`` (watchdog fired; the underlying
#: call may still be draining -- threads cannot be killed, but the result is
#: not trusted), ``interrupted`` (process restarted mid-job; only from the
#: durable store).
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "timed_out", "interrupted"})


@dataclass
class Job:
    """A background job record."""

    id: str
    type: str  # "scrape", "research", "playwright", "document", "extract", "selenium", "agent"
    status: str  # queued | processing | completed | failed | cancelled | timed_out | interrupted
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None
    artifacts: list[str] = field(default_factory=list)  # list of artifact IDs
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    retry_traces: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0  # execution attempts so far (1 = first run)

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
            "attempts": self.attempts,
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


class JobQueueFull(Exception):
    """The bounded pending queue is at capacity (HTTP face maps to 429)."""


# ---------------------------------------------------------------------------
# Durable stores
# ---------------------------------------------------------------------------


class MemoryJobStore:
    """No persistence -- the pre-2.9 behavior (state lives in the process)."""

    def upsert(self, job: Job) -> None:
        return None

    def load_all(self) -> list[Job]:
        return []

    def delete(self, job_id: str) -> None:
        return None

    def mark_interrupted(self) -> int:
        return 0

    def close(self) -> None:
        return None


class SqliteJobStore:
    """Write-through SQLite persistence for job status.

    One row per job, upserted on every transition. On open, jobs still
    ``queued``/``processing`` from a previous process are marked
    ``interrupted`` -- restart recovery: nothing is ever silently lost, and
    nothing stale is silently re-run.
    """

    def __init__(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(target)
        self._write_lock = threading.Lock()
        self._conn = self._open(self.path)
        self.mark_interrupted()

    @staticmethod
    def _open(path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                status TEXT NOT NULL,
                params TEXT NOT NULL DEFAULT '{}',
                result TEXT,
                error TEXT,
                artifacts TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                started_at REAL,
                completed_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                retry_traces TEXT NOT NULL DEFAULT '[]'
            )
            """
        )
        conn.commit()
        return conn

    def _reconnect(self) -> None:
        """Replace a connection that ended up unusable (Windows file locks)."""

        with contextlib.suppress(Exception):
            self._conn.close()
        self._conn = self._open(self.path)

    def upsert(self, job: Job) -> None:
        """Write-through mirror. Persistence NEVER blocks job execution:
        a connection that goes bad (Windows file-lock state, mid-write
        damage) gets one reconnect+retry; if that fails too, the write is
        dropped -- the in-memory manager remains the source of truth.
        """
        try:
            with self._write_lock:
                self._upsert_now(job)
        except sqlite3.Error:
            try:
                with self._write_lock:
                    self._reconnect()
                    self._upsert_now(job)
            except sqlite3.Error:
                pass

    def _upsert_now(self, job: Job) -> None:
        self._conn.execute(
            """
            INSERT INTO jobs (id, type, status, params, result, error, artifacts,
                              created_at, started_at, completed_at, attempts, retry_traces)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, result=excluded.result, error=excluded.error,
                artifacts=excluded.artifacts, started_at=excluded.started_at,
                completed_at=excluded.completed_at, attempts=excluded.attempts,
                retry_traces=excluded.retry_traces
            """,
            (
                job.id,
                job.type,
                job.status,
                json.dumps(job.params),
                json.dumps(job.result) if job.result is not None else None,
                job.error,
                json.dumps(job.artifacts),
                job.created_at,
                job.started_at,
                job.completed_at,
                job.attempts,
                json.dumps(job.retry_traces),
            ),
        )
        self._conn.commit()

    def load_all(self) -> list[Job]:
        try:
            rows = self._conn.execute(
                "SELECT id, type, status, params, result, error, artifacts, created_at,"
                " started_at, completed_at, attempts, retry_traces FROM jobs"
            ).fetchall()
        except sqlite3.Error:
            return []  # unreadable store: in-memory history still works
        jobs: list[Job] = []
        for row in rows:
            try:
                jobs.append(
                    Job(
                        id=row[0],
                        type=row[1],
                        status=row[2],
                        params=json.loads(row[3]),
                        result=json.loads(row[4]) if row[4] is not None else None,
                        error=row[5],
                        artifacts=json.loads(row[6]),
                        created_at=row[7],
                        started_at=row[8],
                        completed_at=row[9],
                        attempts=row[10],
                        retry_traces=json.loads(row[11]),
                    )
                )
            except (ValueError, TypeError):
                continue  # a corrupt row must not break startup
        return jobs

    def delete(self, job_id: str) -> None:
        try:
            with self._write_lock:
                self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
                self._conn.commit()
        except sqlite3.Error:
            pass

    def mark_interrupted(self) -> int:
        try:
            with self._write_lock:
                cur = self._conn.execute(
                    "UPDATE jobs SET status = 'interrupted',"
                    " error = 'interrupted: the server restarted before this job finished',"
                    " completed_at = ?"
                    " WHERE status IN ('queued', 'processing')",
                    (time.time(),),
                )
                self._conn.commit()
                return cur.rowcount
        except sqlite3.Error:
            return 0

    def close(self) -> None:
        with_id = getattr(self._conn, "close", None)
        if with_id is not None:
            with_id()


class JobManager:
    """In-memory job and artifact manager, optionally durable via a store."""

    def __init__(
        self,
        artifact_dir: str | Path | None = None,
        *,
        store: MemoryJobStore | SqliteJobStore | None = None,
    ) -> None:
        self.jobs: dict[str, Job] = {}
        self.artifacts: dict[str, Artifact] = {}
        self._lock = threading.Lock()
        self._store = store if store is not None else MemoryJobStore()
        if artifact_dir is None:
            artifact_dir = Path(tempfile.gettempdir()) / "freegem_artifacts"
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        # Restart recovery: durable rows land back in memory (interrupted
        # ones already relabeled by the store on open).
        for job in self._store.load_all():
            self.jobs[job.id] = job
        self._sweep_orphan_artifact_files()

    def _sweep_orphan_artifact_files(self) -> None:
        """Startup sweep: unlink artifact files whose artifact id is no
        longer known to any job (issue #39 -- evictions leaked the files
        before the purge fix, and a crash can orphan a mid-write file).
        Files referenced by a restored durable job are kept; anything that
        is not a JobManager artifact (no ``art-`` id prefix) is never
        touched. Best-effort by design."""
        referenced = {aid for job in self.jobs.values() for aid in job.artifacts}
        referenced.update(self.artifacts)
        try:
            entries = list(self.artifact_dir.iterdir())
        except OSError:
            return
        for path in entries:
            name = path.name
            if not name.startswith("art-"):
                continue
            if name.split("_", 1)[0] in referenced:
                continue
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                continue

    def attach_store(self, store: MemoryJobStore | SqliteJobStore) -> None:
        """Make this manager durable (once): merge stored rows into memory."""
        with self._lock:
            if isinstance(self._store, SqliteJobStore):
                return  # already durable
            self._store = store
            for job in store.load_all():
                self.jobs.setdefault(job.id, job)

    def create_job(self, job_type: str, params: dict[str, Any]) -> Job:
        """Create and queue a new job."""
        job_id = f"job-{uuid.uuid4().hex[:12]}"
        job = Job(id=job_id, type=job_type, status="queued", params=params)
        with self._lock:
            self.jobs[job_id] = job
        self._store.upsert(job)
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
    ) -> bool:
        """Update status and record results or retry trace for a job.

        Returns True when the write was applied. Terminal states are final:
        once a job is completed/failed/cancelled/timed_out/interrupted, later
        updates are ignored -- except an idempotent re-write of the same
        state. Without this guard, a body that keeps draining after the
        watchdog stamped ``timed_out`` would overwrite it with ``completed``
        and deliver a result nobody trusts (issue #26a).
        """
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                return False
            if job.status in TERMINAL_STATUSES and status != job.status:
                return False
            job.status = status
            now = time.time()
            if status == "processing" and job.started_at is None:
                job.started_at = now
            elif status in TERMINAL_STATUSES:
                job.completed_at = now
            if result is not None:
                job.result = result
            if error is not None:
                job.error = error
            if retry_trace is not None:
                job.retry_traces.append(retry_trace)
            self._store.upsert(job)
            return True

    def stamp_timed_out(
        self,
        job_id: str,
        *,
        error: str,
        retry_trace: dict[str, Any] | None = None,
    ) -> bool:
        """Watchdog-only: stamp ``timed_out`` and drop any result in ONE
        locked step, refusing jobs that already reached a terminal state.

        The atomicity is the point (#26b): a body finishing exactly at the
        deadline could otherwise slip its result in between the status stamp
        and a separate result clear, leaving a ``completed`` job without its
        result -- or, pre-guard, have its untrusted output overwrite the
        timeout. Returns True when the stamp was applied.
        """
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None or job.status in TERMINAL_STATUSES:
                return False
            job.status = "timed_out"
            job.completed_at = time.time()
            job.error = error
            job.result = None  # the call may still be draining: not trusted
            if retry_trace is not None:
                job.retry_traces.append(retry_trace)
            self._store.upsert(job)
            return True

    def job_status(self, job_id: str) -> str | None:
        """Read a job's status under the manager lock (a consistent read)."""
        with self._lock:
            job = self.jobs.get(job_id)
            return job.status if job is not None else None

    def mark_cancelled(
        self,
        job_id: str,
        *,
        error: str,
        retry_trace: dict[str, Any] | None = None,
        override: bool = False,
    ) -> bool:
        """Stamp ``cancelled`` and drop any result in one locked step.

        Without ``override`` this is terminal-guarded like every status
        write (#26c): a cancel stamp that lands after the job already
        finished must not flip it to ``cancelled`` while its result
        survives. With ``override=True`` -- used only by the runner when it
        observed the cancel flag set while THIS run was executing -- a
        completion that raced the cancel is overridden (the documented
        contract: the runner never delivers a result for a cancelled job);
        a watchdog ``timed_out`` state still wins in that case.
        """
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            if override:
                if job.status == "timed_out":
                    return False
            elif job.status in TERMINAL_STATUSES:
                return False
            job.status = "cancelled"
            job.completed_at = time.time()
            job.error = error
            job.result = None
            if retry_trace is not None:
                job.retry_traces.append(retry_trace)
            self._store.upsert(job)
            return True

    def bump_attempts(self, job_id: str) -> int:
        """Count one execution attempt; returns the new attempt number."""
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                return 0
            job.attempts += 1
            self._store.upsert(job)
            return job.attempts

    def clear_result(self, job_id: str) -> None:
        """Drop a stored result (cancellation semantics: untrusted output)."""
        with self._lock:
            job = self.jobs.get(job_id)
            if job:
                job.result = None
                self._store.upsert(job)

    def enforce_retention(self, keep: int) -> int:
        """Evict the oldest TERMINAL jobs beyond ``keep``. Returns evicted count.

        Eviction is whole-object (#39, same as duplicate #32): the job's
        artifact records and their files on disk go with it, not just the
        job row -- otherwise freegem_artifacts/ grows without bound no
        matter how small the job registry is kept.
        """
        if keep <= 0:
            return 0
        with self._lock:
            terminal = [
                j
                for j in self.jobs.values()
                if j.status in TERMINAL_STATUSES and j.completed_at is not None
            ]
            terminal.sort(key=lambda j: j.completed_at or 0.0)
            overflow = len(terminal) - keep
            evicted = 0
            for job in terminal[: max(0, overflow)]:
                self._purge_artifacts_locked(job.id)
                self.jobs.pop(job.id, None)
                self._store.delete(job.id)
                evicted += 1
            return evicted

    def _purge_artifacts_locked(self, job_id: str) -> None:
        """Drop a job's artifact records and unlink their files.

        Caller must hold ``self._lock``. File removal is best-effort: a
        missing file is fine (already swept), an unreadable one must not
        block the eviction of the registry entries."""
        doomed = [aid for aid, art in self.artifacts.items() if art.job_id == job_id]
        for aid in doomed:
            art = self.artifacts.pop(aid, None)
            if art is None:
                continue
            with contextlib.suppress(OSError):
                os.unlink(art.file_path)

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
                job.artifacts.append(art.id)
                self._store.upsert(job)
        return art

    def get_artifact(self, art_id: str) -> Artifact | None:
        """Retrieve artifact metadata and file path."""
        with self._lock:
            return self.artifacts.get(art_id)


# Global singleton job manager instance
GLOBAL_JOB_MANAGER = JobManager()


# ---------------------------------------------------------------------------
# Bounded execution engine
# ---------------------------------------------------------------------------


class JobRunner:
    """Bounded worker pool + bounded queue for background jobs.

    Guarantees (the whole point, versus the original thread-per-job model):

    - **Fixed pool**: exactly ``max_workers`` execution threads -- a burst of
      submissions cannot spawn unbounded threads.
    - **Bounded queue**: at most ``max_pending`` queued jobs; beyond that
      :meth:`submit` raises :class:`JobQueueFull` (REST face: HTTP 429) --
      backpressure is visible instead of silent memory growth.
    - **Timeouts**: every running job has a deadline (per-job
      ``params["timeout_sec"]`` override, clamped to 1..3600). A watchdog
      thread marks exceeded jobs ``timed_out``. Threads cannot be killed in
      Python, so the underlying call may still drain; the result is not
      trusted and the slot frees when it returns.
    - **Cancellation**: cooperative. Queued jobs are cancelled outright;
      running jobs get a cancel flag -- the runner never delivers a result
      for a cancelled job (result is dropped, status ``cancelled``).
    - **Retry limits**: a failed job re-queues with exponential backoff
      until ``max_retries`` (per-job ``params["max_retries"]`` override,
      clamped 0..5; default ``default_max_retries``). Retry traces land in
      the job record. ``timed_out`` and ``cancelled`` jobs never retry.
    - **Retention**: terminal jobs beyond ``retention`` are evicted (oldest
      first) from memory and the durable store.
    - **Optional external queue**: with ``queue_url`` (``memory://``,
      ``sqlite://``, ``redis://``, ``amqp://``), submissions go through the
      external queue and a dispatcher feeds the bounded internal queue --
      other processes can submit work to the same queue.
    """

    def __init__(
        self,
        manager: JobManager,
        body: Callable[[Job], None],
        *,
        max_workers: int = 4,
        max_pending: int = 100,
        job_timeout_sec: float = 600.0,
        default_max_retries: int = 2,
        retention: int = 500,
        queue_url: str | None = None,
    ) -> None:
        self.manager = manager
        self._body = body
        self.max_workers = max(1, int(max_workers))
        self.max_pending = max(1, int(max_pending))
        self.job_timeout_sec = max(1.0, float(job_timeout_sec))
        self.default_max_retries = max(0, min(5, int(default_max_retries)))
        self.retention = max(1, int(retention))
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=self.max_pending)
        self._lock = threading.Lock()
        self._running: dict[str, float] = {}  # job_id -> monotonic deadline
        self._cancel_requested: set[str] = set()
        self._stop = threading.Event()
        self._external: Any = None
        self._threads: list[threading.Thread] = []
        for index in range(self.max_workers):
            t = threading.Thread(target=self._worker_loop, daemon=True, name=f"freegem-job-{index}")
            t.start()
            self._threads.append(t)
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="freegem-job-watchdog"
        )
        self._watchdog.start()
        if queue_url:
            from .queues import open_queue

            self._external = open_queue(queue_url)
            t = threading.Thread(
                target=self._external_loop, daemon=True, name="freegem-job-external"
            )
            t.start()
            self._threads.append(t)

    # -- submission ---------------------------------------------------------

    def submit(self, job_type: str, params: dict[str, Any]) -> Job:
        """Create a job and enqueue it for execution (bounded)."""
        job = self.manager.create_job(job_type, params)
        if self._external is not None:
            try:
                self._external.enqueue({"id": job.id, "type": job.type, "params": job.params})
            except Exception as exc:
                self.manager.update_job_status(job.id, "failed", error=f"queue unavailable: {exc}")
                raise JobQueueFull(job.id) from None
            return job
        return self._enqueue_local(job)

    def _enqueue_local(self, job: Job) -> Job:
        try:
            self._queue.put_nowait(job.id)
        except queue.Full:
            self.manager.update_job_status(
                job.id, "failed", error="job queue is full; retry later (bounded backlog)"
            )
            raise JobQueueFull(job.id) from None
        return job

    def cancel(self, job_id: str) -> bool:
        """Request cancellation. Queued: immediate. Running: cooperative.

        The status is read under the manager's lock (job_status), and the
        stamp itself is terminal-guarded -- so a job that completes while the
        cancel request is in flight keeps its terminal status and result.
        Pre-fix (#26c), the unlocked ``job.status`` read let a cancel racing
        a completion flip the finished job back to ``cancelled`` while its
        result survived, an incoherent state.
        """
        status = self.manager.job_status(job_id)
        if status is None or status in TERMINAL_STATUSES:
            return False
        with self._lock:
            self._cancel_requested.add(job_id)
        if status == "queued":
            # May lose to a worker that picked the job up first (the stamp
            # is terminal-guarded); the flag set above still owns the
            # outcome via the cooperative path.
            self.manager.mark_cancelled(job_id, error="cancelled before start")
        return True

    def stats(self) -> dict[str, Any]:
        """Operational snapshot for /metrics (no job contents)."""
        with self._lock:
            running = len(self._running)
        return {
            "workers": self.max_workers,
            "pending": self._queue.qsize(),
            "running": running,
            "max_pending": self.max_pending,
            "external_queue": self._external is not None,
        }

    def close(self) -> None:
        """Stop the loops (used on server shutdown and in tests)."""
        self._stop.set()
        for _ in self._threads:
            try:
                self._queue.put_nowait("__stop__")
            except queue.Full:
                break

    # -- worker side --------------------------------------------------------

    def _timeout_for(self, job: Job) -> float:
        raw = job.params.get("timeout_sec")
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return self.job_timeout_sec
        return max(1.0, min(3600.0, value))

    def _max_retries_for(self, job: Job) -> int:
        raw = job.params.get("max_retries")
        try:
            value = int(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return self.default_max_retries
        return max(0, min(5, value))

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if job_id is None or job_id == "__stop__":
                break
            job = self.manager.get_job(job_id)
            if job is None or job.status != "queued":
                continue  # cancelled/evicted while queued
            self._run_one(job)

    def _run_one(self, job: Job) -> None:
        with self._lock:
            if job.id in self._cancel_requested:
                self._cancel_requested.discard(job.id)
                self.manager.update_job_status(job.id, "cancelled", error="cancelled before start")
                return
            self._running[job.id] = time.monotonic() + self._timeout_for(job)
        self.manager.update_job_status(job.id, "processing")
        self.manager.bump_attempts(job.id)
        failure: Exception | None = None
        try:
            self._body(job)
        except Exception as exc:
            failure = exc
        finally:
            with self._lock:
                self._running.pop(job.id, None)
                cancelled = job.id in self._cancel_requested
                self._cancel_requested.discard(job.id)
        # The job is no longer registered as running: the watchdog owns no
        # deadline for it from here on. Failure handling -- including the
        # retry backoff sleep -- therefore happens AFTER the pop (#28): the
        # old code slept up to 30s inside the except, still holding the
        # slot, so the watchdog could mark a sleeping job ``timed_out`` and
        # the retry then resurrected it -- and the sleeping worker stalled
        # the pool either way.
        if failure is not None:
            self._handle_failure(job, failure, cancelled=cancelled)
            self.manager.enforce_retention(self.retention)
            return
        if cancelled:
            # Cooperative cancel landed while the body ran: the worker's
            # completion is overridden -- cancelled results are not trusted.
            # The override is atomic and refuses a watchdog ``timed_out``;
            # a completion that beat the cancel flag keeps its result.
            self.manager.mark_cancelled(job.id, error="cancelled during run", override=True)
        self.manager.enforce_retention(self.retention)

    def _handle_failure(self, job: Job, exc: Exception, *, cancelled: bool = False) -> None:
        trace = {"timestamp": time.time(), "attempt": job.attempts, "error": str(exc)}
        current = self.manager.get_job(job.id)
        if current is None:
            return
        if cancelled:
            # The cancel flag was set while this run executed: cancelled wins
            # over the failure (result dropped, atomic), but a watchdog
            # ``timed_out`` stamp is not downgraded.
            self.manager.mark_cancelled(
                job.id, error="cancelled during run", retry_trace=trace, override=True
            )
            return
        if current.status == "timed_out":
            # The watchdog owns this job now; the body's exception is just
            # the drain. Never retry a timed-out job automatically.
            return
        if current.attempts <= self._max_retries_for(current):
            delay = min(30.0, 2.0 ** (current.attempts - 1))
            # The re-queue stamp is terminal-guarded (#26a): if a cancel or a
            # timeout won the race for this job, the write is refused and the
            # job is NOT resurrected out of a terminal state.
            if not self.manager.update_job_status(
                job.id,
                "queued",
                error=f"attempt {current.attempts} failed: {exc}; retrying in {delay:.0f}s",
                retry_trace=trace,
            ):
                return
            # Backoff happens with the job already popped from _running (the
            # caller's finally ran before this method), so the watchdog
            # cannot stamp the sleeper mid-backoff (#28) and the pool slot is
            # free while we wait.
            time.sleep(delay)
            if self._stop.is_set():
                return
            try:
                self._queue.put_nowait(job.id)
            except queue.Full:
                self.manager.update_job_status(
                    job.id, "failed", error="retry dropped: job queue is full"
                )
        else:
            self.manager.update_job_status(job.id, "failed", error=str(exc), retry_trace=trace)

    def _watchdog_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(0.5)
            self._reap_expired()

    def _reap_expired(self) -> None:
        """One watchdog sweep: stamp deadline-exceeded jobs still registered
        as running. Extracted from the loop so tests can drive a sweep
        deterministically."""
        now = time.monotonic()
        with self._lock:
            expired = [jid for jid, deadline in self._running.items() if now >= deadline]
        for job_id in expired:
            with self._lock:
                # TOCTOU guard (#26b): the snapshot above was taken before we
                # held the lock; the job may have finished and been popped by
                # its worker's finally between the snapshot and now. Only a
                # job still registered as running may be reaped.
                if job_id not in self._running:
                    continue
                self._running.pop(job_id, None)
                self._cancel_requested.discard(job_id)
            job = self.manager.get_job(job_id)
            limit = self._timeout_for(job) if job else self.job_timeout_sec
            # stamp_timed_out is atomic (stamp + result drop) and refuses a
            # job that went terminal between the re-check and the stamp, so
            # the sweep can never overwrite a completed job's result.
            self.manager.stamp_timed_out(
                job_id,
                error=f"job exceeded {limit:.0f}s execution timeout",
                retry_trace={"timestamp": time.time(), "error": "execution timeout"},
            )
            self.manager.enforce_retention(self.retention)

    def _external_loop(self) -> None:
        """Consume payloads from the external queue into the bounded queue."""
        while not self._stop.is_set():
            try:
                handle, payload = self._external.reserve(timeout_sec=1.0)
            except Exception:
                time.sleep(0.5)
                continue
            if handle is None or not isinstance(payload, dict):
                continue
            job_id = str(payload.get("id", ""))
            job = self.manager.get_job(job_id)
            if job is None or job.status != "queued":
                # Unknown or already-terminal (duplicate delivery): ack it away.
                with contextlib.suppress(Exception):
                    self._external.ack(handle)
                continue
            try:
                self._queue.put_nowait(job.id)
                self._external.ack(handle)
            except queue.Full:
                with contextlib.suppress(Exception):
                    self._external.nack(handle)  # redelivery when there is room
                time.sleep(0.2)


# ---------------------------------------------------------------------------
# Shared job body + global wiring
# ---------------------------------------------------------------------------


def execute_job(job: Job, cfg: Any) -> None:
    """Run one job to completion (the shared body for REST and MCP faces).

    Sets the terminal status itself; unexpected exceptions propagate to the
    runner, which owns retry/failure handling.
    """
    from .metrics import record_audit_event

    client_ip = "job-runner"
    req_id = job.id
    record_audit_event("job_started", client_ip, req_id, {"job_id": job.id, "type": job.type})

    if job.type == "scrape":
        from .web_scraper import scrape_page

        url = str(job.params.get("url", ""))
        scraped = scrape_page(cfg, url)
        GLOBAL_JOB_MANAGER.update_job_status(
            job.id,
            "completed",
            result={
                "url": scraped.url,
                "title": scraped.title,
                "text": scraped.text,
                "links": scraped.links,
            },
        )
    elif job.type == "research":
        import json as _json

        from .web_research import render_json, research

        topic = str(job.params.get("topic", ""))
        urls = [str(u) for u in job.params.get("urls", [])]
        if not topic and not urls:
            GLOBAL_JOB_MANAGER.update_job_status(
                job.id, "failed", error="topic or urls is required"
            )
            return
        researched = research(cfg, topic or urls[0], urls)
        payload = _json.loads(render_json(researched))
        GLOBAL_JOB_MANAGER.update_job_status(job.id, "completed", result=payload)
    elif job.type == "playwright":
        from .playwright_fetcher import PlaywrightOptions, fetch_with_playwright

        # Filter like the selenium branch below: params also carry the
        # runner-managed keys (timeout_sec, max_retries -- documented per-job
        # overrides), and PlaywrightOptions(**job.params) used to TypeError
        # on them, failing the job after pointless retries.
        allowed = {f.name for f in fields(PlaywrightOptions)}
        opts = PlaywrightOptions(**{k: v for k, v in job.params.items() if k in allowed})
        pw_res = fetch_with_playwright(opts)
        if pw_res.screenshot_path and os.path.exists(pw_res.screenshot_path):
            GLOBAL_JOB_MANAGER.save_artifact(
                job.id, "screenshot.png", "image/png", pw_res.screenshot_path
            )
        if pw_res.pdf_path and os.path.exists(pw_res.pdf_path):
            GLOBAL_JOB_MANAGER.save_artifact(
                job.id, "document.pdf", "application/pdf", pw_res.pdf_path
            )
        if pw_res.trace_path and os.path.exists(pw_res.trace_path):
            GLOBAL_JOB_MANAGER.save_artifact(
                job.id, "trace.zip", "application/zip", pw_res.trace_path
            )
        GLOBAL_JOB_MANAGER.update_job_status(
            job.id,
            "completed" if pw_res.ok else "failed",
            result={
                "url": pw_res.url,
                "final_url": pw_res.final_url,
                "title": pw_res.title,
                "text": pw_res.text,
                "status": pw_res.status,
            },
            error=pw_res.error,
        )
    elif job.type == "document":
        from .adapters import parse_document

        doc_data = (
            job.params.get("data", "").encode("utf-8")
            if isinstance(job.params.get("data"), str)
            else job.params.get("data", b"")
        )
        filename = job.params.get("filename", "")
        doc_res = parse_document(doc_data, filename)
        GLOBAL_JOB_MANAGER.update_job_status(
            job.id,
            "completed",
            result={
                "text": doc_res.text,
                "markdown": doc_res.markdown,
                "metadata": doc_res.metadata,
            },
        )
    elif job.type == "extract":
        # Schema-constrained LLM extraction (2.8.0): optional text_pipeline
        # pre-selection keeps the prompt small.
        from .llm_extract import BridgeProvider, ExtractionSchema, FieldSpec, extract_structured

        specs = []
        for f in job.params.get("fields", []):
            specs.append(
                FieldSpec(
                    name=str(f.get("name", "")),
                    type=str(f.get("type", "string")),
                    description=str(f.get("description", "")),
                    required=bool(f.get("required", True)),
                    enum=tuple(str(x) for x in f.get("enum", ())),
                    items_type=str(f.get("items_type", "string")),
                )
            )
        if not specs:
            GLOBAL_JOB_MANAGER.update_job_status(job.id, "failed", error="fields[] is required")
            return
        schema = ExtractionSchema(
            name=str(job.params.get("schema_name", "extraction")), fields=tuple(specs)
        )
        text = str(job.params.get("text", ""))
        chunk_query = str(job.params.get("select_query", "") or "")
        if chunk_query and len(text) > 50_000:
            from .text_pipeline import chunk_text, select_relevant

            chunks = chunk_text(text, max_chars=8_000)
            picked = select_relevant(chunks, chunk_query, k=4)
            text = "\n\n".join(c.text for c in picked) if picked else text[:50_000]
        clean, problems = extract_structured(
            schema,
            text,
            BridgeProvider(cfg),
            max_repairs=int(job.params.get("max_repairs", 1)),
        )
        GLOBAL_JOB_MANAGER.update_job_status(
            job.id, "completed", result={"data": clean, "problems": problems}
        )
    elif job.type == "selenium":
        from .selenium_fetcher import SeleniumOptions, fetch_with_selenium

        allowed = {
            "url",
            "grid_url",
            "wait_selector",
            "implicit_wait_sec",
            "page_load_timeout_sec",
            "viewport_width",
            "viewport_height",
            "user_agent",
            "storage_state",
            "capture_screenshot",
            "screenshot_path",
            "capture_pdf",
            "pdf_path",
        }
        sel_opts = SeleniumOptions(**{k: v for k, v in job.params.items() if k in allowed})
        sel_res = fetch_with_selenium(sel_opts)
        art_ids = []
        if sel_res.screenshot_path and os.path.exists(sel_res.screenshot_path):
            art = GLOBAL_JOB_MANAGER.save_artifact(
                job.id, "screenshot.png", "image/png", sel_res.screenshot_path
            )
            art_ids.append(art.id)
        if sel_res.pdf_path and os.path.exists(sel_res.pdf_path):
            art = GLOBAL_JOB_MANAGER.save_artifact(
                job.id, "document.pdf", "application/pdf", sel_res.pdf_path
            )
            art_ids.append(art.id)
        GLOBAL_JOB_MANAGER.update_job_status(
            job.id,
            "completed" if sel_res.ok else "failed",
            result={
                "url": sel_res.url,
                "final_url": sel_res.final_url,
                "title": sel_res.title,
                "text": sel_res.text,
                "artifact_ids": art_ids,
            },
            error=sel_res.error,
        )
    elif job.type == "agent":
        from .agent import run_agent, validate_agent_params

        try:
            task, limits = validate_agent_params(job.params)
        except ValueError as exc:
            GLOBAL_JOB_MANAGER.update_job_status(job.id, "failed", error=str(exc))
            return

        result = run_agent(cfg, task, limits)
        GLOBAL_JOB_MANAGER.update_job_status(
            job.id,
            result.status,
            result=result.to_dict() if result.status == "completed" else None,
            error=None if result.status == "completed" else "; ".join(result.limitations),
        )
    else:
        GLOBAL_JOB_MANAGER.update_job_status(
            job.id, "failed", error=f"Unknown job type {job.type!r}"
        )

    record_audit_event("job_finished", client_ip, req_id, {"job_id": job.id})


_GLOBAL_RUNNER: JobRunner | None = None
_RUNNER_LOCK = threading.Lock()

#: Default durable store location (overridable via ``job_db`` config; the
#: special value ``memory`` disables persistence).
DEFAULT_JOB_DB = Path.home() / ".config" / "freegem" / "jobs.sqlite3"


#: One live SqliteJobStore per resolved path, process-wide. Two connections
#: to the same store file racing their init writes (mark_interrupted) hit
#: Windows file-lock conflicts (sqlite3.InterfaceError: bad parameter or
#: other API misuse); caching keeps exactly one connection per path.
_STORE_CACHE: dict[str, MemoryJobStore | SqliteJobStore] = {}
_STORE_CACHE_LOCK = threading.Lock()


def _store_from_config(cfg: Any) -> MemoryJobStore | SqliteJobStore:
    job_db = getattr(cfg, "job_db", None)
    if job_db and str(job_db).strip() and str(job_db) != "memory":
        path = str(job_db)
    elif job_db == "memory":
        return MemoryJobStore()
    else:
        path = str(DEFAULT_JOB_DB)
    with _STORE_CACHE_LOCK:
        cached = _STORE_CACHE.get(path)
        if cached is None:
            try:
                cached = SqliteJobStore(path)
            except Exception:
                return MemoryJobStore()  # an unwritable HOME must not kill the server
            _STORE_CACHE[path] = cached
        return cached


def configure_job_runner(cfg: Any, *, manager: JobManager | None = None) -> JobRunner:
    """Build the global runner once (idempotent) from config defaults."""
    global _GLOBAL_RUNNER
    with _RUNNER_LOCK:
        if _GLOBAL_RUNNER is None:
            mgr = manager if manager is not None else GLOBAL_JOB_MANAGER
            store = _store_from_config(cfg)
            if isinstance(store, SqliteJobStore):
                mgr.attach_store(store)
            _GLOBAL_RUNNER = JobRunner(
                mgr,
                body=lambda job: execute_job(job, cfg),
                max_workers=int(getattr(cfg, "job_max_workers", 4)),
                max_pending=int(getattr(cfg, "job_max_pending", 100)),
                job_timeout_sec=float(getattr(cfg, "job_timeout_sec", 600.0)),
                retention=int(getattr(cfg, "job_retention", 500)),
                queue_url=getattr(cfg, "job_queue_url", None) or None,
            )
    return _GLOBAL_RUNNER


def reset_job_runner_for_tests() -> None:
    """Test hook: drop the global runner so the next configure rebuilds it."""
    global _GLOBAL_RUNNER
    with _RUNNER_LOCK:
        if _GLOBAL_RUNNER is not None:
            _GLOBAL_RUNNER.close()
        _GLOBAL_RUNNER = None


def job_runner() -> JobRunner | None:
    """The configured global runner, if any (MCP face reads this)."""
    return _GLOBAL_RUNNER
