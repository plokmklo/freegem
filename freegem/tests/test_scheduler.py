"""Tests for the recurring scheduler (freegem.scheduler)."""

from __future__ import annotations

import sqlite3
import time
import unittest
from typing import Any

from freegem.crawler.scheduling import ChangePolicy, ensure_history_schema, record_fetch
from freegem.scheduler import SchedulerLoop, run_once

POLICY = ChangePolicy(base_interval_sec=100.0, min_interval_sec=10.0, max_interval_sec=1000.0)


def _state_db(path: str, urls_with_age: dict[str, float]) -> None:
    """Build a crawl-state DB the way the runner leaves it: frontier rows in
    status='done' (Frontier.mark_done's value) plus content_history
    observations."""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS frontier(
          url TEXT PRIMARY KEY, host TEXT NOT NULL, depth INTEGER NOT NULL DEFAULT 0,
          priority INTEGER NOT NULL DEFAULT 5, status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
          added_at REAL NOT NULL DEFAULT 0, fetched_at REAL, http_status INTEGER,
          error TEXT, etag TEXT, last_modified TEXT, content_hash TEXT
        );
        """
    )
    ensure_history_schema(conn)
    now = time.time()
    for url, age in urls_with_age.items():
        host = url.split("/")[2]
        conn.execute(
            "INSERT OR REPLACE INTO frontier(url, host, status, fetched_at, content_hash, etag)"
            " VALUES(?,?,?,?,?,?)",
            (url, host, "done", now - age, "h1", f"etag-{url}"),
        )
        record_fetch(conn, url, content_hash="h1", observed_at=now - age)
    conn.commit()
    conn.close()


class RunOnceTests(unittest.TestCase):
    def test_due_collection_and_plan_persistence(self) -> None:
        _state_db("/tmp/p3_sched_a.sqlite", {"https://a/1": 10_000.0, "https://a/2": 1.0})
        # a/2 was recently planned: not due for another 500s
        conn = sqlite3.connect("/tmp/p3_sched_a.sqlite")
        conn.execute(
            "UPDATE frontier SET next_attempt_at = ? WHERE url = 'https://a/2'",
            (time.time() + 500.0,),
        )
        conn.commit()
        conn.close()
        attempts: list[str] = []

        def runner(candidates: list[Any]) -> dict[str, bool]:
            attempts.extend(c.url for c in candidates)
            return {c.url: True for c in candidates}

        summary = run_once("/tmp/p3_sched_a.sqlite", runner, policy=POLICY)
        self.assertEqual(summary["due"], 1)  # only the stale url is due
        self.assertEqual(attempts, ["https://a/1"])
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["applied"], 1)
        # applied plan moves next_attempt_at forward: an immediate second tick finds nothing
        summary2 = run_once("/tmp/p3_sched_a.sqlite", runner, policy=POLICY)
        self.assertEqual(summary2["due"], 0)
        self.assertEqual(summary2["attempted"], 0)

    def test_without_runner_still_books(self) -> None:
        _state_db("/tmp/p3_sched_b.sqlite", {"https://b/1": 10_000.0})
        summary = run_once("/tmp/p3_sched_b.sqlite", None, policy=POLICY)
        self.assertEqual(summary["due"], 1)
        self.assertEqual(summary["attempted"], 0)
        self.assertEqual(summary["applied"], 1)  # due time moved even with no runner

    def test_runner_failure_does_not_hot_loop(self) -> None:
        _state_db("/tmp/p3_sched_c.sqlite", {"https://c/1": 10_000.0})

        def failing_runner(_candidates: list[Any]) -> dict[str, bool]:
            return {}  # runner failed wholesale

        summary = run_once("/tmp/p3_sched_c.sqlite", failing_runner, policy=POLICY)
        self.assertEqual(summary["due"], 1)
        self.assertEqual(summary["attempted"], 0)
        self.assertEqual(summary["applied"], 1)
        # next tick is not immediately due again: interval applies regardless
        summary2 = run_once("/tmp/p3_sched_c.sqlite", failing_runner, policy=POLICY)
        self.assertEqual(summary2["due"], 0)

    def test_limit_bounds_candidates(self) -> None:
        _state_db(
            "/tmp/p3_sched_d.sqlite",
            {f"https://d/{i}": 10_000.0 for i in range(10)},
        )
        summary = run_once("/tmp/p3_sched_d.sqlite", None, policy=POLICY, limit=3)
        self.assertEqual(summary["due"], 3)


class SchedulerLoopTests(unittest.TestCase):
    def test_loop_ticks_and_stops(self) -> None:
        _state_db("/tmp/p3_sched_e.sqlite", {"https://e/1": 10_000.0})
        loop = SchedulerLoop("/tmp/p3_sched_e.sqlite", None, every_sec=0.5, policy=POLICY)
        loop.start()
        deadline = time.time() + 5
        while loop.ticks < 1 and time.time() < deadline:
            time.sleep(0.05)
        loop.stop()
        self.assertGreaterEqual(loop.ticks, 1)
        self.assertIsNotNone(loop.last_summary)
        assert loop.last_summary is not None
        self.assertEqual(loop.last_summary["applied"], 1)
        self.assertLessEqual(loop.ticks, 12)  # no runaway hot loop

    def test_double_start_refused(self) -> None:
        loop = SchedulerLoop("/tmp/p3_sched_e.sqlite", None, every_sec=5.0)
        loop.start()
        with self.assertRaises(RuntimeError):
            loop.start()
        loop.stop()

    def test_survives_runner_exception(self) -> None:
        _state_db("/tmp/p3_sched_f.sqlite", {"https://f/1": 10_000.0})

        def exploding_runner(_c: list[Any]) -> dict[str, bool]:
            raise RuntimeError("boom")

        loop = SchedulerLoop(
            "/tmp/p3_sched_f.sqlite", exploding_runner, every_sec=0.3, policy=POLICY
        )
        loop.start()
        deadline = time.time() + 5
        while loop.ticks < 1 and time.time() < deadline:
            time.sleep(0.05)
        loop.stop()
        self.assertIn("error", (loop.last_summary or {}))


if __name__ == "__main__":
    unittest.main()
