"""Campaign checkpoint manifest: interrupt, verify, resume (issue #93).

The acceptance contract, pinned hermetically:

- interrupting a campaign produces a recoverable manifest (format, digests,
  counters, per-row outcomes);
- resuming re-fetches exactly the seeds WITHOUT an ``ok`` outcome --
  attempted-and-failed AND never-attempted (the LEFT JOIN contract: a
  results-only query would silently lose queued URLs) -- and never
  re-processes completed work;
- corrupt, stale, version-incompatible, or edited manifests fail clearly
  with named disagreements, and a refused resume leaves the state db
  byte-identical (verification runs before any write);
- the manifest is a LOWER BOUND: outcomes added to the state after the
  manifest was written are progress (a killed resume), not tampering --
  that asymmetry is what makes interrupt-during-resume itself resumable;
- the CLI round-trips the whole thing and exits 2 with one clear JSON line
  on refusal instead of a traceback.

No test touches the network: ``fetch_resilient`` is mocked, outcomes are
hand-built ``FetchOutcome`` values.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from typing import Any
from unittest import mock

from freegem.config import BridgeConfig
from freegem.crawler.campaign import _cli, run_campaign
from freegem.crawler.checkpoint import (
    CHECKPOINT_FORMAT,
    CheckpointError,
    queue_digest,
    read_checkpoint,
    results_digest,
    seeds_digest,
    verify_checkpoint,
    write_checkpoint,
)
from freegem.crawler.fetch import FetchOutcome


def _ok(url: str) -> FetchOutcome:
    return FetchOutcome(url=url, kind="ok", status=200, body=b"<html>fine</html>", body_size=17)


def _timeout(url: str) -> FetchOutcome:
    return FetchOutcome(url=url, kind="timeout", status=None, error="read timed out")


class CheckpointTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.db")
        self.manifest_path = os.path.join(self.tmp.name, "campaign.ckpt.json")
        self.report_path = os.path.join(self.tmp.name, "report.json")
        self.events: list[dict[str, Any]] = []
        self.seeds = [
            {"url": "https://a.test/", "category": "t"},
            {"url": "https://b.test/", "category": "t"},
            {"url": "https://c.test/", "category": "t"},
        ]

    def _run(self, seeds: list[dict[str, str]], **kwargs: Any) -> Any:
        return run_campaign(
            BridgeConfig(guard=True, log_requests=False),
            seeds,
            state_path=self.state,
            concurrency=1,
            host_delay_sec=0.0,
            on_event=self.events.append,
            **kwargs,
        )

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.state)
        self.addCleanup(db.close)
        return db

    def _manifest(self) -> dict[str, Any]:
        return read_checkpoint(self.manifest_path)


class CheckpointWriteTests(CheckpointTestCase):
    def test_manifest_written_after_clean_run(self) -> None:
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        manifest = self._manifest()
        self.assertEqual(manifest["format"], CHECKPOINT_FORMAT)
        self.assertFalse(manifest["interrupted"])
        self.assertIsNone(manifest["resumed_from"])
        self.assertEqual(manifest["seeds_count"], 3)
        self.assertEqual(manifest["seeds_digest"], seeds_digest(self.seeds))
        db = self._db()
        self.assertEqual(manifest["state"]["results_count"], 3)
        self.assertEqual(manifest["state"]["results_digest"], results_digest(db))
        self.assertEqual(manifest["state"]["queue_digest"], queue_digest(db))
        self.assertEqual(manifest["state"]["results_rows"]["https://a.test/"], ["ok", 200])
        self.assertEqual(
            manifest["params"],
            {
                "concurrency": 1,
                "limit": 1000,
                "timeout_sec": 20.0,
                "host_delay_sec": 0.0,
                "retry_failed": False,
                "distill": False,
                "max_attempts": 16,
                "seed_budget_sec": 0.0,
            },
        )
        self.assertIn("attempted", manifest["counters"])
        # atomic write: no temp files left behind
        self.assertEqual(
            [f for f in os.listdir(self.tmp.name) if f.startswith(".freegem-ckpt-")], []
        )

    def test_checkpoint_event_and_report_summary(self) -> None:
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            self._run(self.seeds, checkpoint_path=self.manifest_path, report_path=self.report_path)
        written = [e for e in self.events if e.get("event") == "checkpoint_written"]
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["path"], self.manifest_path)
        self.assertFalse(written[0]["interrupted"])
        self.assertEqual(written[0]["results_count"], 3)
        with open(self.report_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["checkpoint"]["path"], self.manifest_path)
        self.assertFalse(payload["checkpoint"]["interrupted"])
        self.assertFalse(payload["checkpoint"]["resumed"])
        self.assertEqual(payload["checkpoint"]["seeds_digest"], seeds_digest(self.seeds))

    def test_no_checkpoint_by_default(self) -> None:
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            self._run(self.seeds, report_path=self.report_path)
        self.assertFalse(os.path.exists(self.manifest_path))
        self.assertEqual(
            [f for f in os.listdir(self.tmp.name) if "ckpt" in f], [], "stray checkpoint file"
        )
        with open(self.report_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertNotIn("checkpoint", payload)
        self.assertEqual([e for e in self.events if e.get("event") == "checkpoint_written"], [])


class InterruptAndResumeTests(CheckpointTestCase):
    def test_interrupt_writes_recoverable_manifest(self) -> None:
        calls: list[str] = []

        def fake_fetch(cfg: BridgeConfig, url: str, **kwargs: object) -> FetchOutcome:
            calls.append(url)
            if url == "https://b.test/":
                return _timeout(url)
            if url == "https://c.test/":
                # last seed: interrupting the tail makes the call sequence
                # deterministic (an upfront-submitted pool can start one
                # more task before cancel is observed -- that is harmless,
                # recorded work, and this test pins the recoverable state)
                raise KeyboardInterrupt("simulated SIGINT")
            return _ok(url)

        with mock.patch("freegem.crawler.campaign.fetch_resilient", side_effect=fake_fetch):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        self.assertEqual(calls, ["https://a.test/", "https://b.test/", "https://c.test/"])
        manifest = self._manifest()
        self.assertTrue(manifest["interrupted"])
        self.assertEqual(manifest["counters"]["attempted"], 2)
        self.assertEqual(manifest["state"]["results_count"], 2)
        self.assertEqual(manifest["state"]["queue_count"], 3)
        db = self._db()
        results = dict(db.execute("SELECT url, kind FROM results").fetchall())
        queued = {row[0] for row in db.execute("SELECT url FROM queue")}
        self.assertEqual(results, {"https://a.test/": "ok", "https://b.test/": "timeout"})
        self.assertEqual(
            queued,
            {"https://a.test/", "https://b.test/", "https://c.test/"},
            "the interrupted seed must still be queued, not lost",
        )

    def test_resume_skips_completed_and_covers_queued_and_failed(self) -> None:
        calls: list[str] = []

        def first(cfg: BridgeConfig, url: str, **kwargs: object) -> FetchOutcome:
            calls.append(url)
            if url == "https://b.test/":
                return _timeout(url)
            if url == "https://c.test/":
                raise KeyboardInterrupt("simulated SIGINT")
            return _ok(url)

        with mock.patch("freegem.crawler.campaign.fetch_resilient", side_effect=first):
            self._run(self.seeds, checkpoint_path=self.manifest_path, report_path=self.report_path)
        self.assertEqual(calls, ["https://a.test/", "https://b.test/", "https://c.test/"])
        calls.clear()

        def second(cfg: BridgeConfig, url: str, **kwargs: object) -> FetchOutcome:
            calls.append(url)
            return _ok(url)

        with mock.patch("freegem.crawler.campaign.fetch_resilient", side_effect=second):
            self._run(
                self.seeds,
                resume_checkpoint=self.manifest_path,
                report_path=self.report_path,
            )
        # exactly the seeds without an ok outcome: the interrupted one AND
        # the never-attempted one; the completed seed is NOT re-fetched
        self.assertEqual(calls, ["https://b.test/", "https://c.test/"])
        manifest = self._manifest()
        self.assertFalse(manifest["interrupted"])
        self.assertEqual(manifest["state"]["results_count"], 3)
        self.assertIsNotNone(manifest["resumed_from"])
        self.assertEqual(manifest["resumed_from"]["attempted_before"], 2)
        self.assertTrue(manifest["resumed_from"]["interrupted_before"])
        with open(self.report_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertTrue(payload["checkpoint"]["resumed"])
        db = self._db()
        kinds = dict(db.execute("SELECT url, kind FROM results").fetchall())
        self.assertEqual(set(kinds.values()), {"ok"})

    def test_resume_completed_campaign_is_noop(self) -> None:
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        calls: list[str] = []

        def counting(cfg: BridgeConfig, url: str, **kwargs: object) -> FetchOutcome:
            calls.append(url)
            return _ok(url)

        with mock.patch("freegem.crawler.campaign.fetch_resilient", side_effect=counting):
            self._run(self.seeds, resume_checkpoint=self.manifest_path)
        self.assertEqual(calls, [], "resume of a completed campaign re-fetched seeds")
        manifest = self._manifest()
        self.assertFalse(manifest["interrupted"])
        self.assertEqual(manifest["state"]["results_count"], 3)

    def test_manifest_is_lower_bound_progress_allowed(self) -> None:
        """A killed resume advanced the state past the manifest: that is
        progress, not tampering, and the next resume must still work."""

        def first(cfg: BridgeConfig, url: str, **kwargs: object) -> FetchOutcome:
            if url == "https://b.test/":
                return _timeout(url)
            if url == "https://c.test/":
                raise KeyboardInterrupt("simulated SIGINT before c finished")
            return _ok(url)

        with mock.patch("freegem.crawler.campaign.fetch_resilient", side_effect=first):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        manifest = self._manifest()
        self.assertTrue(manifest["interrupted"])
        # simulate a killed resume: c was fetched ok, then the process died
        # before the manifest could be rewritten
        db = self._db()
        with db:
            db.execute(
                "INSERT INTO results(url, category, kind, status) VALUES(?,?,?,?)",
                ("https://c.test/", "t", "ok", 200),
            )
        db.close()
        calls: list[str] = []

        def second(cfg: BridgeConfig, url: str, **kwargs: object) -> FetchOutcome:
            calls.append(url)
            return _ok(url)

        with mock.patch("freegem.crawler.campaign.fetch_resilient", side_effect=second):
            self._run(self.seeds, resume_checkpoint=self.manifest_path)
        self.assertEqual(calls, ["https://b.test/"], "only the failed seed is re-fetched")
        manifest = self._manifest()
        self.assertEqual(manifest["state"]["results_count"], 3)


class RefusalTests(CheckpointTestCase):
    def test_refused_resume_does_not_lock_state_file(self) -> None:
        """A refused resume must close the state db handle before raising:
        an open handle locks the file on Windows (WinError 32 on any later
        unlink/move -- caught by CI on 8a20128)."""
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        with self.assertRaisesRegex(CheckpointError, "seeds digest mismatch"):
            with mock.patch(
                "freegem.crawler.campaign.fetch_resilient",
                side_effect=lambda cfg, url, **kw: _ok(url),
            ):
                self._run(other_seeds(), resume_checkpoint=self.manifest_path)
        # no handle is held: the state file is immediately replaceable
        moved = self.state + ".moved"
        os.replace(self.state, moved)
        self.assertTrue(os.path.exists(moved))

    def test_resume_with_different_seeds_refused(self) -> None:
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        other = [{"url": "https://other.test/", "category": "x"}]
        with self.assertRaisesRegex(CheckpointError, "seeds digest mismatch"):
            with mock.patch(
                "freegem.crawler.campaign.fetch_resilient",
                side_effect=lambda cfg, url, **kw: _ok(url),
            ):
                self._run(other, resume_checkpoint=self.manifest_path)

    def test_resume_with_tampered_state_refused_and_state_untouched(self) -> None:
        def first(cfg: BridgeConfig, url: str, **kwargs: object) -> FetchOutcome:
            if url == "https://b.test/":
                return _timeout(url)
            return _ok(url)

        with mock.patch("freegem.crawler.campaign.fetch_resilient", side_effect=first):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        db = self._db()
        with db:
            db.execute("UPDATE results SET kind = 'ok' WHERE url = 'https://b.test/'")
        db.close()
        tampered_digest = results_digest(self._db())
        with self.assertRaisesRegex(CheckpointError, "state digest drift"):
            with mock.patch(
                "freegem.crawler.campaign.fetch_resilient",
                side_effect=lambda cfg, url, **kw: _ok(url),
            ):
                self._run(self.seeds, resume_checkpoint=self.manifest_path)
        # the refusal did not touch the (already tampered) state either
        self.assertEqual(results_digest(self._db()), tampered_digest)

    def test_resume_with_tampered_queue_refused(self) -> None:
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        db = self._db()
        with db:
            db.execute("INSERT INTO queue(url, category) VALUES('https://extra.test/', 'x')")
        db.close()
        with self.assertRaisesRegex(CheckpointError, "queue digest mismatch"):
            with mock.patch(
                "freegem.crawler.campaign.fetch_resilient",
                side_effect=lambda cfg, url, **kw: _ok(url),
            ):
                self._run(self.seeds, resume_checkpoint=self.manifest_path)

    def test_resume_with_corrupt_manifest_refused(self) -> None:
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            fh.write("not json {{{")
        with self.assertRaisesRegex(CheckpointError, "not valid JSON"):
            with mock.patch(
                "freegem.crawler.campaign.fetch_resilient",
                side_effect=lambda cfg, url, **kw: _ok(url),
            ):
                self._run(self.seeds, resume_checkpoint=self.manifest_path)

    def test_resume_with_unknown_format_refused(self) -> None:
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        manifest = self._manifest()
        manifest["format"] = "freegem-campaign-checkpoint/9.9"
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        with self.assertRaisesRegex(CheckpointError, "format mismatch"):
            with mock.patch(
                "freegem.crawler.campaign.fetch_resilient",
                side_effect=lambda cfg, url, **kw: _ok(url),
            ):
                self._run(self.seeds, resume_checkpoint=self.manifest_path)

    def test_resume_with_missing_manifest_refused(self) -> None:
        with self.assertRaisesRegex(CheckpointError, "unreadable"):
            with mock.patch(
                "freegem.crawler.campaign.fetch_resilient",
                side_effect=lambda cfg, url, **kw: _ok(url),
            ):
                self._run(self.seeds, resume_checkpoint=self.manifest_path)
        self.assertFalse(os.path.exists(self.manifest_path), "refusal must not create a manifest")

    def test_resume_against_fresh_state_refused(self) -> None:
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        os.unlink(self.state)  # the state db is gone; a fresh one gets built
        with self.assertRaisesRegex(CheckpointError, "queue digest mismatch"):
            with mock.patch(
                "freegem.crawler.campaign.fetch_resilient",
                side_effect=lambda cfg, url, **kw: _ok(url),
            ):
                self._run(self.seeds, resume_checkpoint=self.manifest_path)

    def test_manifest_self_integrity_refused(self) -> None:
        """Hand-editing results_rows without recomputing the digest is caught."""

        def first(cfg: BridgeConfig, url: str, **kwargs: object) -> FetchOutcome:
            if url == "https://b.test/":
                return _timeout(url)
            return _ok(url)

        with mock.patch("freegem.crawler.campaign.fetch_resilient", side_effect=first):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        manifest = self._manifest()
        manifest["state"]["results_rows"]["https://b.test/"] = ["ok", 200]
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)
        with self.assertRaisesRegex(CheckpointError, "self-integrity"):
            with mock.patch(
                "freegem.crawler.campaign.fetch_resilient",
                side_effect=lambda cfg, url, **kw: _ok(url),
            ):
                self._run(self.seeds, resume_checkpoint=self.manifest_path)


class DigestUnitTests(CheckpointTestCase):
    def test_seeds_digest_is_order_independent(self) -> None:
        shuffled = list(reversed(self.seeds))
        self.assertEqual(seeds_digest(self.seeds), seeds_digest(shuffled))
        self.assertNotEqual(seeds_digest(self.seeds), seeds_digest(other_seeds()))

    def test_results_digest_stable_across_connections(self) -> None:
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        first = results_digest(self._db())
        second = results_digest(self._db())  # a fresh connection, same file
        self.assertEqual(first, second)

    def test_results_digest_reflects_kind_change(self) -> None:
        def first(cfg: BridgeConfig, url: str, **kwargs: object) -> FetchOutcome:
            if url == "https://b.test/":
                return _timeout(url)
            return _ok(url)

        with mock.patch("freegem.crawler.campaign.fetch_resilient", side_effect=first):
            self._run(self.seeds)
        db = self._db()
        before = results_digest(db)
        with db:
            db.execute("UPDATE results SET kind = 'ok' WHERE url = 'https://b.test/'")
        self.assertNotEqual(before, results_digest(db))


def other_seeds() -> list[dict[str, str]]:
    return [{"url": "https://other.test/", "category": "x"}]


class CheckpointCliTests(CheckpointTestCase):
    def _seeds_file(self) -> str:
        path = os.path.join(self.tmp.name, "seeds.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for seed in self.seeds:
                fh.write(json.dumps(seed) + "\n")
        return path

    def test_cli_checkpoint_then_resume_roundtrip(self) -> None:
        seeds_path = self._seeds_file()
        base = [
            "run",
            "--seeds",
            seeds_path,
            "--state",
            self.state,
            "--concurrency",
            "1",
        ]
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            rc = _cli([*base, "--checkpoint", self.manifest_path, "--report", self.report_path])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.manifest_path))
        with open(self.report_path, encoding="utf-8") as fh:
            first_payload = json.load(fh)
        self.assertFalse(first_payload["checkpoint"]["resumed"])

        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            rc = _cli([*base, "--resume", self.manifest_path, "--report", self.report_path])
        self.assertEqual(rc, 0)
        with open(self.report_path, encoding="utf-8") as fh:
            second_payload = json.load(fh)
        self.assertTrue(second_payload["checkpoint"]["resumed"])
        manifest = read_checkpoint(self.manifest_path)
        self.assertFalse(manifest["interrupted"])

    def test_cli_refused_resume_exits_2_with_clear_line(self) -> None:
        seeds_path = self._seeds_file()
        with open(self.manifest_path, "w", encoding="utf-8") as fh:
            fh.write("corrupt")
        stdout = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            mock.patch(
                "freegem.crawler.campaign.fetch_resilient",
                side_effect=lambda cfg, url, **kw: _ok(url),
            ),
        ):
            rc = _cli(
                [
                    "run",
                    "--seeds",
                    seeds_path,
                    "--state",
                    self.state,
                    "--resume",
                    self.manifest_path,
                ]
            )
        self.assertEqual(rc, 2)
        line = json.loads(stdout.getvalue().strip().splitlines()[-1])
        self.assertEqual(line["error"], "checkpoint_refused")
        self.assertIn("not valid JSON", line["detail"])


class VerifyCheckpointUnitTests(CheckpointTestCase):
    def test_verify_accepts_matching_manifest(self) -> None:
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        manifest = self._manifest()
        db = self._db()
        verify_checkpoint(manifest, seeds=self.seeds, db=db)  # must not raise

    def test_verify_rejects_non_dict_manifest(self) -> None:
        with self.assertRaisesRegex(CheckpointError, "JSON object"):
            verify_checkpoint(["not", "a", "dict"], seeds=self.seeds, db=self._db())  # type: ignore[arg-type]

    def test_verify_rejects_missing_state_section(self) -> None:
        manifest: dict[str, Any] = {
            "format": CHECKPOINT_FORMAT,
            "seeds_digest": seeds_digest(self.seeds),
        }
        with self.assertRaisesRegex(CheckpointError, "state"):
            verify_checkpoint(manifest, seeds=self.seeds, db=self._db())

    def test_write_then_read_roundtrip_is_lossless(self) -> None:
        with mock.patch(
            "freegem.crawler.campaign.fetch_resilient",
            side_effect=lambda cfg, url, **kw: _ok(url),
        ):
            self._run(self.seeds, checkpoint_path=self.manifest_path)
        db = self._db()
        written = write_checkpoint(
            self.manifest_path + ".2",
            seeds=self.seeds,
            db=db,
            params={"concurrency": 1},
            counters={"attempted": 3},
            interrupted=False,
        )
        reread = read_checkpoint(self.manifest_path + ".2")
        self.assertEqual(written["state"]["results_rows"], reread["state"]["results_rows"])
        self.assertEqual(written["written_at"], reread["written_at"])
        self.assertEqual(reread["counters"], {"attempted": 3})


if __name__ == "__main__":
    unittest.main()
