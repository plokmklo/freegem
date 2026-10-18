"""Fetch-feature tests: the batch concurrency ceiling (#15), tag-level content
selection (#29), the optional browser-render ladder rung (#14), and the
research-cache de-poisoning residual (#21).

Hermetic by construction: every test patches the network seam
(``web_scraper._fetch`` / ``web_scraper.scrape_page``) or the Playwright
entry point -- no test performs a real HTTP request or launches a browser.
"""

from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from freegem.config import BridgeConfig
from freegem.smart_fetch import STRATEGIES, clear_memory
from freegem.web_scraper import (
    ScrapeError,
    ScrapeResult,
    _clamp_batch_concurrency,
    scrape_many,
    scrape_page,
)


def _recording_pool() -> tuple[Any, list[int | None]]:
    """Patch ``ThreadPoolExecutor`` to record ``max_workers``, return a real pool.

    scrape_many imports the pool inside its function body, so patching the
    ``concurrent.futures`` attribute is what the call-time import resolves.
    The wrapper returns a REAL executor, so behavior is untouched.
    """
    import concurrent.futures

    real_pool = concurrent.futures.ThreadPoolExecutor
    seen: list[int | None] = []

    def recording_pool(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("max_workers"))
        return real_pool(*args, **kwargs)

    return mock.patch("concurrent.futures.ThreadPoolExecutor", new=recording_pool), seen


def _ok_scrape(cfg: Any, url: str, **_kwargs: Any) -> ScrapeResult:
    """A scrape_page stand-in: every URL succeeds with a little text."""
    return ScrapeResult(url=url, title="T", text="batch page text")


class BatchConcurrencyClampTests(unittest.TestCase):
    """#15: the batch ceiling is config-driven and a clamp says so on stderr."""

    def test_within_cap_not_clamped_and_silent(self) -> None:
        cfg = BridgeConfig(guard=True, batch_max_concurrency=32)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(_clamp_batch_concurrency(cfg, 20), 20)
        self.assertEqual(err.getvalue(), "")

    def test_over_default_cap_clamped_with_exactly_one_warning(self) -> None:
        cfg = BridgeConfig(
            guard=True,
        )  # default ceiling: 16
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(_clamp_batch_concurrency(cfg, 20), 16)
        self.assertEqual(
            err.getvalue(),
            "scrape: requested concurrency 20 clamped to 16 (batch_max_concurrency)\n",
        )

    def test_raised_ceiling_never_prints_within_bounds(self) -> None:
        cfg = BridgeConfig(guard=True, batch_max_concurrency=32)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(_clamp_batch_concurrency(cfg, 20), 20)
            self.assertEqual(_clamp_batch_concurrency(cfg, 32), 32)
            self.assertEqual(_clamp_batch_concurrency(cfg, 1), 1)
        self.assertEqual(err.getvalue(), "")

    def test_zero_or_negative_cap_floors_at_one_with_warning(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(
                _clamp_batch_concurrency(BridgeConfig(guard=True, batch_max_concurrency=0), 20), 1
            )
            self.assertEqual(
                _clamp_batch_concurrency(BridgeConfig(guard=True, batch_max_concurrency=-3), 5), 1
            )
        self.assertEqual(err.getvalue().count("clamped to 1 (batch_max_concurrency)"), 2)

    def test_request_below_floor_is_normalized_silently(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(
                _clamp_batch_concurrency(
                    BridgeConfig(
                        guard=True,
                    ),
                    0,
                ),
                1,
            )
            self.assertEqual(
                _clamp_batch_concurrency(
                    BridgeConfig(
                        guard=True,
                    ),
                    -5,
                ),
                1,
            )
        self.assertEqual(err.getvalue(), "")


class BatchPoolWidthTests(unittest.TestCase):
    """The clamp feeds the pool: ``max_workers`` honors the config ceiling (#15)."""

    def _run_batch(self, cfg: BridgeConfig, requested: int, count: int = 40) -> list[int | None]:
        patcher, seen = _recording_pool()
        with patcher, mock.patch("freegem.web_scraper.scrape_page", _ok_scrape):
            items = scrape_many(
                cfg,
                [f"https://host{i}.example/p" for i in range(count)],
                max_chars=200,
                concurrency=requested,
                smart=False,
            )
        self.assertTrue(all(item.ok for item in items))
        return seen

    def test_raised_ceiling_builds_the_bigger_pool(self) -> None:
        self.assertEqual(
            self._run_batch(BridgeConfig(guard=True, batch_max_concurrency=32), 20), [20]
        )

    def test_default_ceiling_still_clamps_to_sixteen_once(self) -> None:
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            seen = self._run_batch(
                BridgeConfig(
                    guard=True,
                ),
                20,
            )
        self.assertEqual(seen, [16])
        # ONE warning, on stderr only: stdout stays pipeline-safe.
        self.assertEqual(err.getvalue().count("clamped to 16 (batch_max_concurrency)"), 1)
        self.assertEqual(out.getvalue(), "")

    def test_zero_ceiling_takes_the_sequential_path(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            seen = self._run_batch(BridgeConfig(guard=True, batch_max_concurrency=0), 20, count=3)
        self.assertEqual(seen, [], "a floor-1 batch needs no pool at all")
        self.assertEqual(err.getvalue().count("clamped to 1 (batch_max_concurrency)"), 1)


#: Fixture page for tag selection. The sharp assertions use NON-extractor-
#: skipped wrappers (``div``/``article``): ``nav``/``footer`` text is dropped
#: by the default extractor anyway, so the nav element rides along to prove
#: it never leaks into a selection, while the filter assertions that must
#: DISTINGUISH behavior use elements a plain extraction keeps.
TAG_PAGE = """<!DOCTYPE html>
<html><head><title>Fixture Gazette</title></head>
<body>
<nav id="topnav"><a href="/nav">Nav links only</a></nav>
<div class="menu"><p>Menu lead-in text here.</p></div>
<article class="story" id="lead"><h1>Headline Here</h1>
<p>Article body paragraph with &amp; entity.</p>
<div class="inline-note">A note inside the article.</div>
</article>
<div class="ads"><p>Advert copy text here.</p></div>
<footer><p>Footer boilerplate text.</p></footer>
</body></html>
"""

_TAG_URL = "https://fixture.example/page"


class _FakeHeaders:
    def __init__(self, data: dict[str, str] | None = None) -> None:
        self.data = data or {}

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.data.get(name, default)


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8") -> None:
        self._body = body
        self.headers = _FakeHeaders(
            {"Content-Type": content_type, "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"}
        )

    def read(self, size: int = -1) -> bytes:
        if not self._body:
            return b""
        out, self._body = self._body, b""
        return out


class TagSelectionTests(unittest.TestCase):
    """#29: include_tags/exclude_tags shape the extraction, default is untouched."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(guard=True, log_requests=False)

    def _scrape(self, **kwargs: Any) -> ScrapeResult:
        with mock.patch(
            "freegem.web_scraper._fetch",
            return_value=_FakeResponse(TAG_PAGE.encode("utf-8")),
        ):
            return scrape_page(self.cfg, _TAG_URL, **kwargs)

    def _plain_extract(self, html: str) -> str:
        """The pre-#29 pipeline, reproduced from the still-current primitives:
        parse + clean. This is the golden string the default path must match."""
        from freegem.web_scraper import _clean_text, _parse_html

        extractor = _parse_html(self.cfg, _TAG_URL, html)
        return _clean_text(extractor.text_parts)

    def test_default_path_is_byte_identical_to_the_old_pipeline(self) -> None:
        result = self._scrape()
        self.assertEqual(result.text, self._plain_extract(TAG_PAGE))
        self.assertIn("Menu lead-in text here.", result.text)
        self.assertIn("Headline Here", result.text)
        self.assertIn("Advert copy text here.", result.text)
        # Extractor-skipped elements stay skipped in the default path.
        self.assertNotIn("Footer boilerplate", result.text)
        self.assertNotIn("Nav links only", result.text)
        self.assertEqual(result.title, "Fixture Gazette")

    def test_include_tags_extracts_only_the_article(self) -> None:
        result = self._scrape(include_tags=["article"])
        self.assertIn("Headline Here", result.text)
        self.assertIn("Article body paragraph with & entity.", result.text)
        self.assertIn("A note inside the article.", result.text)
        self.assertNotIn("Menu lead-in", result.text)
        self.assertNotIn("Advert copy", result.text)
        self.assertNotIn("Footer boilerplate", result.text)
        # The selection cut <head>; the title survives from the full document.
        self.assertEqual(result.title, "Fixture Gazette")

    def test_exclude_tags_drops_the_matching_element(self) -> None:
        result = self._scrape(exclude_tags=[".ads"])
        self.assertIn("Menu lead-in text here.", result.text)
        self.assertIn("Headline Here", result.text)
        self.assertNotIn("Advert copy", result.text)
        # Everything else is exactly the untouched extraction minus the ads.
        self.assertEqual(
            result.text,
            self._plain_extract(
                TAG_PAGE.replace('<div class="ads"><p>Advert copy text here.</p></div>', "")
            ),
        )

    def test_exclude_nav_matches_the_untouched_extraction(self) -> None:
        # nav is skipped by the extractor anyway, so excluding it must not
        # disturb anything else: byte-identical to the default pipeline.
        result = self._scrape(exclude_tags=["nav"])
        self.assertEqual(result.text, self._plain_extract(TAG_PAGE))
        self.assertIn("Menu lead-in", result.text)

    def test_include_and_exclude_compose(self) -> None:
        result = self._scrape(include_tags=["article"], exclude_tags=[".inline-note"])
        self.assertIn("Headline Here", result.text)
        self.assertIn("Article body paragraph", result.text)
        self.assertNotIn("A note inside the article.", result.text)
        self.assertNotIn("Advert copy", result.text)

    def test_class_and_id_selectors_and_comma_alternatives(self) -> None:
        by_class = self._scrape(include_tags=[".story"])
        self.assertIn("Headline Here", by_class.text)
        self.assertNotIn("Advert copy", by_class.text)
        by_id = self._scrape(include_tags=["#lead"])
        self.assertEqual(by_id.text, by_class.text)
        comma = self._scrape(include_tags=["h1, .ads"])
        self.assertIn("Headline Here", comma.text)
        self.assertIn("Advert copy text here.", comma.text)
        self.assertNotIn("Menu lead-in", comma.text)

    def test_include_matching_nothing_yields_empty_text(self) -> None:
        result = self._scrape(include_tags=["#does-not-exist"])
        self.assertEqual(result.text, "")
        self.assertEqual(result.title, "Fixture Gazette")

    def test_exclude_only_still_uses_main_region_anchoring(self) -> None:
        # Exclusion-only composes with the existing main-content anchoring:
        # the filter runs first, the anchor then keeps the surrounding chrome
        # out (include_tags skips anchoring -- the selection IS the region).
        page = (
            "<html><head><title>Wiki Fixture</title></head><body>"
            '<div class="sidebar">Sidebar chatter text.</div>'
            '<div id="mw-content-text"><p>' + ("Main article text lives here. " * 10) + "</p>"
            '<div class="vw-junk">Junk inside the content.</div></div>'
            "</body></html>"
        )
        with mock.patch(
            "freegem.web_scraper._fetch", return_value=_FakeResponse(page.encode("utf-8"))
        ):
            result = scrape_page(self.cfg, _TAG_URL, exclude_tags=[".vw-junk"])
        self.assertIn("Main article text lives here.", result.text)
        self.assertNotIn("Junk inside the content.", result.text)
        self.assertNotIn("Sidebar chatter", result.text)

    def test_unclosed_paragraphs_do_not_fuse_regions(self) -> None:
        page = (
            "<html><head><title>T</title></head><body>"
            "<p class='keep'>first para"
            "<p class='keep'>second para"
            "<div>tail text</div>"
            "</body></html>"
        )
        with mock.patch(
            "freegem.web_scraper._fetch", return_value=_FakeResponse(page.encode("utf-8"))
        ):
            result = scrape_page(self.cfg, _TAG_URL, include_tags=["p.keep"])
        self.assertIn("first para", result.text)
        self.assertIn("second para", result.text)
        self.assertNotIn("tail text", result.text)

    def test_unsupported_selector_is_rejected_loudly(self) -> None:
        with self.assertRaises(ValueError):
            self._scrape(include_tags=["div > p"])

    def test_tag_selection_never_touches_the_cache(self) -> None:
        # A selected view is caller-specific: reading OR writing it into the
        # shared cache would poison full-page entries (#21's lesson).
        cache = mock.Mock()
        cache.get.return_value = {
            "title": "Poison",
            "text": "cached text",
            "fetched_at": "1",
            "last_modified": None,
        }
        with mock.patch(
            "freegem.web_scraper._fetch",
            return_value=_FakeResponse(TAG_PAGE.encode("utf-8")),
        ):
            result = scrape_page(self.cfg, _TAG_URL, include_tags=["article"], cache=cache)
        self.assertIn("Headline Here", result.text)
        cache.get.assert_not_called()
        cache.put.assert_not_called()
        # And without tags the same cache IS used, unchanged behavior.
        with mock.patch(
            "freegem.web_scraper._fetch",
            return_value=_FakeResponse(TAG_PAGE.encode("utf-8")),
        ):
            plain = scrape_page(self.cfg, _TAG_URL, cache=cache)
        self.assertEqual(plain.via, "cache")
        self.assertEqual(plain.text, "cached text")


RENDERED_HTML = (
    "<html><head><title>Rendered Fresh</title></head><body><main>"
    "<p>Client-side article text, rendered fresh by the browser.</p>"
    "</main></body></html>"
)
_SNAPSHOT_HTML = "<html><body><p>archived copy text for the blocked page</p></body></html>"
_BLOCKED_URL = "https://blocked.example/page"


class _FakeLadder:
    """Scripted seams for ladder walks: the network and the browser."""

    def __init__(
        self,
        *,
        render: Any = None,
        playwright_installed: bool = True,
        snapshot: str | None = _SNAPSHOT_HTML,
    ) -> None:
        self.events: list[tuple[str, str]] = []
        self._render = render
        self._installed = playwright_installed
        self._snapshot = snapshot

    def fetch(self, _cfg: Any, url: str, timeout: float = 30, headers: Any = None) -> Any:
        self.events.append(("fetch", url))
        if url.startswith("https://archive.org/wayback/available"):
            import json as _json

            body = _json.dumps(
                {
                    "archived_snapshots": {
                        "closest": {
                            "url": "https://web.archive.org/web/20240101000000/" + _BLOCKED_URL
                        }
                    }
                }
            ).encode("utf-8")
            return _FakeResponse(body, "application/json")
        if url.startswith("https://web.archive.org/web/"):
            return _FakeResponse((self._snapshot or "").encode("utf-8"))
        raise ScrapeError(f"HTTP 403 fetching {url!r}")

    def render(self, opts: Any) -> Any:
        self.events.append(("render", opts.url))
        outcome = self._render
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def patches(self) -> list[Any]:
        return [
            mock.patch("freegem.web_scraper._fetch", side_effect=self.fetch),
            mock.patch(
                "freegem.playwright_fetcher.is_playwright_available",
                return_value=self._installed,
            ),
            mock.patch("freegem.playwright_fetcher.fetch_with_playwright", side_effect=self.render),
            mock.patch("freegem.smart_fetch.time.sleep"),
        ]


class BrowserRenderRungTests(unittest.TestCase):
    """#14: the optional browser-render rung -- after reader, before wayback."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(guard=True, log_requests=False, smart_fetch_browser_render=True)
        clear_memory()

    def tearDown(self) -> None:
        clear_memory()

    def _ok_render(self) -> Any:
        from freegem.playwright_fetcher import PlaywrightResult

        return PlaywrightResult(
            url=_BLOCKED_URL,
            final_url=_BLOCKED_URL,
            status=200,
            text=RENDERED_HTML,
            title="Rendered Fresh",
        )

    def test_rung_sits_between_reader_and_wayback(self) -> None:
        from freegem.smart_fetch import STRATEGIES

        self.assertEqual(
            STRATEGIES, ("browser-headers", "reader", "browser-render", "wayback", "ai-search")
        )

    def test_render_rescues_a_page_the_http_rungs_cannot_read(self) -> None:
        from freegem.smart_fetch import fallback_scrape

        ladder = _FakeLadder(render=self._ok_render())
        with contextlib.ExitStack() as stack:
            for patcher in ladder.patches():
                stack.enter_context(patcher)
            result = fallback_scrape(self.cfg, _BLOCKED_URL, max_chars=2000, first_error="HTTP 403")
        self.assertEqual(result.via, "browser-render")
        self.assertIn("Client-side article text", result.text)
        self.assertEqual(result.title, "Rendered Fresh")
        # The render came after the two HTTP rungs and wayback never ran.
        kinds = [kind for kind, _ in ladder.events]
        self.assertIn("render", kinds)
        self.assertNotIn("https://archive.org", " ".join(url for _, url in ladder.events))

    def test_render_is_attempted_before_wayback(self) -> None:
        """A fresh render beats an old archive: when the render fails, the
        wayback rung still runs AFTER it."""
        from freegem.smart_fetch import fallback_scrape

        ladder = _FakeLadder(render=RuntimeError("Playwright package is not installed."))
        with contextlib.ExitStack() as stack:
            for patcher in ladder.patches():
                stack.enter_context(patcher)
            result = fallback_scrape(self.cfg, _BLOCKED_URL, max_chars=2000, first_error="HTTP 403")
        self.assertEqual(result.via, "wayback")
        self.assertIn("archived copy text", result.text)
        wayback_at = next(i for i, (_, url) in enumerate(ladder.events) if "archive.org" in url)
        render_at = next(i for i, (kind, _) in enumerate(ladder.events) if kind == "render")
        self.assertLess(render_at, wayback_at)

    def test_disabled_by_default_never_calls_the_browser(self) -> None:
        from freegem.smart_fetch import fallback_scrape

        cfg = BridgeConfig(guard=True, log_requests=False)  # smart_fetch_browser_render=False
        ladder = _FakeLadder(render=self._ok_render())
        with contextlib.ExitStack() as stack:
            for patcher in ladder.patches():
                stack.enter_context(patcher)
            result = fallback_scrape(cfg, _BLOCKED_URL, max_chars=2000, first_error="HTTP 403")
        self.assertEqual(result.via, "wayback")
        self.assertNotIn("render", [kind for kind, _ in ladder.events])

    def test_playwright_not_installed_continues_to_wayback(self) -> None:
        from freegem.smart_fetch import fallback_scrape

        ladder = _FakeLadder(render=self._ok_render(), playwright_installed=False)
        with contextlib.ExitStack() as stack:
            for patcher in ladder.patches():
                stack.enter_context(patcher)
            result = fallback_scrape(self.cfg, _BLOCKED_URL, max_chars=2000, first_error="HTTP 403")
        self.assertEqual(result.via, "wayback")
        self.assertNotIn("render", [kind for kind, _ in ladder.events])

    def test_budget_exhaustion_skips_the_rung(self) -> None:
        """(#25 discipline) No rung -- browser-render included -- starts once
        the ladder's wall-clock budget is gone."""
        from freegem.smart_fetch import fallback_scrape

        ladder = _FakeLadder(render=self._ok_render())
        with contextlib.ExitStack() as stack:
            for patcher in ladder.patches():
                stack.enter_context(patcher)
            stack.enter_context(mock.patch("freegem.smart_fetch._LADDER_BUDGET_SEC", 0.0))
            with self.assertRaises(ScrapeError) as caught:
                fallback_scrape(self.cfg, _BLOCKED_URL, max_chars=2000, first_error="HTTP 403")
        self.assertEqual(
            [kind for kind, _ in ladder.events],
            [],
            "no rung -- and no fetch -- may run once the budget is gone",
        )
        message = str(caught.exception)
        self.assertEqual(message.count("ladder budget was exhausted"), len(STRATEGIES))
        self.assertIn("browser-render: skipped", message)

    def test_rung_is_time_bounded(self) -> None:
        """(#25 discipline) A wedged render cannot hold the rung: our wait
        lapses into an honest ScrapeError."""
        import time as _time

        from freegem.smart_fetch import _browser_render_read

        def wedged(_opts: Any) -> Any:
            _time.sleep(1.0)
            return self._ok_render()

        with (
            mock.patch("freegem.smart_fetch._BROWSER_RENDER_TIMEOUT_SEC", 0.1),
            mock.patch("freegem.playwright_fetcher.is_playwright_available", return_value=True),
            mock.patch("freegem.playwright_fetcher.fetch_with_playwright", side_effect=wedged),
        ):
            started = time.monotonic()
            with self.assertRaises(ScrapeError) as caught:
                _browser_render_read(self.cfg, _BLOCKED_URL, 2000)
        self.assertLess(time.monotonic() - started, 0.9, "the bound held")
        self.assertIn("timed out", str(caught.exception))

    def test_disabled_flag_gates_the_rung_directly(self) -> None:
        from freegem.smart_fetch import _browser_render_read

        cfg = BridgeConfig(guard=True, log_requests=False)  # default: off
        with mock.patch(
            "freegem.playwright_fetcher.is_playwright_available", return_value=True
        ) as available:
            with self.assertRaises(ScrapeError) as caught:
                _browser_render_read(cfg, _BLOCKED_URL, 2000)
        self.assertIn("disabled", str(caught.exception))
        available.assert_not_called()

    def test_http_error_render_is_a_clean_failure(self) -> None:
        from freegem.playwright_fetcher import PlaywrightResult
        from freegem.smart_fetch import _browser_render_read

        bad = PlaywrightResult(url=_BLOCKED_URL, final_url=_BLOCKED_URL, status=503, ok=False)
        with (
            mock.patch("freegem.playwright_fetcher.is_playwright_available", return_value=True),
            mock.patch("freegem.playwright_fetcher.fetch_with_playwright", return_value=bad),
        ):
            with self.assertRaises(ScrapeError) as caught:
                _browser_render_read(self.cfg, _BLOCKED_URL, 2000)
        self.assertIn("HTTP 503", str(caught.exception))


class ResearchCacheFullTextTests(unittest.TestCase):
    """#21 residual: research's fetch loop must not write budget-cut views.

    The shared cache is passed INTO scrape_page, which stores the FULL
    extraction once; the loop's own fallback write covers only the paths
    scrape_page does not store (the mocked scrape_page seam the tests
    drive -- research never enables the smart ladder) and never overwrites
    a fresh full-text entry with a cut view.
    """

    def setUp(self) -> None:
        self.cfg = BridgeConfig(guard=True, log_requests=False)
        self.tmp = tempfile.mkdtemp(prefix="freegem-fetchfeat-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _cache(self) -> Any:
        from freegem.web_research import ResearchCache

        return ResearchCache(Path(self.tmp) / "cache.json", ttl=9999)

    def _collect(self, urls: list[str], cache: Any, **kwargs: Any) -> Any:
        from freegem.web_research import _collect_sources

        kwargs.setdefault("max_pages", 5)
        kwargs.setdefault("max_chars_per_page", 12000)
        kwargs.setdefault("total_budget", 80000)
        return _collect_sources(
            self.cfg, urls, cache=cache, refresh=kwargs.pop("refresh", False), **kwargs
        )

    def test_research_fetch_caches_full_text_for_bigger_budget_callers(self) -> None:
        page_html = (
            "<html><head><title>Long Article</title></head><body><main>"
            + ("<p>full article body with real words </p>" * 120)
            + "</main></body></html>"
        ).encode("utf-8")
        cache = self._cache()
        with mock.patch("freegem.web_scraper._fetch", return_value=_FakeResponse(page_html)):
            # A tiny allowance: research fetches with 500 chars of budget.
            sources, material = self._collect(
                ["https://long.example/article"],
                cache,
                max_chars_per_page=500,
                total_budget=500,
            )
        self.assertTrue(sources[0].ok)
        # Research still sees exactly its allowance view -- unchanged behavior.
        self.assertLessEqual(len(material[0][1]), 500)
        # But the CACHE holds the full extraction, not the allowance view.
        stored = cache.get("https://long.example/article")
        self.assertIsNotNone(stored)
        self.assertGreater(len(stored["text"]), 1000)
        # A later caller with a bigger budget reads the whole page, no network.
        with mock.patch(
            "freegem.web_scraper._fetch",
            side_effect=AssertionError("a cache hit must not fetch"),
        ) as fetch:
            later = scrape_page(
                self.cfg, "https://long.example/article", max_chars=100_000, cache=cache
            )
        fetch.assert_not_called()
        self.assertEqual(later.text, stored["text"])
        self.assertFalse(later.truncated)
        self.assertEqual(later.via, "cache")

    def test_fallback_write_keeps_the_mocked_seam_contract(self) -> None:
        """When scrape_page could not store (the mocked seam the research
        tests drive), the loop's fallback write keeps the historical
        behavior; the point is it never clobbers a real full-text entry."""
        cache = self._cache()
        pages = {
            "https://a.test/1": ScrapeResult(url="https://a.test/1", title="One", text="short body")
        }

        def fake(cfg: Any, url: str, **_kwargs: Any) -> ScrapeResult:
            return pages[url]

        with mock.patch("freegem.web_scraper.scrape_page", side_effect=fake):
            sources, material = self._collect(["https://a.test/1"], cache)
        self.assertTrue(sources[0].ok)
        self.assertEqual([text for _, text in material], ["short body"])
        self.assertEqual(cache.get("https://a.test/1")["text"], "short body")

    def test_refresh_rewrites_through_scrape_page(self) -> None:
        cache = self._cache()
        cache.put("https://a.test/1", "Old", "old body")
        page_html = (
            b"<html><head><title>New</title></head><body><p>fresh new body</p></body></html>"
        )
        with mock.patch("freegem.web_scraper._fetch", return_value=_FakeResponse(page_html)):
            sources, material = self._collect(["https://a.test/1"], cache, refresh=True)
        self.assertFalse(sources[0].from_cache)
        self.assertEqual([text for _, text in material], ["fresh new body"])
        self.assertEqual(cache.get("https://a.test/1")["title"], "New")

    def test_concurrent_puts_keep_every_entry(self) -> None:
        """Concurrent put()/get() must not lose entries.

        Regression for the Windows CI failure of
        test_concurrent_fetches_store_full_text_too: put() is a whole-file
        read-modify-write, so pool workers on distinct hosts clobbered each
        other's just-written entries (lost update), and on Windows a reader
        holding the file open during tmp.replace() made the write fail with
        a sharing violation that the OSError handler silently swallowed.
        The per-instance lock closes both paths; this hammer fails without
        it on Linux too.
        """
        cache = self._cache()

        def hammer(worker: int) -> None:
            for i in range(15):
                url = f"https://w{worker}.test/{i}"
                cache.put(url, "T", f"body {worker} {i} " + "x" * 60)
                cache.get(url)  # a reader inside the replace window

        threads = [threading.Thread(target=hammer, args=(w,)) for w in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        missing = [
            f"https://w{w}.test/{i}"
            for w in range(8)
            for i in range(15)
            if cache.get(f"https://w{w}.test/{i}") is None
        ]
        self.assertEqual(missing, [], "concurrent put() lost cache entries")

    def test_concurrent_fetches_store_full_text_too(self) -> None:
        cache = self._cache()
        page_html = (
            "<html><head><title>T</title></head><body><p>"
            + ("concurrent body words " * 40)
            + "</p></body></html>"
        )
        urls = ["https://c1.test/1", "https://c2.test/2", "https://c3.test/3"]
        with mock.patch(
            "freegem.web_scraper._fetch",
            side_effect=lambda _cfg, _url, timeout=30, headers=None: _FakeResponse(
                page_html.encode("utf-8")
            ),
        ):
            sources, _ = self._collect(urls, cache, concurrency=3)
        self.assertTrue(all(source.ok for source in sources))
        for url in urls:
            self.assertGreater(
                len(cache.get(url)["text"]),
                500,
                "the pool path must store full text, not a cut view",
            )


if __name__ == "__main__":
    unittest.main()
