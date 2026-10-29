"""Tests for queue adapters (queues.py) — memory + sqlite exercised for real,
redis/rabbit tested for the unavailable-package path and the factory."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from freegem.queues import (
    MemoryQueue,
    QueueError,
    QueueUnavailable,
    SqliteQueue,
    WorkQueue,
    open_queue,
)


def _roundtrip(q: WorkQueue) -> None:
    """Contract every queue implementation must satisfy."""
    h1 = q.enqueue({"url": "https://a", "depth": 1})
    h2 = q.enqueue({"url": "https://b", "depth": 2})
    assert h1 != h2
    assert q.depth() == 2

    got_h, got_payload = q.reserve(timeout_sec=1.0)
    assert got_h == h1 and got_payload == {"url": "https://a", "depth": 1}  # FIFO
    assert q.depth() == 2  # unacked still counts as depth

    q.nack(got_h)
    assert q.depth() == 2
    # nack re-queues the work: it MUST eventually be redelivered (order is
    # implementation detail: both memory and sqlite re-queue at the TAIL so a
    # poisoned item cannot monopolize the head of the queue)
    redelivered = None
    for _ in range(4):
        got_h2, got_payload2 = q.reserve(timeout_sec=1.0)
        assert got_h2 is not None
        if got_h2 == h1:
            redelivered = got_payload2
        else:
            q.ack(got_h2)
        if redelivered is not None and q.depth() == 1:
            break
    assert redelivered is not None and redelivered["url"] == "https://a"
    q.ack(h1)  # h2 already acked during the drain
    assert q.depth() == 0
    assert q.reserve(timeout_sec=0.1) == (None, None)


def test_memory_queue_contract() -> None:
    _roundtrip(MemoryQueue())


def test_memory_queue_concurrent_producers() -> None:
    q = MemoryQueue()

    def produce(n: int) -> None:
        for i in range(n):
            q.enqueue({"i": i})

    threads = [threading.Thread(target=produce, args=(20,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert q.depth() == 80
    seen = 0
    while True:
        handle, _p = q.reserve(timeout_sec=0.1)
        if handle is None:
            break
        q.ack(handle)
        seen += 1
    assert seen == 80


def test_sqlite_queue_contract(tmp_path: Path) -> None:
    _roundtrip(SqliteQueue(str(tmp_path / "q.db")))


def test_sqlite_queue_survives_reopen_and_recovers(tmp_path: Path) -> None:
    path = str(tmp_path / "q.db")
    q1 = SqliteQueue(path)
    q1.enqueue({"job": 1})
    _handle, payload = q1.reserve(timeout_sec=1.0)
    assert payload == {"job": 1}
    # worker "crashes" without ack: reopen, recover, redeliver
    q1.close()
    q2 = SqliteQueue(path)
    assert q2.depth() == 1
    assert q2.recover() == 1
    h2, payload2 = q2.reserve(timeout_sec=1.0)
    assert h2 is not None
    assert payload2 == {"job": 1}
    q2.ack(h2)
    assert q2.depth() == 0
    q2.close()


def test_sqlite_queue_strips_scheme_prefix(tmp_path: Path) -> None:
    q = open_queue(f"sqlite://{tmp_path / 'schemed.db'}")
    q.enqueue({"x": 1})
    assert q.depth() == 1
    q.close()


def test_open_queue_dispatch() -> None:
    assert isinstance(open_queue("memory://"), MemoryQueue)
    with pytest.raises(QueueError, match="unknown queue scheme"):
        open_queue("kafka://nope")


def test_redis_queue_without_package_raises_queue_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def no_redis(name: str, *a: object, **k: object) -> object:
        if name == "redis":
            raise ImportError("no redis here")
        return real_import(name, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", no_redis)
    with pytest.raises(QueueUnavailable, match="pip install redis"):
        open_queue("redis://localhost:6379/0")


def test_rabbit_queue_without_package_raises_queue_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def no_pika(name: str, *a: object, **k: object) -> object:
        if name == "pika":
            raise ImportError("no pika here")
        return real_import(name, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", no_pika)
    with pytest.raises(QueueUnavailable, match="pip install pika"):
        open_queue("amqp://guest:guest@localhost/")


def test_sqlite_queue_concurrent_reserve_never_double_delivers(tmp_path: Path) -> None:
    """The reserve claim must be atomic ACROSS connections (threads or worker
    processes each open their own). The old SELECT-then-UPDATE handed the same
    ready row to two workers: the UPDATE was unconditional, so both callers
    returned the same handle and one ack dropped the other's in-flight work."""
    path = str(tmp_path / "shared.db")
    seed = SqliteQueue(path)
    n = 40
    for i in range(n):
        seed.enqueue({"i": i})
    seed.close()

    handles: list[str] = []
    lock = threading.Lock()

    def drain() -> None:
        q = SqliteQueue(path)
        while True:
            handle, _payload = q.reserve(timeout_sec=0.2)
            if handle is None:
                break
            with lock:
                handles.append(handle)
            q.ack(handle)
        q.close()

    threads = [threading.Thread(target=drain) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(handles) == n, f"expected {n} deliveries, got {len(handles)}"
    assert len(set(handles)) == n, "an item must never be handed to two workers"
    final = SqliteQueue(path)
    assert final.depth() == 0
    final.close()


def test_sqlite_queue_nack_requeues_at_the_tail(tmp_path: Path) -> None:
    """A nacked (poison) task must not stay at the head: reserve would hand
    the same failing item back before untouched work -- a retry storm."""
    q = SqliteQueue(str(tmp_path / "tail.db"))
    h1 = q.enqueue({"n": 1})
    h2 = q.enqueue({"n": 2})
    got, payload = q.reserve(timeout_sec=1.0)
    assert got == h1 and payload == {"n": 1}
    q.nack(got)
    # untouched h2 comes first now, the nacked item goes behind it
    got2, payload2 = q.reserve(timeout_sec=1.0)
    assert got2 == h2 and payload2 == {"n": 2}
    q.ack(got2)
    got3, payload3 = q.reserve(timeout_sec=1.0)
    assert got3 == h1 and payload3 == {"n": 1}
    q.ack(got3)
    assert q.depth() == 0
    q.close()


def test_sqlite_queue_double_reserve_same_instance_is_exclusive(tmp_path: Path) -> None:
    """Even one connection with two racing threads: exactly one wins the row."""
    q = SqliteQueue(str(tmp_path / "excl.db"))
    for i in range(10):
        q.enqueue({"i": i})
    won: list[str] = []
    lock = threading.Lock()

    def drain() -> None:
        while True:
            handle, _payload = q.reserve(timeout_sec=0.1)
            if handle is None:
                break
            with lock:
                won.append(handle)
            q.ack(handle)

    threads = [threading.Thread(target=drain) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 10
    assert len(set(won)) == 10
    q.close()
