"""Queue adapters: the same work queue face over memory, SQLite, Redis, AMQP.

The crawl/extract pipeline runs in-process today; at production scale the
work needs to move to a broker shared by many workers. The adapter contract
is deliberately tiny — the whole surface a worker needs:

* ``enqueue(payload)`` — hand over one JSON-serializable dict;
* ``reserve(timeout_sec)`` — block for one item, return a ``(handle, payload)``
  or ``(None, None)`` on timeout; the item stays UNACKNOWLEDGED until
  ``ack``/``nack`` decides its fate;
* ``ack(handle)`` / ``nack(handle)`` — success or requeue;
* ``depth()`` — how much work is waiting (visible + unacked).

Implementations:
* :class:`MemoryQueue` — stdlib, for tests and single-process runs;
* :class:`SqliteQueue` — durable, zero-dependency; survives crashes with
  the same one-transaction-per-change contract as the frontier;
* :class:`RedisQueue` / :class:`RabbitMQQueue` — optional deps (``redis``,
  ``pika``), imported lazily; constructing one without the package raises
  :class:`QueueUnavailable` with the pip line, never an ImportError stack.

All payloads go through ``json.dumps`` at the boundary so a payload written
by one implementation reads identically from another.
"""

from __future__ import annotations

import contextlib
import json
import math
import queue
import sqlite3
import threading
import time
import uuid
from typing import Any, Protocol

__all__ = [
    "MemoryQueue",
    "QueueError",
    "QueueUnavailable",
    "RabbitMQQueue",
    "RedisQueue",
    "SqliteQueue",
    "WorkQueue",
    "open_queue",
]


class QueueError(Exception):
    """Queue operation failed."""


class QueueUnavailable(QueueError):
    """The optional client package for this broker is not installed."""


class WorkQueue(Protocol):
    def enqueue(self, payload: dict[str, Any]) -> str: ...
    def reserve(self, timeout_sec: float = 5.0) -> tuple[str | None, dict[str, Any] | None]: ...
    def ack(self, handle: str) -> None: ...
    def nack(self, handle: str) -> None: ...
    def depth(self) -> int: ...
    def close(self) -> None: ...


class MemoryQueue:
    """Thread-safe in-memory queue with unacked redelivery."""

    def __init__(self) -> None:
        self._q: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._unacked: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def enqueue(self, payload: dict[str, Any]) -> str:
        task_id = uuid.uuid4().hex[:12]
        self._q.put((task_id, payload))
        return task_id

    def reserve(self, timeout_sec: float = 5.0) -> tuple[str | None, dict[str, Any] | None]:
        try:
            task_id, payload = self._q.get(timeout=max(0.0, timeout_sec))
        except queue.Empty:
            return None, None
        with self._lock:
            self._unacked[task_id] = payload
        return task_id, payload

    def ack(self, handle: str) -> None:
        with self._lock:
            self._unacked.pop(handle, None)

    def nack(self, handle: str) -> None:
        with self._lock:
            payload = self._unacked.pop(handle, None)
        if payload is not None:
            self._q.put((handle, payload))

    def depth(self) -> int:
        with self._lock:
            unacked = len(self._unacked)
        # qsize() is read outside the lock (queue.Queue is internally safe);
        # only the unacked snapshot needs ours, so the sum stays consistent
        # with a concurrent reserve/ack instead of a torn read.
        return self._q.qsize() + unacked

    def close(self) -> None:
        return None


class SqliteQueue:
    """Durable work queue in one SQLite file.

    Same durability contract as the frontier: every state change commits in
    its own transaction, so a killed worker leaves the file in a valid state
    and unacked work is redeliverable after restart (``recover()`` requeues
    everything still marked unacked -- a crashed worker cannot ack).
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS tasks(
      task_id    TEXT PRIMARY KEY,
      payload    TEXT NOT NULL,
      state      TEXT NOT NULL DEFAULT 'ready',  -- ready | unacked | done
      enqueued_at REAL NOT NULL,
      reserved_at REAL
    );
    CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, enqueued_at);
    """

    def __init__(self, path: str) -> None:
        if path.startswith("sqlite://"):
            path = path[len("sqlite://") :]
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.executescript(self._SCHEMA)
        self._lock = threading.Lock()

    def enqueue(self, payload: dict[str, Any]) -> str:
        task_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks(task_id, payload, state, enqueued_at) VALUES(?,?, 'ready', ?)",
                (task_id, json.dumps(payload, ensure_ascii=False), time.time()),
            )
        return task_id

    def reserve(self, timeout_sec: float = 5.0) -> tuple[str | None, dict[str, Any] | None]:
        deadline = time.monotonic() + max(0.0, timeout_sec)
        while True:
            handle: str | None = None
            payload: dict[str, Any] | None = None
            with self._lock:
                row = self._conn.execute(
                    "SELECT task_id, payload FROM tasks WHERE state = 'ready'"
                    " ORDER BY enqueued_at ASC LIMIT 1"
                ).fetchone()
                if row:
                    # Claim atomically: a plain SELECT-then-UPDATE lets two
                    # workers (each with their own connection -- the broker is
                    # shared) read the same ready row and BOTH hand it out.
                    # Only the UPDATE that actually flips state may deliver.
                    cur = self._conn.execute(
                        "UPDATE tasks SET state = 'unacked', reserved_at = ?"
                        " WHERE task_id = ? AND state = 'ready'",
                        (time.time(), row[0]),
                    )
                    if cur.rowcount == 1:
                        handle, payload = row[0], json.loads(row[1])
                    else:
                        # Lost the race: retry the selection immediately.
                        continue
            if handle is not None:
                return handle, payload
            if time.monotonic() >= deadline:
                return None, None
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def ack(self, handle: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tasks WHERE task_id = ?", (handle,))

    def nack(self, handle: str) -> None:
        with self._lock:
            # Requeue at the TAIL (fresh enqueued_at): a poison task that is
            # nacked instantly must not sit at the head and monopolize every
            # worker's next reserve -- that is a retry storm with no backoff.
            self._conn.execute(
                "UPDATE tasks SET state = 'ready', reserved_at = NULL, enqueued_at = ?"
                " WHERE task_id = ? AND state = 'unacked'",
                (time.time(), handle),
            )

    def recover(self) -> int:
        """Requeue everything left unacked (crashed-worker cleanup)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET state = 'ready', reserved_at = NULL WHERE state = 'unacked'"
            )
            return cur.rowcount or 0

    def depth(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE state IN ('ready', 'unacked')"
            ).fetchone()
            return int(row[0])

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class RedisQueue:
    """Redis list-based lease queue (optional ``redis`` package).

    ``enqueue`` LPUSHes onto ``key:ready``; ``reserve`` RPOPLPUSHes to
    ``key:unacked`` (the durable in-flight list, redeliverable by
    ``recover()`` after a crash); ack deletes, nack pushes back. The
    handle->raw-item map lives in the worker process -- that is honest
    at-least-once delivery, not exactly-once: after a crash, unacked work
    is redelivered and must be idempotent downstream.
    """

    def __init__(self, url: str, key: str = "freegem:work") -> None:
        try:
            import redis
        except ImportError as exc:
            raise QueueUnavailable("pip install redis") from exc
        self._key = key
        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._pending_raw: dict[str, str] = {}

    def enqueue(self, payload: dict[str, Any]) -> str:
        task_id = uuid.uuid4().hex[:12]
        self._r.lpush(f"{self._key}:ready", json.dumps({"id": task_id, "payload": payload}))
        return task_id

    def reserve(self, timeout_sec: float = 5.0) -> tuple[str | None, dict[str, Any] | None]:
        budget = max(0.0, timeout_sec)
        if budget <= 0:
            # Redis reads brpoplpush timeout=0 as "block FOREVER": a caller
            # asking for a non-blocking try must not get an infinite block.
            item = self._r.rpoplpush(f"{self._key}:ready", f"{self._key}:unacked")
        else:
            # brpoplpush takes whole seconds only; a 0.5s ask truncated to 0
            # would degenerate into the same infinite block, so ceil to 1s.
            item = self._r.brpoplpush(
                f"{self._key}:ready", f"{self._key}:unacked", timeout=math.ceil(budget)
            )
        if item is None:
            return None, None
        rec = json.loads(item)
        self._pending_raw[rec["id"]] = item
        return rec["id"], rec["payload"]

    def ack(self, handle: str) -> None:
        raw = self._pending_raw.pop(handle, None)
        if raw is not None:
            self._r.lrem(f"{self._key}:unacked", 1, raw)

    def nack(self, handle: str) -> None:
        raw = self._pending_raw.pop(handle, None)
        if raw is not None:
            self._r.lrem(f"{self._key}:unacked", 1, raw)
            self._r.lpush(f"{self._key}:ready", raw)

    def recover(self) -> int:
        n = int(self._r.llen(f"{self._key}:unacked"))
        while True:
            item = self._r.rpoplpush(f"{self._key}:unacked", f"{self._key}:ready")
            if item is None:
                break
        return n

    def depth(self) -> int:
        return int(self._r.llen(f"{self._key}:ready")) + int(self._r.llen(f"{self._key}:unacked"))

    def close(self) -> None:
        return None


class RabbitMQQueue:
    """AMQP queue via optional ``pika`` package (durable queue, manual ack)."""

    def __init__(self, url: str, queue_name: str = "freegem.work") -> None:
        try:
            import pika
        except ImportError as exc:
            raise QueueUnavailable("pip install pika") from exc
        self._queue = queue_name
        self._params = pika.URLParameters(url)
        self._pika = pika
        self._conn = pika.BlockingConnection(self._params)
        self._ch = self._conn.channel()
        self._ch.queue_declare(queue=queue_name, durable=True)
        self._pending: dict[str, tuple[Any, Any]] = {}

    def enqueue(self, payload: dict[str, Any]) -> str:
        task_id = uuid.uuid4().hex[:12]
        self._ch.basic_publish(
            exchange="",
            routing_key=self._queue,
            body=json.dumps({"id": task_id, "payload": payload}).encode("utf-8"),
            properties=self._pika.BasicProperties(delivery_mode=2),
        )
        return task_id

    def reserve(self, timeout_sec: float = 5.0) -> tuple[str | None, dict[str, Any] | None]:
        # basic_get is non-blocking; the WorkQueue contract says reserve BLOCKS
        # up to timeout_sec, so poll like SqliteQueue does -- the old single
        # get returned (None, None) instantly even with a 5s ask.
        deadline = time.monotonic() + max(0.0, timeout_sec)
        while True:
            method, _props, body = self._ch.basic_get(self._queue, auto_ack=False)
            if method is not None and body is not None:
                rec = json.loads(body.decode("utf-8"))
                self._pending[rec["id"]] = (method.delivery_tag, rec)
                return rec["id"], rec["payload"]
            if time.monotonic() >= deadline:
                return None, None
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    def ack(self, handle: str) -> None:
        entry = self._pending.pop(handle, None)
        if entry:
            self._ch.basic_ack(entry[0])

    def nack(self, handle: str) -> None:
        entry = self._pending.pop(handle, None)
        if entry:
            self._ch.basic_nack(entry[0], requeue=True)

    def depth(self) -> int:
        q = self._ch.queue_declare(queue=self._queue, durable=True, passive=True)
        return int(q.method.message_count)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.close()


def open_queue(url: str) -> WorkQueue:
    """Scheme-dispatch factory: memory://, sqlite:///path, redis://, amqp://."""
    scheme = url.split(":", 1)[0].lower()
    if scheme == "memory":
        return MemoryQueue()
    if scheme == "sqlite":
        return SqliteQueue(url)
    if scheme in ("redis", "rediss"):
        return RedisQueue(url)
    if scheme in ("amqp", "amqps"):
        return RabbitMQQueue(url)
    raise QueueError(f"unknown queue scheme {scheme!r} in {url!r}")
