"""Distributed control-plane primitives — optional deployment (2.9.0, P3 #6).

Single-node freegem needs none of this: the SQLite frontier and the in-process
thread pool are the whole control plane. This module exists for the OPTIONAL
deployment — several worker processes (or hosts) draining one shared work
queue — and it is built so that the OPTIONAL part stays optional:

* The default store is one SQLite file (same durability contract as the
  frontier: every transition commits, a killed worker leaves a valid file).
* The work-queue half is any :class:`freegem.queues.WorkQueue` — the SQLite
  queue locally, the Redis queue for multi-host. This module never imports
  ``redis`` itself.
* Nothing here activates automatically. A deployment opts in by constructing
  a :class:`ControlPlane` in its own process.

What it adds over a bare queue:

* **Worker identity** — workers register (``join``) with a kind and metadata,
  heartbeat, and expire: a worker silent past ``worker_ttl_sec`` is dead.
* **Leases with expiry** — a claimed task is leased to one worker until
  ``lease_ttl_sec`` passes; long tasks renew via ``heartbeat``. ``reap()``
  returns expired leases to the queue and reports dead workers, so a crashed
  worker cannot strand work.
* **Stats** — workers alive, tasks in flight, reaped counts: the data a
  dashboard or health endpoint surfaces.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .queues import WorkQueue

__all__ = ["ControlPlane", "ControlPlaneError", "WorkerInfo"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workers(
  worker_id       TEXT PRIMARY KEY,
  kind            TEXT NOT NULL,
  metadata        TEXT NOT NULL DEFAULT '{}',
  started_at      REAL NOT NULL,
  last_heartbeat  REAL NOT NULL,
  stopped_at      REAL
);
CREATE TABLE IF NOT EXISTS leases(
  task_id       TEXT PRIMARY KEY,
  worker_id     TEXT NOT NULL,
  claimed_at    REAL NOT NULL,
  renewed_at    REAL NOT NULL
);
"""


class ControlPlaneError(Exception):
    """The plane refused an operation (unknown worker, stale lease...)."""


@dataclass
class WorkerInfo:
    worker_id: str
    kind: str
    metadata: dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    last_heartbeat: float = 0.0


class ControlPlane:
    """Worker registry + lease bookkeeping over one SQLite file + a WorkQueue."""

    def __init__(
        self,
        queue: WorkQueue,
        store_path: str,
        *,
        lease_ttl_sec: float = 120.0,
        worker_ttl_sec: float = 60.0,
    ) -> None:
        self.queue = queue
        self.lease_ttl_sec = lease_ttl_sec
        self.worker_ttl_sec = worker_ttl_sec
        self._conn = sqlite3.connect(store_path, check_same_thread=False, isolation_level=None)
        self._conn.executescript(_SCHEMA)
        self._lock = threading.Lock()
        self.reaped_tasks_total = 0
        self.reaped_workers_total = 0

    # -- worker lifecycle ------------------------------------------------------

    def join(self, kind: str, metadata: dict[str, Any] | None = None) -> str:
        worker_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO workers(worker_id, kind, metadata, started_at, last_heartbeat)"
                " VALUES(?,?,?,?,?)",
                (worker_id, kind, json.dumps(metadata or {}), now, now),
            )
        return worker_id

    def heartbeat(self, worker_id: str) -> None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE workers SET last_heartbeat = ? WHERE worker_id = ? AND stopped_at IS NULL",
                (time.time(), worker_id),
            )
            if cur.rowcount == 0:
                raise ControlPlaneError(f"unknown or stopped worker {worker_id!r}")

    def leave(self, worker_id: str) -> None:
        """Clean shutdown: releases this worker's leases back to the queue."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id FROM leases WHERE worker_id = ?", (worker_id,)
            ).fetchall()
            self._conn.execute(
                "UPDATE workers SET stopped_at = ? WHERE worker_id = ?", (time.time(), worker_id)
            )
            self._conn.execute("DELETE FROM leases WHERE worker_id = ?", (worker_id,))
        for (task_id,) in rows:
            self.queue.nack(task_id)

    def list_workers(self, *, include_stopped: bool = False) -> list[WorkerInfo]:
        sql = "SELECT worker_id, kind, metadata, started_at, last_heartbeat FROM workers"
        if not include_stopped:
            sql += " WHERE stopped_at IS NULL"
        rows = self._conn.execute(sql).fetchall()
        out = []
        for worker_id, kind, metadata, started_at, last_heartbeat in rows:
            try:
                meta = json.loads(metadata)
            except (TypeError, ValueError):
                meta = {}
            out.append(
                WorkerInfo(
                    worker_id=worker_id,
                    kind=kind,
                    metadata=meta,
                    started_at=started_at,
                    last_heartbeat=last_heartbeat,
                )
            )
        return out

    # -- work lifecycle ----------------------------------------------------------

    def claim(
        self, worker_id: str, timeout_sec: float = 0.0
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Reserve one task from the queue and lease it to this worker."""
        handle, payload = self.queue.reserve(timeout_sec=timeout_sec)
        if handle is None:
            return None, None
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO leases(task_id, worker_id, claimed_at, renewed_at)"
                " VALUES(?,?,?,?)",
                (handle, worker_id, now, now),
            )
        return handle, payload

    def renew(self, worker_id: str, task_id: str) -> None:
        """Extend one lease (long task) and the worker's own heartbeat."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE leases SET renewed_at = ? WHERE task_id = ? AND worker_id = ?",
                (time.time(), task_id, worker_id),
            )
            if cur.rowcount == 0:
                raise ControlPlaneError(
                    f"no live lease for task {task_id!r} on worker {worker_id!r} (reaped?)"
                )
        self.heartbeat(worker_id)

    def complete(self, worker_id: str, task_id: str, *, ok: bool = True) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM leases WHERE task_id = ? AND worker_id = ?", (task_id, worker_id)
            )
        if ok:
            self.queue.ack(task_id)
        else:
            self.queue.nack(task_id)

    # -- supervision ---------------------------------------------------------------

    def reap(self, *, now: float | None = None) -> dict[str, list[str]]:
        """Return expired leases to the queue; flag silent workers as stopped.

        Safe to call from any supervisor loop (or the server's metrics tick).
        """
        now_ts = now if now is not None else time.time()
        with self._lock:
            expired = self._conn.execute(
                "SELECT task_id, worker_id FROM leases WHERE renewed_at < ?",
                (now_ts - self.lease_ttl_sec,),
            ).fetchall()
            if expired:
                self._conn.executemany(
                    "DELETE FROM leases WHERE task_id = ?", [(t,) for t, _ in expired]
                )
                self.reaped_tasks_total += len(expired)
            dead = self._conn.execute(
                "SELECT worker_id FROM workers WHERE stopped_at IS NULL AND last_heartbeat < ?",
                (now_ts - self.worker_ttl_sec,),
            ).fetchall()
            if dead:
                self._conn.executemany(
                    "UPDATE workers SET stopped_at = ? WHERE worker_id = ?",
                    [(now_ts, w) for (w,) in dead],
                )
                self.reaped_workers_total += len(dead)
        for task_id, _worker_id in expired:
            self.queue.nack(task_id)  # back to the ready pool
        return {
            "requeued_tasks": [t for t, _ in expired],
            "dead_workers": [w for (w,) in dead],
        }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            workers = self._conn.execute(
                "SELECT COUNT(*) FROM workers WHERE stopped_at IS NULL"
            ).fetchone()[0]
            in_flight = self._conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
        return {
            "workers_alive": workers,
            "tasks_in_flight": in_flight,
            "queue_depth": self.queue.depth(),
            "reaped_tasks_total": self.reaped_tasks_total,
            "reaped_workers_total": self.reaped_workers_total,
        }

    def close(self) -> None:
        self._conn.close()
