"""Tests for optional Playwright HTTP-first / browser-fallback fetcher."""

import sys
import tempfile
import types
import unittest
from typing import Any, Literal
from unittest.mock import patch

from freegem.config import BridgeConfig
from freegem.crawler.fetch import FetchOutcome
from freegem.playwright_fetcher import (
    PlaywrightOptions,
    fetch_http_first,
    fetch_with_playwright,
    is_playwright_available,
    should_fallback_to_browser,
)


class TestPlaywrightFetcher(unittest.TestCase):
    def test_is_playwright_available(self) -> None:
        result = is_playwright_available()
        self.assertIsInstance(result, bool)

    def test_should_fallback_to_browser(self) -> None:
        self.assertTrue(should_fallback_to_browser(None))

        outcome_failed = FetchOutcome(url="https://example.com", kind="http_error", status=500)
        self.assertTrue(should_fallback_to_browser(outcome_failed))

        outcome_spa = FetchOutcome(
            url="https://example.com",
            kind="ok",
            body=b"<html><script>app()</script>Please enable JavaScript</html>",
        )
        self.assertTrue(should_fallback_to_browser(outcome_spa))

        outcome_ok = FetchOutcome(
            url="https://example.com",
            kind="ok",
            body=b"<html><body>" + b"x" * 500 + b"</body></html>",
        )
        self.assertFalse(should_fallback_to_browser(outcome_ok))

    @patch(
        "freegem.playwright_fetcher.browser_request_allowed",
        side_effect=lambda url: url,
    )
    @patch("freegem.playwright_fetcher.is_playwright_available", return_value=False)
    def test_fetch_with_playwright_not_installed(self, mock_avail: Any, mock_allowed: Any) -> None:
        # The guard is a pass-through here: fetch_with_playwright validates
        # the FULL SSRF policy (strict resolve-and-check, i.e. DNS) before
        # the availability check, and a transient outage made that refusal
        # fire before the expected RuntimeError. The not-installed path must
        # be observable regardless of resolver behavior.
        opts = PlaywrightOptions(url="https://example.com")
        with self.assertRaises(RuntimeError):
            fetch_with_playwright(opts)

    @patch("freegem.playwright_fetcher.is_playwright_available", return_value=True)
    @patch("freegem.playwright_fetcher.browser_request_allowed")
    def test_fetch_with_playwright_ssrf_refusal(self, mock_allowed: Any, mock_avail: Any) -> None:
        from freegem.ssrf import SSRFRefused

        # The browser fetcher validates the FULL policy (strict resolve-and-
        # check, not just syntax) before any browser launches.
        mock_allowed.side_effect = SSRFRefused("127.0.0.1 refused")

        opts = PlaywrightOptions(url="http://127.0.0.1")
        res = fetch_with_playwright(opts)
        self.assertFalse(res.ok)
        assert res.error is not None
        self.assertIn("SSRF refused", res.error)

    def test_fetch_with_playwright_refuses_loopback_without_launching(self) -> None:
        # No mocks on the guard: a loopback URL must be refused by the REAL
        # policy, and refusal must happen BEFORE any browser import/launch
        # (this sandbox has no playwright installed, so a launch would raise
        # RuntimeError -- refusal returning a clean result proves ordering).
        res = fetch_with_playwright(
            PlaywrightOptions(url="http://169.254.169.254/latest/meta-data/")
        )
        self.assertFalse(res.ok)
        assert res.error is not None
        self.assertIn("SSRF refused", res.error)

    @patch("freegem.playwright_fetcher.crawl_fetch")
    @patch("freegem.playwright_fetcher.is_playwright_available", return_value=False)
    def test_fetch_http_first_normal(self, mock_pw: Any, mock_fetch: Any) -> None:
        cfg = BridgeConfig(
            guard=True,
        )
        outcome = FetchOutcome(url="https://example.com", kind="ok", body=b"Hello World" * 50)
        mock_fetch.return_value = outcome

        http_res, pw_res = fetch_http_first("https://example.com", cfg)
        self.assertEqual(http_res, outcome)
        self.assertIsNone(pw_res)


# ---------------------------------------------------------------------------
# Fake Playwright stack: exercises the fetcher's cleanup/capture paths with
# no browser installed, via sys.modules injection (same style as the
# Selenium tests in test_selenium_fetcher.py).
# ---------------------------------------------------------------------------

_RENDERED_HTML = (
    "<html><head><title>Rendered</title></head><body>"
    "<p>rendered body with plenty of content for the extractor to chew on</p>"
    "</body></html>"
)


class _FakeRoute:
    def __init__(self, url: str, navigation: bool = True) -> None:
        self.request = types.SimpleNamespace(
            url=url,
            is_navigation=lambda: navigation,
            resource_type="document",
        )

    def abort(self) -> None:
        pass

    def continue_(self) -> None:
        pass


class _FakeTracing:
    def start(self, **_kwargs: Any) -> None:
        pass

    def stop(self, path: str | None = None) -> None:
        _ = path


class _FakePWResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status


class _FakePWPage:
    def __init__(self, ctx: "_FakePWContext") -> None:
        self._ctx = ctx
        self.url = "https://example.org/final"
        self.screenshot_raises = False
        self.goto_raises: Exception | None = None

    def goto(self, url: str, wait_until: str = "", timeout: int = 0) -> Any:
        _ = (url, wait_until, timeout)
        if self._ctx.block_nav_url is not None:
            # Mimic a guard-blocked main frame: the route handler aborts the
            # navigation and Playwright surfaces a generic error.
            self._ctx.handler(_FakeRoute(self._ctx.block_nav_url))
            raise RuntimeError("net::ERR_FAILED")
        if self.goto_raises is not None:
            raise self.goto_raises
        return _FakePWResponse(200)

    def wait_for_selector(self, selector: str, timeout: int = 0) -> None:
        _ = (selector, timeout)

    def wait_for_timeout(self, ms: int) -> None:
        _ = ms

    def title(self) -> str:
        return "Rendered"

    def content(self) -> str:
        return _RENDERED_HTML

    def screenshot(self, path: str, full_page: bool = True) -> None:
        _ = full_page
        if self.screenshot_raises:
            raise RuntimeError("screenshot failed: viewport is closed")
        with open(path, "wb") as fh:
            fh.write(b"png")

    def pdf(self, path: str) -> None:
        with open(path, "wb") as fh:
            fh.write(b"%PDF-fake")


class _FakePWContext:
    def __init__(self) -> None:
        self.closed = False
        self.tracing = _FakeTracing()
        self.handler: Any = None
        self.block_nav_url: str | None = None

    def route(self, pattern: str, handler: Any) -> None:
        _ = pattern
        self.handler = handler

    def new_page(self) -> _FakePWPage:
        return _FakePWPage(self)

    def close(self) -> None:
        self.closed = True


class _FakePWBrowser:
    def __init__(self) -> None:
        self.closed = False
        self.contexts: list[_FakePWContext] = []

    def new_context(self, **_kwargs: Any) -> _FakePWContext:
        ctx = _FakePWContext()
        self.contexts.append(ctx)
        return ctx

    def close(self) -> None:
        self.closed = True


class _FakePW:
    def __init__(self, browser: _FakePWBrowser) -> None:
        self.chromium = types.SimpleNamespace(launch=lambda headless=True: browser)

    def __enter__(self) -> "_FakePW":
        return self

    def __exit__(self, *exc: Any) -> Literal[False]:
        return False


class TestPlaywrightCleanupAndCapture(unittest.TestCase):
    """Error-path cleanup (no browser leak) and best-effort captures."""

    def _fetch(
        self,
        opts: PlaywrightOptions,
        *,
        block_nav_url: str | None = None,
        goto_raises: Exception | None = None,
        screenshot_raises: bool = False,
        browser: _FakePWBrowser | None = None,
    ) -> tuple[Any, _FakePWBrowser]:
        browser = browser or _FakePWBrowser()
        sync_api = types.ModuleType("playwright.sync_api")
        sync_api.sync_playwright = lambda: _FakePW(browser)  # type: ignore[attr-defined]
        playwright_mod = types.ModuleType("playwright")
        playwright_mod.sync_api = sync_api  # type: ignore[attr-defined]

        orig_new_context = browser.new_context

        def new_context(**kwargs: Any) -> _FakePWContext:
            ctx = orig_new_context(**kwargs)
            ctx.block_nav_url = block_nav_url
            real_new_page = ctx.new_page

            def new_page() -> _FakePWPage:
                page = real_new_page()
                page.goto_raises = goto_raises
                page.screenshot_raises = screenshot_raises
                return page

            ctx.new_page = new_page  # type: ignore[method-assign]
            return ctx

        browser.new_context = new_context  # type: ignore[method-assign]

        with (
            patch.dict(
                sys.modules,
                {"playwright": playwright_mod, "playwright.sync_api": sync_api},
            ),
            patch("freegem.playwright_fetcher.is_playwright_available", return_value=True),
            patch(
                "freegem.playwright_fetcher.browser_request_allowed",
                side_effect=lambda url: url,
            ),
        ):
            res = fetch_with_playwright(opts)
        return res, browser

    def test_blocked_navigation_is_refused_and_browser_closed(self) -> None:
        """A guard-blocked main frame returns an SSRF refusal (not a raise)
        AND closes context+browser: error paths must not leak processes."""
        res, browser = self._fetch(
            PlaywrightOptions(url="https://example.org/page"),
            block_nav_url="http://127.0.0.1/admin",
        )
        self.assertFalse(res.ok)
        assert res.error is not None
        self.assertIn("SSRF refused", res.error)
        self.assertTrue(browser.closed)
        self.assertTrue(browser.contexts[0].closed)

    def test_exception_mid_fetch_still_closes_context_and_browser(self) -> None:
        browser = _FakePWBrowser()
        with self.assertRaises(RuntimeError):
            self._fetch(
                PlaywrightOptions(url="https://example.org/page"),
                goto_raises=RuntimeError("renderer died"),
                browser=browser,
            )
        self.assertTrue(browser.contexts, "the fetch did launch a context")
        self.assertTrue(browser.closed)
        self.assertTrue(browser.contexts[0].closed)

    def test_screenshot_failure_keeps_the_rendered_content(self) -> None:
        res, browser = self._fetch(
            PlaywrightOptions(url="https://example.org/page", capture_screenshot=True),
            screenshot_raises=True,
        )
        self.assertTrue(res.ok)
        self.assertIn("rendered body", res.text)
        self.assertIsNone(res.screenshot_path, "a failed capture must not claim a path")
        self.assertTrue(browser.closed)

    def test_happy_path_with_explicit_screenshot_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shot = f"{tmp}/shot.png"
            res, _browser = self._fetch(
                PlaywrightOptions(
                    url="https://example.org/page",
                    capture_screenshot=True,
                    screenshot_path=shot,
                ),
            )
        self.assertTrue(res.ok)
        self.assertEqual(res.screenshot_path, shot)


if __name__ == "__main__":
    unittest.main()

