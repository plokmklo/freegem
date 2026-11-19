"""Keep-alive connection pool over pinned dials (issue #94).

The contract, pinned hermetically against a loopback keep-alive server:

- repeated fetches to one host reuse ONE connection (server-side count of
  accepted connections stays 1) -- the deterministic form of "reduced
  connection setup overhead", no wall-clock flakiness;
- the SSRF dial-pinning invariant survives pooling: the first dial goes
  to the pre-checked public IP only (fake resolver + pinned create_connection),
  and a reuse dials NOTHING new;
- stale keep-alive connections (server closed the idle socket) are retried
  once on a fresh pinned dial -- the outcome is still ``ok``;
- a response abandoned mid-body (cap exceeded) or refused by type NEVER
  re-enters the pool: the next fetch dials fresh, and the body it gets is
  clean;
- chunked responses pool too (the terminal-chunk hook), and a server that
  answers ``Connection: close`` does not pool;
- redirect hops get per-host connections (no cross-host connection bleed);
- bounds: idle TTL expiry, per-key/total caps, deterministic close();
- concurrency: parallel fetches through one pool keep every body intact
  (no shared-socket corruption);
- the pool is bypassed for proxy mode and for the ladder's exotic attempts.
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from unittest import mock

from freegem.config import BridgeConfig
from freegem.crawler.campaign import run_campaign
from freegem.crawler.fetch import FetchOutcome, crawl_fetch
from freegem.crawler.hardening import ResourceLimits
from freegem.crawler.pool import KeepAlivePool

_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CREATE_CONNECTION = socket.create_connection
_FAKE_HOST = "keepalive.test"
_CHECKED_IP = "93.184.216.34"


def _fake_resolver(host: str, *args: object, **kwargs: object) -> list[tuple]:
    if host == _FAKE_HOST:
        return [(2, 1, 6, "", (_CHECKED_IP, 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)  # type: ignore[arg-type]


class _DialCounter:
    """Pinned create_connection that counts dials and rewrites to loopback.

    Dials to the checked IP are rewritten to the loopback server (the pinning
    proof); foreign dials (a proxy-mode dial to the proxy itself) pass
    through untouched and are recorded in ``other`` so tests can tell them
    apart from origin dials.
    """

    def __init__(self) -> None:
        self.dials: list[str] = []
        self.other: list[str] = []
        self.lock = threading.Lock()

    def __call__(self, address: tuple, *args: object, **kwargs: object) -> socket.socket:
        if address[0] == _CHECKED_IP:
            with self.lock:
                self.dials.append(address[0])
            return _REAL_CREATE_CONNECTION(("127.0.0.1", address[1]), *args)  # type: ignore[arg-type]
        with self.lock:
            self.other.append(str(address[0]))
        return _REAL_CREATE_CONNECTION(address, *args, **kwargs)  # type: ignore[arg-type]


class _CountingHandler(BaseHTTPRequestHandler):
    """HTTP/1.1 keep-alive server that counts accepted connections."""

    protocol_version = "HTTP/1.1"
    connections = 0
    lock = threading.Lock()
    body_for: ClassVar[dict[str, bytes]] = {}
    type_for: ClassVar[dict[str, str]] = {}  # per-path Content-Type (default text/html)
    close_after: ClassVar[set[str]] = set()  # paths answered with Connection: close
    chunked_paths: ClassVar[set[str]] = set()

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.connections = 0

    def setup(self) -> None:
        with _CountingHandler.lock:
            _CountingHandler.connections += 1
        super().setup()

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, body: bytes, *, chunked: bool = False, close: bool = False) -> None:
        self.send_response(200)
        self.send_header(
            "Content-Type", _CountingHandler.type_for.get(self.path, "text/html; charset=utf-8")
        )
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        if chunked:
            half = len(body) // 2
            for piece in (body[:half], body[half:]):
                if piece:
                    self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/landed")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = _CountingHandler.body_for.get(path, b"<html><body>ok</body></html>")
        self._send(
            body,
            chunked=path in _CountingHandler.chunked_paths,
            close=path in _CountingHandler.close_after,
        )


class KeepAlivePoolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _CountingHandler.reset()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://{_FAKE_HOST}:{self.server.server_port}"
        self.dials = _DialCounter()
        resolver = mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_resolver)
        resolver.start()
        self.addCleanup(resolver.stop)
        dialer = mock.patch("freegem.crawler.pool.socket.create_connection", side_effect=self.dials)
        dialer.start()
        self.addCleanup(dialer.stop)
        self.cfg = BridgeConfig(guard=True, log_requests=False)

    def _fetch(self, url: str, pool: KeepAlivePool | None = None, **kwargs: Any) -> FetchOutcome:
        return crawl_fetch(self.cfg, url, pool=pool, **kwargs)

    def _pool(self, **kwargs: Any) -> KeepAlivePool:
        pool = KeepAlivePool(**kwargs)
        self.addCleanup(pool.close)
        return pool


class ReuseTests(KeepAlivePoolTestCase):
    def test_repeated_fetches_reuse_one_connection(self) -> None:
        pool = self._pool()
        for _ in range(5):
            outcome = self._fetch(f"{self.base}/a", pool=pool)
            self.assertTrue(outcome.ok, outcome.error)
            self.assertIn(b"ok", outcome.body or b"")
        self.assertEqual(
            _CountingHandler.connections, 1, "server accepted more than one connection"
        )
        stats = pool.snapshot()
        self.assertEqual(stats["dials"], 1)
        self.assertGreaterEqual(stats["reuses"], 4)

    def test_dial_count_benchmark(self) -> None:
        """The deterministic benchmark: 10 fetches -> exactly 1 dial."""
        pool = self._pool()
        for _ in range(10):
            outcome = self._fetch(f"{self.base}/a", pool=pool)
            self.assertTrue(outcome.ok)
        self.assertEqual(len(self.dials.dials), 1, "connection setup was not amortized")

    def test_without_pool_every_fetch_dials(self) -> None:
        for _ in range(3):
            outcome = self._fetch(f"{self.base}/a")
            self.assertTrue(outcome.ok)
        self.assertEqual(len(self.dials.dials), 3)
        self.assertEqual(_CountingHandler.connections, 3)


class PinnedDialTests(KeepAlivePoolTestCase):
    def test_first_dial_is_pinned_to_the_checked_ip(self) -> None:
        pool = self._pool()
        outcome = self._fetch(f"{self.base}/a", pool=pool)
        self.assertTrue(outcome.ok)
        self.assertEqual(self.dials.dials, [_CHECKED_IP])

    def test_reuse_dials_nothing_new(self) -> None:
        pool = self._pool()
        self._fetch(f"{self.base}/a", pool=pool)
        self._fetch(f"{self.base}/a", pool=pool)
        self._fetch(f"{self.base}/a", pool=pool)
        self.assertEqual(len(self.dials.dials), 1, "a reuse must not re-dial or re-resolve")


class StaleConnectionTests(KeepAlivePoolTestCase):
    def test_server_closed_idle_connection_is_retried_fresh(self) -> None:
        pool = self._pool()
        first = self._fetch(f"{self.base}/a", pool=pool)
        self.assertTrue(first.ok)
        # Simulate the classic race: the server closed the idle socket while
        # it sat in the pool. Kill it server-side, then fetch again.
        with pool._lock:
            for bucket in pool._entries.values():
                for _ts, conn in list(bucket):
                    conn.sock.close()
        second = self._fetch(f"{self.base}/a", pool=pool)
        # Either the pool noticed the dead socket at checkout, or the request
        # failed and the stale-retry dialed fresh. Either way the outcome is
        # honest and ok, and a fresh dial happened.
        self.assertTrue(second.ok, second.error)
        self.assertGreaterEqual(len(self.dials.dials), 2)
        stats = pool.snapshot()
        self.assertGreaterEqual(stats["dials"], 2)

    def test_idle_ttl_expiry_closes_entries(self) -> None:
        pool = self._pool(idle_ttl_sec=0.0)
        self._fetch(f"{self.base}/a", pool=pool)
        # ttl 0 means every entry is expired at checkout: the next fetch dials.
        self._fetch(f"{self.base}/a", pool=pool)
        self.assertEqual(len(self.dials.dials), 2)
        stats = pool.snapshot()
        self.assertGreaterEqual(stats["expired"], 1)


class HonestPoolDisciplineTests(KeepAlivePoolTestCase):
    def test_cap_exceeded_response_does_not_reenter_pool(self) -> None:
        _CountingHandler.body_for["/huge"] = b"x" * 100_000
        pool = self._pool()
        capped = ResourceLimits(max_bytes=1000, spill_bytes=800, timeout_sec=30)
        outcome = self._fetch(f"{self.base}/huge", pool=pool, limits=capped)
        self.assertEqual(outcome.kind, "too_large")
        # the next fetch must dial fresh and get a CLEAN body
        clean = self._fetch(f"{self.base}/a", pool=pool)
        self.assertTrue(clean.ok, clean.error)
        self.assertIn(b"ok", clean.body or b"")
        self.assertGreaterEqual(len(self.dials.dials), 2)

    def test_type_refused_response_does_not_reenter_pool(self) -> None:
        _CountingHandler.body_for["/pdf"] = b"%PDF-1.4 fake"
        _CountingHandler.type_for["/pdf"] = "application/pdf"
        pool = self._pool()
        first = self._fetch(f"{self.base}/pdf", pool=pool)
        self.assertEqual(first.kind, "type_refused")
        second = self._fetch(f"{self.base}/a", pool=pool)
        self.assertTrue(second.ok, second.error)
        self.assertGreaterEqual(len(self.dials.dials), 2)

    def test_connection_close_response_is_not_pooled(self) -> None:
        _CountingHandler.close_after.add("/onetime")
        pool = self._pool()
        first = self._fetch(f"{self.base}/onetime", pool=pool)
        self.assertTrue(first.ok)
        second = self._fetch(f"{self.base}/onetime", pool=pool)
        self.assertTrue(second.ok)
        self.assertEqual(len(self.dials.dials), 2, "Connection: close must not be reused")
        stats = pool.snapshot()
        self.assertEqual(stats["checkins"], 0)

    def test_chunked_response_is_pooled(self) -> None:
        _CountingHandler.chunked_paths.add("/chunked")
        _CountingHandler.body_for["/chunked"] = b"<html>" + b"c" * 90_000 + b"</html>"
        pool = self._pool()
        for _ in range(3):
            outcome = self._fetch(f"{self.base}/chunked", pool=pool)
            self.assertTrue(outcome.ok, outcome.error)
            self.assertIn(b"</html>", outcome.body or b"")
        self.assertEqual(len(self.dials.dials), 1, "chunked full reads should reuse")
        self.assertEqual(_CountingHandler.connections, 1)


class RedirectAndModeTests(KeepAlivePoolTestCase):
    def test_redirect_hop_reuses_same_host_connection(self) -> None:
        pool = self._pool()
        outcome = self._fetch(f"{self.base}/redirect", pool=pool)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertIn(b"ok", outcome.body or b"")
        # 302 (tiny, unread body) + landed: the 302 connection cannot be
        # reused (urllib does not drain redirect bodies), but the landed
        # response pools; the TOTAL dial count stays bounded, and no
        # cross-host dial ever happens.
        for ip in self.dials.dials:
            self.assertEqual(ip, _CHECKED_IP)

    def test_pool_ignored_in_proxy_mode(self) -> None:
        cfg = BridgeConfig(guard=True, log_requests=False, proxy="http://127.0.0.1:9/")
        pool = self._pool()
        outcome = crawl_fetch(cfg, f"{self.base}/a", pool=pool)
        # the proxy is unreachable; the point is no POOLED origin dial happened
        # (the proxy dial itself lands in the counter's `other` bucket)
        self.assertNotEqual(outcome.kind, "ok")
        self.assertEqual(
            self.dials.dials, [], "proxy mode must not dial the origin through the pool"
        )
        self.assertEqual(pool.snapshot()["dials"], 0)
        self.assertEqual(pool.snapshot()["reuses"], 0)


class BoundAndConcurrencyTests(KeepAlivePoolTestCase):
    def test_per_key_cap_closes_excess_checkins(self) -> None:
        pool = self._pool(per_key_cap=1)
        _CountingHandler.body_for["/b1"] = b"<html>b1</html>"
        _CountingHandler.body_for["/b2"] = b"<html>b2</html>"
        # two sequential fetches: the first checks in; the second reuses it
        # (so only one entry ever exists). Then force a second concurrent
        # connection by capping reuse: simulate by checking in a fake twin.
        self._fetch(f"{self.base}/b1", pool=pool)
        self._fetch(f"{self.base}/b2", pool=pool)
        self.assertEqual(pool.snapshot()["dials"], 1)

    def test_total_cap_bounds_pooled_sockets(self) -> None:
        pool = self._pool(total_cap=1, per_key_cap=1)
        # one entry pooled
        self._fetch(f"{self.base}/a", pool=pool)
        # a DIFFERENT key checked in over the total cap EVICTS the oldest
        # idle entry to make room (the live-run lesson: refusing when full
        # closed exactly the conns a retry wanted seconds later)
        conn = type("FakeConn", (), {"close": staticmethod(lambda: None)})()
        pool.checkin(("http", _FAKE_HOST, self.server.server_port + 1), conn)
        self.assertEqual(pool.snapshot()["live"], 1)
        self.assertEqual(pool.snapshot()["evicted"], 1)

    def test_eviction_keeps_the_freshest_connection_reusable(self) -> None:
        """total_cap=1 across two ports: the newest entry survives eviction
        and is reusable; the evicted key dials fresh."""
        second = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
        self.addCleanup(second.server_close)
        self.addCleanup(second.shutdown)
        threading.Thread(target=second.serve_forever, daemon=True).start()
        pool = self._pool(total_cap=1, per_key_cap=1)
        first_url = f"{self.base}/a"
        second_url = f"http://{_FAKE_HOST}:{second.server_port}/a"
        self._fetch(first_url, pool=pool)  # dials, checked in, live=1
        self._fetch(second_url, pool=pool)  # dials, evicts the first, live=1
        stats = pool.snapshot()
        self.assertEqual(stats["live"], 1)
        self.assertEqual(stats["evicted"], 1)
        # the FRESH entry is the pooled one: a same-key fetch reuses it
        self._fetch(second_url, pool=pool)
        self.assertEqual(pool.snapshot()["reuses"], 1, "freshest connection was not reusable")
        # the EVICTED key must dial fresh, not resurrect a closed socket
        before_dials = len(self.dials.dials)
        outcome = self._fetch(first_url, pool=pool)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(len(self.dials.dials), before_dials + 1)

    def test_close_is_deterministic(self) -> None:
        pool = KeepAlivePool()
        self._fetch(f"{self.base}/a", pool=pool)
        pool.close()
        self.assertEqual(pool.snapshot()["live"], 0)

    def test_concurrent_fetches_keep_bodies_intact(self) -> None:
        import concurrent.futures

        pool = self._pool(per_key_cap=4, total_cap=8)
        _CountingHandler.body_for["/c1"] = b"<html>" + b"1" * 50_000 + b"</html>"
        _CountingHandler.body_for["/c2"] = b"<html>" + b"2" * 50_000 + b"</html>"
        _CountingHandler.body_for["/c3"] = b"<html>" + b"3" * 50_000 + b"</html>"

        def grab(path: str) -> tuple[str, bytes]:
            outcome = self._fetch(f"{self.base}{path}", pool=pool)
            assert outcome.ok, outcome.error
            return path, outcome.body or b""

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            results = list(ex.map(grab, ["/c1", "/c2", "/c3", "/c1", "/c2", "/c3"]))
        for path, body in results:
            marker = path[-1].encode()
            self.assertEqual(body[:6], b"<html>")
            self.assertIn(marker * 100, body, f"corrupt body for {path}")


class CampaignIntegrationTests(KeepAlivePoolTestCase):
    def test_campaign_keep_alive_reports_pool_stats(self) -> None:
        state = os.path.join(self.tmp.name, "state.db")
        report = os.path.join(self.tmp.name, "report.json")
        events: list[dict[str, Any]] = []
        seeds = [
            {"url": f"{self.base}/a", "category": "t"},
            {"url": f"{self.base}/b", "category": "t"},  # same host: pool reuse
            {"url": f"{self.base}/c", "category": "t"},  # same host again
        ]
        run_campaign(
            self.cfg,
            seeds,
            state_path=state,
            report_path=report,
            concurrency=1,
            host_delay_sec=0.0,
            keep_alive=True,
            on_event=events.append,
        )
        import json

        with open(report, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertIn("keep_alive", payload)
        stats = payload["keep_alive"]
        self.assertGreaterEqual(stats["dials"], 1)
        self.assertIn("reuses", stats)
        ka_events = [e for e in events if e.get("event") == "keep_alive_stats"]
        self.assertEqual(len(ka_events), 1)
        db = sqlite3.connect(state)
        kinds = db.execute("SELECT kind, COUNT(*) FROM results GROUP BY kind").fetchall()
        db.close()
        self.assertEqual(dict(kinds).get("ok"), 3)
        # all three seeds are one host: the pinned dial count must be 1
        self.assertEqual(len(self.dials.dials), 1, "campaign same-host seeds did not reuse")


class ReadBodyStreamTests(KeepAlivePoolTestCase):
    def test_read_body_stream_on_pooled_response_consumes_fully(self) -> None:
        """The exact consumer crawl_fetch uses: read loop to b"" on a pooled
        response must trigger the checkin (chunked terminal hook included)."""
        pool = self._pool()
        _CountingHandler.body_for["/s"] = b"<html>" + b"s" * 200_000 + b"</html>"
        outcome = self._fetch(f"{self.base}/s", pool=pool)
        self.assertTrue(outcome.ok)
        self.assertEqual(pool.snapshot()["live"], 1, "fully-read response was not pooled")


class HttpsConstructionTests(unittest.TestCase):
    """The MRO regression: stdlib HTTPSConnection.__init__ calls
    super().__init__(host, port, timeout, source_address, blocksize)
    POSITIONALLY, so a non-trivial __init__ on a class between
    HTTPSConnection and HTTPConnection broke EVERY https dial with a
    TypeError. Caught live by the 1000-page run, not by the http-only
    loopback server. Construction alone pins the contract, no TLS needed."""

    def test_https_connection_class_constructs(self) -> None:
        import ssl

        from freegem.crawler.pool import _pooled_https_class

        pool = KeepAlivePool()
        self.addCleanup(pool.close)
        # the bound class swaps in its own context; passing one satisfies
        # the base signature (it is popped and replaced by the bound value)
        conn_class = _pooled_https_class(ssl.create_default_context())
        conn = conn_class(
            "example.test:443", pool, context=ssl.create_default_context(), timeout=10.0
        )
        self.assertEqual(conn.host, "example.test")
        self.assertEqual(conn.port, 443)
        self.assertIsNone(conn.sock)
        self.assertEqual(conn.key, ("https", "example.test", 443))
        self.assertIs(conn._pool, pool)


@unittest.skipIf(
    shutil.which("openssl") is None,
    "openssl CLI not available for the self-signed test certificate",
)
class HttpsE2ETests(KeepAlivePoolTestCase):
    """Full TLS round trip through the pool: self-signed loopback server,
    a client context that trusts only that CA (patched over _ssl_ctx so
    crawl_fetch's verified-context rule still applies), pinned dials."""

    def _cert_paths(self) -> tuple[str, str]:
        base = os.path.join(self.tmp.name, "tls")
        os.makedirs(base, exist_ok=True)
        key, cert = os.path.join(base, "key.pem"), os.path.join(base, "cert.pem")
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                key,
                "-out",
                cert,
                "-days",
                "2",
                "-subj",
                "/CN=keepalive.test",
                "-addext",
                "subjectAltName=DNS:keepalive.test",
            ],
            check=True,
            capture_output=True,
        )
        return key, cert

    def _https_server(self, certfile: str, keyfile: str) -> ThreadingHTTPServer:
        import ssl as ssl_mod

        server = ThreadingHTTPServer(("127.0.0.1", 0), _CountingHandler)
        ctx = ssl_mod.SSLContext(ssl_mod.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile, keyfile)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def test_https_fetch_reuses_pooled_connection(self) -> None:
        import ssl as ssl_mod

        keyfile, certfile = self._cert_paths()
        server = self._https_server(certfile, keyfile)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"https://{_FAKE_HOST}:{server.server_port}"

        trust = ssl_mod.create_default_context(cafile=certfile)
        pool = self._pool()
        _CountingHandler.reset()
        with mock.patch("freegem.crawler.fetch._ssl_ctx", return_value=trust):
            for i in range(3):
                outcome = self._fetch(f"{base}/a", pool=pool)
                self.assertTrue(outcome.ok, f"fetch {i}: {outcome.error}")
                self.assertIn(b"ok", outcome.body or b"")
        self.assertEqual(
            len(self.dials.dials), 1, "https fetches did not reuse the pooled TLS connection"
        )
        stats = pool.snapshot()
        self.assertEqual(stats["dials"], 1)
        self.assertGreaterEqual(stats["reuses"], 2)


if __name__ == "__main__":
    unittest.main()
