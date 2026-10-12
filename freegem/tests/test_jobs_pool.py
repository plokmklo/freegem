"""Tests for the bounded job runner (Phase 2).

Bounded worker pool, queue capacity + saturation, retention, cancellation,
timeouts, retry limits, durable SQLite status, restart recovery, and
optional external-queue execution.
"""

import threading
import time
import unittest
from pathlib import Path
from typing import Any

from freegem.jobs import (
    Job,
    JobManager,
    JobQueueFull,
    JobRunner,
    MemoryJobStore,
    SqliteJobStore,
)

ART_DIR = "/tmp/freegem-test-artifacts"


class BoundedPoolTests(unittest.TestCase):
    def test_max_concurrent_never_exceeds_workers(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        lock = threading.Lock()
        peak = {"n": 0, "cur": 0}
        gate = threading.Event()

        def body(job: Job) -> None:
            with lock:
                peak["cur"] += 1
                peak["n"] = max(peak["n"], peak["cur"])
            gate.wait(timeout=10)
            with lock:
                peak["cur"] -= 1
            mgr.update_job_status(job.id, "completed")

        runner = JobRunner(mgr, body, max_workers=2, max_pending=50, job_timeout_sec=30)
        self.addCleanup(runner.close)
        jobs = [runner.submit("scrape", {}) for _ in range(4)]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and peak["cur"] < 2:
            time.sleep(0.01)
        # Both workers are inside the gate: a third cannot have entered.
        self.assertEqual(peak["n"], 2)
        gate.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(
            (mgr.get_job(j.id) is None or mgr.get_job(j.id).status != "completed")  # type: ignore[union-attr]
            for j in jobs
        ):
            time.sleep(0.02)
        self.assertLessEqual(peak["n"], 2)

    def test_queue_saturation_raises(self) -> None:
        gate = threading.Event()
        mgr = JobManager(artifact_dir=ART_DIR)

        def body(job: Job) -> None:
            gate.wait(timeout=10)
            mgr.update_job_status(job.id, "completed")

        runner = JobRunner(mgr, body, max_workers=1, max_pending=1, job_timeout_sec=30)
        self.addCleanup(runner.close)
        # Worker takes job 1; job 2 fills the single pending slot.
        first = runner.submit("scrape", {})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
            mgr.get_job(first.id) is None or mgr.get_job(first.id).status != "processing"  # type: ignore[union-attr]
        ):
            time.sleep(0.01)
        runner.submit("scrape", {})
        with self.assertRaises(JobQueueFull):
            runner.submit("scrape", {})
        gate.set()

    def test_cancel_queued_job_never_runs(self) -> None:
        gate = threading.Event()
        mgr = JobManager(artifact_dir=ART_DIR)
        ran: list[str] = []

        def body(job: Job) -> None:
            ran.append(job.id)
            gate.wait(timeout=5)
            mgr.update_job_status(job.id, "completed")

        runner = JobRunner(mgr, body, max_workers=1, max_pending=10, job_timeout_sec=30)
        self.addCleanup(runner.close)
        blocker = runner.submit("scrape", {})  # occupies the one worker
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
            mgr.get_job(blocker.id) is None or mgr.get_job(blocker.id).status != "processing"  # type: ignore[union-attr]
        ):
            time.sleep(0.01)
        queued = runner.submit("scrape", {})
        self.assertTrue(runner.cancel(queued.id))
        fetched = mgr.get_job(queued.id)
        assert fetched is not None
        self.assertEqual(fetched.status, "cancelled")
        gate.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (
            mgr.get_job(blocker.id) is None or mgr.get_job(blocker.id).status != "completed"  # type: ignore[union-attr]
        ):
            time.sleep(0.02)
        time.sleep(0.3)  # let the worker observe the cancelled queue entry
        self.assertNotIn(queued.id, ran)

    def test_cancel_running_drops_result(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        started = threading.Event()
        release = threading.Event()

        def body(job: Job) -> None:
            started.set()
            release.wait(timeout=10)
            mgr.update_job_status(job.id, "completed", result={"ok": True})

        runner = JobRunner(mgr, body, max_workers=1, max_pending=10, job_timeout_sec=30)
        self.addCleanup(runner.close)
        job = runner.submit("scrape", {})
        self.assertTrue(started.wait(timeout=5))
        self.assertTrue(runner.cancel(job.id))
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = mgr.get_job(job.id)
            if current and current.status == "cancelled":
                break
            time.sleep(0.02)
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "cancelled")
        self.assertIsNone(current.result)  # cancelled results are not delivered

    def test_timeout_watchdog_marks_timed_out(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)

        def body(job: Job) -> None:
            time.sleep(3)

        runner = JobRunner(mgr, body, max_workers=1, max_pending=10, job_timeout_sec=0.3)
        self.addCleanup(runner.close)
        job = runner.submit("scrape", {})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = mgr.get_job(job.id)
            if current and current.status == "timed_out":
                break
            time.sleep(0.05)
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "timed_out")
        self.assertIn("timeout", current.error or "")

    def test_retry_succeeds_within_limit(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        calls = {"n": 0}

        def flaky(job: Job) -> None:
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient")
            mgr.update_job_status(job.id, "completed", result={"attempts": calls["n"]})

        runner = JobRunner(
            mgr, flaky, max_workers=1, max_pending=10, job_timeout_sec=30, default_max_retries=3
        )
        self.addCleanup(runner.close)
        job = runner.submit("scrape", {})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = mgr.get_job(job.id)
            if current and current.status == "completed":
                break
            time.sleep(0.05)
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "completed")
        self.assertEqual(current.attempts, 3)
        self.assertGreaterEqual(len(current.retry_traces), 2)

    def test_permanent_failure_after_retries_exhausted(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        calls = {"n": 0}

        def always_fails(job: Job) -> None:
            calls["n"] += 1
            raise RuntimeError("nope")

        runner = JobRunner(
            mgr,
            always_fails,
            max_workers=1,
            max_pending=10,
            job_timeout_sec=30,
            default_max_retries=1,
        )
        self.addCleanup(runner.close)
        job = runner.submit("scrape", {})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = mgr.get_job(job.id)
            if current and current.status == "failed":
                break
            time.sleep(0.05)
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "failed")
        self.assertEqual(calls["n"], 2)  # first + one retry, then stop

    def test_retention_evicts_oldest_terminal(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        runner = JobRunner(mgr, _make_noop(mgr), max_workers=2, max_pending=20, retention=3)
        self.addCleanup(runner.close)
        jobs = [runner.submit("scrape", {}) for _ in range(6)]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            statuses = []
            for j in jobs:
                cur = mgr.get_job(j.id)
                statuses.append(cur.status if cur else "")
            if all(s == "completed" for s in statuses):
                break
            time.sleep(0.02)
        time.sleep(0.3)  # eviction runs after the last completion
        remaining = set(mgr.jobs.keys())
        self.assertLessEqual(len(remaining), 3)
        # The OLDEST jobs are the ones gone.
        self.assertNotIn(jobs[0].id, remaining)
        self.assertIn(jobs[-1].id, remaining)

    def test_stats_shape(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        runner = JobRunner(mgr, _make_noop(mgr), max_workers=3, max_pending=7)
        self.addCleanup(runner.close)
        stats = runner.stats()
        self.assertEqual(stats["workers"], 3)
        self.assertEqual(stats["max_pending"], 7)
        self.assertIn("pending", stats)
        self.assertIn("running", stats)
        self.assertFalse(stats["external_queue"])


def _make_noop(mgr: JobManager) -> Any:
    def body(job: Job) -> None:
        mgr.update_job_status(job.id, "completed", result={"ok": True})

    return body


class DurableStoreTests(unittest.TestCase):
    def test_status_survives_reopen(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "jobs.sqlite3"
            # Windows cannot delete an open SQLite file: every store must be
            # closed before the TemporaryDirectory context exits.
            store1 = SqliteJobStore(db)
            try:
                mgr = JobManager(artifact_dir=ART_DIR, store=store1)
                job = mgr.create_job("scrape", {"url": "https://example.com/"})
                mgr.update_job_status(job.id, "processing")
                mgr.update_job_status(job.id, "completed", result={"text": "hi"})
            finally:
                store1.close()

            store2 = SqliteJobStore(db)
            try:
                reopened = JobManager(artifact_dir=ART_DIR, store=store2)
                fetched = reopened.get_job(job.id)
                assert fetched is not None
                self.assertEqual(fetched.status, "completed")
                assert fetched.result is not None
                self.assertEqual(fetched.result["text"], "hi")
            finally:
                store2.close()

    def test_restart_recovers_interrupted(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "jobs.sqlite3"
            store1 = SqliteJobStore(db)
            try:
                mgr = JobManager(artifact_dir=ART_DIR, store=store1)
                queued = mgr.create_job("scrape", {})
                running = mgr.create_job("scrape", {})
                mgr.update_job_status(running.id, "processing")
                done = mgr.create_job("scrape", {})
                mgr.update_job_status(done.id, "completed")
            finally:
                store1.close()

            store2 = SqliteJobStore(db)
            try:
                reopened = JobManager(artifact_dir=ART_DIR, store=store2)
                q = reopened.get_job(queued.id)
                r = reopened.get_job(running.id)
                d = reopened.get_job(done.id)
                assert q is not None and r is not None and d is not None
                self.assertEqual(q.status, "interrupted")
                self.assertEqual(r.status, "interrupted")
                self.assertIn("restarted", q.error or "")
                self.assertEqual(d.status, "completed")  # terminal rows untouched
            finally:
                store2.close()

    def test_memory_store_is_a_noop(self) -> None:
        store = MemoryJobStore()
        job = Job(id="j", type="scrape", status="queued")
        store.upsert(job)
        self.assertEqual(store.load_all(), [])
        self.assertEqual(store.mark_interrupted(), 0)
        store.delete("j")
        store.close()

    def test_eviction_deletes_from_store(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "jobs.sqlite3"
            store = SqliteJobStore(db)
            try:
                mgr = JobManager(artifact_dir=ART_DIR, store=store)
                for _ in range(3):
                    job = mgr.create_job("scrape", {})
                    mgr.update_job_status(job.id, "completed")
                self.assertEqual(len(store.load_all()), 3)
                self.assertEqual(mgr.enforce_retention(0), 0)  # keep<=0 is a no-op
                self.assertEqual(mgr.enforce_retention(1), 2)  # evict to one
                self.assertEqual(mgr.enforce_retention(1), 0)  # already within
                self.assertEqual(len(store.load_all()), 1)
                self.assertEqual(len(mgr.jobs), 1)
            finally:
                store.close()


class ExternalQueueTests(unittest.TestCase):
    def test_memory_queue_round_trip(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        runner = JobRunner(
            mgr, _make_noop(mgr), max_workers=1, max_pending=10, queue_url="memory://"
        )
        self.addCleanup(runner.close)
        self.assertTrue(runner.stats()["external_queue"])
        job = runner.submit("scrape", {"url": "https://example.com/"})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = mgr.get_job(job.id)
            if current and current.status == "completed":
                break
            time.sleep(0.02)
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "completed")
        ext: Any = runner._external
        self.assertEqual(ext.depth(), 0)  # payload was acked

    def test_sqlite_queue_durable_submission(self) -> None:
        import tempfile

        from freegem.queues import SqliteQueue

        with tempfile.TemporaryDirectory() as tmp:
            qpath = Path(tmp) / "q.sqlite3"
            mgr = JobManager(artifact_dir=ART_DIR)
            runner = JobRunner(
                mgr, _make_noop(mgr), max_workers=1, max_pending=10, queue_url=f"sqlite://{qpath}"
            )
            self.addCleanup(runner.close)
            job = runner.submit("scrape", {})
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = mgr.get_job(job.id)
                if current and current.status == "completed":
                    break
                time.sleep(0.02)
            current = mgr.get_job(job.id)
            assert current is not None
            self.assertEqual(current.status, "completed")
            # The dispatcher consumed + acked the durable payload.
            probe = SqliteQueue(f"sqlite://{qpath}")
            try:
                self.assertEqual(probe.depth(), 0)
            finally:
                probe.close()
                # Windows: release the file before the tempdir is removed.
                ext: Any = runner._external
                ext.close()
                runner._external = None


if __name__ == "__main__":
    unittest.main()
