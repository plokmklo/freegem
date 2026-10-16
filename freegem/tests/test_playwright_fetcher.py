"""Tests for optional Playwright HTTP-first / browser-fallback fetcher."""

import unittest
from typing import Any
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
    def test_fetch_with_playwright_not_installed(
        self, mock_avail: Any, mock_allowed: Any
    ) -> None:
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
        cfg = BridgeConfig()
        outcome = FetchOutcome(url="https://example.com", kind="ok", body=b"Hello World" * 50)
        mock_fetch.return_value = outcome

        http_res, pw_res = fetch_http_first("https://example.com", cfg)
        self.assertEqual(http_res, outcome)
        self.assertIsNone(pw_res)


if __name__ == "__main__":
    unittest.main()
