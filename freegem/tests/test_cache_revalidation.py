"""(#113) Conditional-GET revalidation for the shared scrape/research cache.

The pain these pin (user-reported, issue #105): a TTL lapses but the origin
would answer 304 to "still this?" -- freegem ignored its own validators and
re-transferred the world on every re-run. The robots cache already did ETag
revalidation; now page reads do too, through the SAME pinned opener.

All state is test-instance-bound (handler factory closures): the first
draft of this file used class attributes and raced when the OS re-used an
ephemeral port across tests with handler threads still being drained --
the same lesson the repo's robots-cache conftest already documents.

Event ledger per server, recorded by the /v route:
  'f'  unconditional GET served a full body      (first fetch / plain re-fetch)
  'cx' conditional GET answered 200 (validator stale, body must be new)
  'c3' conditional GET answered 304 (validator held; zero body bytes)
urllib's transparent same-request retry (dying keep-alive) can add extra
'f' events -- protocol, not product behavior -- so the tests bound 'f'
counts where relevant but NEVER tolerate 'cx'/'c3' anomalies.
Every test states its EXPECTATIONS in its docstring.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from freegem.config import BridgeConfig
from freegem.tests.test_scraper import (
    _FAKE_HOST,
    _fake_create_connection,
    _fake_resolver,
)
from freegem.web_research import ResearchCache
from freegem.web_scraper import ScrapeError, scrape_page

PAGE_A = b"<html><head><title>V Page</title></head><body><p>Alpha content.</p></body></html>"
PAGE_B = b"<html><head><title>V Page 2</title></head><body><p>Beta content.</p></body></html>"
NO_ETAG_PAGE = (
    b"<html><head><title>No Validator</title></head><body><p>Plain content.</p></body></html>"
)


def make_site(etag_holder: dict, events: list) -> type[BaseHTTPRequestHandler]:
    """Handler factory whose state is owned by one test, not by the module."""

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, body: bytes, extra: dict[str, str] | None = None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            if path == "/v":
                inm = self.headers.get("If-None-Match")
                cur = etag_holder["etag"]
                if inm is None:
                    events.append("f")
                    self._send(etag_holder["body"], {"ETag": cur} if cur else {})
                    return
                if cur is not None and inm == cur:
                    events.append("c3")
                    self.send_response(304)
                    self.send_header("ETag", cur)
                    self.end_headers()
                    return
                events.append("cx")
                self._send(etag_holder["body"], {"ETag": cur} if cur else {})
                return
            if path == "/noet":
                events.append("f")
                self._send(NO_ETAG_PAGE)
                return
            self.send_error(404)

    return _Handler


class _CachedSiteCase(unittest.TestCase):
    def setUp(self) -> None:
        self.etag_holder = {"etag": '"v1"', "body": PAGE_A}
        self.events: list = []
        self.cfg = BridgeConfig(guard=True, log_requests=False)
        resolver = mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_resolver)
        resolver.start()
        self.addCleanup(resolver.stop)
        dial = mock.patch(
            "freegem.ssrf.socket.create_connection", side_effect=_fake_create_connection
        )
        dial.start()
        self.addCleanup(dial.stop)
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_site(self.etag_holder, self.events)
        )
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://{_FAKE_HOST}:{self.server.server_port}"
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)

    def cache(self, ttl: float = 60.0) -> ResearchCache:
        return ResearchCache(self.tmp_path / "cache.json", ttl)

    def age(self, url: str) -> None:
        path = self.tmp_path / "cache.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data[url]["fetched_at"] = time.time() - 99_999.0
        path.write_text(json.dumps(data), encoding="utf-8")

    def raw_entry(self, url: str) -> dict:
        return json.loads((self.tmp_path / "cache.json").read_text(encoding="utf-8"))[url]


class ValidatorStorageTests(_CachedSiteCase):
    def test_first_fetch_stores_server_etag(self) -> None:
        """EXPECT: the cache entry carries the ETag the origin sent."""
        cache = self.cache()
        url = f"{self.base}/v"
        first = scrape_page(self.cfg, url, cache=cache)
        self.assertFalse(first.revalidated)
        self.assertEqual(self.raw_entry(url)["etag"], '"v1"')
        self.assertEqual(self.events, ["f"])

    def test_server_without_etag_stores_none(self) -> None:
        """EXPECT: no-validator entries have no 'etag' key and re-fetch
        plainly forever -- no If-* ever sent (byte-identical legacy path)."""
        cache = self.cache()
        url = f"{self.base}/noet"
        result = scrape_page(self.cfg, url, cache=cache)
        self.assertIsNone(result.etag)
        self.assertNotIn("etag", self.raw_entry(url))
        self.age(url)
        scrape_page(self.cfg, url, cache=cache)
        self.assertEqual(set(self.events), {"f"})

    def test_fresh_hit_costs_zero_requests(self) -> None:
        """EXPECT: within TTL the cache answers without ANY network call."""
        cache = self.cache()
        url = f"{self.base}/v"
        scrape_page(self.cfg, url, cache=cache)
        before = len(self.events)
        second = scrape_page(self.cfg, url, cache=cache)
        self.assertEqual(second.via, "cache")
        self.assertEqual(len(self.events), before)


class RevalidationTests(_CachedSiteCase):
    def test_stale_entry_304_serves_cache(self) -> None:
        """EXPECT: after TTL lapse a conditional goes out, the 304 ships the
        cached text with revalidated=True, the entry is re-stamped, and the
        body is NEVER re-transferred. Event pattern: f then c3 (retry-tolerant
        f-count, zero tolerance for a conditional answered with a body)."""
        cache = self.cache()
        url = f"{self.base}/v"
        scrape_page(self.cfg, url, cache=cache)
        self.age(url)
        result = scrape_page(self.cfg, url, cache=cache)
        self.assertNotIn("cx", self.events, "a conditional must never get a full body")
        self.assertIn("c3", self.events)
        self.assertEqual(self.events[-1], "c3")
        self.assertTrue(result.revalidated)
        self.assertEqual(result.via, "cache")
        self.assertIn("Alpha content.", result.text)
        self.assertGreater(self.raw_entry(url)["fetched_at"], time.time() - 5.0)

    def test_stale_entry_200_new_body_replaces(self) -> None:
        """EXPECT: stale validator -> conditional answered 'cx' 200 with the
        NEW body, cache text and validator both follow the server."""
        cache = self.cache()
        url = f"{self.base}/v"
        scrape_page(self.cfg, url, cache=cache)
        self.age(url)
        self.etag_holder["etag"] = '"v2"'
        self.etag_holder["body"] = PAGE_B
        result = scrape_page(self.cfg, url, cache=cache)
        self.assertFalse(result.revalidated)
        self.assertIn("Beta content.", result.text)
        self.assertEqual(self.events, ["f", "cx"])
        self.assertEqual(self.raw_entry(url)["etag"], '"v2"')

    def test_conditional_non304_error_propagates(self) -> None:
        """EXPECT: when the conditional GET itself fails (HTTP 5xx), the
        ScrapeError surfaces honestly -- no silent stale serving."""
        cache = self.cache()
        url = f"{self.base}/v"
        scrape_page(self.cfg, url, cache=cache)
        self.age(url)
        with mock.patch(
            "freegem.web_scraper._fetch",
            side_effect=ScrapeError("HTTP 500 fetching 'https://flaky.example/v'"),
        ):
            with self.assertRaises(ScrapeError) as caught:
                scrape_page(self.cfg, url, cache=cache, smart=False)
        self.assertIn("HTTP 500", str(caught.exception))

    def test_max_chars_view_applies_after_304(self) -> None:
        """EXPECT: the 304 path truncates exactly like the fresh-cache path."""
        cache = self.cache()
        url = f"{self.base}/v"
        scrape_page(self.cfg, url, cache=cache)
        self.age(url)
        result = scrape_page(self.cfg, url, cache=cache, max_chars=8)
        self.assertTrue(result.revalidated)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.text), 8)


class DisabledAndStubCompatibilityTests(_CachedSiteCase):
    def test_ttl_zero_never_revalidates(self) -> None:
        """EXPECT: ttl<=0 = "never serve from cache" -- plain re-fetches, no
        If-* headers ever (a 304 would silently serve disk content)."""
        cache = self.cache(ttl=0)
        url = f"{self.base}/v"
        scrape_page(self.cfg, url, cache=cache)
        result = scrape_page(self.cfg, url, cache=cache)
        self.assertNotIn("c3", self.events)
        self.assertNotIn("cx", self.events)
        self.assertFalse(result.revalidated)
        self.assertEqual(result.via, "html")

    def test_stub_cache_without_entry_api(self) -> None:
        """EXPECT: plain get/put stubs take the legacy path AND the shared
        writer falls back to the 4-argument put signature via TypeError."""

        class StubCache:
            def __init__(self) -> None:
                self.items: dict[str, dict] = {}
                self.calls: list[int] = []  # arg-count each put() received

            def get(self, url: str) -> dict | None:
                return self.items.get(url)

            def put(
                self, url: str, title: str, text: str, last_modified: str | None = None
            ) -> None:
                self.calls.append(4)
                self.items[url] = {"title": title, "text": text}

        stub = StubCache()
        url = f"{self.base}/v"
        result = scrape_page(self.cfg, url, cache=stub)
        self.assertEqual(result.via, "html")
        self.assertTrue(stub.items[url]["text"].strip())
        self.assertEqual(set(stub.calls), {4})

    def test_refresh_true_always_refetches_plain(self) -> None:
        """EXPECT: refresh=True keeps its meaning -- a real GET, never a
        304 from cache."""
        cache = self.cache()
        url = f"{self.base}/v"
        scrape_page(self.cfg, url, cache=cache)
        result = scrape_page(self.cfg, url, cache=cache, refresh=True)
        self.assertNotIn("c3", self.events)
        self.assertNotIn("cx", self.events)
        self.assertFalse(result.revalidated)


class ResearchPipelineTests(_CachedSiteCase):
    """research()'s fallback shared-cache writers must tolerate result
    objects predating #113 fields (getattr guards in web_research.py)."""

    def test_writer_signature_guard_source_grep(self) -> None:
        """EXPECT: both fallback writers use the guarded etag form."""
        src = Path("freegem/web_research.py").read_text(encoding="utf-8")
        self.assertEqual(src.count('etag=getattr(page, "etag", None)'), 2)

    def test_fallback_writer_survives_old_shape_page(self) -> None:
        """EXPECT: research() with a mocked fetch seam whose page lacks
        .etag completes and still writes the shared cache."""

        class ThinResult:  # old-shaped result: no etag attribute at all
            def __init__(self) -> None:
                self.url = "https://thin.example/x"
                self.title = "T"
                self.text = "Real prose content that is reasonably long. " * 12
                self.truncated = False
                self.links: list = []
                self.via = "html"
                self.last_modified = None
                self.chars = 600

        cache = self.cache()
        from freegem import web_research

        with (
            mock.patch("freegem.web_research._fetch_page", return_value=ThinResult()),
            mock.patch("freegem.web_research._gemini_complete", return_value="[S1] ok"),
        ):
            res = web_research.research(
                self.cfg, "q?", ["https://thin.example/x"], cache=cache, max_pages=1
            )
        self.assertEqual(len(res.sources), 1)
        stored = json.loads((self.tmp_path / "cache.json").read_text(encoding="utf-8"))
        self.assertIn("thin.example/x", json.dumps(stored))
