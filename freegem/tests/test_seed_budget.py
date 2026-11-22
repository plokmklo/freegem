"""Per-seed wall-clock budget for the resilient ladder (issue #103 evidence).

``max_attempts`` bounds how many rungs a ladder walks but not how long they
take. A host whose A records all silently drop SYNs costs
``attempts x A-records x timeout`` per seed -- a parked domain with six
blackhole IPs measured ~10 minutes per seed live on the 10k-site run (and
the campaign's executor join waited on it, so ONE seed stalled the whole
run). ``seed_budget_sec`` cuts the walk at a wall-clock deadline; the last
attempt's honest outcome stands with "(seed budget)" appended to its error,
so kind/status taxonomy buckets do not move.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from typing import Any
from unittest import mock

from freegem.config import BridgeConfig
from freegem.crawler.campaign import run_campaign
from freegem.crawler.fetch import FetchOutcome
from freegem.crawler.resilient import fetch_resilient


def _slow_timeout(url: str, sleep_sec: float = 0.6) -> FetchOutcome:
    return FetchOutcome(url=url, kind="timeout", status=None, error="read timed out")


def _sleeping_attempt(
    cfg: Any, url: str, base: Any, state: Any, *args: Any, **kw: Any
) -> FetchOutcome:
    """A fake ladder rung: burns real time and keeps failing."""
    time.sleep(0.6)
    state.attempts += 1
    return _slow_timeout(url)


class SeedBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = BridgeConfig(guard=True, log_requests=False)

    def test_budget_cuts_the_walk_and_marks_the_error(self) -> None:
        with mock.patch("freegem.crawler.resilient._attempt", side_effect=_sleeping_attempt):
            started = time.monotonic()
            outcome = fetch_resilient(self.cfg, "https://blackhole.test/", seed_budget_sec=1.5)
            elapsed = time.monotonic() - started
        # many rungs would mean >= 9.6s at 0.6s/rung; the budget stops it fast
        self.assertLess(elapsed, 5.0, "the seed budget did not stop the ladder walk")
        self.assertGreaterEqual(elapsed, 1.5, "the budget cut fired too early")
        self.assertEqual(outcome.kind, "timeout", "kind must stay the honest bucket")
        self.assertIsNone(outcome.status)
        self.assertIn("(seed budget)", outcome.error or "", "the cut must be visible")
        self.assertGreaterEqual(outcome.attempts, 1)

    def test_budget_off_walks_the_ladder_as_before(self) -> None:
        with mock.patch("freegem.crawler.resilient._attempt", side_effect=_sleeping_attempt):
            outcome = fetch_resilient(
                self.cfg, "https://slow.test/", max_attempts=3, seed_budget_sec=0.0
            )
        self.assertEqual(outcome.attempts, 3, "default must preserve the ladder")
        self.assertNotIn("(seed budget)", outcome.error or "")

    def test_budget_never_cuts_a_successful_first_attempt(self) -> None:
        def ok_attempt(
            cfg: Any, url: str, base: Any, state: Any, *a: Any, **kw: Any
        ) -> FetchOutcome:
            state.attempts += 1
            time.sleep(0.3)
            return FetchOutcome(url=url, kind="ok", status=200, body=b"<p>hi</p>", body_size=9)

        with mock.patch("freegem.crawler.resilient._attempt", side_effect=ok_attempt):
            outcome = fetch_resilient(self.cfg, "https://fine.test/", seed_budget_sec=0.1)
        self.assertEqual(outcome.kind, "ok")
        self.assertEqual(outcome.status, 200)
        self.assertNotIn("(seed budget)", outcome.error or "", "happy outcomes stay clean")


class CampaignPlumbingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.db")
        self.manifest_path = os.path.join(self.tmp.name, "ckpt.json")
        self.seeds = [{"url": "https://blackhole.test/", "category": "t"}]

    def test_seed_budget_reaches_the_ladder_and_the_manifest(self) -> None:
        seen_kwargs: dict[str, Any] = {}

        def stub(cfg: Any, url: str, **kw: Any) -> FetchOutcome:
            seen_kwargs.update(kw)
            return FetchOutcome(url=url, kind="timeout", status=None, error="timed out")

        with mock.patch("freegem.crawler.campaign.fetch_resilient", side_effect=stub):
            run_campaign(
                BridgeConfig(guard=True, log_requests=False),
                self.seeds,
                state_path=self.state,
                concurrency=1,
                host_delay_sec=0.0,
                checkpoint_path=self.manifest_path,
                seed_budget_sec=1.5,
            )
        self.assertEqual(seen_kwargs.get("seed_budget_sec"), 1.5, "CLI value must reach the ladder")
        from freegem.crawler.checkpoint import read_checkpoint

        manifest = read_checkpoint(self.manifest_path)
        self.assertEqual(manifest["params"]["seed_budget_sec"], 1.5)
        db = sqlite3.connect(self.state)
        self.addCleanup(db.close)
        self.assertEqual(
            db.execute("select kind from results").fetchone()[0],
            "timeout",
            "a budgeted blackhole seed still records its honest kind",
        )

    def test_default_leaves_the_budget_off_in_the_manifest(self) -> None:
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: FetchOutcome(
                url=url, kind="ok", status=200, body=b"x", body_size=1
            ),
        ):
            run_campaign(
                BridgeConfig(guard=True, log_requests=False),
                self.seeds,
                state_path=self.state,
                concurrency=1,
                host_delay_sec=0.0,
                checkpoint_path=self.manifest_path,
            )
        from freegem.crawler.checkpoint import read_checkpoint

        self.assertEqual(read_checkpoint(self.manifest_path)["params"]["seed_budget_sec"], 0.0)


if __name__ == "__main__":
    unittest.main()
