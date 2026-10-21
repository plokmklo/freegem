"""Tests for the optional control-plane primitives (freegem.control_plane)."""

from __future__ import annotations

import tempfile
import time
import unittest

from freegem.control_plane import ControlPlane, ControlPlaneError
from freegem.queues import MemoryQueue, SqliteQueue


class ControlPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = f"{self.tmp.name}/cp.sqlite"

    def test_full_lifecycle_memory_queue(self) -> None:
        queue = MemoryQueue()
        cp = ControlPlane(queue, self.store, lease_ttl_sec=60.0, worker_ttl_sec=60.0)
        self.addCleanup(cp.close)  # Windows: release the sqlite handle
        task_id = queue.enqueue({"url": "https://example.com/"})
        worker = cp.join("crawler", {"host": "box-1"})
        handle, payload = cp.claim(worker, timeout_sec=1.0)
        assert handle is not None  # narrow for mypy
        self.assertIsNotNone(handle)
        self.assertEqual(payload, {"url": "https://example.com/"})
        cp.heartbeat(worker)
        cp.renew(worker, handle)  # long task keeps its lease
        cp.complete(worker, handle, ok=True)
        stats = cp.stats()
        self.assertEqual(stats["tasks_in_flight"], 0)
        self.assertEqual(stats["workers_alive"], 1)
        self.assertEqual(stats["queue_depth"], 0)
        cp.leave(worker)
        self.assertEqual(cp.stats()["workers_alive"], 0)
        del task_id  # queue hands out its own handles

    def test_crashed_worker_task_is_reaped_and_redelivered(self) -> None:
        queue = MemoryQueue()
        cp = ControlPlane(queue, self.store, lease_ttl_sec=5.0, worker_ttl_sec=30.0)
        self.addCleanup(cp.close)  # Windows: release the sqlite handle
        queue.enqueue({"url": "https://example.com/x"})
        ghost = cp.join("crawler")
        handle, _payload = cp.claim(ghost, timeout_sec=1.0)
        assert handle is not None  # narrow for mypy
        self.assertIsNotNone(handle)
        # ghost dies: no heartbeat, ttl passes
        report = cp.reap(now=time.time() + 40.0)
        self.assertEqual(report["requeued_tasks"], [handle])
        self.assertEqual(report["dead_workers"], [ghost])
        # a fresh worker can claim the same work again
        fresh = cp.join("crawler")
        handle2, payload2 = cp.claim(fresh, timeout_sec=1.0)
        self.assertIsNotNone(handle2)
        self.assertEqual(payload2, {"url": "https://example.com/x"})
        self.assertEqual(cp.reaped_tasks_total, 1)
        self.assertEqual(cp.reaped_workers_total, 1)

    def test_long_task_renewal_prevents_reaping(self) -> None:
        queue = MemoryQueue()
        cp = ControlPlane(queue, self.store, lease_ttl_sec=5.0, worker_ttl_sec=60.0)
        self.addCleanup(cp.close)  # Windows: release the sqlite handle
        queue.enqueue({"n": 1})
        worker = cp.join("crawler")
        handle, _ = cp.claim(worker, timeout_sec=1.0)
        assert handle is not None  # narrow for mypy
        # simulate a renewal 3s into a 5s lease
        with cp._lock:
            cp._conn.execute(
                "UPDATE leases SET renewed_at = ? WHERE task_id = ?", (time.time() - 3.0, handle)
            )
        report = cp.reap(now=time.time())
        self.assertEqual(report["requeued_tasks"], [])  # renewed lease survives
        cp.complete(worker, handle, ok=True)

    def test_stale_lease_renewal_raises(self) -> None:
        queue = MemoryQueue()
        cp = ControlPlane(queue, self.store)
        self.addCleanup(cp.close)  # Windows: release the sqlite handle
        queue.enqueue({"n": 1})
        worker = cp.join("crawler")
        handle, _ = cp.claim(worker, timeout_sec=1.0)
        assert handle is not None  # narrow for mypy
        cp.reap(now=time.time() + 999.0)  # lease gone
        with self.assertRaises(ControlPlaneError):
            cp.renew(worker, handle)

    def test_unknown_worker_heartbeat_raises(self) -> None:
        cp = ControlPlane(MemoryQueue(), self.store)
        self.addCleanup(cp.close)  # Windows: release the sqlite handle
        with self.assertRaises(ControlPlaneError):
            cp.heartbeat("no-such-worker")

    def test_sqlite_queue_end_to_end(self) -> None:
        queue = SqliteQueue(f"{self.tmp.name}/work.sqlite")
        self.addCleanup(queue.close)  # Windows: release the sqlite handle
        cp = ControlPlane(queue, self.store)
        self.addCleanup(cp.close)  # Windows: release the sqlite handle
        queue.enqueue({"url": "https://example.com/1"})
        queue.enqueue({"url": "https://example.com/2"})
        w1 = cp.join("crawler")
        w2 = cp.join("crawler")
        h1, _ = cp.claim(w1, timeout_sec=1.0)
        h2, _ = cp.claim(w2, timeout_sec=1.0)
        assert h1 is not None and h2 is not None  # narrow for mypy
        self.assertNotEqual(h1, h2)  # no double-claim across workers
        cp.complete(w1, h1, ok=True)
        cp.complete(w2, h2, ok=False)  # nack -> redeliverable
        self.assertEqual(cp.stats()["queue_depth"], 1)
        cp.close()

    def test_list_workers_and_metadata(self) -> None:
        cp = ControlPlane(MemoryQueue(), self.store)
        self.addCleanup(cp.close)  # Windows: release the sqlite handle
        cp.join("crawler", {"host": "a"})
        cp.join("supervisor", {"host": "b"})
        workers = cp.list_workers()
        self.assertEqual({w.kind for w in workers}, {"crawler", "supervisor"})
        self.assertEqual(workers[0].metadata, {"host": "a"})

    def test_store_parent_directory_created(self) -> None:
        """A store path whose directory does not exist yet is a fresh
        deployment, not a crash (same convention as SqliteJobStore)."""
        nested = f"{self.tmp.name}/fresh/deploy/cp.sqlite"
        cp = ControlPlane(MemoryQueue(), nested)
        self.addCleanup(cp.close)  # Windows: release the sqlite handle
        worker = cp.join("crawler")
        self.assertEqual([w.worker_id for w in cp.list_workers()], [worker])
        del worker


if __name__ == "__main__":
    unittest.main()
