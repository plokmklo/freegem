"""The escalation ladder, the streaming body reader, distillation, campaign.

These tests fake the network (loopback server + pinned resolver, like the
runner tests) or skip it entirely (unit tests over fabricated outcomes), so
they stay fast and deterministic.
"""

from __future__ import annotations

import gzip
import os
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from unittest import mock

from freegem.config import BridgeConfig
from freegem.crawler import resilient
from freegem.crawler.campaign import _SCHEMA, _migrate, run_campaign
from freegem.crawler.distill import distill_text, extractive_summary
from freegem.crawler.fetch import FetchOutcome, _is_dns_failure, decode_file, read_body_stream
from freegem.crawler.hardening import ResourceLimits
from freegem.crawler.resilient import (
    PROFILES,
    HostPacer,
    _fallback_of,
    _has_path,
    _host_swap,
    _LadderState,
    _next_action,
    _origin_root,
    _scheme_swap,
    _wayback_url,
    fetch_resilient,
)
from freegem.web_scraper import ScrapeError

_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CREATE_CONNECTION = socket.create_connection
_FAKE_HOST = "wall.test"
_CHECKED_IP = "93.184.216.34"


def _fake_resolver(host: str, *args: object, **kwargs: object) -> list[tuple]:
    if host == _FAKE_HOST:
        return [(2, 1, 6, "", (_CHECKED_IP, 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)  # type: ignore[arg-type]


def _fake_create_connection(address: tuple, *args: object, **kwargs: object) -> socket.socket:
    ip, port = address[0], address[1]
    assert ip == _CHECKED_IP, f"dial target {ip!r} is not the checked IP"
    return _REAL_CREATE_CONNECTION(("127.0.0.1", port), *args, **kwargs)  # type: ignore[arg-type]


class _LadderSite(BaseHTTPRequestHandler):
    """Behavior-scripted origin: config maps path -> list of (status, body)."""

    protocol_version = "HTTP/1.1"
    script: ClassVar[dict[str, list[tuple[int, bytes, str]]]] = {}
    requests: ClassVar[list[tuple[str, str, str]]] = []
    #: Paths that 302 to themselves until the request carries the cookie the
    #: 302 itself granted (the anonymous-entitlement pattern).
    loop_paths: ClassVar[set[str]] = set()

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        ua = self.headers.get("User-Agent") or ""
        cookie = self.headers.get("Cookie") or ""
        type(self).requests.append((path, ua, cookie))
        if path in type(self).loop_paths and "freegem_entitlement" not in cookie:
            self.send_response(302)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "3")
            self.send_header("Set-Cookie", "freegem_entitlement=1; Path=/")
            self.send_header("Location", path)
            self.end_headers()
            self.wfile.write(b"302")
            return
        script = type(self).script
        queue = script.get(path) or [
            (200, b"<html><title>ok</title><body>fine</body></html>", "text/html")
        ]
        status, body, ctype = queue[0]
        if len(queue) > 1:
            script[path] = queue[1:]
        extra: dict[str, str] = {}
        if status == 429:
            extra["Retry-After"] = "1"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


class LadderTestCase(unittest.TestCase):
    def setUp(self) -> None:
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
        _LadderSite.script = {}
        _LadderSite.requests = []
        _LadderSite.loop_paths = set()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _LadderSite)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://{_FAKE_HOST}:{self.server.server_port}"
        # Keep the wayback LAST-resort hermetic: its attempts hit this scripted
        # site under /wb2id_/ instead of the real web.archive.org.
        self.wayback_patch = mock.patch.object(
            resilient, "_WAYBACK_PREFIX", f"http://{_FAKE_HOST}:{self.server.server_port}/wb2id_/"
        )
        self.wayback_patch.start()
        self.addCleanup(self.wayback_patch.stop)
        self.cfg = BridgeConfig(log_requests=False)
        # Real delays would slow the suite; the ladder still exercises every
        # branch with a zero floor, and Retry-After comes back as a sleep
        # bounded by the pacer penalty cap.
        self.pacer = HostPacer(default_delay_sec=0.0)

    def fetch(self, url: str, **kwargs: Any) -> FetchOutcome:
        return fetch_resilient(
            self.cfg, url, limits=ResourceLimits(timeout_sec=5), pacer=self.pacer, **kwargs
        )


class BrowserProfileTests(LadderTestCase):
    def test_403_wall_flips_to_browser_profile(self) -> None:
        chrome_ua = PROFILES[0]["User-Agent"]
        _LadderSite.script["/walled"] = [
            (403, b"bot wall", "text/plain"),
            (200, b"<html><title>in</title><body>human content</body></html>", "text/html"),
        ]
        outcome = self.fetch(f"{self.base}/walled")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.fallback, "profile_1")
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(_LadderSite.requests[1][1], chrome_ua)

    def test_plain_success_needs_no_fallback(self) -> None:
        outcome = self.fetch(f"{self.base}/")
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.fallback, "")
        self.assertEqual(outcome.attempts, 1)

    def test_429_retries_and_succeeds(self) -> None:
        _LadderSite.script["/busy"] = [
            (429, b"slow down", "text/plain"),
            (200, b"<html><title>in</title><body>eventually</body></html>", "text/html"),
        ]
        outcome = self.fetch(f"{self.base}/busy")
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.attempts, 2)
        self.assertEqual(outcome.fallback, "")

    def test_404_falls_back_to_origin_root(self) -> None:
        _LadderSite.script["/about-does-not-exist"] = [(404, b"no such page", "text/plain")]
        outcome = self.fetch(f"{self.base}/about-does-not-exist")
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.fallback, "origin_root")
        assert outcome.final_url is not None
        self.assertTrue(outcome.final_url.endswith("/"))

    def test_451_recovered_from_wayback_snapshot(self) -> None:
        """A legal-block page still readable via its public archived copy."""
        _LadderSite.script["/legal"] = [(451, b"blocked by law", "text/plain")] * 9
        outcome = self.fetch(f"{self.base}/legal")
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.fallback, "wayback")
        self.assertEqual(outcome.via, "wayback")

    def test_451_stays_refused_when_archive_refuses_too(self) -> None:
        _LadderSite.script["/legal"] = [(451, b"blocked by law", "text/plain")] * 9
        _LadderSite.script[f"/wb2id_/{self.base}/legal"] = [
            (451, b"also blocked", "text/plain")
        ] * 9
        outcome = self.fetch(f"{self.base}/legal")
        self.assertEqual(outcome.status, 451)
        self.assertFalse(outcome.ok)

    def test_5xx_path_recovers_via_origin_root(self) -> None:
        _LadderSite.script["/broken"] = [(502, b"origin error", "text/plain")] * 2
        with mock.patch.object(resilient, "_sleep_backoff", return_value=0.01):
            outcome = self.fetch(f"{self.base}/broken")
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.fallback, "origin_root")

    def test_cookie_loop_resolves_with_shared_jar(self) -> None:
        _LadderSite.loop_paths.add("/gate")
        outcome = self.fetch(f"{self.base}/gate")
        self.assertTrue(outcome.ok, outcome.error)
        # urllib extracts the 302's Set-Cookie only after the redirect chain
        # unwinds, so the FIRST follow is cookieless and the shared jar pays
        # off on the ladder's next attempt: the final request carries it.
        self.assertIn("freegem_entitlement", _LadderSite.requests[-1][2])
        self.assertNotIn("freegem_entitlement", _LadderSite.requests[0][2])

    def test_dns_failure_resolves_via_doh(self) -> None:
        # doh.test NXDOMAINs locally (real resolver); the ladder's DoH step
        # resolves it and the guard still dials only the checked IP.
        with mock.patch.object(resilient, "_doh_ips", return_value=[_CHECKED_IP]):
            outcome = self.fetch(f"http://doh.test:{self.server.server_port}/dohpage")
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.fallback, "doh")

    def test_persistent_timeout_reaches_wayback_within_bounded_attempts(self) -> None:
        """Issue #45: a URL that ALWAYS times out must reach the wayback
        last resort after the two escalation dials, not burn the whole
        16-attempt budget on timeout_up retries at up to 90s each."""
        dials: list[str] = []

        def always_times_out(cfg: BridgeConfig, url: str, **kwargs: Any) -> FetchOutcome:
            dials.append(url)
            if "/wb2id_/" in url:
                return FetchOutcome(
                    url=url, kind="ok", status=200, body=b"archived copy", body_size=13
                )
            return FetchOutcome(url=url, kind="timeout", error="timed out")

        with mock.patch.object(resilient, "crawl_fetch", side_effect=always_times_out):
            outcome = self.fetch(f"{self.base}/walled-by-silence")
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.via, "wayback")
        self.assertEqual(outcome.fallback, "wayback")
        # initial dial + 2 capped escalations + 1 wayback read
        self.assertEqual(outcome.attempts, 4)
        self.assertEqual(len(dials), 4)
        self.assertIn("/wb2id_/", dials[-1])


class LadderUnitTests(unittest.TestCase):
    """Pure decisions over fabricated outcomes: no network, no sleeps."""

    def _state(self, **kwargs: Any) -> _LadderState:
        return _LadderState(timeout_sec=10.0, **kwargs)

    def _decide(
        self,
        url: str,
        outcome: FetchOutcome,
        state: _LadderState,
        host: str = "x.test",
        pacer: HostPacer | None = None,
    ) -> tuple[str, str, dict[str, str] | None, bool]:
        step = _next_action(url, outcome, state, host, pacer or HostPacer())
        assert step is not None, "expected a ladder step, got None"
        return step

    def test_timeout_escalates_then_falls_through_to_wayback(self) -> None:
        state = self._state()
        outcome = FetchOutcome(url="https://x.test/", kind="timeout", error="timed out")
        action = self._decide("https://x.test/", outcome, state)
        self.assertEqual(action[0], "timeout_up")
        self.assertGreaterEqual(state.timeout_sec, 30.0)
        outcome2 = FetchOutcome(url="https://x.test/", kind="timeout", error="timed out")
        action2 = self._decide("https://x.test/", outcome2, state)
        self.assertEqual(action2[0], "timeout_up")
        self.assertGreaterEqual(state.timeout_sec, 60.0)  # capped
        # Issue #45: past the escalation cap the ladder must STOP returning
        # timeout_up (which used to consume all 16 attempts at up to 90s a
        # dial) and fall through to the remaining rungs -- here, the wayback
        # last resort. It must not burn a backoff sleep on another identical
        # dial either.
        outcome3 = FetchOutcome(url="https://x.test/", kind="timeout", error="timed out")
        action3 = self._decide("https://x.test/", outcome3, state)
        self.assertEqual(action3[0], "wayback")
        self.assertEqual(state.timeout_sec, 60.0)  # no further escalation
        self.assertEqual(state.sleeps_used, 0)

    def test_connection_refused_swaps_scheme(self) -> None:
        state = self._state()
        outcome = FetchOutcome(
            url="https://x.test/", kind="network", error="[Errno 111] Connection refused"
        )
        action = self._decide("https://x.test/", outcome, state)
        self.assertEqual(action[0], "scheme_swap")
        self.assertEqual(action[1], "http://x.test/")

    def test_cert_error_tries_host_swap_then_tls_relax(self) -> None:
        state = self._state()
        outcome = FetchOutcome(
            url="https://www.catb.org/",
            kind="network",
            error="SSL: CERTIFICATE_VERIFY_FAILED certificate verify failed: hostname mismatch",
        )
        action = self._decide("https://www.catb.org/", outcome, state, host="catb.org")
        self.assertEqual(action[0], "host_swap")
        self.assertEqual(action[1], "https://catb.org/")
        action2 = self._decide(
            "https://www.catb.org/",
            FetchOutcome(
                url="https://catb.org/", kind="network", error="certificate verify failed"
            ),
            state,
            host="catb.org",
        )
        self.assertEqual(action2[0], "tls_unverified")
        self.assertTrue(action2[3])

    def test_unreachable_uses_backoff_then_stops(self) -> None:
        state = self._state()
        outcome = FetchOutcome(
            url="https://x.test/", kind="network", error="[Errno 101] Network is unreachable"
        )
        action = self._decide("https://x.test/", outcome, state)
        self.assertEqual(action[0], "scheme_swap")
        action2 = self._decide("https://x.test/", outcome, state)
        self.assertEqual(action2[0], "host_swap")
        action3 = self._decide("https://x.test/", outcome, state)
        self.assertEqual(action3[0], "sleep")

    def test_ssrf_and_too_large_are_final(self) -> None:
        for kind in ("ssrf", "too_large", "type_refused"):
            state = self._state()
            outcome = FetchOutcome(url="https://x.test/", kind=kind)
            self.assertIsNone(
                _next_action("https://x.test/", outcome, state, "x.test", HostPacer())
            )

    def test_429_penalizes_host_pacer(self) -> None:
        pacer = HostPacer(default_delay_sec=0.0)
        state = self._state()
        outcome = FetchOutcome(
            url="https://x.test/", kind="http_error", status=429, retry_after=3.0
        )
        action = self._decide("https://x.test/", outcome, state, pacer=pacer)
        self.assertEqual(action[0], "wait_429")
        self.assertGreaterEqual(pacer.reserve("x.test"), 2.0)

    def test_429_falls_through_to_wayback_after_three_waits(self) -> None:
        state = self._state()
        state.attempts = 6
        outcome = FetchOutcome(url="https://x.test/", kind="http_error", status=429)
        seen: list[str] = []
        for _ in range(4):
            step = _next_action("https://x.test/", outcome, state, "x.test", HostPacer())
            assert step is not None
            seen.append(step[0])
        self.assertEqual(seen[:3], ["wait_429", "wait_429", "wait_429"])
        self.assertEqual(seen[3], "wayback")

    def test_wayback_url_uses_raw_capture_view(self) -> None:
        self.assertEqual(
            _wayback_url("https://a.test/x?q=1"),
            "https://web.archive.org/web/2id_/https://a.test/x?q=1",
        )

    def test_three_browser_profiles_exist(self) -> None:
        self.assertEqual(len(PROFILES), 3)
        for profile in PROFILES:
            self.assertIn("User-Agent", profile)
            self.assertEqual(profile["Accept-Encoding"], "gzip")

    def test_fallback_names(self) -> None:
        self.assertEqual(_fallback_of(["wait_429"]), "")
        self.assertEqual(_fallback_of(["timeout_up"]), "")
        self.assertEqual(_fallback_of(["profile_1"]), "profile_1")
        self.assertEqual(_fallback_of(["origin_root"]), "origin_root")
        self.assertEqual(_fallback_of(["scheme_swap", "host_swap"]), "scheme_swap")
        self.assertEqual(_fallback_of(["doh"]), "doh")
        self.assertEqual(_fallback_of(["wayback"]), "wayback")
        self.assertEqual(_fallback_of(["doh", "wayback"]), "doh")

    def test_url_helpers(self) -> None:
        self.assertTrue(_has_path("https://a.test/x/about"))
        self.assertFalse(_has_path("https://a.test/"))
        self.assertEqual(_origin_root("https://a.test/x/about?y=1"), "https://a.test/")
        self.assertEqual(_scheme_swap("https://a.test/x"), "http://a.test/x")
        self.assertIsNone(_scheme_swap("ftp://a.test/"))
        self.assertEqual(_host_swap("https://www.a.test/x"), "https://a.test/x")
        self.assertEqual(_host_swap("https://a.test/x"), "https://www.a.test/x")

    def test_dns_failure_messages_classify_as_network(self) -> None:
        # Regression: the first campaign recorded wyborcza.pl's DNS failure
        # as "ssrf". Every resolver-failure phrasing must read as network.
        messages = (
            "cannot resolve 'wyborcza.pl': [Errno -3] Temporary failure in name resolution",
            "[Errno -2] Name or service not known",
            "no addresses resolved for 'dead.test'",
        )
        for message in messages:
            self.assertTrue(_is_dns_failure(message), message)


class HostPacerTests(unittest.TestCase):
    def test_reserves_monotonic_slots(self) -> None:
        pacer = HostPacer(default_delay_sec=0.05)
        first = pacer.reserve("h")
        second = pacer.reserve("h")
        self.assertGreater(second, first)
        # Independent hosts do not wait on each other.
        self.assertEqual(pacer.reserve("other"), 0.0)

    def test_penalize_pushes_slot_out(self) -> None:
        pacer = HostPacer(default_delay_sec=0.0)
        pacer.penalize("h", 0.5)
        self.assertGreaterEqual(pacer.reserve("h"), 0.4)


class _FakeResponse:
    def __init__(self, chunks: list[bytes], headers: dict[str, str]) -> None:
        self._chunks = list(chunks)
        self.headers = _HeaderMap(headers)
        self.status = 200

    def read(self, n: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _HeaderMap:
    def __init__(self, data: dict[str, str]) -> None:
        self._data = {k.lower(): v for k, v in data.items()}

    def get(self, key: str, default: str = "") -> str:
        return self._data.get(key.lower(), default)


class StreamBodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_small_body_stays_in_memory(self) -> None:
        response = _FakeResponse([b"hello ", b"world"], {"Content-Type": "text/plain"})
        body, path, total = read_body_stream(
            response, max_bytes=1 << 20, spill_bytes=1 << 20, spill_dir=self.tmp.name
        )
        self.assertEqual(body, b"hello world")
        self.assertIsNone(path)
        self.assertEqual(total, 11)

    def test_gzip_body_is_decompressed_with_cap(self) -> None:
        raw = b"gzip payload " * 1000
        compressed = gzip.compress(raw)
        response = _FakeResponse(
            [compressed[:100], compressed[100:]],
            {"Content-Encoding": "gzip", "Content-Type": "text/plain"},
        )
        body, _path, total = read_body_stream(
            response, max_bytes=1 << 20, spill_bytes=1 << 20, spill_dir=self.tmp.name
        )
        self.assertEqual(body, raw)
        self.assertEqual(total, len(raw))

    def test_gzip_bomb_dies_at_cap_during_decompression(self) -> None:
        raw = b"A" * (4 << 20)
        compressed = gzip.compress(raw)
        response = _FakeResponse(
            [compressed], {"Content-Encoding": "gzip", "Content-Type": "text/plain"}
        )
        with self.assertRaises(ScrapeError):
            read_body_stream(
                response, max_bytes=1 << 20, spill_bytes=1 << 20, spill_dir=self.tmp.name
            )

    def test_big_body_spills_to_disk(self) -> None:
        raw = (b"XY" * 512) * 4096  # 4 MiB
        response = _FakeResponse(
            [raw[: 1 << 20], raw[1 << 20 :], b""], {"Content-Type": "text/plain"}
        )
        body, path, total = read_body_stream(
            response, max_bytes=100 << 20, spill_bytes=1 << 20, spill_dir=self.tmp.name
        )
        self.assertIsNone(body)
        self.assertTrue(path)
        self.assertEqual(total, len(raw))
        self.assertEqual(decode_file(path or "", "text/plain; charset=utf-8"), raw.decode())

    def test_cap_without_spill_dir_refuses(self) -> None:
        response = _FakeResponse([b"Z" * 4096], {"Content-Type": "text/plain"})
        with self.assertRaises(ScrapeError):
            read_body_stream(response, max_bytes=1 << 20, spill_bytes=2048, spill_dir="")

    def test_decode_file_honors_meta_charset(self) -> None:
        path = os.path.join(self.tmp.name, "meta.bin")
        with open(path, "wb") as fh:
            fh.write(b'<meta charset="iso-8859-1">' + b"caf\xe9")
        self.assertIn("caf\xe9", decode_file(path, ""))


class DistillTests(unittest.TestCase):
    def test_extractive_summary_keeps_headings_and_dense_text(self) -> None:
        text = (
            "Architecture\n"
            + "The system uses a push-pull design with three independent workers and one queue. "
            * 3
            + "\n\nCookie preferences\n"
            + "We value your privacy.\n\n"
            + "Benchmarks\n"
            + "On the standard suite the implementation reaches 1.4x the baseline throughput. " * 3
        )
        out = extractive_summary(text, max_chars=600)
        self.assertIn("Architecture", out)
        self.assertIn("Benchmarks", out)
        self.assertNotIn("Cookie preferences", out)
        self.assertLessEqual(len(out), 600)

    def test_distill_degrades_to_extractive_when_gemini_fails(self) -> None:
        # generate() raising must degrade to the deterministic cut, never raise.
        body = ("Paragraph 0 with enough substance to survive a summary cut. " * 1) * 40
        with mock.patch("freegem.crawler.distill.generate", side_effect=RuntimeError("no session")):
            text, mode = distill_text(BridgeConfig(log_requests=False), "https://x.test/", body)
        self.assertEqual(mode, "extractive")
        self.assertIn("Paragraph 0", text)

    def test_distill_uses_gemini_when_available(self) -> None:
        with mock.patch(
            "freegem.crawler.distill.generate", return_value="THE IMPORTANT BITS ONLY"
        ) as gen:
            text, mode = distill_text(
                BridgeConfig(log_requests=False), "https://x.test/", "long body " * 100
            )
        self.assertEqual(mode, "gemini")
        self.assertEqual(text, "THE IMPORTANT BITS ONLY")
        prompt = gen.call_args[0][1]
        self.assertIn("IMPORTANT content only", prompt)


class CampaignRetryTests(unittest.TestCase):
    """`--retry-failed` re-attempts exactly the rows that were not ok."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.db")
        db = sqlite3.connect(self.state)
        with db:
            db.executescript(_SCHEMA)
            db.execute(
                "INSERT INTO results(url, category, kind, status) VALUES(?,?,?,?)",
                ("https://fine.test/", "c", "ok", 200),
            )
            db.execute(
                "INSERT INTO results(url, category, kind, status) VALUES(?,?,?,?)",
                ("https://dead.test/", "c", "http_error", 403),
            )
        db.close()
        # v1 state file (no via/fallback columns): migration must add them.
        migration_db = sqlite3.connect(self.state)
        try:
            _migrate(migration_db)
        finally:
            migration_db.close()

    def test_retry_failed_touches_only_failures(self) -> None:
        calls: list[str] = []

        def fake_fetch(cfg: BridgeConfig, url: str, **kwargs: Any) -> FetchOutcome:
            calls.append(url)
            return FetchOutcome(url=url, kind="ok", status=200, body=b"<p>x</p>", body_size=5)

        with mock.patch("freegem.crawler.campaign.fetch_resilient", side_effect=fake_fetch):
            result = run_campaign(
                BridgeConfig(log_requests=False),
                [
                    {"url": "https://fine.test/", "category": "c"},
                    {"url": "https://dead.test/", "category": "c"},
                ],
                state_path=self.state,
                retry_failed=True,
                concurrency=1,
            )
        self.assertEqual(calls, ["https://dead.test/"])
        self.assertEqual(result.attempted, 1)
        self.assertEqual(result.readable(), 1)

    def test_report_counts_fallbacks_and_readable_pct(self) -> None:
        outcomes = {
            "https://a.test/": FetchOutcome(
                url="https://a.test/",
                kind="ok",
                status=200,
                body=b"x",
                body_size=1,
                fallback="profile_1",
            ),
            "https://b.test/": FetchOutcome(
                url="https://b.test/",
                kind="ok",
                status=200,
                body=b"x",
                body_size=1,
                fallback="profile_1",
                attempts=2,
            ),
        }
        # Shape the state file: a.test was read via a browser profile, b.test 403'd.
        db = sqlite3.connect(self.state)
        with db:
            db.execute("DELETE FROM results")
            db.execute(
                "INSERT INTO results(url, category, kind, status, fallback) VALUES(?,?,?,?,?)",
                ("https://a.test/", "c", "ok", 200, ""),
            )
            db.execute(
                "INSERT INTO results(url, category, kind, status, fallback) VALUES(?,?,?,?,?)",
                ("https://b.test/", "c", "http_error", 403, ""),
            )
        db.close()

        def fake_fetch(cfg: BridgeConfig, url: str, **kwargs: Any) -> FetchOutcome:
            return outcomes[url]

        with mock.patch("freegem.crawler.campaign.fetch_resilient", side_effect=fake_fetch):
            result = run_campaign(
                BridgeConfig(log_requests=False),
                [
                    {"url": "https://a.test/", "category": "c"},
                    {"url": "https://b.test/", "category": "c"},
                ],
                state_path=self.state,
                retry_failed=True,
                concurrency=1,
            )
        data = result.as_dict()
        self.assertEqual(data["attempted"], 1)  # only the 403 was retried
        self.assertEqual(data["readable_pct"], 100.0)
        self.assertEqual(data["by_fallback"], {"profile_1": 1})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
