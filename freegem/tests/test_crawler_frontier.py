"""The persistent frontier: dedup, claiming, retries, politeness, resume."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
import unittest

from freegem.crawler.frontier import PENDING, Frontier


class FrontierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "frontier.db")

    def test_push_dedup(self) -> None:
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        self.assertTrue(frontier.push("https://a.example/", host="a.example"))
        self.assertFalse(frontier.push("https://a.example/", host="a.example"))
        self.assertTrue(frontier.seen("https://a.example/"))
        self.assertFalse(frontier.seen("https://a.example/other"))

    def test_claim_respects_priority_then_age(self) -> None:
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        frontier.push("https://a.example/low", host="a", priority=9)
        frontier.push("https://a.example/high", host="a", priority=0)
        frontier.push("https://a.example/mid", host="a", priority=5)
        batch = frontier.claim(3)
        urls = [item.url for item in batch]
        self.assertEqual(
            urls, ["https://a.example/high", "https://a.example/mid", "https://a.example/low"]
        )
        # claimed items are inflight, not pending
        self.assertEqual(frontier.inflight_count(), 3)
        self.assertEqual(frontier.stats().queued, 0)

    def test_claim_leaves_future_retries(self) -> None:
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        frontier.push("https://a.example/x", host="a")
        with frontier._lock:
            frontier._db.execute(
                "UPDATE frontier SET next_attempt_at=? WHERE url=?",
                (2**31, "https://a.example/x"),
            )
            frontier._db.commit()
        self.assertEqual(frontier.claim(5), [])

    def test_done_and_stats(self) -> None:
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        frontier.push("https://a.example/ok", host="a.example")
        frontier.push("https://a.example/bad", host="a.example")
        batch = frontier.claim(2)
        self.assertEqual(len(batch), 2)
        frontier.mark_done(batch[0].url, http_status=200, etag='"e1"', content_hash="abc123")
        frontier.mark_error(batch[1].url, "HTTP 404", retry=False)
        stats = frontier.stats()
        self.assertEqual(stats.by_status.get("done"), 1)
        self.assertEqual(stats.by_status.get("error"), 1)
        # the done row keeps its recorded validators for conditional refetch
        self.assertTrue(frontier.seen("https://a.example/ok"))

    def test_error_retry_backoff(self) -> None:
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        frontier.push("https://a.example/boom", host="a.example")
        (item,) = frontier.claim(1)
        will_retry = frontier.mark_error(
            item.url, "timeout", retry=True, max_retries=2, backoff_sec=10
        )
        self.assertTrue(will_retry)
        stats = frontier.stats()
        self.assertEqual(stats.by_status.get(PENDING), 1)
        # attempts recorded; next_attempt scheduled in the future
        with frontier._lock:
            row = frontier._db.execute(
                "SELECT attempts, next_attempt_at FROM frontier WHERE url=?",
                (item.url,),
            ).fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertGreater(row["next_attempt_at"], 0)

    def test_retry_after_is_honored(self) -> None:
        """A 429 with Retry-After schedules the retry when the server said."""
        import time as _time

        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        frontier.push("https://a.example/rate-limited", host="a.example")
        (item,) = frontier.claim(1)
        frontier.mark_error(
            item.url, "HTTP 429", retry=True, max_retries=2, retry_after=30.0, backoff_sec=5.0
        )
        with frontier._lock:
            row = frontier._db.execute(
                "SELECT next_attempt_at FROM frontier WHERE url=?", (item.url,)
            ).fetchone()
        delay = row["next_attempt_at"] - _time.time()
        self.assertGreater(delay, 25.0)  # the server's 30s wins over 2.5-7.5s backoff

    def test_error_gives_up_after_max(self) -> None:
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        frontier.push("https://a.example/boom", host="a.example")
        future = time.time() + 3600
        for _ in range(3):
            (item,) = frontier.claim(1, now=future)
            will_retry = frontier.mark_error(item.url, "timeout", retry=True, max_retries=2)
            if not will_retry:
                break
        self.assertEqual(frontier.stats().by_status.get("error"), 1)

    def test_skip(self) -> None:
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        frontier.push("https://a.example/x", host="a.example")
        (item,) = frontier.claim(1)
        frontier.mark_skipped(item.url, "robots_disallow")
        stats = frontier.stats()
        self.assertEqual(stats.by_status.get("skipped"), 1)

    def test_resume_state_persists(self) -> None:
        first = Frontier(self.path)
        first.push("https://a.example/persist", host="a.example")
        (item,) = first.claim(1)
        first.mark_done(item.url, http_status=200)
        first.checkpoint()
        first.close()
        # a new process reopens the same file
        second = Frontier(self.path)
        self.addCleanup(second.close)
        self.assertTrue(second.seen("https://a.example/persist"))
        self.assertEqual(second.stats().by_status.get("done"), 1)
        self.assertEqual(second.abandon_inflight(), 0)

    def test_abandon_inflight_recovers_crash(self) -> None:
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        frontier.push("https://a.example/crash", host="a.example")
        frontier.claim(1)
        self.assertEqual(frontier.inflight_count(), 1)
        self.assertEqual(frontier.abandon_inflight(), 1)
        self.assertEqual(frontier.stats().by_status.get(PENDING), 1)

    def test_host_politeness(self) -> None:
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        self.assertEqual(frontier.host_ready("a.example", delay_sec=1.0), 0.0)
        frontier.touch_host("a.example")
        wait = frontier.host_ready("a.example", delay_sec=1.0)
        self.assertGreater(wait, 0.9)
        self.assertLessEqual(wait, 1.0)
        self.assertEqual(frontier.host_ready("b.example", delay_sec=1.0), 0.0)

    def test_meta_roundtrip(self) -> None:
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        frontier.set_meta("seeds", '["https://a/"]')
        self.assertEqual(frontier.get_meta("seeds"), '["https://a/"]')
        self.assertIsNone(frontier.get_meta("missing"))

    def test_thread_safety_under_contention(self) -> None:
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                for i in range(20):
                    frontier.push(f"https://h{i % 5}.example/{n}-{i}", host=f"h{i % 5}.example")
                    frontier.claim(2)
                    frontier.touch_host(f"h{i % 5}.example")
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_db_file_is_valid_sqlite(self) -> None:
        frontier = Frontier(self.path)
        frontier.push("https://a.example/", host="a.example")
        frontier.close()
        conn = sqlite3.connect(self.path)
        rows = conn.execute("SELECT COUNT(*) FROM frontier").fetchone()
        conn.close()
        self.assertEqual(rows[0], 1)


    def test_error_count_recorded_without_prior_host_touch(self) -> None:
        """mark_error/mark_done must create the host_state row themselves.

        The counters used to be a plain UPDATE ... WHERE host=(subquery),
        which matches zero rows when the host was never touched -- the
        failure silently vanished from stats().top_hosts.
        """
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        frontier.push("https://stats.example/err", host="stats.example")
        (item,) = frontier.claim(1)
        self.assertFalse(frontier.mark_error(item.url, "boom", retry=False))
        with frontier._lock:
            row = frontier._db.execute(
                "SELECT err_count FROM host_state WHERE host=?", ("stats.example",)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["err_count"], 1)
        self.assertEqual(frontier.stats().top_hosts, [("stats.example", 1)])

    def test_ok_count_recorded_without_prior_host_touch(self) -> None:
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        frontier.push("https://stats.example/ok", host="stats.example")
        (item,) = frontier.claim(1)
        frontier.mark_done(item.url, http_status=200)
        with frontier._lock:
            row = frontier._db.execute(
                "SELECT ok_count FROM host_state WHERE host=?", ("stats.example",)
            ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["ok_count"], 1)

    def test_host_counters_accumulate_across_fetches(self) -> None:
        frontier = Frontier(self.path)
        self.addCleanup(frontier.close)
        frontier.push("https://h.example/a", host="h.example")
        frontier.push("https://h.example/b", host="h.example")
        for item in frontier.claim(2):
            frontier.mark_done(item.url, http_status=200)
        frontier.push("https://h.example/c", host="h.example")
        (item,) = frontier.claim(1)
        frontier.mark_error(item.url, "HTTP 500", retry=False)
        with frontier._lock:
            row = frontier._db.execute(
                "SELECT ok_count, err_count FROM host_state WHERE host=?", ("h.example",)
            ).fetchone()
        self.assertEqual((row["ok_count"], row["err_count"]), (2, 1))
        self.assertEqual(frontier.stats().top_hosts, [("h.example", 1)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
