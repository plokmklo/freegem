"""The #52-#64 hardening round plus ``--ai-clean``: one class per fix.

Every class pins the BEHAVIORAL CONTRACT its numbered fix introduced, not
the implementation: poll errors are recorded lines (not crashes), junk
polls never touch a baseline, saves are atomic under concurrency, watch
state is listable/forgettable/prunable, stored fingerprints are verified,
challenge gates decide by density, renders are daemon-threaded, capped,
budget-sized and two-phase, gated-off rungs leave the ladder, UNC targets
and non-http schemes are refused before any packet, and ai-clean is an
explicit, bounded, honestly-labeled AI view. Fully hermetic: no network,
no browser, no model -- every seam is injected or patched.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from freegem.cli import main
from freegem.config import BridgeConfig
from freegem.dedup import Simhash
from freegem.smart_fetch import (
    _AI_CLEAN_MIN_CHARS,
    _MAX_CONCURRENT_RENDERS,
    STRATEGIES,
    _looks_like_challenge,
    _strategy_order,
    ai_clean,
    clear_memory,
    default_memory_path,
)
from freegem.watch import (
    WatchState,
    _sha256,
    forget_all_watches,
    forget_watch,
    list_watches,
    parse_interval,
    prune_watches,
    render_text,
    watch_poll,
    watch_url,
)
from freegem.web_scraper import ScrapeError

_URL = "https://watched.example/page"

_CHALLENGE_WALL = "Just a moment... Enable JavaScript and cookies to continue."
_CHALLENGE_INTERSTITIAL = (
    "Please wait. Verify you are human to access this site. Enable JavaScript "
    "and cookies. This challenge exists so we can stop bots. Just a moment. "
) * 20  # >800 chars, 3+ marker phrases: a rendered JS wall, not an article
_LONG_ARTICLE = (
    "This long article discusses web scraping policy, rate limits, and the "
    "history of polite crawlers. " * 40
) + "One section mentions captcha history."  # 1 hit in a long head: content


def _handler_types(opener: urllib.request.OpenerDirector) -> list[type[Any]]:
    """The opener's handler classes; ``handlers`` is missing from the stubs."""
    opener_any: Any = opener
    return [type(handler) for handler in opener_any.handlers]


class _StateDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state_dir = Path(tmp.name)
        self.cfg = BridgeConfig(guard=True, log_requests=False)
        clear_memory()

    def tearDown(self) -> None:
        clear_memory()


# ─── #52: one failed poll is a line, not a crash ────────────────────────────


class Fix52PollErrorTests(_StateDirTestCase):
    def test_failed_poll_yields_recorded_result_and_the_loop_continues(self) -> None:
        calls: list[str] = []

        def flaky(url: str) -> str:
            calls.append(url)
            if len(calls) == 1:
                raise ScrapeError("HTTP 503 fetching 'https://watched.example/page'")
            return "the real page text"

        results = list(
            watch_poll(
                _URL,
                self.cfg,
                fetcher=flaky,
                state_dir=self.state_dir,
                max_polls=2,
                sleep=lambda _sec: None,
            )
        )
        self.assertEqual(len(results), 2, "a failed poll must not end the loop")
        self.assertIsNone(results[0].changed, "no verdict on a failed poll")
        self.assertIn("HTTP 503", results[0].error or "")
        self.assertTrue(results[1].baseline, "the retry polls normally")

    def test_error_and_junk_fields_round_trip_through_json(self) -> None:
        from freegem.watch import WatchResult

        result = WatchResult(
            url=_URL,
            baseline=False,
            changed=None,
            simhash_before=None,
            simhash_after=0,
            simhash_distance=None,
            checked_at=1.0,
            state_path="s",
            error="HTTP 503",
        )
        payload = result.to_dict()
        self.assertEqual(payload["error"], "HTTP 503")
        self.assertIsNone(payload["junk_reason"])

    def test_failed_poll_renders_the_honest_line(self) -> None:
        from freegem.watch import WatchResult

        result = WatchResult(
            url=_URL,
            baseline=False,
            changed=None,
            simhash_before=None,
            simhash_after=0,
            simhash_distance=None,
            checked_at=1_700_000_000.0,
            state_path="s",
            error="HTTP 503 fetching 'u'",
        )
        text = render_text(result)
        self.assertIn("fetch failed", text)
        self.assertIn("no verdict this poll; the stored baseline is kept", text)

    def test_single_check_failure_is_machine_grepable_exit_one(self) -> None:
        """The CLI --once path: exit 1 with the recorded line on stdout."""
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch(
                "freegem.watch._default_fetcher",
                side_effect=ScrapeError("HTTP 500 fetching 'u'"),
            ),
            mock.patch("freegem.cli.find_config", return_value=None),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = main(["watch", _URL, "--once"])
        self.assertEqual(code, 1)
        self.assertIn("fetch failed", out.getvalue())
        self.assertEqual(err.getvalue(), "")


# ─── #53: junk polls never touch the baseline ───────────────────────────────


class Fix53JunkPollTests(_StateDirTestCase):
    def test_empty_poll_keeps_the_baseline(self) -> None:
        watch_url(_URL, self.cfg, fetcher=lambda u: "real content", state_dir=self.state_dir)
        junk = watch_url(_URL, self.cfg, fetcher=lambda u: "   ", state_dir=self.state_dir)
        self.assertIsNone(junk.changed)
        self.assertIn("zero readable text", junk.junk_reason or "")
        again = watch_url(
            _URL, self.cfg, fetcher=lambda u: "real content", state_dir=self.state_dir
        )
        self.assertFalse(again.changed, "diffing resumes against the ORIGINAL baseline")

    def test_challenge_poll_keeps_the_baseline(self) -> None:
        watch_url(_URL, self.cfg, fetcher=lambda u: "real content", state_dir=self.state_dir)
        junk = watch_url(
            _URL, self.cfg, fetcher=lambda u: _CHALLENGE_WALL, state_dir=self.state_dir
        )
        self.assertIn("bot-challenge", junk.junk_reason or "")
        self.assertIsNone(junk.changed)

    def test_junk_never_baselines_a_fresh_watch(self) -> None:
        with self.assertRaises(ScrapeError) as caught:
            watch_url(_URL, self.cfg, fetcher=lambda u: "", state_dir=self.state_dir)
        self.assertIn("refused to baseline", str(caught.exception))
        self.assertEqual(list(self.state_dir.glob("*.json")), [], "no state file written")

    def test_short_but_real_pages_are_not_junk(self) -> None:
        result = watch_url(_URL, self.cfg, fetcher=lambda u: "42", state_dir=self.state_dir)
        self.assertTrue(result.baseline)
        self.assertIsNone(result.junk_reason)


# ─── #54: atomic saves with unique tmp files ────────────────────────────────


class Fix54AtomicSaveTests(_StateDirTestCase):
    def _state(self, text: str) -> WatchState:
        return WatchState(
            url=_URL,
            text=text,
            text_hash=_sha256(text),
            simhash=Simhash.from_text(text).value,
            checked_at=1.0,
            first_seen=1.0,
        )

    def test_concurrent_savers_never_tear_the_file(self) -> None:
        target = self.state_dir / "watch.json"
        errors: list[BaseException] = []

        def writer(text: str) -> None:
            state = self._state(text)
            for _ in range(25):
                try:
                    state.save(target)
                except BaseException as exc:  # collected and asserted below
                    errors.append(exc)

        threads = [threading.Thread(target=writer, args=(f"w{index} " * 50,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # A save that LOSES a persistent contention race (Windows: Defender
        # holds the target past the retry window) is an HONEST FAILED SAVE,
        # never a torn one — OSError may surface, nothing else may.
        for exc in errors:
            self.assertIsInstance(exc, OSError, f"a non-OS failure leaked through save(): {exc!r}")
        loaded = WatchState.load(target)
        self.assertIsNotNone(loaded, "the installed file is always COMPLETE")
        assert loaded is not None  # narrow for mypy
        self.assertIn(loaded.text, [f"w{i} " * 50 for i in range(4)])

    def test_no_fixed_tmp_file_is_left_behind(self) -> None:
        target = self.state_dir / "watch.json"
        self._state("some text").save(target)
        self.assertTrue(target.exists())
        self.assertEqual(
            list(target.parent.glob(target.name + ".*.tmp")),
            [],
            "unique tmp files are consumed by the rename, never accumulated",
        )
        self.assertFalse(target.with_name(target.name + ".tmp").exists())

    def test_failed_save_cleans_its_tmp(self) -> None:
        target = self.state_dir / "watch.json"
        state = self._state("some text")
        with mock.patch("freegem.watch.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                state.save(target)
        self.assertEqual(list(self.state_dir.glob("*.tmp")), [], "a failed save removes its tmp")
        self.assertFalse(target.exists())


# ─── #55: watch state management ────────────────────────────────────────────


class Fix55StateManagementTests(_StateDirTestCase):
    def _save(self, url: str, text: str, checked_at: float) -> None:
        from freegem.watch import state_path_for

        WatchState(
            url=url,
            text=text,
            text_hash=_sha256(text),
            simhash=Simhash.from_text(text).value,
            checked_at=checked_at,
            first_seen=checked_at,
        ).save(state_path_for(url, self.state_dir))

    def test_parse_interval_accepts_days(self) -> None:
        self.assertEqual(parse_interval("30d"), 30 * 86400.0)
        self.assertEqual(parse_interval("2d"), 172800.0)

    def test_list_empty_and_nonempty(self) -> None:
        self.assertEqual(list_watches(self.state_dir), [])
        self._save("https://a.example/", "alpha", checked_at=100.0)
        self._save("https://b.example/", "beta", checked_at=50.0)
        entries = list_watches(self.state_dir)
        self.assertEqual([e["url"] for e in entries], ["https://b.example/", "https://a.example/"])
        for entry in entries:
            self.assertFalse(entry["corrupt"])
            self.assertGreater(entry["bytes"], 0)
            self.assertIn("last_changed_at", entry)

    def test_corrupt_entries_are_listed_not_hidden_and_sort_first(self) -> None:
        self._save("https://a.example/", "alpha", checked_at=100.0)
        (self.state_dir / "garbage.json").write_text("{not json", encoding="utf-8")
        entries = list_watches(self.state_dir)
        self.assertTrue(entries[0]["corrupt"], "the corrupt file sorts first")
        self.assertEqual(entries[0]["url"], None)
        self.assertEqual(len(entries), 2)

    def test_forget_watch_removes_one_and_reports_misses(self) -> None:
        self._save("https://a.example/", "alpha", checked_at=1.0)
        self.assertTrue(forget_watch("https://a.example/", self.state_dir))
        self.assertFalse(forget_watch("https://a.example/", self.state_dir), "already gone")
        self.assertEqual(list_watches(self.state_dir), [])

    def test_forget_all_clears_json_and_tmp(self) -> None:
        self._save("https://a.example/", "alpha", checked_at=1.0)
        self._save("https://b.example/", "beta", checked_at=2.0)
        (self.state_dir / "watch.json.123.tmp").write_text("junk", encoding="utf-8")
        (self.state_dir / "unrelated.txt").write_text("keep me", encoding="utf-8")
        removed = forget_all_watches(self.state_dir)
        self.assertEqual(removed, 3, "two states plus the abandoned tmp")
        self.assertEqual(list_watches(self.state_dir), [])
        self.assertTrue((self.state_dir / "unrelated.txt").exists(), "non-state files stay")

    def test_prune_max_age_drops_unpolled_entries(self) -> None:
        self._save("https://old.example/", "old", checked_at=100.0)
        self._save("https://new.example/", "new", checked_at=900.0)
        removed = prune_watches(self.state_dir, max_age_sec=300.0, now=1000.0)
        self.assertEqual(removed, 1)
        self.assertEqual([e["url"] for e in list_watches(self.state_dir)], ["https://new.example/"])

    def test_prune_max_entries_evicts_oldest_checked(self) -> None:
        self._save("https://a.example/", "a", checked_at=100.0)
        self._save("https://b.example/", "b", checked_at=200.0)
        self._save("https://c.example/", "c", checked_at=300.0)
        removed = prune_watches(self.state_dir, max_entries=2)
        self.assertEqual(removed, 1)
        urls = {e["url"] for e in list_watches(self.state_dir)}
        self.assertEqual(urls, {"https://b.example/", "https://c.example/"})

    def test_prune_treats_corrupt_as_oldest(self) -> None:
        self._save("https://a.example/", "a", checked_at=500.0)
        (self.state_dir / "garbage.json").write_text("{", encoding="utf-8")
        removed = prune_watches(self.state_dir, max_entries=1)
        self.assertEqual(removed, 1, "the corrupt file is first out the door")
        self.assertEqual([e["url"] for e in list_watches(self.state_dir)], ["https://a.example/"])

    def test_cli_list_and_forget_all(self) -> None:
        env = mock.patch.dict(os.environ, {"FREEGEM_WATCH_DIR": str(self.state_dir)})
        env.start()
        self.addCleanup(env.stop)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["watch", "--list"])
        self.assertEqual(code, 0)
        self.assertIn("no watches stored", out.getvalue())
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["watch", "--forget-all"])
        self.assertEqual(code, 0)
        self.assertIn("forgot 0", out.getvalue())


# ─── #56: the stored simhash must be what the text hashes to ────────────────


class Fix56SimhashIntegrityTests(_StateDirTestCase):
    def _saved_path(self) -> Path:
        from freegem.watch import state_path_for

        path = state_path_for(_URL, self.state_dir)
        WatchState(
            url=_URL,
            text="stable text",
            text_hash=_sha256("stable text"),
            simhash=Simhash.from_text("stable text").value,
            checked_at=1.0,
            first_seen=1.0,
        ).save(path)
        return path

    def test_tampered_simhash_loads_as_none(self) -> None:
        path = self._saved_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["simhash"] = data["simhash"] ^ 0xDEAD_BEEF
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(
            WatchState.load(path),
            "a simhash the text does not hash to is tampering or damage",
        )

    def test_consistent_state_still_loads(self) -> None:
        path = self._saved_path()
        loaded = WatchState.load(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.text, "stable text")  # type: ignore[union-attr]

    def test_tampered_text_hash_loads_as_none(self) -> None:
        path = self._saved_path()
        data = json.loads(path.read_text(encoding="utf-8"))
        data["simhash"] = Simhash.from_text("other text").value
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(WatchState.load(path))


# ─── #57: challenge detection decides by DENSITY on long pages ──────────────


class Fix57ChallengeDensityTests(unittest.TestCase):
    def test_short_wall_is_a_challenge(self) -> None:
        self.assertTrue(_looks_like_challenge(_CHALLENGE_WALL))

    def test_long_interstitial_with_repeated_markers_is_a_challenge(self) -> None:
        """The fix: >800 chars no longer fails open when the page is a wall
        repeating its boilerplate."""
        self.assertGreater(len(_CHALLENGE_INTERSTITIAL), 800)
        self.assertTrue(_looks_like_challenge(_CHALLENGE_INTERSTITIAL))

    def test_long_article_with_one_mention_is_content(self) -> None:
        self.assertGreater(len(_LONG_ARTICLE), 800)
        self.assertFalse(_looks_like_challenge(_LONG_ARTICLE))

    def test_markers_only_after_the_head_window_do_not_count(self) -> None:
        body = "clean policy prose. " * 120 + "just a moment"
        self.assertFalse(_looks_like_challenge(body), "the head window bounds the scan")

    def test_empty_text_is_not_a_challenge(self) -> None:
        self.assertFalse(_looks_like_challenge(""))


# ─── #58: bounded concurrent renders, daemon workers ────────────────────────


class Fix58RenderSlotTests(_StateDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = BridgeConfig(guard=True, log_requests=False, smart_fetch_browser_render=True)

    def test_cap_is_two_and_held_slots_refuse_honestly(self) -> None:
        from freegem import smart_fetch

        self.assertEqual(_MAX_CONCURRENT_RENDERS, 2)
        held = [smart_fetch._RENDER_SLOTS.acquire(blocking=False) for _ in range(2)]
        self.assertTrue(all(held), "test precondition: both slots acquired")
        try:
            with (
                mock.patch("freegem.playwright_fetcher.is_playwright_available", return_value=True),
                mock.patch("freegem.playwright_fetcher.fetch_with_playwright") as render,
                mock.patch("freegem.smart_fetch._BROWSER_RENDER_TIMEOUT_SEC", 1.0),
            ):
                with self.assertRaises(ScrapeError) as caught:
                    smart_fetch._browser_render_read(self.cfg, "https://r.example/", 2000)
            self.assertIn("browser-render busy", str(caught.exception))
            self.assertFalse(render.called, "no chromium may stack past the cap")
        finally:
            for _ in held:
                smart_fetch._RENDER_SLOTS.release()

    def test_a_failed_render_releases_its_slot(self) -> None:
        from freegem import smart_fetch

        with (
            mock.patch("freegem.playwright_fetcher.is_playwright_available", return_value=True),
            mock.patch(
                "freegem.playwright_fetcher.fetch_with_playwright",
                side_effect=RuntimeError("launch boom"),
            ),
        ):
            with self.assertRaises(ScrapeError):
                smart_fetch._browser_render_read(self.cfg, "https://r.example/", 2000)
        self.assertTrue(
            smart_fetch._RENDER_SLOTS.acquire(timeout=0.1),
            "the failure path must not leak the slot",
        )
        smart_fetch._RENDER_SLOTS.release()

    def test_render_thread_is_daemon(self) -> None:
        """(#58) A wedged driver can no longer block interpreter exit."""
        from freegem import smart_fetch

        release = threading.Event()
        started = threading.Event()

        def blocking_render(_opts: Any) -> Any:
            started.set()
            release.wait(timeout=5.0)
            raise RuntimeError("never unwinds before the assertion")

        with (
            mock.patch("freegem.playwright_fetcher.is_playwright_available", return_value=True),
            mock.patch(
                "freegem.playwright_fetcher.fetch_with_playwright", side_effect=blocking_render
            ),
            mock.patch("freegem.smart_fetch._BROWSER_RENDER_TIMEOUT_SEC", 1.0),
        ):
            with self.assertRaises(ScrapeError):
                smart_fetch._browser_render_read(self.cfg, "https://r.example/", 2000)
        self.assertTrue(started.wait(timeout=1.0))
        # The worker is still alive (bounded by Playwright's own bounds in
        # production); what matters is that it is a daemon.
        workers = [t for t in threading.enumerate() if t.name == "freegem-browser-render"]
        self.assertTrue(workers, "the abandoned worker thread exists")
        self.assertTrue(all(t.daemon for t in workers))
        release.set()


# ─── #59: rungs size their wait by the ladder's remaining budget ────────────


class Fix59BudgetSizingTests(_StateDirTestCase):
    def test_sub_second_budget_skips_ai_search(self) -> None:
        from freegem.smart_fetch import _ai_search_answer

        with mock.patch("freegem.mcp_tools.run_tool") as run_tool:
            with self.assertRaises(ScrapeError) as caught:
                _ai_search_answer(self.cfg, "prompt", "https://x.example/", budget=0.5)
        self.assertIn("skipped", str(caught.exception))
        self.assertIn("0.5", str(caught.exception))
        self.assertFalse(run_tool.called, "a doomed 0.5s thread is not started")

    def test_sub_second_budget_skips_browser_render(self) -> None:
        from freegem.smart_fetch import _browser_render_read

        cfg = BridgeConfig(guard=True, log_requests=False, smart_fetch_browser_render=True)
        with (
            mock.patch("freegem.playwright_fetcher.is_playwright_available", return_value=True),
            mock.patch("freegem.playwright_fetcher.fetch_with_playwright") as render,
        ):
            with self.assertRaises(ScrapeError) as caught:
                _browser_render_read(cfg, "https://x.example/", 2000, budget=0.4)
        self.assertIn("skipped", str(caught.exception))
        self.assertFalse(render.called)

    def test_fallback_passes_the_remaining_budget_to_each_rung(self) -> None:
        from freegem.smart_fetch import fallback_scrape

        seen: dict[str, float | None] = {}

        def try_strategy(
            cfg: Any,
            url: str,
            strategy: str,
            max_chars: int,
            ignore_robots: bool,
            budget: float | None = None,
        ) -> Any:
            seen[strategy] = budget
            if strategy == "reader":
                from freegem.smart_fetch import ScrapeResult

                return ScrapeResult(url=url, title="T", text="rescued")
            raise ScrapeError("HTTP 403")

        with (
            mock.patch("freegem.smart_fetch._try_strategy", side_effect=try_strategy),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            fallback_scrape(self.cfg, "https://b.example/", max_chars=100, first_error="HTTP 403")
        self.assertIn("browser-headers", seen)
        first_budget = seen["browser-headers"]
        self.assertIsNotNone(first_budget)
        assert first_budget is not None  # narrow for mypy
        self.assertGreater(first_budget, 0.0)
        self.assertLessEqual(first_budget, 60.0)


# ─── #60: gated-off rungs leave the ladder ──────────────────────────────────


class Fix60GatedOffRungTests(_StateDirTestCase):
    def test_default_config_drops_browser_render(self) -> None:
        order = _strategy_order("h.example", self.cfg)
        self.assertNotIn("browser-render", order)
        self.assertEqual(order, ["browser-headers", "reader", "wayback", "ai-search"])

    def test_ai_gate_off_drops_ai_search(self) -> None:
        cfg = BridgeConfig(guard=True, log_requests=False, smart_fetch_ai=False)
        order = _strategy_order("h.example", cfg)
        self.assertNotIn("ai-search", order)
        self.assertEqual(order, ["browser-headers", "reader", "wayback"])

    def test_render_gate_on_restores_the_full_ladder(self) -> None:
        # (#60) With every gate open the walk is the complete ladder.
        cfg = BridgeConfig(guard=True, log_requests=False).replace(
            smart_fetch_browser_render=True,
            smart_fetch_tls_browser=True,
            smart_fetch_flaresolverr="http://127.0.0.1:8191",
        )
        self.assertEqual(list(_strategy_order("h.example", cfg)), list(STRATEGIES))

    def test_remembered_rung_cannot_front_rank_while_gated_off(self) -> None:
        from freegem.smart_fetch import StrategyMemory

        StrategyMemory(default_memory_path()).remember("h.example", "browser-render")
        try:
            order = _strategy_order("h.example", self.cfg)
            self.assertNotIn("browser-render", order)
        finally:
            clear_memory()

    def test_memory_front_ranks_within_allowed_rungs(self) -> None:
        from freegem.smart_fetch import StrategyMemory

        StrategyMemory(default_memory_path()).remember("h.example", "wayback")
        try:
            self.assertEqual(_strategy_order("h.example", self.cfg)[0], "wayback")
        finally:
            clear_memory()


# ─── #61: the render rung renders in two phases ─────────────────────────────


class Fix61TwoPhaseRenderTests(_StateDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = BridgeConfig(guard=True, log_requests=False, smart_fetch_browser_render=True)

    @staticmethod
    def _render_result(url: str = "https://spa.example/") -> Any:
        from freegem.playwright_fetcher import PlaywrightResult

        return PlaywrightResult(
            url=url,
            final_url=url,
            status=200,
            text="<html><body><p>rendered body text</p></body></html>",
            title="Rendered",
        )

    def test_quiet_page_needs_only_the_networkidle_phase(self) -> None:
        from freegem import smart_fetch

        calls: list[str] = []

        def render(opts: Any) -> Any:
            calls.append(opts.wait_until)
            return self._render_result()

        with (
            mock.patch("freegem.playwright_fetcher.is_playwright_available", return_value=True),
            mock.patch("freegem.playwright_fetcher.fetch_with_playwright", side_effect=render),
        ):
            result = smart_fetch._browser_render_read(self.cfg, "https://spa.example/", 2000)
        self.assertEqual(calls, ["networkidle"])
        self.assertIn("rendered body", result.text)

    def test_chatty_page_falls_through_to_domcontentloaded_with_settle(self) -> None:
        from freegem import smart_fetch

        calls: list[Any] = []

        def render(opts: Any) -> Any:
            calls.append(opts)
            if opts.wait_until == "networkidle":
                raise RuntimeError("network never goes quiet")
            return self._render_result()

        with (
            mock.patch("freegem.playwright_fetcher.is_playwright_available", return_value=True),
            mock.patch("freegem.playwright_fetcher.fetch_with_playwright", side_effect=render),
        ):
            result = smart_fetch._browser_render_read(self.cfg, "https://spa.example/", 2000)
        self.assertEqual([o.wait_until for o in calls], ["networkidle", "domcontentloaded"])
        self.assertEqual(calls[1].settle_ms, smart_fetch._BROWSER_RENDER_SETTLE_MS)
        self.assertGreater(calls[1].settle_ms, 0)
        self.assertIn("rendered body", result.text)

    def test_both_phases_failing_reports_both_honestly(self) -> None:
        from freegem import smart_fetch

        def render(opts: Any) -> Any:
            raise RuntimeError("boom")

        with (
            mock.patch("freegem.playwright_fetcher.is_playwright_available", return_value=True),
            mock.patch("freegem.playwright_fetcher.fetch_with_playwright", side_effect=render),
            mock.patch("freegem.smart_fetch._BROWSER_RENDER_TIMEOUT_SEC", 2.0),
        ):
            with self.assertRaises(ScrapeError) as caught:
                smart_fetch._browser_render_read(self.cfg, "https://spa.example/", 2000)
        message = str(caught.exception)
        self.assertIn("networkidle:", message)
        self.assertIn("domcontentloaded:", message)


# ─── #62: UNC / network-share targets are refused by shape ──────────────────


class Fix62UncPathTests(_StateDirTestCase):
    def test_unc_targets_refused_before_any_exists_probe(self) -> None:
        from freegem.web_scraper import _local_target

        targets = [
            "\\\\host\\share\\file.txt",
            "//host/share/file.txt",
            "\\\\?\\UNC\\host\\share\\file",
        ]
        with mock.patch(
            "freegem.web_scraper.os.path.exists",
            side_effect=AssertionError("os.path.exists must not be reached"),
        ) as probe:
            for target in targets:
                with self.subTest(target=target):
                    with self.assertRaises(ScrapeError) as caught:
                        _local_target(target)
                    self.assertIn("UNC", str(caught.exception))
        self.assertFalse(probe.called)

    def test_ordinary_local_paths_still_read(self) -> None:
        from freegem.web_scraper import _local_target

        page = self.state_dir / "page.html"
        page.write_text("<p>local</p>", encoding="utf-8")
        self.assertEqual(_local_target(str(page)), str(page))


# ─── #63: scheme gate + hand-built opener (no silent default handlers) ──────


class Fix63SchemeGateAndOpenerTests(_StateDirTestCase):
    def test_non_http_schemes_refused_before_any_dial(self) -> None:
        from freegem.web_scraper import _fetch

        for url in ("ftp://169.254.169.254/latest", "data:text/html,hi", "gopher://x/"):
            with (
                self.subTest(url=url),
                mock.patch(
                    "freegem.web_scraper._network_opener",
                    side_effect=AssertionError("no opener may be built for these"),
                ),
            ):
                with self.assertRaises(ScrapeError) as caught:
                    _fetch(self.cfg, url)
                self.assertIn("refusing scheme", str(caught.exception))

    def test_scheme_refusal_classifies_as_our_refusal(self) -> None:
        from freegem.smart_fetch import classify_failure

        self.assertEqual(classify_failure("refusing scheme 'ftp' for 'ftp://x/'"), "refused")

    def test_direct_opener_installs_only_audited_handlers(self) -> None:
        from freegem.ssrf import build_guarded_opener

        opener = build_guarded_opener(None, None)
        forbidden = (
            urllib.request.FTPHandler,
            urllib.request.FileHandler,
            urllib.request.DataHandler,
            urllib.request.UnknownHandler,
        )
        types = _handler_types(opener)
        for handler_type in forbidden:
            for installed in types:
                self.assertFalse(
                    issubclass(installed, handler_type),
                    f"{installed.__name__} must not ride the guarded opener",
                )

    def test_proxy_opener_dials_explicitly_and_preflights(self) -> None:
        from freegem.ssrf import PreflightedProxyHandler, build_guarded_opener

        opener = build_guarded_opener("http://127.0.0.1:3128", None)
        types = _handler_types(opener)
        self.assertIn(PreflightedProxyHandler, types)
        self.assertIn(urllib.request.HTTPHandler, types, "proxy mode dials explicitly")
        for handler_type in (urllib.request.FTPHandler, urllib.request.FileHandler):
            self.assertNotIn(handler_type, types)


# ─── #64: the IP-pinning opener is ALWAYS the network path ──────────────────


class Fix64AlwaysOnPinningTests(_StateDirTestCase):
    def test_network_opener_is_the_guarded_one_in_both_modes(self) -> None:
        from freegem.web_scraper import _network_opener

        for guard in (False, True):
            with self.subTest(guard=guard):
                cfg = BridgeConfig(guard=guard, log_requests=False)
                sentinel = object()
                with mock.patch(
                    "freegem.web_scraper.build_guarded_opener", return_value=sentinel
                ) as builder:
                    self.assertIs(_network_opener(cfg), sentinel)
                self.assertTrue(builder.called)

    def test_direct_opener_handlers_are_the_pinned_pair(self) -> None:
        from freegem.ssrf import PinnedHTTPHandler, PinnedHTTPSHandler, build_guarded_opener

        types = _handler_types(build_guarded_opener(None, None))
        self.assertIn(PinnedHTTPHandler, types)
        self.assertIn(PinnedHTTPSHandler, types)


# ─── --ai-clean: the bounded, labeled AI view ───────────────────────────────


class AiCleanTests(_StateDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = BridgeConfig(guard=True, log_requests=False, smart_fetch_ai_clean=True)

    @staticmethod
    def _provider_patch(
        answer: str, prompt_sink: list[str] | None = None, delay: float = 0.0
    ) -> Any:
        class FakeProvider:
            def __init__(self, cfg: Any) -> None:
                pass

            def complete(self, prompt: str) -> str:
                if prompt_sink is not None:
                    prompt_sink.append(prompt)
                if delay:
                    time.sleep(delay)
                return answer

        return mock.patch("freegem.llm_extract.BridgeProvider", FakeProvider)

    def test_disabled_by_default(self) -> None:
        cfg = BridgeConfig(guard=True, log_requests=False)
        with self.assertRaises(ScrapeError) as caught:
            ai_clean(cfg, "https://x.example/", "long enough text " * 100)
        self.assertIn("ai-clean disabled", str(caught.exception))

    def test_short_page_is_skipped_not_sent_to_the_model(self) -> None:
        self.assertGreater(_AI_CLEAN_MIN_CHARS, 100)
        with self.assertRaises(ScrapeError) as caught:
            ai_clean(self.cfg, "https://x.example/", "tiny page")
        self.assertIn("skipped", str(caught.exception))
        self.assertIn("minimum", str(caught.exception))

    def test_success_returns_the_models_main_content_view(self) -> None:
        prompts: list[str] = []
        page_text = "NAV MENU\ncookie banner\n" + ("The actual article body. " * 40)
        with self._provider_patch("The actual article body. " * 30, prompt_sink=prompts):
            cleaned = ai_clean(self.cfg, "https://a.example/post", page_text)
        self.assertIn("article body", cleaned)
        self.assertEqual(len(prompts), 1)
        self.assertIn("https://a.example/post", prompts[0])
        self.assertIn("[PAGE TEXT]", prompts[0])

    def test_input_is_head_trimmed_before_the_model_sees_it(self) -> None:
        from freegem.smart_fetch import _AI_CLEAN_MAX_INPUT_CHARS

        prompts: list[str] = []
        with self._provider_patch("word " * 100, prompt_sink=prompts):
            ai_clean(self.cfg, "https://a.example/", "x" * (_AI_CLEAN_MAX_INPUT_CHARS + 5000))
        body = prompts[0].split("[PAGE TEXT]\n", 1)[1].rsplit("\n[/PAGE TEXT]", 1)[0]
        self.assertEqual(len(body), _AI_CLEAN_MAX_INPUT_CHARS)

    def test_no_main_content_marker_is_a_clean_failure(self) -> None:
        with self._provider_patch("NO MAIN CONTENT"):
            with self.assertRaises(ScrapeError) as caught:
                ai_clean(self.cfg, "https://a.example/", "cookie banner " * 100)
        self.assertIn("no main content", str(caught.exception))

    def test_nonanswer_is_a_clean_failure(self) -> None:
        with self._provider_patch("I could not access the page content at that URL."):
            with self.assertRaises(ScrapeError) as caught:
                ai_clean(self.cfg, "https://a.example/", "The actual article body. " * 40)
        self.assertIn("no main content", str(caught.exception))

    def test_tiny_answer_is_not_content(self) -> None:
        with self._provider_patch("ok"):
            with self.assertRaises(ScrapeError) as caught:
                ai_clean(self.cfg, "https://a.example/", "The actual article body. " * 40)
        self.assertIn("no usable content", str(caught.exception))

    def test_bounded_wait_abandons_a_wedged_model_call(self) -> None:
        with (
            self._provider_patch("late answer " * 100, delay=1.2),
            mock.patch("freegem.smart_fetch._AI_CLEAN_TIMEOUT_SEC", 1.0),
        ):
            started = time.monotonic()
            with self.assertRaises(ScrapeError) as caught:
                ai_clean(self.cfg, "https://a.example/", "The actual article body. " * 40)
        self.assertLess(time.monotonic() - started, 2.0, "our wait is bounded")
        self.assertIn("timed out", str(caught.exception))

    def test_sub_second_budget_skips_the_pass(self) -> None:
        with mock.patch("freegem.llm_extract.BridgeProvider") as provider:
            with self.assertRaises(ScrapeError) as caught:
                ai_clean(
                    self.cfg, "https://a.example/", "The actual article body. " * 40, budget=0.3
                )
        self.assertIn("skipped", str(caught.exception))
        self.assertFalse(provider.called)


class AiCleanCliWiringTests(_StateDirTestCase):
    """Validation branches only: no fetch, no model."""

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_ai_clean_rejects_multiple_urls(self) -> None:
        code, _, err = self.run_cli(
            ["scrape", "https://a.example/", "https://b.example/", "--ai-clean"]
        )
        self.assertEqual(code, 2)
        self.assertIn("single URL", err)

    def test_ai_clean_and_ask_are_mutually_exclusive(self) -> None:
        code, _, err = self.run_cli(
            ["scrape", "https://a.example/", "--ai-clean", "--ask", "question?"]
        )
        self.assertEqual(code, 2)
        self.assertIn("pick one", err)

    def test_cli_flag_enables_the_per_run_gate(self) -> None:
        """Smoke-matrix regression: `--ai-clean` must turn the config gate ON
        for the run -- the flag IS the explicit choice, or the feature is
        unreachable for anyone who did not also edit the config file."""
        from freegem.web_scraper import ScrapeResult

        class FakeProvider:
            def __init__(self, cfg: Any) -> None:
                pass

            def complete(self, prompt: str) -> str:
                return "The cleaned article body, free of chrome. " * 30

        page = ScrapeResult(url="https://a.example/", title="T", text="The article body. " * 400)
        with (
            mock.patch("freegem.web_scraper.scrape_page", return_value=page),
            mock.patch("freegem.llm_extract.BridgeProvider", FakeProvider),
        ):
            code, out, _err = self.run_cli(
                ["scrape", "https://a.example/", "--ai-clean", "--no-cache"]
            )
        self.assertEqual(code, 0)
        self.assertIn("ai-clean view", out)
        self.assertIn("cleaned article body", out)

    def test_disabled_without_the_flag_stays_disabled(self) -> None:
        """No --ai-clean and no config: the scrape path never reaches the
        model (the flag gate stays closed)."""
        from freegem.web_scraper import ScrapeResult

        page = ScrapeResult(url="https://a.example/", title="T", text="The article body. " * 400)
        with (
            mock.patch("freegem.web_scraper.scrape_page", return_value=page),
            mock.patch("freegem.llm_extract.BridgeProvider") as provider,
        ):
            code, _out, _ = self.run_cli(["scrape", "https://a.example/", "--no-cache"])
        self.assertEqual(code, 0)
        self.assertFalse(provider.called)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
