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
import socket
import ssl
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    ScrapeError,
    ScrapeResult,
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
        self.assertEqual(_MAX_BYTES, 5 * 1024 * 1024)  # the documented 5 MB cap
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
        self.assertEqual(DEFAULT_MAX_CHARS, 20_000)

    def test_non_html_content_type_refused(self) -> None:
        with self.assertRaises(ScrapeError) as caught:
            scrape_page(self.cfg, f"{self.base}/octet")
        self.assertIn("refusing content type", str(caught.exception))

    def test_robots_disallow_refused(self) -> None:
        with self.assertRaises(ScrapeError) as caught:
            scrape_page(self.cfg, f"{self.base}/private-page")
        self.assertIn("robots.txt disallows", str(caught.exception))

    def test_cli_can_ignore_robots(self) -> None:
        result = scrape_page(self.cfg, f"{self.base}/private-page", ignore_robots=True)
        self.assertIn("Welcome", result.text)

    def test_robots_404_allows(self) -> None:
        _FakeSite.robots_body = None  # server now 404s robots.txt
        result = scrape_page(self.cfg, f"{self.base}/page")
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
                "--ignore-robots",
            ]
        )
        self.assertEqual(args.url, "https://example.com")
        self.assertTrue(args.links)
        self.assertEqual(args.max_chars, 500)
        self.assertEqual(args.ask, "what is this?")
        self.assertTrue(args.ignore_robots)

    def test_parser_defaults(self) -> None:
        args = _build_parser().parse_args(["scrape", "https://example.com"])
        self.assertFalse(args.links)
        self.assertEqual(args.max_chars, 20000)
        self.assertIsNone(args.ask)
        self.assertFalse(args.ignore_robots)

    def test_main_prints_page(self) -> None:
        with (
            mock.patch("freegem.web_scraper.scrape_cli", return_value="PAGE OUT") as scrape,
            redirect_stdout(io.StringIO()) as out,
        ):
            rc = main(["scrape", "https://example.com"])
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "PAGE OUT\n")
        self.assertEqual(scrape.call_args.kwargs["url"], "https://example.com")
        self.assertFalse(scrape.call_args.kwargs["ignore_robots"])

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
