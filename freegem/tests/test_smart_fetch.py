"""The smart-fetch fallback ladder: honest first try, legitimate read paths after."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

from freegem.config import BridgeConfig
from freegem.smart_fetch import (
    STRATEGIES,
    StrategyMemory,
    classify_failure,
    clear_memory,
    default_memory_path,
    fallback_scrape,
)
from freegem.web_scraper import ScrapeError, ScrapeResult, scrape_page


class _FakeHeaders:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = data or {}

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.data.get(name, default)


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self._body = body
        self.headers = _FakeHeaders({"Content-Type": content_type})

    def read(self, size: int = -1) -> bytes:
        if not self._body:
            return b""
        out, self._body = self._body, b""
        return out


class ClassifyFailureTests(unittest.TestCase):
    def test_http_statuses_engage_the_ladder(self) -> None:
        for code in (401, 403, 405, 407, 429, 503):
            self.assertEqual(classify_failure(f"HTTP {code} fetching 'x'"), "http")

    def test_network_failures_engage_the_ladder(self) -> None:
        self.assertEqual(classify_failure("fetch failed for 'x': timed out"), "network")
        self.assertEqual(classify_failure("fetch failed for 'x': [Errno 111] refused"), "network")

    def test_content_type_refusal_engages_the_ladder(self) -> None:
        # A PDF the direct path refuses may still be readable through the reader.
        self.assertEqual(classify_failure("refusing content type 'application/pdf'"), "type")

    def test_our_own_refusals_never_engage_the_ladder(self) -> None:
        self.assertEqual(classify_failure("SSRF guard refused 'x': private host"), "refused")
        self.assertEqual(classify_failure("robots.txt disallows scraping 'x'"), "refused")
        self.assertEqual(classify_failure("response exceeds the 5242880-byte cap"), "cap")
        self.assertEqual(
            classify_failure("decompressed response exceeds the 5242880-byte cap"), "cap"
        )


class MemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "smart_fetch_memory.json"
        clear_memory()

    def tearDown(self) -> None:
        clear_memory()

    def test_remember_and_get_roundtrip(self) -> None:
        memory = StrategyMemory(self.path)
        memory.remember("example.com", "reader")
        self.assertEqual(memory.get("example.com"), "reader")
        # A separate instance reads the same file.
        self.assertEqual(StrategyMemory(self.path).get("example.com"), "reader")

    def test_unknown_strategy_not_stored(self) -> None:
        memory = StrategyMemory(self.path)
        memory.remember("example.com", "teleport")
        self.assertIsNone(memory.get("example.com"))

    def test_corrupt_file_is_empty_memory(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        memory = StrategyMemory(self.path)
        self.assertIsNone(memory.get("example.com"))
        memory.remember("example.com", "wayback")
        self.assertEqual(memory.get("example.com"), "wayback")

    def test_default_path_uses_cache_dir_env(self) -> None:
        with mock.patch.dict("os.environ", {"FREEGEM_CACHE_DIR": str(self.tmp.name)}, clear=False):
            self.assertEqual(default_memory_path(), Path(self.tmp.name) / "smart_fetch_memory.json")


class FallbackLadderTests(unittest.TestCase):
    """fallback_scrape walks browser-headers -> reader -> [browser-render,
    when enabled] -> wayback -> ai-search."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(log_requests=False)
        clear_memory()

    def tearDown(self) -> None:
        clear_memory()

    def _ladder(
        self,
        results: Mapping[str, object],
        expect_order: list[str] | None = None,
    ) -> Any:
        """Patch _try_strategy with a scripted outcome per strategy."""

        def try_strategy(
            cfg: Any,
            url: str,
            strategy: str,
            max_chars: int,
            ignore_robots: bool,
        ) -> object:
            if expect_order is not None:
                expect_order.append(strategy)
            outcome = results[strategy]
            if isinstance(outcome, ScrapeError):
                raise outcome
            return outcome

        return try_strategy

    def test_second_strategy_wins_and_is_remembered(self) -> None:
        order: list[str] = []
        ok_page = ScrapeResult(url="https://blocked.example/", title="T", text="real content")
        scripted = {
            "browser-headers": ScrapeError("HTTP 403 fetching 'x'"),
            "reader": ok_page,
        }
        with (
            mock.patch(
                "freegem.smart_fetch._try_strategy", side_effect=self._ladder(scripted, order)
            ),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            result = fallback_scrape(
                self.cfg,
                "https://blocked.example/page",
                max_chars=1000,
                first_error="HTTP 403 fetching 'https://blocked.example/page'",
            )
        self.assertEqual(result.text, "real content")
        self.assertEqual(order, ["browser-headers", "reader"])
        self.assertEqual(StrategyMemory(default_memory_path()).get("blocked.example"), "reader")

    def test_memory_bumps_the_working_strategy_to_front(self) -> None:
        memory = StrategyMemory(default_memory_path())
        memory.remember("blocked.example", "wayback")
        order: list[str] = []
        ok_page = ScrapeResult(url="u", title="T", text="content from wayback")
        scripted = {"wayback": ok_page}
        with (
            mock.patch(
                "freegem.smart_fetch._try_strategy", side_effect=self._ladder(scripted, order)
            ),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            result = fallback_scrape(
                self.cfg, "https://blocked.example/x", max_chars=100, first_error="HTTP 403"
            )
        self.assertEqual(order, ["wayback"])
        self.assertEqual(result.text, "content from wayback")

    def test_second_fetch_on_same_host_reuses_first_fetch_winner(self) -> None:
        """The memory's whole point, end to end: fetch 1's winner is written
        by fetch 1 itself (no pre-seeding), and fetch 2 on the same host
        tries that strategy FIRST — the losing rungs are never touched."""
        ok_page = ScrapeResult(url="u", title="T", text="the article body")
        scripted = {
            "browser-headers": ScrapeError("HTTP 403 fetching 'x'"),
            "reader": ok_page,
            "wayback": ScrapeError("HTTP 403"),
            "ai-search": ScrapeError("bridge unavailable"),
        }
        first_order: list[str] = []
        second_order: list[str] = []
        with (
            mock.patch(
                "freegem.smart_fetch._try_strategy",
                side_effect=self._ladder(scripted, first_order),
            ),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            result1 = fallback_scrape(
                self.cfg, "https://remembered.example/a", max_chars=200, first_error="HTTP 403"
            )
        self.assertEqual(result1.text, "the article body")
        self.assertEqual(first_order, ["browser-headers", "reader"])
        # Written to DISK by fetch 1 — fetch 2 reads it back with no seeding.
        self.assertEqual(StrategyMemory(default_memory_path()).get("remembered.example"), "reader")
        with (
            mock.patch(
                "freegem.smart_fetch._try_strategy",
                side_effect=self._ladder(scripted, second_order),
            ),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            result2 = fallback_scrape(
                self.cfg, "https://remembered.example/b", max_chars=200, first_error="HTTP 403"
            )
        self.assertEqual(result2.text, "the article body")
        self.assertEqual(second_order, ["reader"])

    def test_our_refusal_is_reraised_untouched(self) -> None:
        with mock.patch("freegem.smart_fetch._try_strategy") as try_strategy:
            with self.assertRaises(ScrapeError) as caught:
                fallback_scrape(
                    self.cfg,
                    "http://127.0.0.1:8080/admin",
                    max_chars=100,
                    first_error="SSRF guard refused 'http://127.0.0.1:8080/admin': loopback",
                )
        self.assertIn("SSRF guard", str(caught.exception))
        self.assertFalse(try_strategy.called)

    def test_total_failure_raises_honest_taxonomy(self) -> None:
        order: list[str] = []
        refusal = ScrapeError("HTTP 403 fetching 'x'")
        scripted = dict.fromkeys(STRATEGIES, refusal)
        with (
            mock.patch(
                "freegem.smart_fetch._try_strategy", side_effect=self._ladder(scripted, order)
            ),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            with self.assertRaises(ScrapeError) as caught:
                fallback_scrape(
                    self.cfg, "https://hard.example/", max_chars=100, first_error="HTTP 403"
                )
        message = str(caught.exception)
        self.assertIn("every read path refused", message)
        self.assertIn("direct: HTTP 403", message)
        for name in STRATEGIES:
            self.assertIn(name, message)
        self.assertEqual(len(order), len(STRATEGIES))

    def test_challenge_page_is_not_content(self) -> None:
        """A 'successful' fetch of a Cloudflare-style wall continues the ladder."""
        order: list[str] = []
        challenge = ScrapeResult(url="u", title="T", text="Just a moment... Verify you are human.")
        ok_page = ScrapeResult(url="u", title="T", text="the actual article " * 100)
        scripted = {"browser-headers": challenge, "reader": ok_page}
        with (
            mock.patch(
                "freegem.smart_fetch._try_strategy", side_effect=self._ladder(scripted, order)
            ),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            result = fallback_scrape(
                self.cfg, "https://cloudguarded.example/", max_chars=2000, first_error="HTTP 405"
            )
        self.assertEqual(order, ["browser-headers", "reader"])
        self.assertIn("actual article", result.text)

    def test_ai_search_attempted_without_session(self) -> None:
        """No cookie file: ai-search is still ATTEMPTED (anonymous Gemini works
        on many networks); failure is recorded honestly by the ladder."""
        order: list[str] = []
        refusal = ScrapeError("HTTP 403")
        no_session = ScrapeError("bridge refused the anonymous request")
        scripted = {
            "browser-headers": refusal,
            "reader": refusal,
            "browser-render": ScrapeError("browser-render disabled (smart_fetch_browser_render=false)"),
            "wayback": refusal,
            "ai-search": no_session,
        }
        self.assertFalse(self.cfg.has_session_cookie())
        with (
            mock.patch(
                "freegem.smart_fetch._try_strategy", side_effect=self._ladder(scripted, order)
            ),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            with self.assertRaises(ScrapeError) as caught:
                fallback_scrape(
                    self.cfg, "https://x.example/", max_chars=100, first_error="HTTP 403"
                )
        self.assertEqual(len(order), len(STRATEGIES))
        self.assertIn("ai-search", str(caught.exception))

    def test_reader_headers_carry_key_only_when_configured(self) -> None:
        from freegem.smart_fetch import _reader_headers

        anon = _reader_headers(BridgeConfig(log_requests=False))
        self.assertNotIn("Authorization", anon)
        keyed = _reader_headers(BridgeConfig(log_requests=False, reader_api_key="rk_test"))
        self.assertEqual(keyed["Authorization"], "Bearer rk_test")

    def test_ai_read_disabled_by_config(self) -> None:
        from freegem.smart_fetch import _ai_read

        cfg = BridgeConfig(log_requests=False, smart_fetch_ai=False)
        with self.assertRaises(ScrapeError) as caught:
            _ai_read(cfg, "https://x.example/", 1000)
        self.assertIn("disabled", str(caught.exception))

    def test_ai_search_rung_is_time_bounded(self) -> None:
        """(#25) A wedged generation cannot hold the rung: _ai_read raises an
        honest ScrapeError once the bound lapses, without waiting for the
        model call to finish."""
        from freegem.smart_fetch import _ai_read

        def wedged_run_tool(cfg, name, kwargs):
            time.sleep(1.0)
            return "a late answer that will be discarded"

        with (
            mock.patch("freegem.smart_fetch._AI_SEARCH_TIMEOUT_SEC", 0.1),
            mock.patch("freegem.mcp_tools.run_tool", side_effect=wedged_run_tool),
        ):
            started = time.monotonic()
            with self.assertRaises(ScrapeError) as caught:
                _ai_read(BridgeConfig(log_requests=False), "https://x.example/", 1000)
        self.assertLess(time.monotonic() - started, 0.9, "the bound held")
        self.assertIn("timed out", str(caught.exception))

    def test_ai_search_returns_within_the_bound(self) -> None:
        """(#25) The bound does not disturb a healthy rung."""
        from freegem.smart_fetch import _ai_read

        with mock.patch("freegem.mcp_tools.run_tool", return_value="word " * 30):
            result = _ai_read(BridgeConfig(log_requests=False), "https://x.example/", 1000)
        self.assertEqual(result.via, "ai-search")
        self.assertIn("word", result.text)
        self.assertFalse(result.truncated)

    def test_progress_receives_ladder_steps(self) -> None:
        events: list[str] = []
        ok_page = ScrapeResult(url="u", title="T", text="content")
        scripted = {"browser-headers": ok_page}
        with (
            mock.patch("freegem.smart_fetch._try_strategy", side_effect=self._ladder(scripted)),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            fallback_scrape(
                self.cfg,
                "https://p.example/",
                max_chars=100,
                first_error="HTTP 403",
                progress=events.append,
            )
        self.assertTrue(any("browser-headers" in event for event in events))
        self.assertTrue(any("read" in event for event in events))

    def test_ladder_budget_skips_rungs_and_records_it(self) -> None:
        """(#25) Past the wall-clock budget the remaining rungs are skipped
        and the taxonomy says so, instead of stacking minutes of silence."""
        order: list[str] = []
        scripted = {name: ScrapeError("HTTP 403") for name in STRATEGIES}
        with (
            mock.patch("freegem.smart_fetch._LADDER_BUDGET_SEC", 0.0),
            mock.patch(
                "freegem.smart_fetch._try_strategy", side_effect=self._ladder(scripted, order)
            ),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            with self.assertRaises(ScrapeError) as caught:
                fallback_scrape(
                    self.cfg, "https://slow.example/", max_chars=100, first_error="HTTP 403"
                )
        self.assertEqual(order, [], "no rung may run once the budget is gone")
        message = str(caught.exception)
        self.assertEqual(message.count("ladder budget was exhausted"), len(STRATEGIES))
        self.assertIn("skipped", message)

    def test_healthy_ladder_still_runs_inside_the_budget(self) -> None:
        """(#25) The default budget never clips a normal two-rung walk."""
        order: list[str] = []
        ok_page = ScrapeResult(url="u", title="T", text="real content")
        scripted = {
            "browser-headers": ScrapeError("HTTP 403"),
            "reader": ok_page,
        }
        with (
            mock.patch(
                "freegem.smart_fetch._try_strategy", side_effect=self._ladder(scripted, order)
            ),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            result = fallback_scrape(
                self.cfg, "https://ok.example/", max_chars=100, first_error="HTTP 403"
            )
        self.assertEqual(order, ["browser-headers", "reader"])
        self.assertEqual(result.text, "real content")


class ReaderParseTests(unittest.TestCase):
    def test_reader_envelope_is_unwrapped(self) -> None:
        from freegem.smart_fetch import _parse_reader_output

        raw = ScrapeResult(
            url="https://r.jina.ai/https://news.example/a",
            title="Title: The Headline\n",
            text=(
                "Title: The Headline\n"
                "URL Source: https://news.example/a\n"
                "Markdown Content:\n"
                "Body of the article.\n"
            ),
        )
        parsed = _parse_reader_output(raw, "https://news.example/a", 1000)
        self.assertEqual(parsed.url, "https://news.example/a")
        self.assertEqual(parsed.title, "The Headline")
        self.assertEqual(parsed.text, "Body of the article.")
        self.assertEqual(parsed.via, "reader")

    def test_reader_envelope_absent_keeps_raw_text(self) -> None:
        from freegem.smart_fetch import _parse_reader_output

        raw = ScrapeResult(url="https://r.jina.ai/x", title="", text="plain body")
        parsed = _parse_reader_output(raw, "https://news.example/a", 1000)
        self.assertEqual(parsed.text, "plain body")

    def test_reader_output_retruncated_to_caller_budget(self) -> None:
        """(#22) The rung fetches with 2x headroom but returns the caller's cut."""
        from freegem.smart_fetch import _parse_reader_output

        body = "word " * 200  # 1000 chars of body behind the envelope
        raw_text = f"Title: T\nURL Source: https://news.example/a\nMarkdown Content:\n{body}"
        raw = ScrapeResult(url="https://r.jina.ai/x", title="T", text=raw_text)
        parsed = _parse_reader_output(raw, "https://news.example/a", 250)
        self.assertLessEqual(len(parsed.text), 250)
        self.assertTrue(parsed.truncated, "a cut at the caller's budget must be flagged")

    def test_reader_truncated_flag_reports_caller_budget_not_internal(self) -> None:
        """(#22) A body that FITS the caller's budget is not flagged as cut,
        even when the internal 2x-budget fetch flagged itself truncated."""
        from freegem.smart_fetch import _parse_reader_output

        raw = ScrapeResult(
            url="https://r.jina.ai/x",
            title="",
            text="Title: T\nURL Source: u\nMarkdown Content:\nshort body",
            truncated=True,  # the INTERNAL fetch hit its own 2x cap
        )
        parsed = _parse_reader_output(raw, "https://news.example/a", 2000)
        self.assertEqual(parsed.text, "short body")
        self.assertFalse(parsed.truncated, "the flag answers to the caller's budget")

    def test_reader_rung_requests_headroom_and_returns_caller_cut(self) -> None:
        """(#22) End to end through _try_strategy: 2x fetch budget in, caller
        budget out."""
        from freegem.smart_fetch import _try_strategy

        body = "word " * 200
        envelope = f"Title: T\nURL Source: https://news.example/a\nMarkdown Content:\n{body}"

        def fake_scrape_page(cfg, url, **kwargs):
            # The reader rung must ask for parse headroom, not caller budget.
            assert kwargs["max_chars"] == 500, "reader rung fetches with 2x headroom"
            return ScrapeResult(url=url, title="T", text=envelope)

        with mock.patch("freegem.smart_fetch.scrape_page", side_effect=fake_scrape_page):
            result = _try_strategy(
                BridgeConfig(log_requests=False), "https://news.example/a", "reader", 250, True
            )
        self.assertEqual(result.url, "https://news.example/a")
        self.assertLessEqual(len(result.text), 250)
        self.assertTrue(result.truncated)


class WaybackSnapshotTests(unittest.TestCase):
    """(#23) The availability API's snapshot must be FOR the requested page."""

    def _snapshot(self, snapshot_url: str) -> Any:
        from freegem.smart_fetch import _wayback_snapshot

        body = json.dumps({"archived_snapshots": {"closest": {"url": snapshot_url}}}).encode()
        with mock.patch("freegem.web_scraper._fetch", return_value=_FakeResponse(body)):
            return _wayback_snapshot(BridgeConfig(log_requests=False), "https://news.example/a")

    def test_matching_snapshot_returned_and_upgraded_to_https(self) -> None:
        out = self._snapshot("http://web.archive.org/web/20240101000000/https://news.example/a")
        self.assertEqual(out, "https://web.archive.org/web/20240101000000/https://news.example/a")

    def test_matching_snapshot_with_id_modifier(self) -> None:
        out = self._snapshot("https://web.archive.org/web/20240101000000id_/https://news.example/a")
        self.assertIn("/a", out)

    def test_bare_domain_snapshot_matches(self) -> None:
        from freegem.smart_fetch import _wayback_snapshot

        # Bare domains: /web/<stamp>/https://example.com (no path) and the
        # trailing-slash variant must both verify.
        for snap in (
            "https://web.archive.org/web/20240101/https://example.com",
            "https://web.archive.org/web/20240101/https://example.com/",
        ):
            body = json.dumps({"archived_snapshots": {"closest": {"url": snap}}}).encode()
            with mock.patch("freegem.web_scraper._fetch", return_value=_FakeResponse(body)):
                out = _wayback_snapshot(BridgeConfig(log_requests=False), "https://example.com")
            self.assertEqual(out, snap)

    def test_snapshot_for_a_different_host_is_rejected(self) -> None:
        with self.assertRaises(ScrapeError) as caught:
            self._snapshot("https://web.archive.org/web/20240101000000/https://OTHER.example/a")
        self.assertIn("different page", str(caught.exception))

    def test_snapshot_for_a_different_path_is_rejected(self) -> None:
        with self.assertRaises(ScrapeError) as caught:
            self._snapshot("https://web.archive.org/web/20240101000000/https://news.example/OTHER")
        self.assertIn("different page", str(caught.exception))

    def test_snapshot_without_a_web_prefix_is_rejected(self) -> None:
        with self.assertRaises(ScrapeError) as caught:
            self._snapshot("https://elsewhere.example/mirror/news.example/a")
        self.assertIn("different page", str(caught.exception))

    def test_rejection_engages_the_ladder(self) -> None:
        """(#23) The mismatch refusal classifies as the site's failure, not
        ours, so the ladder records it and moves on to the next rung."""
        from freegem.smart_fetch import classify_failure

        kind = classify_failure(
            "wayback availability API returned a snapshot for a different page than 'u'"
        )
        self.assertEqual(kind, "network")

    def test_mismatched_snapshot_continues_to_the_next_rung(self) -> None:
        """(#23) End to end: wayback refuses the wrong-page snapshot, the
        ladder proceeds and the next rung rescues the fetch."""
        order: list[str] = []
        refusal = ScrapeError("HTTP 403 fetching 'x'")
        ok_page = ScrapeResult(url="u", title="T", text="rescued after wayback")
        scripted = {
            "browser-headers": refusal,
            "reader": refusal,
            "browser-render": ScrapeError("browser-render disabled (smart_fetch_browser_render=false)"),
            "wayback": ScrapeError(
                "wayback availability API returned a snapshot for a different page"
            ),
            "ai-search": ok_page,
        }

        def try_strategy(cfg, url, strategy, max_chars, ignore_robots):
            order.append(strategy)
            outcome = scripted[strategy]
            if isinstance(outcome, ScrapeError):
                raise outcome
            return outcome

        cfg = BridgeConfig(log_requests=False)
        with (
            mock.patch("freegem.smart_fetch._try_strategy", side_effect=try_strategy),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ):
            result = fallback_scrape(cfg, "https://w.example/", max_chars=100, first_error="403")
        self.assertEqual(
            order,
            ["browser-headers", "reader", "browser-render", "wayback", "ai-search"],
        )
        self.assertEqual(result.text, "rescued after wayback")


class SmartIntegrationTests(unittest.TestCase):
    """scrape_page(smart=True) falls back instead of raising."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(log_requests=False)
        clear_memory()

    def tearDown(self) -> None:
        clear_memory()

    def test_direct_success_never_touches_the_ladder(self) -> None:
        response = _FakeResponse(b"<html><body><p>plain page</p></body></html>")
        with (
            mock.patch("freegem.web_scraper._fetch", return_value=response) as fetch,
            mock.patch("freegem.smart_fetch.fallback_scrape") as fallback,
        ):
            result = scrape_page(self.cfg, "https://ok.example/page", smart=True)
        self.assertEqual(result.text, "plain page")
        self.assertFalse(fallback.called)
        # No browser-identifying override on the first, honest attempt.
        self.assertIsNone(fetch.call_args.kwargs["headers"])

    def test_direct_failure_walks_the_ladder(self) -> None:
        saved = ScrapeResult(url="u", title="T", text="rescued")
        with (
            mock.patch(
                "freegem.web_scraper._fetch",
                side_effect=ScrapeError("HTTP 403 fetching 'u'"),
            ),
            mock.patch("freegem.smart_fetch.fallback_scrape", return_value=saved) as fallback,
        ):
            result = scrape_page(self.cfg, "https://blocked.example/", smart=True)
        self.assertEqual(result.text, "rescued")
        self.assertTrue(fallback.called)
        self.assertIn("HTTP 403", fallback.call_args.kwargs["first_error"])

    def test_smart_off_by_default(self) -> None:
        with (
            mock.patch(
                "freegem.web_scraper._fetch",
                side_effect=ScrapeError("HTTP 403 fetching 'u'"),
            ),
            mock.patch("freegem.smart_fetch.fallback_scrape") as fallback,
        ):
            with self.assertRaises(ScrapeError):
                scrape_page(self.cfg, "https://blocked.example/")
        self.assertFalse(fallback.called)

    def test_challenge_shaped_direct_200_walks_the_ladder(self) -> None:
        """(#12) A 200 that is really a bot wall is not a success."""
        response = _FakeResponse(
            b"<html><body><p>Just a moment... enable javascript and cookies</p></body></html>"
        )
        saved = ScrapeResult(url="u", title="T", text="rescued by the ladder")
        with (
            mock.patch("freegem.web_scraper._fetch", return_value=response),
            mock.patch("freegem.smart_fetch.fallback_scrape", return_value=saved) as fallback,
        ):
            result = scrape_page(self.cfg, "https://cloudguarded.example/", smart=True)
        self.assertEqual(result.text, "rescued by the ladder")
        self.assertTrue(fallback.called)
        self.assertIn("bot-challenge", fallback.call_args.kwargs["first_error"])

    def test_long_page_mentioning_challenge_words_is_content(self) -> None:
        """(#12) The challenge detector only fires on SHORT pages: an article
        that quotes 'captcha' past the detector's length cap stays content."""
        body = ("<p>" + "real article content " * 60 + "about the captcha economy</p>").encode()
        response = _FakeResponse(b"<html><body>" + body + b"</body></html>")
        with (
            mock.patch("freegem.web_scraper._fetch", return_value=response),
            mock.patch("freegem.smart_fetch.fallback_scrape") as fallback,
        ):
            result = scrape_page(self.cfg, "https://news.example/captcha-economy", smart=True)
        self.assertIn("captcha economy", result.text)
        self.assertFalse(fallback.called)

    def test_challenge_check_only_when_smart(self) -> None:
        """(#12) With the ladder off, a challenge 200 is returned as-is (the
        caller asked for the honest direct path, not alternate reads)."""
        response = _FakeResponse(
            b"<html><body><p>Just a moment... verify you are human</p></body></html>"
        )
        with (
            mock.patch("freegem.web_scraper._fetch", return_value=response),
            mock.patch("freegem.smart_fetch.fallback_scrape") as fallback,
        ):
            result = scrape_page(self.cfg, "https://cloudguarded.example/", smart=False)
        self.assertIn("Just a moment", result.text)
        self.assertFalse(fallback.called)

    def test_browser_headers_override_threaded_to_fetch(self) -> None:
        from freegem.smart_fetch import _BROWSER_HEADERS

        response = _FakeResponse(b"<html><body><p>behind a UA wall</p></body></html>")
        with mock.patch("freegem.web_scraper._fetch", return_value=response) as fetch:
            result = scrape_page(
                self.cfg,
                "https://uawall.example/",
                smart=False,
                headers_override=_BROWSER_HEADERS,
            )
        self.assertIn("Mozilla/5.0", fetch.call_args.kwargs["headers"]["User-Agent"])
        self.assertEqual(result.text, "behind a UA wall")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
