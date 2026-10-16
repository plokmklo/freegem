"""Race-condition and eviction tests for the job subsystem.

Every test here pins one deterministic interleaving that the audit found in
the pre-fix code (issues #26, #28, #39): terminal-status overwrites by a
draining body, the watchdog's stale-snapshot TOCTOU, a cancel flipping a
finished job back to ``cancelled`` while keeping its result, the retry
backoff resurrecting watchdog-timed-out jobs, and retention evictions that
leaked artifact records + files.

Determinism rules: threads are choreographed with events, waits poll for a
condition with a deadline (never a bare sleep of faith), and the few settle
allowances only ever widen a window that the assertion then checks is
closed.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from freegem.jobs import Job, JobManager, JobRunner

ART_DIR = "/tmp/freegem-test-artifacts"


def _await_status(mgr: JobManager, job_id: str, *statuses: str, timeout: float = 5.0) -> Job:
    """Poll until the job reaches one of ``statuses``; returns the job."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = mgr.get_job(job_id)
        if job is not None and job.status in statuses:
            return job
        time.sleep(0.01)
    job = mgr.get_job(job_id)
    assert job is not None, "job vanished"
    return job


def _await_running_empty(runner: JobRunner, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and runner.stats()["running"]:
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# Issue #26(a): terminal states are final.
# ---------------------------------------------------------------------------


class TerminalStatusGuardTests(unittest.TestCase):
    def test_update_after_completed_is_ignored(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        job = mgr.create_job("scrape", {})
        mgr.update_job_status(job.id, "completed", result={"trusted": True})
        applied = mgr.update_job_status(
            job.id, "failed", error="late failure", result={"untrusted": True}
        )
        self.assertFalse(applied)
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "completed")
        self.assertEqual(current.result, {"trusted": True})
        self.assertIsNone(current.error)

    def test_queued_write_cannot_resurrect_timed_out(self) -> None:
        """The retry stamp must not pull a watchdog-timed-out job back to
        ``queued`` (#26a / the resurrection half of #28)."""
        mgr = JobManager(artifact_dir=ART_DIR)
        job = mgr.create_job("scrape", {})
        mgr.update_job_status(job.id, "processing")
        self.assertTrue(
            mgr.stamp_timed_out(job.id, error="deadline", retry_trace={"error": "execution timeout"})
        )
        applied = mgr.update_job_status(job.id, "queued", error="retrying")
        self.assertFalse(applied)
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "timed_out")

    def test_idempotent_same_state_write_still_applies(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        job = mgr.create_job("scrape", {})
        mgr.update_job_status(job.id, "completed", result={"n": 1})
        self.assertTrue(mgr.update_job_status(job.id, "completed"))
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "completed")

    def test_nonterminal_transitions_unaffected(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        job = mgr.create_job("scrape", {})
        self.assertTrue(mgr.update_job_status(job.id, "processing"))
        self.assertTrue(mgr.update_job_status(job.id, "completed", result={"ok": True}))
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "completed")

    def test_unknown_job_returns_false(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        self.assertFalse(mgr.update_job_status("job-nope", "completed"))


class WatchdogRaceTests(unittest.TestCase):
    """#26(a): a body that drains after the watchdog fired must not deliver."""

    def test_body_finish_after_watchdog_cannot_overwrite_timed_out(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        started = threading.Event()
        release = threading.Event()
        drained = threading.Event()

        def body(job: Job) -> None:
            started.set()
            release.wait(timeout=10)
            # The watchdog already stamped timed_out; this write is the
            # untrusted result the guard must swallow.
            mgr.update_job_status(job.id, "completed", result={"untrusted": True})
            drained.set()

        runner = JobRunner(mgr, body, max_workers=1, max_pending=5, job_timeout_sec=0.3)
        self.addCleanup(runner.close)
        job = runner.submit("scrape", {})
        self.assertTrue(started.wait(timeout=5))
        timed = _await_status(mgr, job.id, "timed_out")
        self.assertEqual(timed.status, "timed_out")
        release.set()
        self.assertTrue(drained.wait(timeout=5), "body never finished its drain")
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "timed_out")
        self.assertIsNone(current.result)

    def test_stamp_timed_out_refuses_terminal_job(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        job = mgr.create_job("scrape", {})
        mgr.update_job_status(job.id, "completed", result={"kept": True})
        self.assertFalse(mgr.stamp_timed_out(job.id, error="deadline"))
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "completed")
        self.assertEqual(current.result, {"kept": True})


class WatchdogReapTests(unittest.TestCase):
    """#26(b): the watchdog's expired snapshot is stale by the time it acts."""

    def _runner(self, mgr: JobManager) -> JobRunner:
        runner = JobRunner(
            mgr, lambda job: None, max_workers=1, max_pending=5, job_timeout_sec=30
        )
        self.addCleanup(runner.close)
        return runner

    def test_reap_never_overwrites_a_completion_landing_after_recheck(self) -> None:
        """Expired snapshot says the job is still running; by the time the
        sweep re-checks and reaches the stamp, the body has completed and
        delivered a result. The stamp must lose, and the result must stay."""
        mgr = JobManager(artifact_dir=ART_DIR)
        runner = self._runner(mgr)
        job = mgr.create_job("scrape", {})
        mgr.update_job_status(job.id, "processing")
        with runner._lock:
            runner._running[job.id] = time.monotonic() - 1.0  # expired entry

        real_get = mgr.get_job
        fired = {"once": False}

        def finishing_get(job_id: str) -> Any:
            fetched = real_get(job_id)
            if fetched is not None and job_id == job.id and not fired["once"]:
                fired["once"] = True
                # The completion lands after the sweep's membership re-check
                # (the entry is still registered) but before the stamp.
                mgr.update_job_status(job.id, "completed", result={"delivered": True})
            return fetched

        with mock.patch.object(mgr, "get_job", side_effect=finishing_get):
            runner._reap_expired()

        current = real_get(job.id)
        assert current is not None
        self.assertEqual(current.status, "completed")
        self.assertEqual(current.result, {"delivered": True})
        with runner._lock:
            self.assertNotIn(job.id, runner._running)  # entry reaped either way

    def test_reap_stamps_jobs_that_are_actually_expired(self) -> None:
        """Control: the sweep still works for a genuinely running job."""
        mgr = JobManager(artifact_dir=ART_DIR)
        runner = self._runner(mgr)
        job = mgr.create_job("scrape", {})
        mgr.update_job_status(job.id, "processing")
        with runner._lock:
            runner._running[job.id] = time.monotonic() - 1.0
        runner._reap_expired()
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "timed_out")
        self.assertIn("timeout", current.error or "")
        self.assertIsNone(current.result)
        with runner._lock:
            self.assertNotIn(job.id, runner._running)


# ---------------------------------------------------------------------------
# Issue #26(c): cancel must not flip a finished job back to cancelled.
# ---------------------------------------------------------------------------


class CancelRaceTests(unittest.TestCase):
    def test_cancel_stamp_loses_to_completion_in_flight(self) -> None:
        """#26(c): cancel() classified the job as ``queued`` from its status
        read; the job completed while the cancel call was in flight. The
        late "cancelled before start" stamp must be refused -- pre-fix it
        flipped the finished job to ``cancelled`` while its result survived
        (an incoherent terminal state)."""
        mgr = JobManager(artifact_dir=ART_DIR)
        gate = threading.Event()

        def blocker(job: Job) -> None:
            gate.wait(timeout=10)

        runner = JobRunner(mgr, blocker, max_workers=1, max_pending=5, job_timeout_sec=30)
        self.addCleanup(runner.close)
        held = runner.submit("scrape", {})  # occupies the only worker
        self.assertIsNotNone(_await_status(mgr, held.id, "processing"))
        job = runner.submit("scrape", {})  # stays queued

        original_add = runner._cancel_requested.add

        class _CompletingSet(set):
            def add(self, item: str) -> None:
                # The completion lands between cancel()'s status read and
                # its stamp -- the exact window the unlocked read allowed.
                mgr.update_job_status(job.id, "completed", result={"kept": True})
                original_add(item)

        runner._cancel_requested = _CompletingSet()  # type: ignore[assignment]
        try:
            self.assertTrue(runner.cancel(job.id))  # cancel was requested
        finally:
            runner._cancel_requested = set()  # type: ignore[assignment]
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "completed")
        self.assertEqual(current.result, {"kept": True})
        gate.set()

    def test_cancel_on_terminal_job_returns_false(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        runner = JobRunner(mgr, lambda job: None, max_workers=1, max_pending=5, job_timeout_sec=30)
        self.addCleanup(runner.close)
        job = mgr.create_job("scrape", {})
        mgr.update_job_status(job.id, "completed", result={"done": True})
        self.assertFalse(runner.cancel(job.id))
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "completed")
        self.assertEqual(current.result, {"done": True})

    def test_cancel_still_works_for_processing_jobs(self) -> None:
        mgr = JobManager(artifact_dir=ART_DIR)
        started = threading.Event()
        release = threading.Event()

        def body(job: Job) -> None:
            started.set()
            release.wait(timeout=10)

        runner = JobRunner(mgr, body, max_workers=1, max_pending=5, job_timeout_sec=30)
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
        self.assertIsNone(current.result)


# ---------------------------------------------------------------------------
# Issue #28: the retry backoff must not sleep inside the running slot.
# ---------------------------------------------------------------------------


class RetryBackoffTests(unittest.TestCase):
    def test_near_deadline_failure_is_not_resurrected(self) -> None:
        """A failure whose retry backoff outlives the job's timeout must end
        ``failed`` -- never ``timed_out`` (the old code slept INSIDE the
        ``_running`` entry, so the watchdog marked the sleeping job
        ``timed_out`` and the retry machinery resurrected/re-queued it), and
        never stuck ``queued``. The retry itself must still happen."""
        mgr = JobManager(artifact_dir=ART_DIR)
        calls = {"n": 0}
        lock = threading.Lock()

        def failing(job: Job) -> None:
            with lock:
                calls["n"] += 1
            raise RuntimeError("transient boom")

        # timeout 0.3s, first backoff 1s (2**0): the sleep used to cover the
        # whole deadline while the slot was still registered as running.
        runner = JobRunner(
            mgr,
            failing,
            max_workers=1,
            max_pending=5,
            job_timeout_sec=0.3,
            default_max_retries=1,
        )
        self.addCleanup(runner.close)
        job = runner.submit("scrape", {})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = mgr.get_job(job.id)
            if current is not None and current.status in ("failed", "timed_out"):
                break
            time.sleep(0.02)
        # Settle: a wrong resurrection (timed_out or queued) would land here.
        time.sleep(0.5)
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "failed")
        self.assertEqual(current.attempts, 2)
        self.assertEqual(calls["n"], 2)

    def test_retry_eligible_job_still_retries(self) -> None:
        """Guard the backoff restructure: a retryable failure still runs a
        second attempt and completes."""
        mgr = JobManager(artifact_dir=ART_DIR)
        calls = {"n": 0}

        def flaky(job: Job) -> None:
            calls["n"] += 1
            if calls["n"] < 2:
                raise RuntimeError("once")
            mgr.update_job_status(job.id, "completed", result={"attempt": calls["n"]})

        runner = JobRunner(
            mgr,
            flaky,
            max_workers=1,
            max_pending=5,
            job_timeout_sec=30,
            default_max_retries=2,
        )
        self.addCleanup(runner.close)
        job = runner.submit("scrape", {})
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = mgr.get_job(job.id)
            if current is not None and current.status == "completed":
                break
            time.sleep(0.02)
        current = mgr.get_job(job.id)
        assert current is not None
        self.assertEqual(current.status, "completed")
        self.assertEqual(current.attempts, 2)
        self.assertGreaterEqual(len(current.retry_traces), 1)
        assert current.result is not None
        self.assertEqual(current.result["attempt"], 2)


# ---------------------------------------------------------------------------
# Issue #39 (= duplicate #32): retention eviction must purge artifacts too.
# ---------------------------------------------------------------------------


class RetentionArtifactPurgeTests(unittest.TestCase):
    def test_eviction_purges_artifact_records_and_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = JobManager(artifact_dir=tmp)
            victim = mgr.create_job("scrape", {})
            mgr.update_job_status(victim.id, "completed", result={"ok": True})
            survivor = mgr.create_job("scrape", {})
            mgr.update_job_status(survivor.id, "completed", result={"ok": True})
            victim_art = mgr.save_artifact(victim.id, "shot.png", "image/png", b"PNG-bytes")
            survivor_art = mgr.save_artifact(survivor.id, "keep.png", "image/png", b"PNG-bytes")
            self.assertTrue(os.path.exists(victim_art.file_path))

            self.assertEqual(mgr.enforce_retention(keep=1), 1)

            # The evicted job takes its artifacts with it: registry entry
            # AND file on disk.
            self.assertIsNone(mgr.get_job(victim.id))
            self.assertIsNone(mgr.get_artifact(victim_art.id))
            self.assertFalse(os.path.exists(victim_art.file_path))
            # A still-live job's artifacts are untouched.
            self.assertIsNotNone(mgr.get_job(survivor.id))
            self.assertIsNotNone(mgr.get_artifact(survivor_art.id))
            self.assertTrue(os.path.exists(survivor_art.file_path))

    def test_startup_sweep_removes_orphaned_artifact_files(self) -> None:
        """Files left behind by pre-fix evictions or a mid-write crash, whose
        artifact id no longer belongs to any known job, are swept at startup.
        Files that are not JobManager artifacts are never touched."""
        with tempfile.TemporaryDirectory() as tmp:
            orphan = Path(tmp) / "art-orphan12_ghost.png"
            foreign = Path(tmp) / "unrelated.txt"
            orphan.write_bytes(b"orphan")
            foreign.write_bytes(b"keep me")
            JobManager(artifact_dir=tmp)  # no known jobs: every art-* file is an orphan
            self.assertFalse(orphan.exists())
            self.assertTrue(foreign.exists())

    def test_startup_sweep_keeps_durable_job_artifacts(self) -> None:
        """A file referenced by a job restored from the durable store must
        survive the sweep (its artifact id is still known)."""
        from freegem.jobs import SqliteJobStore

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "jobs.sqlite3"
            store1 = SqliteJobStore(db)
            try:
                mgr = JobManager(artifact_dir=tmp, store=store1)
                job = mgr.create_job("scrape", {})
                mgr.update_job_status(job.id, "completed")
                art = mgr.save_artifact(job.id, "shot.png", "image/png", b"PNG")
                art_path = Path(art.file_path)
            finally:
                store1.close()
            store2 = SqliteJobStore(db)
            try:
                JobManager(artifact_dir=tmp, store=store2)  # restart sweep
                self.assertTrue(art_path.exists())
            finally:
                store2.close()


if __name__ == "__main__":
    unittest.main()
