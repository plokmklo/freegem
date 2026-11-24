"""Live-run remediations: chunked batches (#89), wayback chrome (#86),
bounded proxy preflight (#102)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

import freegem.ai_scrape as aisc
import freegem.ssrf as ssrf
import freegem.web_scraper as wscr
from freegem.config import BridgeConfig
from freegem.crawler.fetch import FetchOutcome
from freegem.crawler.resilient import strip_wayback_chrome


def _page(url: str) -> SimpleNamespace:
    return SimpleNamespace(url=url, title="T", text="real body " * 30, via="html", truncated=False)


class ChunkedBatchTests(unittest.TestCase):
    def test_web_batch_unbounded_keeps_order(self) -> None:
        urls = [f"https://h{i}.test/" for i in range(260)]
        cfg: BridgeConfig = mock.MagicMock()
        with mock.patch.object(wscr, "scrape_page", lambda c, u, **kw: _page(u)):
            items = wscr.scrape_many(cfg, urls, concurrency=2)
        self.assertEqual(len(items), 260)
        self.assertEqual([it.url for it in items], urls)
        self.assertTrue(all(it.ok for it in items))

    def test_ai_batch_unbounded_keeps_order(self) -> None:
        urls = [f"https://a{i}.test/" for i in range(150)]
        with (
            mock.patch.object(aisc, "ai_scrape", lambda cfg, u, **kw: _page(u)),
            mock.patch("freegem.ai_scrape.time.sleep", lambda s: None),
        ):
            cfg: BridgeConfig = mock.MagicMock()
            items = aisc.ai_scrape_many(cfg, urls, concurrency=2)
        self.assertEqual(len(items), 150)
        self.assertEqual([getattr(it, "url", None) for it in items], urls)


class WaybackChromeTests(unittest.TestCase):
    def test_classic_toolbar_comment_removed(self) -> None:
        body = (
            b"<html><!-- BEGIN WAYBACK TOOLBAR INSERT -->About this capture"
            b"<div>TIMESTAMPS</div><!-- END WAYBACK TOOLBAR INSERT --><p>REAL</p></html>"
        )
        out = FetchOutcome(url="u", kind="ok", status=200, body=body)
        strip_wayback_chrome(out)
        b = out.body or b""
        self.assertNotIn(b"About this capture", b)
        self.assertNotIn(b"TOOLBAR", b)
        self.assertIn(b"REAL", b)

    def test_modern_ipp_div_removed(self) -> None:
        body = b'<body><div id="wm-ipp-base"><a>x</a></div><p>REAL</p></body>'
        out = FetchOutcome(url="u", kind="ok", status=200, body=body)
        strip_wayback_chrome(out)
        b = out.body or b""
        self.assertNotIn(b"wm-ipp-base", b)
        self.assertIn(b"REAL", b)

    def test_clean_body_untouched(self) -> None:
        body = b"<html><p>pure page, no chrome</p></html>"
        out = FetchOutcome(url="u", kind="ok", status=200, body=body)
        strip_wayback_chrome(out)
        self.assertEqual(out.body or b"", body)


class BoundedPreflightTests(unittest.TestCase):
    def test_proxy_preflight_uses_bounded_resolver(self) -> None:
        calls: list[tuple[str, float]] = []

        def fake(host: str, timeout_sec: float) -> list:
            calls.append((host, timeout_sec))
            raise ssrf.SSRFRefused(
                f"cannot resolve {host!r}: resolver stalled (>{timeout_sec:.1f}s)"
            )

        with mock.patch.object(ssrf, "_getaddrinfo_bounded", fake):
            with self.assertRaises(ssrf.SSRFRefused) as cm:
                ssrf.host_public_or_unresolvable("slow.test")
        self.assertIn("stalled", str(cm.exception))
        self.assertEqual(calls[0][0], "slow.test")
        self.assertGreater(calls[0][1], 0.0)


if __name__ == "__main__":
    unittest.main()
