"""Tests for freegem.metrics: percentiles, windows, run metrics, counters."""

from __future__ import annotations

import unittest

from freegem import metrics as metrics_mod
from freegem.metrics import (
    DEFAULT_LATENCY_WINDOW,
    LatencyWindow,
    RunMetrics,
    ServerMetrics,
    gemini_retries_total,
    get_audit_logs,
    memory_rss_kb,
    new_request_id,
    note_gemini_retry,
    percentile,
    record_audit_event,
)


class PercentileTests(unittest.TestCase):
    def test_empty_is_zero(self) -> None:
        self.assertEqual(percentile([], 95), 0.0)

    def test_nearest_rank_is_an_observed_value(self) -> None:
        # 20 samples 1..20: p95 rank = ceil(0.95*20)=19 -> 19, not 18.05.
        samples = [float(n) for n in range(1, 21)]
        self.assertEqual(percentile(samples, 95), 19.0)
        self.assertEqual(percentile(samples, 50), 10.0)

    def test_single_sample(self) -> None:
        self.assertEqual(percentile([7.5], 99), 7.5)

    def test_unsorted_input_is_sorted(self) -> None:
        self.assertEqual(percentile([9.0, 1.0, 5.0], 50), 5.0)

    def test_p100_is_the_max(self) -> None:
        self.assertEqual(percentile([3.0, 1.0, 2.0], 100), 3.0)


class LatencyWindowTests(unittest.TestCase):
    def test_window_is_bounded(self) -> None:
        window = LatencyWindow(maxlen=3)
        for value in (1.0, 2.0, 3.0, 4.0, 5.0):
            window.record(value)
        self.assertEqual(len(window), 3)
        snapshot = window.snapshot()
        # Only the newest three survive: 3, 4, 5.
        self.assertEqual(snapshot["count"], 3)
        self.assertEqual(snapshot["p50_sec"], 4.0)
        self.assertEqual(snapshot["p99_sec"], 5.0)

    def test_negative_durations_are_refused(self) -> None:
        window = LatencyWindow()
        window.record(-1.0)
        self.assertEqual(len(window), 0)

    def test_empty_snapshot(self) -> None:
        snapshot = LatencyWindow().snapshot()
        self.assertEqual(snapshot["count"], 0)
        self.assertEqual(snapshot["p50_sec"], 0.0)
        self.assertEqual(snapshot["p95_sec"], 0.0)
        self.assertEqual(snapshot["p99_sec"], 0.0)

    def test_default_window_size(self) -> None:
        window = LatencyWindow()
        for i in range(DEFAULT_LATENCY_WINDOW + 50):
            window.record(float(i))
        self.assertEqual(len(window), DEFAULT_LATENCY_WINDOW)


class RunMetricsTests(unittest.TestCase):
    def test_stage_accumulates_and_snapshot_is_json_ready(self) -> None:
        metrics = RunMetrics()
        with metrics.stage("fetch"):
            pass
        with metrics.stage("fetch"):
            pass
        with metrics.stage("generate"):
            pass
        metrics.bump("fetch_ok", 2)
        metrics.bump("fetch_ok")
        snapshot = metrics.snapshot()
        self.assertEqual(set(snapshot["stages"]), {"fetch", "generate"})
        self.assertEqual(snapshot["counters"]["fetch_ok"], 3)
        self.assertGreaterEqual(snapshot["elapsed_sec"], 0.0)
        self.assertIsInstance(snapshot["request_id"], str)
        self.assertTrue(len(snapshot["request_id"]) == 12)

    def test_unknown_stage_names_are_fine(self) -> None:
        metrics = RunMetrics()
        with metrics.stage("discover"):
            pass
        self.assertIn("discover", metrics.snapshot()["stages"])

    def test_request_ids_are_unique(self) -> None:
        self.assertNotEqual(new_request_id(), new_request_id())


class ServerMetricsTests(unittest.TestCase):
    def test_status_counts_and_refusals(self) -> None:
        metrics = ServerMetrics()
        metrics.note_status(200)
        metrics.note_status(200)
        metrics.note_status(404)
        metrics.note_status(503)
        metrics.note_refusal()
        metrics.note_refusal()
        metrics.note_refusal()
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["requests_total"], 4)
        self.assertEqual(snapshot["by_status"]["200"], 2)
        self.assertEqual(snapshot["by_status"]["404"], 1)
        self.assertEqual(snapshot["queue_refusals"], 3)
        self.assertGreaterEqual(snapshot["uptime_sec"], 0.0)
        self.assertIn("latency", snapshot)

    def test_latency_flows_through(self) -> None:
        metrics = ServerMetrics()
        metrics.latency.record(0.5)
        metrics.note_status(200)
        snapshot = metrics.snapshot()
        self.assertEqual(snapshot["latency"]["count"], 1)
        self.assertEqual(snapshot["latency"]["p50_sec"], 0.5)


class RetryCounterTests(unittest.TestCase):
    def test_note_gemini_retry_accumulates(self) -> None:
        before = gemini_retries_total()
        note_gemini_retry()
        note_gemini_retry()
        self.assertEqual(gemini_retries_total(), before + 2)


class MemoryTests(unittest.TestCase):
    def test_memory_rss_kb_is_sane_or_none(self) -> None:
        rss = memory_rss_kb()
        if rss is not None:
            # A running Python process has at least a few MiB resident and
            # is not going to report petabytes.
            self.assertGreater(rss, 1_000)
            self.assertLess(rss, 100_000_000)


class AuditLogTests(unittest.TestCase):
    """record_audit_event / get_audit_logs: bounded global log."""

    def setUp(self) -> None:
        self._saved = list(metrics_mod._AUDIT_LOGS)
        metrics_mod._AUDIT_LOGS.clear()

    def tearDown(self) -> None:
        metrics_mod._AUDIT_LOGS[:] = self._saved

    def test_limit_zero_returns_no_logs(self) -> None:
        record_audit_event("action", "127.0.0.1", "req-1")
        self.assertEqual(get_audit_logs(0), [])
        self.assertEqual(get_audit_logs(-5), [])
        self.assertEqual(len(get_audit_logs(10)), 1)

    def test_recent_logs_keep_insertion_order(self) -> None:
        record_audit_event("a", "127.0.0.1", "r1")
        record_audit_event("b", "127.0.0.1", "r2")
        logs = get_audit_logs(10)
        self.assertEqual([e["request_id"] for e in logs], ["r1", "r2"])

    def test_log_is_capped_at_a_thousand_events(self) -> None:
        for i in range(1005):
            record_audit_event("action", "127.0.0.1", f"r{i}")
        self.assertEqual(len(metrics_mod._AUDIT_LOGS), 1000)
        # oldest trimmed first, newest survives
        self.assertEqual(get_audit_logs(1)[0]["request_id"], "r1004")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
