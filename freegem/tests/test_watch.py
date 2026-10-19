"""Tests for ``freegem watch`` — the change monitor (issue #30).

Fully hermetic: the fetcher is injected (no network), state lives in a
temporary directory (``state_dir=`` or ``$FREEGEM_WATCH_DIR``), the clock is
injected (``now=``), and loop tests pass their own ``sleep`` so nothing
ever waits.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from freegem.cli import main
from freegem.config import BridgeConfig
from freegem.dedup import Simhash
from freegem.watch import (
    DiffBlock,
    WatchResult,
    WatchState,
    diff_blocks,
    normalize_text,
    parse_interval,
    render_text,
    state_path_for,
    watch_poll,
    watch_url,
)

URL = "https://example.com/status"


class WatchCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        self.cfg = BridgeConfig(
            guard=True,
        )

    def watch(self, body: str, *, now: float | None = None, url: str = URL) -> Any:
        return watch_url(
            url,
            self.cfg,
            fetcher=lambda _u: body,
            state_dir=self.state_dir,
            now=now,
        )

    def state_file(self, url: str = URL) -> Path:
        path = state_path_for(url, self.state_dir)
        self.assertTrue(path.exists(), f"state file missing: {path}")
        return path

    def test_first_run_creates_baseline(self) -> None:
        result = self.watch("alpha\nbravo", now=1000.0)
        self.assertTrue(result.baseline)
        self.assertIsNone(result.changed)
        data = json.loads(self.state_file().read_text(encoding="utf-8"))
        self.assertEqual(data["url"], URL)
        self.assertEqual(data["text"], "alpha\nbravo")
        self.assertEqual(data["simhash"], Simhash.from_text("alpha\nbravo").value)
        self.assertEqual(data["checked_at"], 1000.0)
        self.assertEqual(data["first_seen"], 1000.0)
        self.assertIsNone(data["last_changed_at"])

    def test_identical_body_reports_no_change(self) -> None:
        self.watch("alpha\nbravo", now=1000.0)
        result = self.watch("alpha\nbravo", now=2000.0)
        self.assertFalse(result.baseline)
        self.assertIs(result.changed, False)
        self.assertEqual(result.added_chars, 0)
        self.assertEqual(result.removed_chars, 0)
        self.assertEqual(result.changed_blocks, ())
        # the poll touches checked_at but never the baseline itself
        data = json.loads(self.state_file().read_text(encoding="utf-8"))
        self.assertEqual(data["checked_at"], 2000.0)
        self.assertEqual(data["first_seen"], 1000.0)

    def test_whitespace_only_churn_is_not_a_change(self) -> None:
        self.watch("alpha\n  bravo  \n\n\ncharlie", now=1000.0)
        result = self.watch("alpha\nbravo\ncharlie\n\n", now=2000.0)
        self.assertIs(result.changed, False)

    def test_changed_body_detected_with_counts_and_blocks(self) -> None:
        self.watch("alpha\nbravo\ncharlie", now=1000.0)
        result = self.watch("alpha\ncharlie\ndelta", now=2000.0)
        self.assertIs(result.changed, True)
        self.assertEqual(result.added_chars, len("delta"))
        self.assertEqual(result.removed_chars, len("bravo"))
        kinds = {block.kind for block in result.changed_blocks}
        self.assertEqual(kinds, {"added", "removed"})
        by_kind = {block.kind: block for block in result.changed_blocks}
        self.assertEqual(by_kind["removed"].before, "bravo")
        self.assertIsNone(by_kind["removed"].after)
        self.assertEqual(by_kind["added"].after, "delta")
        self.assertIsNone(by_kind["added"].before)
        # simhash bookkeeping: before/after fingerprints and their distance
        self.assertEqual(result.simhash_before, Simhash.from_text("alpha\nbravo\ncharlie").value)
        self.assertEqual(result.simhash_after, Simhash.from_text("alpha\ncharlie\ndelta").value)
        self.assertIsNotNone(result.simhash_distance)
        assert result.simhash_distance is not None
        self.assertGreaterEqual(result.simhash_distance, 0)
        # the baseline rotated: the new text is what the next run compares to
        data = json.loads(self.state_file().read_text(encoding="utf-8"))
        self.assertEqual(data["text"], "alpha\ncharlie\ndelta")
        self.assertEqual(data["last_changed_at"], 2000.0)

    def test_state_round_trip(self) -> None:
        path = self.state_dir / "manual.json"
        state = WatchState(
            url=URL,
            text="some text",
            text_hash=hashlib.sha256(b"some text").hexdigest(),
            # (#56) load() now re-verifies the simhash against the text, so
            # the round trip must store the text's real simhash.
            simhash=Simhash.from_text("some text").value,
            checked_at=42.0,
            first_seen=7.0,
            last_changed_at=None,
        )
        state.save(path)
        loaded = WatchState.load(path)
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.url, state.url)
        self.assertEqual(loaded.text, state.text)
        self.assertEqual(loaded.simhash, state.simhash)
        self.assertEqual(loaded.checked_at, 42.0)
        self.assertEqual(loaded.first_seen, 7.0)
        self.assertIsNone(loaded.last_changed_at)
        self.assertIsNone(WatchState.load(self.state_dir / "missing.json"))

    def test_corrupt_state_file_treated_as_fresh_baseline(self) -> None:
        self.watch("before the corruption", now=1000.0)
        path = self.state_file()
        for garbage in ("not json at all {{{", "[1, 2, 3]", '{"simhash": "x"}'):
            path.write_text(garbage, encoding="utf-8")
            result = self.watch("totally different", now=2000.0)
            self.assertTrue(result.baseline, f"garbage {garbage!r} crashed the baseline")
            self.assertIsNone(result.changed)
        # the fresh baseline replaced the corrupt file and is loadable again
        self.assertIsNotNone(WatchState.load(path))

    def test_tampered_text_hash_treated_as_fresh_baseline(self) -> None:
        self.watch("original", now=1000.0)
        path = self.state_file()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["text"] = "someone edited the file but kept the old hash"
        path.write_text(json.dumps(data), encoding="utf-8")
        result = self.watch("original", now=2000.0)
        self.assertTrue(result.baseline)

    def test_normalize_text(self) -> None:
        self.assertEqual(normalize_text("  a \n\n b  \n"), "a\nb")
        self.assertEqual(normalize_text(""), "")

    def test_diff_blocks_pure_add(self) -> None:
        blocks, added, removed = diff_blocks("one", "one\ntwo")
        self.assertEqual(added, len("two"))
        self.assertEqual(removed, 0)
        self.assertEqual([b.kind for b in blocks], ["added"])


class IntervalParseTests(unittest.TestCase):
    def test_known_units(self) -> None:
        cases = {
            "45s": 45.0,
            "10m": 600.0,
            "2h": 7200.0,
            "90": 90.0,
            "1h30m": 5400.0,
            "1.5h": 5400.0,
            " 30m ": 1800.0,
            "0.5s": 0.5,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_interval(text), expected)

    def test_invalid_intervals_rejected(self) -> None:
        for text in ("", "abc", "-5", "0", "0s", "5x", "h", "1h 30x"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_interval(text)


class WatchLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        self.cfg = BridgeConfig(
            guard=True,
        )

    def test_loop_polls_without_sleeping(self) -> None:
        bodies = iter(["alpha", "alpha", "beta"])
        sleeps: list[float] = []
        results = list(
            watch_poll(
                URL,
                self.cfg,
                interval_sec=600.0,
                fetcher=lambda _u: next(bodies),
                state_dir=self.state_dir,
                sleep=sleeps.append,
                max_polls=3,
            )
        )
        self.assertEqual([r.baseline for r in results], [True, False, False])
        self.assertEqual([r.changed for r in results], [None, False, True])
        self.assertEqual(sleeps, [600.0, 600.0])

    def test_stop_callable_ends_the_loop(self) -> None:
        results = list(
            watch_poll(
                URL,
                self.cfg,
                interval_sec=600.0,
                fetcher=lambda _u: "same",
                state_dir=self.state_dir,
                sleep=lambda _s: None,
                stop=lambda: True,
                max_polls=5,
            )
        )
        self.assertEqual(len(results), 1)


class RenderTextTests(unittest.TestCase):
    def _changed_result(self, blocks: tuple[DiffBlock, ...]) -> WatchResult:
        return WatchResult(
            url=URL,
            baseline=False,
            changed=True,
            simhash_before=0x0123456789ABCDEF,
            simhash_after=0xFEDCBA9876543210,
            simhash_distance=7,
            added_chars=12,
            removed_chars=5,
            changed_blocks=blocks,
            checked_at=1000.0,
            state_path="/tmp/state.json",
        )

    @staticmethod
    def _seven_blocks() -> tuple[DiffBlock, ...]:
        return tuple(
            DiffBlock(kind="changed", before=f"old {i}", after=f"new {i}") for i in range(7)
        )

    def test_summary_caps_blocks_without_delta(self) -> None:
        text = render_text(self._changed_result(self._seven_blocks()))
        self.assertIn("old 0", text)
        self.assertIn("new 0", text)
        self.assertNotIn("old 5", text)
        self.assertIn("2 more block(s)", text)
        self.assertIn("+12 / -5 chars", text)
        self.assertIn("(7/64 bits differ)", text)

    def test_delta_prints_every_block(self) -> None:
        text = render_text(self._changed_result(self._seven_blocks()), delta=True)
        self.assertIn("old 6", text)
        self.assertNotIn("more block(s)", text)

    def test_baseline_and_no_change_lines(self) -> None:
        baseline = WatchResult(
            url=URL,
            baseline=True,
            changed=None,
            simhash_before=None,
            simhash_after=42,
            simhash_distance=None,
            checked_at=1000.0,
            state_path="/tmp/state.json",
        )
        self.assertIn("baseline saved", render_text(baseline))
        quiet = WatchResult(
            url=URL,
            baseline=False,
            changed=False,
            simhash_before=42,
            simhash_after=42,
            simhash_distance=0,
            checked_at=1000.0,
            state_path="/tmp/state.json",
        )
        self.assertIn("no change", render_text(quiet))


class CliWatchTests(unittest.TestCase):
    """``freegem watch URL --once`` wiring: exit codes, formats, state dir."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        env = mock.patch.dict(os.environ, {"FREEGEM_WATCH_DIR": str(self.state_dir)})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_once_baseline_exits_zero(self) -> None:
        with mock.patch("freegem.watch._default_fetcher", return_value="hello page") as fetcher:
            code, out, _ = self.run_cli(["watch", URL, "--once"])
        self.assertEqual(code, 0)
        self.assertIn("baseline saved", out)
        self.assertTrue(fetcher.called)
        self.assertEqual(len(list(self.state_dir.glob("*.json"))), 1)

    def test_once_no_change_exits_zero(self) -> None:
        with mock.patch("freegem.watch._default_fetcher", return_value="hello page"):
            first, _, _ = self.run_cli(["watch", URL, "--once"])
            second, out, _ = self.run_cli(["watch", URL, "--once"])
        self.assertEqual(first, 0)
        self.assertEqual(second, 0)
        self.assertIn("no change", out)

    def test_once_changed_exits_three_with_delta_blocks(self) -> None:
        with mock.patch("freegem.watch._default_fetcher", return_value="alpha\nbravo"):
            self.run_cli(["watch", URL, "--once"])
        with mock.patch("freegem.watch._default_fetcher", return_value="alpha\ncharlie"):
            code, out, _ = self.run_cli(["watch", URL, "--once", "--delta"])
        self.assertEqual(code, 3)
        self.assertIn("CHANGED", out)
        self.assertIn("- bravo", out)
        self.assertIn("+ charlie", out)

    def test_once_json_format_shape(self) -> None:
        with mock.patch("freegem.watch._default_fetcher", return_value="alpha\nbravo"):
            code, out, _ = self.run_cli(["watch", URL, "--once", "--format", "json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        for key in (
            "url",
            "changed",
            "simhash_before",
            "simhash_after",
            "added_chars",
            "removed_chars",
            "changed_blocks",
        ):
            self.assertIn(key, payload)
        self.assertIsNone(payload["changed"])  # baseline marker
        self.assertIsNone(payload["simhash_before"])
        self.assertEqual(payload["changed_blocks"], [])

        with mock.patch("freegem.watch._default_fetcher", return_value="alpha\ndelta"):
            code, out, _ = self.run_cli(["watch", URL, "--once", "--format", "json"])
        self.assertEqual(code, 3)
        payload = json.loads(out)
        self.assertIs(payload["changed"], True)
        self.assertIsNotNone(payload["simhash_before"])
        self.assertGreater(payload["added_chars"] + payload["removed_chars"], 0)
        self.assertTrue(payload["changed_blocks"])

    def test_fetch_failure_exits_nonzero_with_honest_stderr(self) -> None:
        """(#52) A failed --once poll is a RECORDED LINE (the baseline is
        kept, a watcher script gets exit 1), not a crash traceback."""
        from freegem.web_scraper import ScrapeError

        with mock.patch(
            "freegem.watch._default_fetcher",
            side_effect=ScrapeError("HTTP 503 fetching 'https://example.com/status'"),
        ):
            code, out, err = self.run_cli(["watch", URL, "--once"])
        self.assertEqual(code, 1)
        self.assertIn("fetch failed: https://example.com/status", out)
        self.assertIn("HTTP 503", out)
        self.assertIn("no verdict this poll; the stored baseline is kept", out)
        self.assertEqual(err, "")

    def test_bad_interval_is_a_usage_error(self) -> None:
        code, _, err = self.run_cli(["watch", URL, "--interval", "nonsense", "--once"])
        self.assertEqual(code, 2)
        self.assertIn("--interval", err)

    def test_interval_with_once_stays_single_check(self) -> None:
        sleeps: list[float] = []

        with (
            mock.patch("freegem.watch._default_fetcher", return_value="hello page"),
            mock.patch("freegem.watch.time.sleep", side_effect=sleeps.append),
        ):
            code, out, _ = self.run_cli(["watch", URL, "--interval", "10m", "--once"])
        self.assertEqual(code, 0)
        self.assertIn("baseline saved", out)
        self.assertEqual(sleeps, [])

    def test_loop_mode_repolls_and_handles_interrupt(self) -> None:
        # The fake fetcher raises KeyboardInterrupt on the SECOND poll: that
        # proves the loop re-polled after its sleep, and exercises the clean
        # Ctrl-C exit. time.sleep is patched (resolved at call time by
        # watch_poll), so the test never waits.
        polls: list[str] = []

        def fetch(*_args: Any) -> str:
            if polls:
                raise KeyboardInterrupt
            polls.append("polled")
            return "alpha\nbravo"

        sleeps: list[float] = []
        with (
            mock.patch("freegem.watch._default_fetcher", side_effect=fetch),
            mock.patch("freegem.watch.time.sleep", side_effect=sleeps.append),
        ):
            code, out, err = self.run_cli(["watch", URL, "--interval", "10m"])
        self.assertEqual(code, 0)
        self.assertIn("baseline saved", out)
        self.assertEqual(len(polls), 1)
        self.assertEqual(sleeps, [600.0])
        self.assertIn("stopped", err)


if __name__ == "__main__":
    unittest.main()
