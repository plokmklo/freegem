"""Tests for the web scraper and the shared SSRF guard.

Two halves:

* Refusal tests run the guard UNSTUBBED. Literal IP hosts (``127.0.0.1``,
  ``10.255.255.1``, ``169.254.169.254``) resolve locally without any network
  traffic, so the refusal exercises production code end to end, and
  ``socket.create_connection`` is asserted never to fire.
* Fetch tests run against a throwaway ``ThreadingHTTPServer`` on loopback.
  The URL host is a fake name (``site.test``) that the DNS fake resolves to a
  public TEST-NET IP; a ``create_connection`` fake asserts the dial went to
  that CHECKED IP and then forwards to the loopback server -- so every fetch
  test also re-verifies the DNS-pinning invariant.
"""

from __future__ import annotations

import gzip
import io
import json
import socket
import ssl
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

from freegem.cli import _build_parser, main
from freegem.config import BridgeConfig
from freegem.gemini import BridgeError
from freegem.mcp_tools import TOOL_SPECS, run_tool
from freegem.multimodal import fetch_image_bytes
from freegem.ssrf import (
    PinnedHTTPHandler,
    PinnedHTTPSHandler,
    SafeRedirect,
    SSRFRefused,
    _pinned_https_class,
    build_guarded_opener,
    pick_public_ip,
    pick_public_ips,
    url_allowed,
)
from freegem.web_scraper import (
    _MAX_BYTES,
    DEFAULT_MAX_CHARS,
    RobotsCache,
    ScrapeError,
    ScrapeResult,
    clear_robots_cache,
    extraction_quality,
    format_page,
    run_scrape_tool,
    scrape_page,
)

_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CREATE_CONNECTION = socket.create_connection

#: The fake hostname the test server answers on, and the fake "public" IP the
#: DNS fake hands out for it. The IP must pass the guard's public-address
#: check, but it is never dialed for real: the dial fake asserts the target
#: and forwards to loopback, so no packet ever leaves the test process.
_FAKE_HOST = "site.test"
_CHECKED_IP = "93.184.216.34"


def _fake_resolver(host: str, *args: object, **kwargs: object) -> list[tuple]:
    """site.test -> checked public IP; every other host is judged for real."""
    if host == _FAKE_HOST:
        return [(2, 1, 6, "", (_CHECKED_IP, 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)  # type: ignore[arg-type]


def _fake_create_connection(address: tuple, *args: object, **kwargs: object) -> socket.socket:
    """Assert the dial went to the checked IP, then forward to loopback."""
    ip, port = address[0], address[1]
    assert ip == _CHECKED_IP, f"dial target {ip!r} is not the checked IP {_CHECKED_IP!r}"
    return _REAL_CREATE_CONNECTION(("127.0.0.1", port), *args, **kwargs)  # type: ignore[arg-type]


def _info(ip: str) -> tuple:
    return (2, 1, 6, "", (ip, 0))


PAGE_HTML = (
    "<!DOCTYPE html><html><head><title>Freegem Target</title>"
    "<style>body { color: red }</style></head><body>"
    "<nav>skip me nav junk</nav>"
    "<h1>Welcome</h1>"
    "<p>First   paragraph with &amp; entity.</p>"
    "<script>alert('evil')</script>"
    "<ul><li>alpha</li><li>beta</li></ul>"
    "<p><a href='/rel'>Relative</a> and <a href='https://outside.example/x'>Outside</a></p>"
    "<footer>footer junk</footer>"
    "</body></html>"
)


class _FakeSite(BaseHTTPRequestHandler):
    """Loopback stand-in with a robots.txt that disallows /private*."""

    protocol_version = "HTTP/1.1"
    robots_body: str | None = "User-agent: *\nDisallow: /private\n"

    def log_message(self, format: str, *args: object) -> None:
        pass  # silence the console

    def _send(self, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/robots.txt":
            if self.robots_body is None:
                self.send_error(404)
                return
            self._send(self.robots_body.encode(), "text/plain")
            return
        if path in ("/redirect", "/redirect-private"):
            target = "/page" if path == "/redirect" else "http://10.255.255.1/steal"
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/plain":
            self._send(b"just plain text here", "text/plain; charset=utf-8")
            return
        if path == "/octet":
            self._send(b"\x00\x01binary", "application/octet-stream")
            return
        if path == "/gzip":
            html = "<html><head><title>Gzipped</title></head><body><p>gz body</p></body></html>"
            self._send(gzip.compress(html.encode()), "text/html", {"Content-Encoding": "gzip"})
            return
        if path == "/bomb":
            # 100 KB of zeros compresses to ~100 wire bytes: only the
            # decompressed cap can catch this, never the wire cap.
            self._send(gzip.compress(b"0" * 100_000), "text/html", {"Content-Encoding": "gzip"})
            return
        if path == "/big":
            html = (
                "<html><head><title>Big</title></head><body><p>"
                + "x" * 20_000
                + "</p></body></html>"
            )
            self._send(html.encode(), "text/html")
            return
        if path == "/page" or path == "/private-page":
            self._send(PAGE_HTML.encode(), "text/html; charset=utf-8")
            return
        self.send_error(404)


class _FakeSiteTestCase(unittest.TestCase):
    """Base with a loopback test server and the DNS/dial fakes."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(log_requests=False)
        self.resolver = mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_resolver)
        self.resolver.start()
        self.addCleanup(self.resolver.stop)
        self.dial = mock.patch(
            "freegem.ssrf.socket.create_connection", side_effect=_fake_create_connection
        )
        self.dial.start()
        self.addCleanup(self.dial.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSite)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://{_FAKE_HOST}:{self.server.server_port}"

    def tearDown(self) -> None:
        _FakeSite.robots_body = "User-agent: *\nDisallow: /private\n"


class UrlAllowedTests(unittest.TestCase):
    """The syntactic half of the guard, unstubbed."""

    def test_non_http_schemes_refused(self) -> None:
        for url in (
            "file:///etc/passwd",
            "ftp://host/x",
            "javascript:alert(1)",
            "data:text/html,x",
        ):
            with self.subTest(url=url), self.assertRaises(SSRFRefused):
                url_allowed(url)

    def test_embedded_credentials_refused(self) -> None:
        with self.assertRaises(SSRFRefused):
            url_allowed("http://user:pass@example.com/")

    def test_missing_host_refused(self) -> None:
        with self.assertRaises(SSRFRefused):
            url_allowed("http:///nohost")

    def test_whitespace_in_url_refused(self) -> None:
        with self.assertRaises(SSRFRefused):
            url_allowed("https://example.com/a b")

    def test_clean_https_url_passes_through(self) -> None:
        self.assertEqual(url_allowed("https://example.com/x"), "https://example.com/x")


class PickPublicIpTests(unittest.TestCase):
    """Literal private IPs are refused unstubbed; fake DNS covers the rest."""

    def test_literal_private_targets_refused_without_stubbing(self) -> None:
        for host in (
            "127.0.0.1",
            "10.255.255.1",
            "192.168.1.1",
            "172.16.0.9",
            "169.254.169.254",
            "0.0.0.0",
            "::1",
        ):
            with self.subTest(host=host), self.assertRaises(SSRFRefused):
                pick_public_ip(host)

    def test_resolution_failure_is_refusal(self) -> None:
        with mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=OSError("no dns")):
            with self.assertRaises(SSRFRefused):
                pick_public_ip("down.example.invalid")

    def test_empty_result_is_refusal(self) -> None:
        with mock.patch("freegem.ssrf.socket.getaddrinfo", return_value=[]):
            with self.assertRaises(SSRFRefused):
                pick_public_ip("empty.example")

    def test_mapped_ipv6_of_private_is_refused(self) -> None:
        with mock.patch("freegem.ssrf.socket.getaddrinfo", return_value=[_info("::ffff:10.0.0.9")]):
            with self.assertRaises(SSRFRefused):
                pick_public_ip("sneaky.example")

    def test_refuses_when_any_address_is_private(self) -> None:
        with mock.patch(
            "freegem.ssrf.socket.getaddrinfo",
            return_value=[_info("93.184.216.34"), _info("192.168.0.1")],
        ):
            with self.assertRaises(SSRFRefused):
                pick_public_ip("mixed.example")

    def test_returns_first_public_address(self) -> None:
        with mock.patch(
            "freegem.ssrf.socket.getaddrinfo",
            return_value=[_info("93.184.216.34"), _info("93.184.216.34")],
        ):
            self.assertEqual(pick_public_ip("example.test"), "93.184.216.34")

    def test_port_is_stripped_before_resolution(self) -> None:
        with mock.patch(
            "freegem.ssrf.socket.getaddrinfo", return_value=[_info("93.184.216.34")]
        ) as ga:
            pick_public_ip("example.com:8080")
        self.assertEqual(ga.call_args[0][0], "example.com")


class SafeRedirectTests(unittest.TestCase):
    """Redirect hops are judged by the same guard, unstubbed."""

    def setUp(self) -> None:
        self.handler = SafeRedirect()

    def _redirect(self, newurl: str) -> urllib.request.Request | None:
        return self.handler.redirect_request(
            urllib.request.Request("https://origin.test/a"),
            None,
            302,
            "Found",
            {},
            newurl,
        )

    def test_redirect_to_private_literal_refused(self) -> None:
        with self.assertRaises(urllib.error.URLError):
            self._redirect("http://10.255.255.1/steal")

    def test_redirect_to_metadata_ip_refused(self) -> None:
        with self.assertRaises(urllib.error.URLError):
            self._redirect("http://169.254.169.254/latest/meta-data/")

    def test_redirect_to_loopback_refused(self) -> None:
        with self.assertRaises(urllib.error.URLError):
            self._redirect("http://127.0.0.1:8080/admin")

    def test_redirect_to_ftp_refused(self) -> None:
        with self.assertRaises(urllib.error.URLError):
            self._redirect("ftp://origin.test/b")

    def test_redirect_to_public_literal_allowed(self) -> None:
        new = self._redirect("https://93.184.216.34/b")
        self.assertIsInstance(new, urllib.request.Request)


class PinningTests(unittest.TestCase):
    """The anti-rebinding point: dial the checked IP, never re-resolve."""

    def test_http_dials_checked_ip_not_hostname(self) -> None:
        with (
            mock.patch("freegem.ssrf.socket.getaddrinfo", return_value=[_info("93.184.216.34")]),
            mock.patch(
                "freegem.ssrf.socket.create_connection", side_effect=OSError("refused")
            ) as create,
        ):
            req = urllib.request.Request("http://example.test/page")
            req.timeout = 0.1
            with self.assertRaises(OSError):
                PinnedHTTPHandler().http_open(req)
        self.assertEqual(create.call_args[0][0], ("93.184.216.34", 80))

    def test_https_pins_ip_but_verifies_hostname(self) -> None:
        context = mock.MagicMock()
        conn = _pinned_https_class(["93.184.216.34"], context)("example.test")
        with mock.patch(
            "freegem.ssrf.socket.create_connection", return_value=mock.MagicMock()
        ) as create:
            conn.connect()
        self.assertEqual(create.call_args[0][0], ("93.184.216.34", 443))
        self.assertEqual(
            context.wrap_socket.call_args.kwargs.get("server_hostname"), "example.test"
        )

    def test_http_falls_back_to_next_checked_ip(self) -> None:
        # The first checked IP is unreachable: the dial must move on WITHIN
        # the checked list, never re-resolving the hostname.
        dials: list[tuple] = []

        def fake_dial(address: tuple, *args: object, **kwargs: object) -> socket.socket:
            dials.append(address)
            # Refuse every dial: the test asserts the ORDER of the attempts,
            # not the HTTP exchange that would follow a successful one.
            raise OSError("unreachable")

        with (
            mock.patch(
                "freegem.ssrf.socket.getaddrinfo",
                return_value=[_info("93.184.216.34"), _info("104.20.23.154")],
            ),
            mock.patch("freegem.ssrf.socket.create_connection", side_effect=fake_dial),
        ):
            req = urllib.request.Request("http://example.test/page")
            req.timeout = 0.1
            with self.assertRaises(OSError):
                PinnedHTTPHandler().http_open(req)
        self.assertEqual(dials, [("93.184.216.34", 80), ("104.20.23.154", 80)])

    def test_pick_public_ips_dedupes_and_keeps_order(self) -> None:
        with mock.patch(
            "freegem.ssrf.socket.getaddrinfo",
            return_value=[
                _info("104.20.23.154"),
                _info("104.20.23.154"),
                _info("2606:4700:10::6814:179a"),
            ],
        ):
            self.assertEqual(
                pick_public_ips("example.test"),
                ["104.20.23.154", "2606:4700:10::6814:179a"],
            )


class ScrapeGuardTests(unittest.TestCase):
    """scrape_page refuses before any socket exists, guard unstubbed."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(log_requests=False)

    def _refused(self, url: str) -> None:
        with mock.patch("freegem.ssrf.socket.create_connection") as create:
            with self.assertRaises(ScrapeError):
                scrape_page(self.cfg, url)
            create.assert_not_called()

    def test_file_scheme_refused_before_any_request(self) -> None:
        self._refused("file:///etc/passwd")

    def test_ftp_scheme_refused_before_any_request(self) -> None:
        self._refused("ftp://host/x")

    def test_embedded_credentials_refused(self) -> None:
        self._refused("http://user:pass@example.com/")

    def test_loopback_refused(self) -> None:
        self._refused("http://127.0.0.1:9/x")

    def test_metadata_ip_refused(self) -> None:
        self._refused("http://169.254.169.254/latest/meta-data/")

    def test_private_lan_refused(self) -> None:
        self._refused("http://10.1.2.3/admin")

    def test_mcp_tool_cannot_reach_private_hosts(self) -> None:
        with self.assertRaises(BridgeError):
            run_scrape_tool(self.cfg, {"url": "http://169.254.169.254/x"})


class ScrapeFetchTests(_FakeSiteTestCase):
    """Happy paths and caps, against the loopback fake site."""

    def test_extracts_title_text_and_strips_junk(self) -> None:
        result = scrape_page(self.cfg, f"{self.base}/page")
        self.assertEqual(result.title, "Freegem Target")
        self.assertIn("Welcome", result.text)
        self.assertIn("First paragraph with & entity.", result.text)
        self.assertIn("- alpha", result.text)
        self.assertIn("- beta", result.text)
        self.assertNotIn("alert", result.text)
        self.assertNotIn("nav junk", result.text)
        self.assertNotIn("footer junk", result.text)

    def test_output_is_wrapped_in_untrusted_markers(self) -> None:
        out = format_page(scrape_page(self.cfg, f"{self.base}/page"))
        self.assertTrue(out.startswith("[UNTRUSTED WEB CONTENT"))
        self.assertTrue(out.endswith("[END UNTRUSTED WEB CONTENT]"))

    def test_links_resolved_and_deduped(self) -> None:
        result = scrape_page(self.cfg, f"{self.base}/page")
        hrefs = [href for href, _ in result.links]
        self.assertIn(f"{self.base}/rel", hrefs)
        self.assertIn("https://outside.example/x", hrefs)
        self.assertIn("Links:", format_page(result, include_links=True))
        self.assertNotIn("Links:", format_page(result))

    def test_anchor_text_is_captured(self) -> None:
        result = scrape_page(self.cfg, f"{self.base}/page")
        self.assertEqual(dict(result.links).get(f"{self.base}/rel"), "Relative")

    def test_gzip_response_decoded(self) -> None:
        result = scrape_page(self.cfg, f"{self.base}/gzip")
        self.assertEqual(result.title, "Gzipped")
        self.assertIn("gz body", result.text)

    def test_wire_cap_refuses_oversized_body(self) -> None:
        with mock.patch("freegem.web_scraper._MAX_BYTES", 100):
            with self.assertRaises(ScrapeError) as caught:
                scrape_page(self.cfg, f"{self.base}/big")
        self.assertIn("response exceeds", str(caught.exception))

    def test_gzip_bomb_refused_at_decompress_cap(self) -> None:
        self.assertEqual(_MAX_BYTES, 100 * 1024 * 1024)  # the documented 100 MB cap
        with mock.patch("freegem.web_scraper._MAX_BYTES", 1000):
            with self.assertRaises(ScrapeError) as caught:
                scrape_page(self.cfg, f"{self.base}/bomb")
        self.assertIn("decompressed response exceeds", str(caught.exception))

    def test_max_chars_truncates(self) -> None:
        result = scrape_page(self.cfg, f"{self.base}/big", max_chars=50)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.text), 50)
        self.assertIn("[content truncated", format_page(result))

    def test_default_max_chars_constant(self) -> None:
        # 2.9: raised 20,000 -> 50,000 so long pages (HN threads, API
        # references) are not truncated before the interesting part.
        self.assertEqual(DEFAULT_MAX_CHARS, 50_000)

    def test_non_html_content_type_refused(self) -> None:
        with self.assertRaises(ScrapeError) as caught:
            scrape_page(self.cfg, f"{self.base}/octet")
        self.assertIn("refusing content type", str(caught.exception))

    def test_robots_not_consulted_by_default(self) -> None:
        """robots.txt is attacker-controlled text; by default freegem never
        fetches it, so a Disallow-all robots.txt must not block a scrape."""
        _FakeSite.robots_body = "User-agent: *\nDisallow: /\n"
        result = scrape_page(self.cfg, f"{self.base}/private-page")
        self.assertIn("Welcome", result.text)

    def test_robots_opt_in_still_refused(self) -> None:
        """The legacy behavior stays available as an explicit opt-in."""
        with self.assertRaises(ScrapeError) as caught:
            scrape_page(self.cfg, f"{self.base}/private-page", ignore_robots=False)
        self.assertIn("robots.txt disallows", str(caught.exception))

    def test_robots_404_allows(self) -> None:
        _FakeSite.robots_body = None  # server now 404s robots.txt
        result = scrape_page(self.cfg, f"{self.base}/page", ignore_robots=False)
        self.assertIn("Welcome", result.text)

    def test_redirect_to_public_hop_is_followed(self) -> None:
        result = scrape_page(self.cfg, f"{self.base}/redirect")
        self.assertIn("Welcome", result.text)

    def test_redirect_to_private_host_refused(self) -> None:
        # The hop is judged by the REAL guard: 10.255.255.1 is a literal
        # private IP, resolved locally, refused without any network.
        with self.assertRaises(ScrapeError):
            scrape_page(self.cfg, f"{self.base}/redirect-private")

    def test_plain_text_passes_through(self) -> None:
        result = scrape_page(self.cfg, f"{self.base}/plain")
        self.assertEqual(result.title, "")
        self.assertIn("just plain text here", result.text)


class ToolDispatchTests(_FakeSiteTestCase):
    """MCP dispatch and the Gemini question mode."""

    def test_web_scrape_in_tool_specs(self) -> None:
        self.assertIn("web_scrape", {spec["name"] for spec in TOOL_SPECS})

    def test_run_tool_dispatches_web_scrape(self) -> None:
        with mock.patch("freegem.web_scraper.run_scrape_tool", return_value="page out") as runner:
            out = run_tool(
                self.cfg, "web_scrape", {"url": f"{self.base}/page", "include_links": True}
            )
        self.assertEqual(out, "page out")
        self.assertTrue(runner.call_args[0][1]["include_links"])

    def test_missing_url_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            run_scrape_tool(self.cfg, {})

    def test_question_mode_sends_marked_page_to_gemini(self) -> None:
        with (
            mock.patch(
                "freegem.web_scraper.scrape_page",
                return_value=ScrapeResult(url="https://x.test/", title="T", text="page body"),
            ),
            mock.patch("freegem.mcp_tools.run_tool", return_value="the answer") as run,
        ):
            out = run_scrape_tool(self.cfg, {"url": "https://x.test/", "question": "summarize"})
        self.assertEqual(out, "the answer")
        prompt = run.call_args[0][2]["question"]
        self.assertIn("[UNTRUSTED WEB CONTENT", prompt)
        self.assertIn("[END UNTRUSTED WEB CONTENT]", prompt)
        self.assertIn("Question: summarize", prompt)
        self.assertIn("not instructions", prompt)

    def test_question_mode_still_refused_by_guard(self) -> None:
        with mock.patch("freegem.mcp_tools.run_tool") as run:
            with self.assertRaises(BridgeError):
                run_scrape_tool(self.cfg, {"url": "http://169.254.169.254/x", "question": "q"})
            run.assert_not_called()

    def test_max_chars_clamped(self) -> None:
        with (
            mock.patch(
                "freegem.web_scraper.scrape_page",
                return_value=ScrapeResult("u", "", "t"),
            ) as scrape,
            mock.patch("freegem.mcp_tools.run_tool") as run,
        ):
            run_scrape_tool(self.cfg, {"url": "https://x.test/", "max_chars": 10**9})
        self.assertLessEqual(scrape.call_args.kwargs["max_chars"], 200_000)
        run.assert_not_called()

    def test_mcp_server_exposes_web_scrape(self) -> None:
        try:
            import mcp  # noqa: F401
        except ImportError:  # pragma: no cover - depends on install
            self.skipTest("mcp extra not installed")
        import asyncio

        from freegem.mcp_tools import build_server

        server = build_server(self.cfg)
        tools = asyncio.run(server.list_tools())
        self.assertIn("web_scrape", {tool.name for tool in tools})


class ScrapeCliTests(_FakeSiteTestCase):
    """CLI surface: flags, stdout, error exit."""

    def test_parser_accepts_scrape_flags(self) -> None:
        args = _build_parser().parse_args(
            [
                "scrape",
                "https://example.com",
                "--links",
                "--max-chars",
                "500",
                "--ask",
                "what is this?",
                "--respect-robots",
            ]
        )
        self.assertEqual(args.url, ["https://example.com"])
        self.assertTrue(args.links)
        self.assertEqual(args.max_chars, 500)
        self.assertEqual(args.ask, "what is this?")
        self.assertTrue(args.respect_robots)

    def test_parser_defaults(self) -> None:
        args = _build_parser().parse_args(["scrape", "https://example.com"])
        self.assertFalse(args.links)
        # None means "caller did not choose": 50k, or 200k with --ask.
        self.assertIsNone(args.max_chars)
        self.assertIsNone(args.ask)
        self.assertFalse(args.respect_robots)
        self.assertFalse(args.no_smart)
        self.assertFalse(args.no_cache)

    def test_parser_accepts_multiple_urls(self) -> None:
        args = _build_parser().parse_args(
            ["scrape", "https://a.example", "https://b.example", "--format", "jsonl"]
        )
        self.assertEqual(args.url, ["https://a.example", "https://b.example"])
        self.assertEqual(args.format, "jsonl")

    def test_main_prints_page(self) -> None:
        with (
            mock.patch("freegem.web_scraper.scrape_cli", return_value="PAGE OUT") as scrape,
            redirect_stdout(io.StringIO()) as out,
        ):
            rc = main(["scrape", "https://example.com"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "PAGE OUT\n")
        self.assertEqual(scrape.call_args.kwargs["url"], "https://example.com")
        self.assertTrue(scrape.call_args.kwargs["ignore_robots"])

    def test_main_error_exits_one(self) -> None:
        with (
            mock.patch("freegem.web_scraper.scrape_cli", side_effect=ScrapeError("nope")),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as err,
        ):
            rc = main(["scrape", "https://example.com"])
        self.assertEqual(rc, 1)
        self.assertIn("error: nope", err.getvalue())

    def test_main_against_fake_site(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["scrape", f"{self.base}/page", "--links"])
        self.assertEqual(rc, 0)
        self.assertIn("Freegem Target", out.getvalue())
        self.assertIn(f"{self.base}/rel", out.getvalue())

    def test_main_ask_mode_uses_gemini(self) -> None:
        with mock.patch("freegem.mcp_tools.run_tool", return_value="answer text") as run:
            out = io.StringIO()
            with redirect_stdout(out):
                rc = main(["scrape", f"{self.base}/page", "--ask", "what is this page?"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "answer text\n")
        self.assertIn("Question: what is this page?", run.call_args[0][2]["question"])


class ImageFetchGuardTests(_FakeSiteTestCase):
    """The image fetcher now shares the pinned guard: regression cover."""

    def test_image_fetch_through_pinned_guard(self) -> None:
        png = b"\x89PNG\r\n\x1a\npayload"
        with mock.patch.object(_FakeSite, "do_GET", _png_getter(png)):
            data = fetch_image_bytes(self.cfg, f"{self.base}/pic.png")
        self.assertEqual(data, png)

    def test_image_fetch_loopback_refused(self) -> None:
        # The fake resolver passes unknown hosts to the REAL resolver, so this
        # refusal is unstubbed end to end.
        with mock.patch("freegem.ssrf.socket.create_connection") as create:
            self.assertEqual(fetch_image_bytes(self.cfg, "http://127.0.0.1:9/a.png"), b"")
            create.assert_not_called()

    def test_image_fetch_metadata_ip_refused(self) -> None:
        with mock.patch("freegem.ssrf.socket.create_connection") as create:
            self.assertEqual(
                fetch_image_bytes(self.cfg, "http://169.254.169.254/x.png"),
                b"",
            )
            create.assert_not_called()


def _png_getter(png: bytes) -> Callable[[BaseHTTPRequestHandler], None]:
    def do_GET(self: BaseHTTPRequestHandler) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(png)))
        self.end_headers()
        self.wfile.write(png)

    return do_GET


class GuardedOpenerContextTests(unittest.TestCase):
    """build_guarded_opener must use the caller's SSLContext, not a fresh one.

    The project keeps one cached TLS context (gemini._ssl_ctx) and every fetch
    path is supposed to share it. A context built per fetch works but silently
    re-parses the CA bundle on every scrape and splits trust stores across
    callers -- the inconsistency this test pins shut.
    """

    @staticmethod
    def _pinned_https_handler_of(opener: Any) -> PinnedHTTPSHandler:
        """The single PinnedHTTPSHandler in an opener's runtime handler list.

        ``OpenerDirector.handlers`` is a runtime instance attribute that the
        typeshed stubs do not declare, hence the __dict__ access.
        """
        found = [h for h in opener.__dict__["handlers"] if isinstance(h, PinnedHTTPSHandler)]
        assert len(found) == 1, f"expected exactly one pinned HTTPS handler, got {found}"
        return found[0]

    def test_shared_context_reaches_the_https_handler(self) -> None:
        context = ssl.create_default_context()
        opener = build_guarded_opener(None, context)
        self.assertIs(self._pinned_https_handler_of(opener)._pinned_context, context)

    def test_default_context_when_none_given(self) -> None:
        opener = build_guarded_opener(None, None)
        self.assertIsNotNone(self._pinned_https_handler_of(opener)._pinned_context)


# ─── robots.txt cache ─────────────────────────────────────────────────────────


class _FakeHeaders(dict):
    """dict with a headers-like .get contract (already is one)."""


class _FakeBytesResponse:
    """A read()-able response with headers, for robots and JSON bodies."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = _FakeHeaders(headers or {})

    def read(self, size: int = -1) -> bytes:
        if not self._body:
            return b""
        out, self._body = self._body, b""
        return out


class _FakeOpener:
    """build_guarded_opener stand-in serving queued responses per request."""

    def __init__(self, script: list[Any]) -> None:
        self.script = script  # each entry: response, or an exception instance
        self.requests: list[urllib.request.Request] = []

    def open(self, request: urllib.request.Request, timeout: float = 30) -> Any:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _not_modified() -> urllib.error.HTTPError:
    """A 304 the way urllib raises it (hdrs must be an email Message)."""
    return urllib.error.HTTPError("http://s.test/robots.txt", 304, "Not Modified", Message(), None)


class RobotsCacheTests(unittest.TestCase):
    """The per-origin robots cache: TTL hits, validators, refusal rules."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(log_requests=False)
        clear_robots_cache()

    def _robots(self, body: str, etag: str | None = None) -> _FakeBytesResponse:
        headers = {"Content-Encoding": ""}
        if etag:
            headers["ETag"] = etag
        return _FakeBytesResponse(body.encode(), headers)

    def test_second_call_within_ttl_skips_the_network(self) -> None:
        opener = _FakeOpener([self._robots("User-agent: *\nDisallow: /private\n")])
        with mock.patch("freegem.web_scraper.build_guarded_opener", return_value=opener):
            cache = RobotsCache()
            cache.can_fetch(self.cfg, "http://s.test/page", "freegem-web-scraper/2.1")
            cache.can_fetch(self.cfg, "http://s.test/other", "freegem-web-scraper/2.1")
        self.assertEqual(len(opener.requests), 1)  # one fetch, two checks

    def test_disallow_enforced_from_cache_without_network(self) -> None:
        opener = _FakeOpener([self._robots("User-agent: *\nDisallow: /private\n")])
        with mock.patch("freegem.web_scraper.build_guarded_opener", return_value=opener):
            cache = RobotsCache()
            cache.can_fetch(self.cfg, "http://s.test/page", "freegem-web-scraper/2.1")
            with self.assertRaises(ScrapeError) as caught:
                cache.can_fetch(self.cfg, "http://s.test/private", "freegem-web-scraper/2.1")
        self.assertIn("robots.txt disallows", str(caught.exception))

    def test_revalidation_sends_validators_and_304_keeps_rules(self) -> None:
        opener = _FakeOpener(
            [
                self._robots("User-agent: *\nDisallow: /private\n", etag='"r1"'),
                _not_modified(),
                _not_modified(),
            ]
        )
        with mock.patch("freegem.web_scraper.build_guarded_opener", return_value=opener):
            cfg = self.cfg.replace(robots_cache_ttl_sec=0)  # revalidate every call
            cache = RobotsCache()
            cache.can_fetch(cfg, "http://s.test/page", "freegem-web-scraper/2.1")
            cache.can_fetch(cfg, "http://s.test/page", "freegem-web-scraper/2.1")
            # The 304 kept the parsed rules: /private is still disallowed.
            with self.assertRaises(ScrapeError):
                cache.can_fetch(cfg, "http://s.test/private", "freegem-web-scraper/2.1")
        self.assertEqual(len(opener.requests), 3)  # ttl=0: every call revalidates
        revalidation = opener.requests[1]
        # urllib normalizes header casing ("If-none-match"); the wire name is
        # case-insensitive either way.
        self.assertEqual(revalidation.headers.get("If-none-match"), '"r1"')

    def test_network_failure_serves_stale_rules(self) -> None:
        opener = _FakeOpener(
            [
                self._robots("User-agent: *\nDisallow: /private\n"),
                urllib.error.URLError("network gone"),
                urllib.error.URLError("network gone again"),
            ]
        )
        with mock.patch("freegem.web_scraper.build_guarded_opener", return_value=opener):
            cfg = self.cfg.replace(robots_cache_ttl_sec=0)
            cache = RobotsCache()
            cache.can_fetch(cfg, "http://s.test/page", "freegem-web-scraper/2.1")
            # Last known rules survive the outage: /private stays disallowed.
            with self.assertRaises(ScrapeError):
                cache.can_fetch(cfg, "http://s.test/private", "freegem-web-scraper/2.1")
            # A URL the rules allow still works.
            cache.can_fetch(cfg, "http://s.test/page", "freegem-web-scraper/2.1")


# ─── MediaWiki source adapter ─────────────────────────────────────────────────


class MediaWikiDetectionTests(unittest.TestCase):
    """URL -> (canonical host, article title), or None for everything else."""

    def test_plain_article(self) -> None:
        from freegem.web_scraper import _mediawiki_article

        self.assertEqual(
            _mediawiki_article("https://en.wikipedia.org/wiki/Alexander_the_Great"),
            ("en.wikipedia.org", "Alexander_the_Great"),
        )

    def test_mobile_host_is_canonicalized(self) -> None:
        from freegem.web_scraper import _mediawiki_article

        self.assertEqual(
            _mediawiki_article("https://en.m.wikipedia.org/wiki/Dune_(novel)"),
            ("en.wikipedia.org", "Dune_(novel)"),
        )

    def test_percent_decoding(self) -> None:
        from freegem.web_scraper import _mediawiki_article

        self.assertEqual(
            _mediawiki_article("https://de.wikipedia.org/wiki/F%C3%BCr_Elise"),
            ("de.wikipedia.org", "Für_Elise"),
        )

    def test_non_articles_return_none(self) -> None:
        from freegem.web_scraper import _mediawiki_article

        self.assertIsNone(_mediawiki_article("https://en.wikipedia.org/wiki/Special:Search"))
        self.assertIsNone(_mediawiki_article("https://en.wikipedia.org/w/index.php?title=X"))
        self.assertIsNone(_mediawiki_article("https://example.com/wiki/Page"))
        self.assertIsNone(_mediawiki_article("ftp://en.wikipedia.org/wiki/X"))


class MediaWikiAdapterTests(unittest.TestCase):
    """scrape_page routes wiki articles through the site's own Action API."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(log_requests=False)

    def _api_body(self, title: str, extract: str) -> bytes:
        return json.dumps(
            {"query": {"pages": [{"title": title, "extract": extract}]}},
        ).encode()

    def test_article_fetched_via_api(self) -> None:
        # Two responses: the full-parse API is tried first; this mock only
        # speaks the extracts dialect, so the adapter falls back to it.
        responses = [
            _FakeBytesResponse(self._api_body("Alexander the Great", "")),
            _FakeBytesResponse(
                self._api_body("Alexander the Great", "Alexander III of Macedon ... " * 30)
            ),
        ]
        with (
            mock.patch("freegem.web_scraper._check_robots") as robots,
            mock.patch("freegem.web_scraper._fetch", side_effect=responses) as fetch,
        ):
            result = scrape_page(self.cfg, "https://en.wikipedia.org/wiki/Alexander_the_Great")
        self.assertEqual(result.via, "mediawiki-api")
        self.assertEqual(result.title, "Alexander the Great")
        self.assertIn("Alexander III of Macedon", result.text)
        # robots.txt is never consulted, by design: the adapter does not call
        # _check_robots for either URL.
        self.assertEqual(robots.call_count, 0)
        api_url = fetch.call_args[0][1]
        self.assertTrue(api_url.startswith("https://en.wikipedia.org/w/api.php?"))
        self.assertIn("titles=Alexander_the_Great", api_url)

    def test_full_parse_api_returns_whole_article(self) -> None:
        """action=parse returns the COMPLETE rendered article (tables and all)
        -- the extracts API caps its output and loses the tail of long pages."""
        long_html = (
            '<div class="mw-parser-output"><p>' + ("Real content sentence. " * 500) + "</p></div>"
        )
        parse_body = json.dumps({"parse": {"title": "Big Article", "text": long_html}}).encode()
        with (
            mock.patch("freegem.web_scraper._check_robots"),
            mock.patch(
                "freegem.web_scraper._fetch", return_value=_FakeBytesResponse(parse_body)
            ) as fetch,
        ):
            result = scrape_page(self.cfg, "https://en.wikipedia.org/wiki/Big_Article")
        self.assertEqual(result.via, "mediawiki-api-parse")
        self.assertEqual(result.title, "Big Article")
        self.assertIn("Real content sentence.", result.text)
        self.assertGreater(len(result.text), 10_000)
        self.assertIn("action=parse", fetch.call_args[0][1])

    def test_missing_article_is_a_scrape_error(self) -> None:
        responses = [
            _FakeBytesResponse(json.dumps({"error": {"code": "missingtitle"}}).encode()),
            _FakeBytesResponse(json.dumps({"query": {"pages": [{"missing": True}]}}).encode()),
        ]
        with (
            mock.patch("freegem.web_scraper._check_robots"),
            mock.patch("freegem.web_scraper._fetch", side_effect=responses),
        ):
            with self.assertRaises(ScrapeError) as caught:
                scrape_page(self.cfg, "https://en.wikipedia.org/wiki/No_Such_Article_Xyz")
        self.assertIn("no article named", str(caught.exception))

    def test_api_used_even_when_robots_would_object(self) -> None:
        """robots.txt is not consulted anywhere in the scrape path -- even a
        _check_robots that would refuse must never be called, and the wiki
        adapter keeps using the site's own API."""
        responses = [
            _FakeBytesResponse(self._api_body("Alexander the Great", "")),
            _FakeBytesResponse(
                self._api_body("Alexander the Great", "Alexander III of Macedon ... " * 30)
            ),
        ]
        with (
            mock.patch(
                "freegem.web_scraper._check_robots",
                side_effect=ScrapeError("robots.txt disallows everything"),
            ) as robots,
            mock.patch("freegem.web_scraper._fetch", side_effect=responses) as fetch,
        ):
            result = scrape_page(self.cfg, "https://en.wikipedia.org/wiki/Alexander_the_Great")
        self.assertEqual(result.via, "mediawiki-api")
        self.assertIn("Alexander III of Macedon", result.text)
        self.assertEqual(robots.call_count, 0)
        self.assertTrue(fetch.call_args[0][1].startswith("https://en.wikipedia.org/w/api.php?"))

    def test_non_wiki_url_still_uses_html_path(self) -> None:
        response = _FakeBytesResponse(
            b"<html><head><title>Local</title></head><body><p>plain html body here</p></body></html>",
            {"Content-Type": "text/html; charset=utf-8", "Content-Encoding": ""},
        )
        with (
            mock.patch("freegem.web_scraper._check_robots"),
            mock.patch("freegem.web_scraper._fetch", return_value=response),
        ):
            result = scrape_page(self.cfg, "https://example.org/wiki/Lookalike")
        self.assertEqual(result.via, "html")
        self.assertIn("plain html body here", result.text)

    def test_extract_truncated_to_max_chars(self) -> None:
        body = self._api_body("Big", "word " * 5000)
        responses = [
            _FakeBytesResponse(self._api_body("Big", "")),
            _FakeBytesResponse(body),
        ]
        with (
            mock.patch("freegem.web_scraper._check_robots"),
            mock.patch("freegem.web_scraper._fetch", side_effect=responses),
        ):
            result = scrape_page(self.cfg, "https://en.wikipedia.org/wiki/Big", max_chars=500)
        self.assertTrue(result.truncated)
        # 500 minus the trailing space rstrip() removes from "word ".
        self.assertEqual(len(result.text), 499)


class ExtractionQualityTests(unittest.TestCase):
    def test_labels(self) -> None:
        self.assertEqual(extraction_quality(""), "empty")
        self.assertEqual(extraction_quality("   \n  "), "empty")
        self.assertEqual(extraction_quality("Jump to content\nLog in\nDonate"), "nav_only")
        self.assertEqual(extraction_quality("word " * 200), "ok")

    def test_short_but_real_prose_is_ok(self) -> None:
        # Regression: the old 200-char floor labeled a ~100-char real
        # paragraph nav_only. Short real content must pass the floor.
        text = (
            "The pancreas sits behind the stomach and secretes digestive "
            "enzymes into the small intestine every day."
        )
        self.assertEqual(len(text), 103)
        self.assertEqual(extraction_quality(text), "ok")

    def test_under_floor_is_nav_only(self) -> None:
        self.assertEqual(extraction_quality("word " * 15), "nav_only")  # 75 chars

    def test_punctuation_heavy_junk_is_nav_only_even_when_long(self) -> None:
        # 144 chars of copyright/separator chrome: clears the 80-char floor
        # but is mostly digits, punctuation, and symbols, not prose.
        junk = "© 2026 • v2.8.1 • " * 8
        self.assertEqual(len(junk), 144)
        self.assertEqual(extraction_quality(junk), "nav_only")

    def test_json_payload_is_nav_only_even_when_long(self) -> None:
        payload = '{"error": "not found", "status": 404, "path": "/api/v1/x"}' * 2
        self.assertGreaterEqual(len(payload), 80)
        self.assertEqual(extraction_quality(payload), "nav_only")


class VoidElementSkipTests(unittest.TestCase):
    """A void <input> in a page header must not suppress the whole article.

    Regression: Wikipedia skins open <input type="checkbox"> inside <nav>
    for CSS-only menus. <input> is a void element -- no </input> exists --
    so pushing it on the skip stack jammed the stack and 1.8 MB of article
    extracted to "Jump to content".
    """

    def test_void_input_does_not_jam_the_skip_stack(self) -> None:
        html = (
            "<html><head><title>T</title></head><body>"
            "<nav><input type='checkbox' id='menu'><label>Menu</label></nav>"
            "<h1>Real heading</h1><p>Real paragraph content.</p>"
            "</body></html>"
        )
        response = _FakeBytesResponse(
            html.encode(), {"Content-Type": "text/html; charset=utf-8", "Content-Encoding": ""}
        )
        with (
            mock.patch("freegem.web_scraper._check_robots"),
            mock.patch("freegem.web_scraper._fetch", return_value=response),
        ):
            result = scrape_page(BridgeConfig(log_requests=False), "https://example.org/page")
        self.assertEqual(result.title, "T")
        self.assertIn("Real heading", result.text)
        self.assertIn("Real paragraph content.", result.text)
        self.assertNotIn("Menu", result.text)  # nav itself still skipped


# ─── batch scrape, shared cache, hnmain anchor, ask budget (2.9) ────────────


class BatchScrapeTests(_FakeSiteTestCase):
    """scrape_many: one bounded pool, per-URL results, failures recorded.

    smart=False throughout: these tests exercise the BATCH mechanics, not
    the fallback ladder (which would try real reader/archive hosts on
    failure -- the ladder has its own test module with scripted mocks).
    """

    def test_results_keep_caller_order(self) -> None:
        from freegem.web_scraper import scrape_many

        urls = [f"{self.base}/plain", f"{self.base}/page", f"{self.base}/big"]
        items = scrape_many(self.cfg, urls, max_chars=1000, concurrency=3, smart=False)
        self.assertEqual([item.url for item in items], urls)
        self.assertTrue(all(item.ok for item in items))
        self.assertIn("just plain text here", items[0].text)

    def test_failures_recorded_per_url(self) -> None:
        from freegem.web_scraper import scrape_many

        items = scrape_many(
            self.cfg,
            [f"{self.base}/no-such-page", f"{self.base}/page"],
            concurrency=2,
            smart=False,
        )
        self.assertFalse(items[0].ok)
        self.assertIn("HTTP 404", items[0].error)
        self.assertTrue(items[1].ok)
        # A dead URL never kills the batch.

    def test_all_failure_is_reportable(self) -> None:
        from freegem.web_scraper import scrape_many

        items = scrape_many(self.cfg, [f"{self.base}/no-such-page"], concurrency=1, smart=False)
        self.assertFalse(items[0].ok)

    def test_batch_over_cap_refused(self) -> None:
        from freegem.web_scraper import scrape_many

        with self.assertRaises(ValueError):
            scrape_many(self.cfg, [f"{self.base}/page"] * 101)

    def _record_pool(self) -> Any:
        """Patch concurrent.futures.ThreadPoolExecutor to record max_workers.

        scrape_many imports the pool inside the function body, so patching
        the source module is what the import actually resolves at call time.
        The recording wrapper returns a REAL executor, so behaviour (order,
        failures, shutdown) is untouched.
        """
        import concurrent.futures

        real_pool = concurrent.futures.ThreadPoolExecutor
        seen: list[int | None] = []

        def recording_pool(*args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs.get("max_workers"))
            return real_pool(*args, **kwargs)

        patcher = mock.patch("concurrent.futures.ThreadPoolExecutor", new=recording_pool)
        return patcher, seen

    def test_pool_clamps_request_over_sixteen(self) -> None:
        """--concurrency 20 must build a 16-worker pool (the documented cap)."""
        from freegem.web_scraper import scrape_many

        urls = [f"{self.base}/page"] * 40
        patcher, seen = self._record_pool()
        with patcher:
            items = scrape_many(self.cfg, urls, max_chars=200, concurrency=20, smart=False)
        self.assertEqual(seen, [16])
        self.assertEqual([item.url for item in items], urls)
        self.assertTrue(all(item.ok for item in items))

    def test_pool_honors_request_within_bounds(self) -> None:
        from freegem.web_scraper import scrape_many

        patcher, seen = self._record_pool()
        with patcher:
            scrape_many(self.cfg, [f"{self.base}/page"] * 4, concurrency=8, smart=False)
        self.assertEqual(seen, [8])

    def test_clamped_to_one_runs_sequentially_without_a_pool(self) -> None:
        """concurrency <= 0 clamps to 1 and takes the sequential fast path —
        no ThreadPoolExecutor is ever constructed."""
        from freegem.web_scraper import scrape_many

        patcher, seen = self._record_pool()
        with patcher:
            items = scrape_many(self.cfg, [f"{self.base}/page"] * 3, concurrency=0, smart=False)
        self.assertEqual(seen, [])
        self.assertTrue(all(item.ok for item in items))


class ScrapeCacheTests(_FakeSiteTestCase):
    """scrape_page(cache=...) shares the research cache with one-shot scrapes."""

    def _cache(self, tmp: str, ttl: int = 600) -> Any:
        from freegem.web_research import ResearchCache

        return ResearchCache(Path(tmp) / "cache.json", ttl)

    def test_hit_skips_the_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            first = scrape_page(self.cfg, f"{self.base}/page", cache=cache)
            with mock.patch(
                "freegem.web_scraper._fetch", side_effect=AssertionError("network hit")
            ):
                second = scrape_page(self.cfg, f"{self.base}/page", cache=cache)
            self.assertEqual(second.via, "cache")
            self.assertEqual(second.text, first.text)
            self.assertEqual(second.title, first.title)

    def test_refresh_refetches_and_rewrites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            scrape_page(self.cfg, f"{self.base}/page", cache=cache)
            with mock.patch("freegem.web_scraper._fetch") as fetch:
                fetch.return_value = _FakeBytesResponse(
                    PAGE_HTML.encode(), {"Content-Type": "text/html; charset=utf-8"}
                )
                refreshed = scrape_page(self.cfg, f"{self.base}/page", cache=cache, refresh=True)
            self.assertTrue(fetch.called)
            self.assertNotEqual(refreshed.via, "cache")

    def test_no_cache_argument_misses_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp, ttl=0)  # ttl<=0: never serve from cache
            scrape_page(self.cfg, f"{self.base}/page", cache=cache)
            second = scrape_page(self.cfg, f"{self.base}/page", cache=cache)
            self.assertEqual(second.via, "html")

    def test_scrape_many_writes_through(self) -> None:
        from freegem.web_scraper import scrape_many

        with tempfile.TemporaryDirectory() as tmp:
            cache = self._cache(tmp)
            scrape_many(self.cfg, [f"{self.base}/page", f"{self.base}/plain"], cache=cache)
            # Both entries are now cached: zero network on the second run.
            with mock.patch(
                "freegem.web_scraper._fetch", side_effect=AssertionError("network hit")
            ):
                again = scrape_many(
                    self.cfg,
                    [f"{self.base}/page", f"{self.base}/plain"],
                    cache=cache,
                    concurrency=2,
                )
            self.assertTrue(all(item.via == "cache" for item in again))


class MainRegionAnchorsTests(unittest.TestCase):
    """Site-specific content anchors: Hacker News' hnmain container."""

    def test_hnmain_anchor_selected(self) -> None:
        from freegem.web_scraper import _main_region

        html = (
            "<html><head><title>HN</title></head><body><center>logo bar</center>"
            '<div id="hnmain"><table><tr><td>1. Story one</td></tr>'
            "<tr><td>2. Story two</td></tr></table></div>"
            "<div>footer junk</div></body></html>"
        )
        region = _main_region(html)
        assert region is not None
        start, end = region
        self.assertIn("Story one", html[start:end])
        self.assertNotIn("logo bar", html[start:end])

    def test_hnmain_single_quote_variant(self) -> None:
        from freegem.web_scraper import _main_region

        html = "<html><body><div id='hnmain'><p>thread body</p></div></body></html>"
        self.assertIsNotNone(_main_region(html))


class AskBudgetTests(unittest.TestCase):
    """Question mode reads the whole page: --ask auto-raises max_chars."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(log_requests=False)

    def _page(self) -> ScrapeResult:
        return ScrapeResult(url="https://x.example/", title="T", text="page text " * 50)

    def test_ask_raises_default_budget_to_hard_limit(self) -> None:
        from freegem.web_scraper import _MAX_CHARS_HARD_LIMIT, scrape_cli

        with (
            mock.patch(
                "freegem.web_scraper.scrape_page", return_value=self._page()
            ) as scrape_page_mock,
            mock.patch("freegem.web_scraper._ask_gemini", return_value="an answer"),
        ):
            scrape_cli(self.cfg, url="https://x.example/", question="what?")
        self.assertEqual(scrape_page_mock.call_args.kwargs["max_chars"], _MAX_CHARS_HARD_LIMIT)

    def test_explicit_budget_is_respected(self) -> None:
        from freegem.web_scraper import scrape_cli

        with (
            mock.patch(
                "freegem.web_scraper.scrape_page", return_value=self._page()
            ) as scrape_page_mock,
            mock.patch("freegem.web_scraper._ask_gemini", return_value="an answer"),
        ):
            scrape_cli(self.cfg, url="https://x.example/", question="what?", max_chars=800)
        self.assertEqual(scrape_page_mock.call_args.kwargs["max_chars"], 800)

    def test_no_question_keeps_default(self) -> None:
        from freegem.web_scraper import DEFAULT_MAX_CHARS, scrape_cli

        with mock.patch(
            "freegem.web_scraper.scrape_page", return_value=self._page()
        ) as scrape_page_mock:
            scrape_cli(self.cfg, url="https://x.example/")
        self.assertEqual(scrape_page_mock.call_args.kwargs["max_chars"], DEFAULT_MAX_CHARS)
