"""The crawl loop, end to end, against a loopback fake site.

Same discipline as test_scraper: the URL host is a fake name the DNS fake
resolves to a public TEST-NET IP, and the dial fake forwards to loopback --
so every crawler fetch re-verifies the SSRF pinning invariant for free.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from unittest import mock

from freegem.config import BridgeConfig
from freegem.crawler import CrawlSpec, Frontier, ResourceLimits, Scope, plan, run
from freegem.web_scraper import clear_robots_cache

_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CREATE_CONNECTION = socket.create_connection

_FAKE_HOST = "site.test"
_CHECKED_IP = "93.184.216.34"

INDEX_HTML = """
<html><head><title>Crawl Me</title></head><body>
<h1>Index</h1>
<a href="/docs/a.html">A</a>
<a href="/docs/b.html">B</a>
<a href="https://outside.example/x">Outside</a>
<a href="#anchor">Anchor</a>
<a href="mailto:webmaster@site.test">Mail</a>
</body></html>
"""

A_HTML = """
<html><head><title>Doc A</title></head><body>
<article><h2>Alpha</h2><span class="score">3</span></article>
<article><h2>Beta</h2><span class="score">9</span></article>
<a href="/docs/deep/c.html">Deeper</a>
</body></html>
"""

B_HTML = "<html><head><title>Doc B</title></head><body><p>plain b</p></body></html>"
C_HTML = "<html><head><title>Doc C</title></head><body><p>deep c</p></body></html>"

SITEMAP_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
    "<url><loc>https://site.test/docs/sitemap-page.html</loc></url></urlset>"
)

SCHEMA_JSON: dict[str, Any] = {
    "item_selector": "article",
    "fields": [
        {"name": "title", "selector": "h2", "required": True},
        {"name": "score", "selector": "span.score", "type": "int"},
    ],
}


def _fake_resolver(host: str, *args: object, **kwargs: object) -> list[tuple]:
    if host == _FAKE_HOST:
        return [(2, 1, 6, "", (_CHECKED_IP, 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)  # type: ignore[arg-type]


def _fake_create_connection(address: tuple, *args: object, **kwargs: object) -> socket.socket:
    ip, port = address[0], address[1]
    assert ip == _CHECKED_IP, f"dial target {ip!r} is not the checked IP"
    return _REAL_CREATE_CONNECTION(("127.0.0.1", port), *args, **kwargs)  # type: ignore[arg-type]


class _CrawlSite(BaseHTTPRequestHandler):
    """Loopback stand-in: an index page with links, robots, sitemap."""

    protocol_version = "HTTP/1.1"
    config: ClassVar[dict[str, Any]] = {}
    #: Every path requested, for tests that assert what the crawler does NOT
    #: fetch (robots.txt, notably).
    requests: ClassVar[list[str]] = []

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(
        self,
        body: bytes,
        ctype: str = "text/html; charset=utf-8",
        status: int = 200,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        config = type(self).config
        type(self).requests.append(path)
        if path == "/robots.txt":
            body = config.get("robots", "User-agent: *\nDisallow: /private\n")
            if body is None:
                self.send_error(404)
            else:
                self._send(body.encode(), "text/plain")
            return
        if path == "/sitemap.xml":
            if config.get("sitemap", True):
                # <loc> must be absolute: reuse the Host header the fetcher sent.
                host_header = self.headers.get("Host") or _FAKE_HOST
                loc = f"http://{host_header}/docs/sitemap-page.html"
                body = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                    f"<url><loc>{loc}</loc></url></urlset>"
                )
                self._send(body.encode(), "application/xml")
            else:
                self.send_error(404)
            return
        if path.startswith("/private"):
            self._send(b"<p>secret</p>", status=200)
            return
        if path == "/docs/a.html":
            self._send(A_HTML.encode())
            return
        if path == "/docs/b.html":
            self._send(B_HTML.encode())
            return
        if path == "/docs/deep/c.html":
            self._send(C_HTML.encode())
            return
        if path == "/docs/sitemap-page.html":
            self._send(B_HTML.encode())
            return
        if path == "/boom":
            self._send(b"nope", status=500)
            return
        if path == "/":
            self._send(INDEX_HTML.encode())
            return
        self.send_error(404)


class CrawlRunnerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        clear_robots_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        self.resolver = mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_resolver)
        self.resolver.start()
        self.addCleanup(self.resolver.stop)
        self.dial = mock.patch(
            "freegem.ssrf.socket.create_connection", side_effect=_fake_create_connection
        )
        self.dial.start()
        self.addCleanup(self.dial.stop)
        _CrawlSite.config = {}
        _CrawlSite.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CrawlSite)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://{_FAKE_HOST}:{self.server.server_port}"
        self.cfg = BridgeConfig(log_requests=False)

    def write_schema(self) -> str:
        path = os.path.join(self.dir, "schema.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(SCHEMA_JSON, fh)
        return path

    def spec(self, **kwargs: Any) -> CrawlSpec:
        defaults: dict[str, Any] = {
            "seeds": (f"{self.base}/",),
            "scope": Scope(max_depth=1, max_pages=20),
            "limits": ResourceLimits(timeout_sec=5),
            "delay_ms": 0,
            "concurrency": 1,
        }
        defaults.update(kwargs)
        return CrawlSpec(**defaults)


class DnsTaxonomyTests(CrawlRunnerTestCase):
    """A dead domain is a NETWORK failure, not an SSRF refusal."""

    def test_unresolvable_host_is_network(self) -> None:
        from freegem.crawler.fetch import crawl_fetch

        outcome = crawl_fetch(
            self.cfg,
            "https://this-domain-does-not-exist-freegem-test.example/",
            limits=ResourceLimits(timeout_sec=5),
        )
        self.assertEqual(outcome.kind, "network")
        self.assertFalse(outcome.blocked)


class PlanTests(CrawlRunnerTestCase):
    def test_plan_is_offline_and_reports_verdicts(self) -> None:
        report = plan(self.spec())
        data = report.as_dict()
        self.assertEqual(len(data["seeds"]), 1)
        self.assertEqual(data["rejected"], [])
        self.assertEqual(data["hosts"], [_FAKE_HOST])

    def test_plan_rejects_bad_seeds(self) -> None:
        report = plan(self.spec(seeds=("not a url",)))
        self.assertEqual(len(report.rejected), 1)
        self.assertEqual(report.seeds, [])

    def test_plan_sitemaps_listed_when_enabled(self) -> None:
        report = plan(self.spec(sitemaps=True))
        self.assertEqual(report.sitemaps_planned, [f"{self.base}/sitemap.xml"])


class RunTests(CrawlRunnerTestCase):
    def test_crawl_fetches_index_and_links_within_scope(self) -> None:
        frontier = Frontier(os.path.join(self.dir, "state.db"))
        self.addCleanup(frontier.close)
        counters = run(self.cfg, self.spec(), frontier)
        data = counters.as_dict()
        # index + a + b (depth 1); c.html is depth 2 > max_depth 1; outside refused
        self.assertEqual(data["pages_ok"], 3)
        # counted http links from the index: a, b, outside (anchor/mailto skipped)
        self.assertEqual(data["links_found"], 3)
        self.assertEqual(data["links_queued"], 2)
        self.assertEqual(frontier.stats().queued, 0)

    def test_depth_budget_respected(self) -> None:
        frontier = Frontier(os.path.join(self.dir, "state.db"))
        self.addCleanup(frontier.close)
        counters = run(self.cfg, self.spec(scope=Scope(max_depth=2, max_pages=20)), frontier)
        self.assertEqual(counters.as_dict()["pages_ok"], 4)  # index, a, b, c

    def test_robots_txt_is_never_fetched_and_never_blocks(self) -> None:
        """The crawler must not read robots.txt at all: it is attacker-
        controlled text and a prompt-injection vector. Even a Disallow-all
        robots.txt must not stop the crawl, and no request to /robots.txt
        may ever hit the wire."""
        _CrawlSite.config["robots"] = "User-agent: *\nDisallow: /\n"
        frontier = Frontier(os.path.join(self.dir, "state.db"))
        self.addCleanup(frontier.close)
        counters = run(self.cfg, self.spec(), frontier)
        data = counters.as_dict()
        self.assertEqual(data["pages_ok"], 3)  # the crawl proceeds untouched
        self.assertNotIn("/robots.txt", _CrawlSite.requests)  # never fetched

    def test_page_budget_stops_the_crawl(self) -> None:
        frontier = Frontier(os.path.join(self.dir, "state.db"))
        self.addCleanup(frontier.close)
        counters = run(self.cfg, self.spec(scope=Scope(max_depth=2, max_pages=2)), frontier)
        self.assertLessEqual(counters.as_dict()["pages_ok"], 2)

    def test_schema_extraction_writes_results(self) -> None:
        out_path = os.path.join(self.dir, "results.jsonl")
        frontier = Frontier(os.path.join(self.dir, "state.db"))
        self.addCleanup(frontier.close)
        run(
            self.cfg,
            self.spec(
                scope=Scope(max_depth=1, max_pages=20),
                output_path=out_path,
                schema_path=self.write_schema(),
            ),
            frontier,
        )
        with open(out_path, encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        titles = [r["title"] for r in records]
        self.assertEqual(titles, ["Alpha", "Beta"])
        provenance = records[0]
        self.assertIn("url", provenance)
        self.assertIn("fetched_at", provenance)
        self.assertIn("content_hash", provenance)

    def test_artifacts_store_bodies_and_manifest(self) -> None:
        artifacts = os.path.join(self.dir, "artifacts")
        frontier = Frontier(os.path.join(self.dir, "state.db"))
        self.addCleanup(frontier.close)
        run(self.cfg, self.spec(artifacts_dir=artifacts), frontier)
        manifest_path = os.path.join(artifacts, "manifest.jsonl")
        self.assertTrue(os.path.exists(manifest_path))
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = [json.loads(line) for line in fh if line.strip()]
        self.assertGreaterEqual(len(manifest), 3)
        for entry in manifest:
            body_path = os.path.join(artifacts, entry["hash"][:2], f"{entry['hash']}.body")
            self.assertTrue(os.path.exists(body_path), f"missing artifact {body_path}")

    def test_cancel_event_stops_the_loop(self) -> None:
        import threading as th

        frontier = Frontier(os.path.join(self.dir, "state.db"))
        self.addCleanup(frontier.close)
        cancel = th.Event()
        cancel.set()  # cancelled before start: nothing happens
        counters = run(self.cfg, self.spec(), frontier, cancel=cancel)
        self.assertEqual(counters.as_dict()["pages_ok"], 0)

    def test_resume_reuses_state_without_refetch(self) -> None:
        state = os.path.join(self.dir, "state.db")
        frontier = Frontier(state)
        counters1 = run(self.cfg, self.spec(), frontier)
        first_ok = counters1.as_dict()["pages_ok"]
        frontier.close()
        # resume: same state file re-opened; everything is done, nothing refetches
        frontier2 = Frontier(state)
        self.addCleanup(frontier2.close)
        counters2 = run(self.cfg, self.spec(), frontier2)
        self.assertEqual(first_ok, 3)
        self.assertEqual(counters2.as_dict()["pages_ok"], 0)
        self.assertEqual(counters2.as_dict()["pages_not_modified"], 0)

    def test_runtime_cap_stops_long_crawl(self) -> None:
        frontier = Frontier(os.path.join(self.dir, "state.db"))
        self.addCleanup(frontier.close)
        spec = self.spec(
            scope=Scope(max_depth=2, max_pages=100),
            limits=ResourceLimits(timeout_sec=5, max_runtime_sec=0.0),
        )
        counters = run(self.cfg, spec, frontier)
        self.assertEqual(counters.as_dict()["pages_ok"], 0)

    def test_sitemap_discovery_queues_urls(self) -> None:
        frontier = Frontier(os.path.join(self.dir, "state.db"))
        self.addCleanup(frontier.close)
        counters = run(
            self.cfg, self.spec(sitemaps=True, scope=Scope(max_depth=1, max_pages=30)), frontier
        )
        data = counters.as_dict()
        self.assertEqual(data["sitemap_urls"], 1)
        # the sitemap page is fetched within the budget
        self.assertTrue(frontier.seen(f"{self.base}/docs/sitemap-page.html"))

    def test_failed_page_retries_then_errors(self) -> None:
        frontier = Frontier(os.path.join(self.dir, "state.db"))
        self.addCleanup(frontier.close)
        spec = CrawlSpec(
            seeds=(f"{self.base}/boom",),
            scope=Scope(max_depth=0, max_pages=5),
            limits=ResourceLimits(timeout_sec=5, max_retries=1),
            delay_ms=0,
            concurrency=1,  # single-worker path waits out the backoff window
            sitemaps=False,
        )
        counters = run(self.cfg, spec, frontier)
        data = counters.as_dict()
        self.assertGreaterEqual(data["retries"] + data["pages_failed"], 1)
        stats = frontier.stats()
        self.assertEqual(stats.by_status.get("error"), 1)

    def test_multiworker_run_also_drains(self) -> None:
        """concurrency=2: index + links still all fetched, nothing stranded."""
        frontier = Frontier(os.path.join(self.dir, "state.db"))
        self.addCleanup(frontier.close)
        counters = run(self.cfg, self.spec(concurrency=2), frontier)
        self.assertEqual(counters.as_dict()["pages_ok"], 3)
        self.assertEqual(frontier.stats().queued, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
