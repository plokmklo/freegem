"""Tests for the browser SSRF layer (browser_guard + ssrf additions).

Phase 1 security: browser SSRF protection, Playwright/Selenium redirect
validation, subresource restrictions, private/loopback/link-local/metadata
IP blocking, and proxy-mode initial + redirect validation.
"""

import ipaddress
import unittest
import urllib.error
from typing import Any, cast
from unittest.mock import patch

from freegem.browser_guard import (
    BrowserRequestGuard,
    browser_request_allowed,
    install_playwright_route_guard,
    selenium_blocked_url_patterns,
    selenium_resolver_rules,
)
from freegem.ssrf import (
    PreflightedProxyHandler,
    SafeRedirect,
    SSRFRefused,
    build_guarded_opener,
    host_public_or_unresolvable,
    host_public_strict,
    ip_is_nonpublic,
    pick_public_ip,
)


def _fake_getaddrinfo(host: str, *args: Any) -> list[tuple[Any, ...]]:
    """Resolver table for tests: names -> IPs (private answers = refused)."""

    table: dict[str, list[tuple[Any, ...]]] = {
        "public.example": [("AF_INET", None, None, "", ("93.184.216.34", 0))],
        "private.example": [("AF_INET", None, None, "", ("10.1.2.3", 0))],
        "metadatahost.example": [("AF_INET", None, None, "", ("169.254.169.254", 0))],
        "123.public.example": [("AF_INET", None, None, "", ("93.184.216.34", 0))],
        "nat64.example": [("AF_INET6", None, None, "", ("64:ff9b::7f00:1", 0, 0, 0))],
    }
    # Any name NOT in the table ("split.example", "dead.example") raises --
    # matching real getaddrinfo, which errors on unresolvable names instead
    # of returning an empty list.
    if host not in table:
        raise OSError(f"test resolver: no entry for {host!r}")
    return table[host]


class BrowserRequestPolicyTests(unittest.TestCase):
    """The per-request policy: syntax half + strict public-only host half."""

    def test_refuses_nonpublic_ip_literals(self) -> None:
        for url in (
            "http://127.0.0.1:8081/admin",
            "http://10.0.0.5/",
            "http://172.16.0.9/",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.1.1/",  # CGNAT
            "http://0.0.0.0/",
            "https://[::1]/",
            "https://[fe80::1]/",
            "https://[fd00::1]/",
            "https://[::ffff:127.0.0.1]/",  # IPv4-mapped IPv6
        ):
            with self.assertRaises(SSRFRefused, msg=url):
                browser_request_allowed(url)

    def test_refuses_metadata_and_special_names(self) -> None:
        for url in (
            "http://metadata.google.internal/computeMetadata/v1/",
            "http://metadata.goog/",
            "http://metadata/",
            "http://localhost/x",
            "http://localhost:8081/x",
            "http://db.internal/",
            "http://printer.local/",
            "http://host.localhost/",
            "http://box.home.arpa/",
        ):
            with self.assertRaises(SSRFRefused, msg=url):
                browser_request_allowed(url)

    def test_refuses_non_http_schemes(self) -> None:
        # A JS/meta-refresh redirect to file:// must never hand local files
        # back through page.content().
        for url in ("file:///etc/passwd", "ftp://example.org/x", "data:text/html,hi"):
            with self.assertRaises(SSRFRefused, msg=url):
                browser_request_allowed(url)

    def test_refuses_credentials_and_whitespace(self) -> None:
        with self.assertRaises(SSRFRefused):
            browser_request_allowed("http://user:pass@example.com/")
        with self.assertRaises(SSRFRefused):
            browser_request_allowed("http://example.com/a b")

    def test_allows_public_url(self) -> None:
        with patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo):
            allowed = browser_request_allowed("https://public.example/page")
        self.assertEqual(allowed, "https://public.example/page")

    def test_refuses_host_resolving_nonpublic(self) -> None:
        with patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo):
            with self.assertRaises(SSRFRefused):
                browser_request_allowed("http://private.example/")
        with patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo):
            with self.assertRaises(SSRFRefused):
                browser_request_allowed("http://metadatahost.example/")

    def test_host_public_strict_refuses_unresolvable(self) -> None:
        with patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo):
            with self.assertRaises(SSRFRefused):
                host_public_strict("dead.example")


class ProxyModeTests(unittest.TestCase):
    """Proxy-mode initial URL validation via PreflightedProxyHandler."""

    def test_handler_refuses_private_resolution_before_proxy(self) -> None:
        opener = build_guarded_opener("http://127.0.0.1:3128")
        with patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo):
            with self.assertRaises(SSRFRefused):
                opener.open("http://private.example/")

    def test_handler_refuses_metadata_names(self) -> None:
        opener = build_guarded_opener("http://127.0.0.1:3128")
        with self.assertRaises(SSRFRefused):
            opener.open("http://metadata.google.internal/computeMetadata/v1/")

    def test_handler_allows_public_and_unresolvable(self) -> None:
        # Public: passes preflight, then fails dialing the (dead) proxy --
        # as URLError, NOT SSRFRefused. Locally-unresolvable names are also
        # allowed through, because in proxy mode the proxy owns DNS.
        opener = build_guarded_opener("http://127.0.0.1:9")
        with patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo):
            for url in ("http://public.example/", "http://split.example/"):
                with self.assertRaises(urllib.error.URLError) as ctx:
                    opener.open(url)
                self.assertNotIsInstance(ctx.exception, SSRFRefused, url)

    def test_preflighted_handler_is_a_proxy_handler(self) -> None:
        handler = PreflightedProxyHandler("http://p:1")
        proxies = cast(Any, handler).proxies
        self.assertIn("http", proxies)
        self.assertIn("https", proxies)

    def test_direct_mode_unchanged(self) -> None:
        # Direct mode must NOT gain the lenient unresolvable-allowed path:
        # pinned handlers still refuse non-public hosts outright.
        opener = build_guarded_opener()
        with patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo):
            with self.assertRaises(SSRFRefused):
                opener.open("http://private.example/")

    def test_proxy_redirect_to_nonpublic_is_refused(self) -> None:
        # SafeRedirect revalidates every hop in proxy mode: a redirect from a
        # public origin to an internal address must be refused.

        class FakeHeaders(dict[str, Any]):
            def get(self, key: str, default: Any = None) -> Any:
                return dict.get(self, key.lower(), default)

        req = _FakeRequest("http://public.example/")
        with self.assertRaises(urllib.error.URLError):
            SafeRedirect().redirect_request(
                cast(Any, req), None, 302, "Found", FakeHeaders(), "http://169.254.169.254/latest/"
            )

    def test_proxy_redirect_to_unresolvable_is_refused(self) -> None:
        # Redirects are attacker-influenced, so the unresolvable-allowed
        # leniency of the initial preflight does NOT apply per hop.

        class FakeHeaders(dict[str, Any]):
            def get(self, key: str, default: Any = None) -> Any:
                return dict.get(self, key.lower(), default)

        req = _FakeRequest("http://public.example/")
        with patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo):
            with self.assertRaises(urllib.error.URLError):
                SafeRedirect().redirect_request(
                    cast(Any, req), None, 302, "Found", FakeHeaders(), "http://split.example/"
                )


class _FakeRequest:  # minimal stand-in for urllib Request in redirect checks
    def __init__(self, url: str) -> None:
        self.url = url
        self.type = "http"

    def get_full_url(self) -> str:
        return self.url


class PlaywrightRouteGuardTests(unittest.TestCase):
    """Per-request validation of navigations and subresources."""

    @staticmethod
    def _route(url: str, navigation: bool) -> Any:
        class FakeRequest:
            def __init__(self, url: str) -> None:
                self.url = url
                self.resource_type = "document" if navigation else "xhr"

            def is_navigation(self) -> bool:
                return navigation

        class FakeRoute:
            def __init__(self, url: str) -> None:
                self.request = FakeRequest(url)
                self.continued = False
                self.aborted = False

            def continue_(self) -> None:
                self.continued = True

            def abort(self) -> None:
                self.aborted = True

        return FakeRoute(url)

    def test_blocks_internal_subresource_and_records_it(self) -> None:
        guard = BrowserRequestGuard()
        route = self._route("http://169.254.169.254/latest/meta-data/", navigation=False)
        guard(route)
        self.assertTrue(route.aborted)
        self.assertFalse(route.continued)
        self.assertEqual(len(guard.blocked_subresources), 1)
        self.assertEqual(len(guard.blocked_navigations), 0)

    def test_blocks_internal_navigation(self) -> None:
        guard = BrowserRequestGuard()
        route = self._route("http://127.0.0.1:8081/admin", navigation=True)
        guard(route)
        self.assertTrue(route.aborted)
        self.assertEqual(len(guard.blocked_navigations), 1)
        self.assertIn("127.0.0.1", guard.blocked_navigations[0])
        self.assertIn("non-public", guard.blocked_navigations[0])
        self.assertTrue(guard.blocked_any_navigation)

    def test_continues_public_request(self) -> None:
        guard = BrowserRequestGuard()
        route = self._route("https://public.example/x", navigation=False)
        with patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo):
            guard(route)
        self.assertTrue(route.continued)
        self.assertFalse(route.aborted)
        self.assertEqual(len(guard.blocked_navigations), 0)
        self.assertEqual(len(guard.blocked_subresources), 0)

    def test_blocks_file_scheme_navigation(self) -> None:
        guard = BrowserRequestGuard()
        route = self._route("file:///etc/passwd", navigation=True)
        guard(route)
        self.assertTrue(route.aborted)
        self.assertEqual(len(guard.blocked_navigations), 1)

    def test_install_registers_wildcard_route(self) -> None:
        registered: dict[str, Any] = {}

        class FakeContext:
            def route(self, pattern: str, handler: Any) -> None:
                registered["pattern"] = pattern
                registered["handler"] = handler

        guard = install_playwright_route_guard(FakeContext())
        self.assertEqual(registered["pattern"], "**/*")
        self.assertIs(registered["handler"], guard)
        self.assertIsInstance(guard, BrowserRequestGuard)


class SeleniumPatternLayerTests(unittest.TestCase):
    """The CDP blocklist + resolver rules + final-URL re-validation."""

    def test_blocked_url_patterns_cover_key_targets(self) -> None:
        patterns = selenium_blocked_url_patterns()
        self.assertGreater(len(patterns), 100)
        self.assertEqual(len(patterns), len(set(patterns)))
        for must in (
            "http://10.*",
            "https://10.*",
            "http://127.*",
            "http://169.254.*",
            "http://192.168.*",
            "http://172.16.*",
            "http://172.31.*",
            "http://100.64.*",
            "http://100.127.*",
            "wss://[fc*]*",
            "http://localhost/",
            "http://localhost:*",
            "http://*.localhost/",
            "http://metadata.google.internal/",
            "https://metadata.goog/",
            "ws://*.internal/",
        ):
            self.assertIn(must, patterns, must)
        # Global-unicast IPv6 must NOT be blocked.
        self.assertNotIn("http://[2*]*", patterns)

    def test_resolver_rules_kill_metadata_names(self) -> None:
        rules = selenium_resolver_rules()
        for must in (
            "MAP localhost ~NOTFOUND",
            "MAP *.internal ~NOTFOUND",
            "MAP metadata.google.internal ~NOTFOUND",
            "MAP *.local ~NOTFOUND",
        ):
            self.assertIn(must, rules)

    def test_final_url_refusal_returns_no_content(self) -> None:
        from freegem.selenium_fetcher import SeleniumOptions, SeleniumResult, fetch_with_selenium

        class FakeDriver:
            def set_page_load_timeout(self, _: float) -> None:
                pass

            def implicitly_wait(self, _: float) -> None:
                pass

            def get(self, url: str) -> None:
                pass

            @property
            def current_url(self) -> str:
                # The redirect landed on the cloud metadata service.
                return "http://169.254.169.254/latest/meta-data/"

            @property
            def title(self) -> str:
                return "INTERNAL - must not leak"

            @property
            def page_source(self) -> str:
                return "<html>secret internal dashboard</html>"

            def quit(self) -> None:
                pass

        driver = FakeDriver()
        with (
            patch("freegem.selenium_fetcher._build_driver", return_value=driver),
            patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo),
        ):
            res = fetch_with_selenium(SeleniumOptions(url="https://public.example/"))
        self.assertIsInstance(res, SeleniumResult)
        self.assertFalse(res.ok)
        assert res.error is not None
        self.assertIn("SSRF refused", res.error)
        self.assertIn("final URL", res.error)
        self.assertEqual(res.text, "")  # content withheld
        self.assertEqual(res.title, "")

    def test_initial_url_refusal_never_builds_driver(self) -> None:
        from freegem.selenium_fetcher import SeleniumOptions, fetch_with_selenium

        built: list[Any] = []

        def boom(*args: Any, **kwargs: Any) -> Any:
            built.append(1)
            raise AssertionError("driver must not be built for a refused URL")

        with patch("freegem.selenium_fetcher._build_driver", boom):
            res = fetch_with_selenium(SeleniumOptions(url="http://127.0.0.1:9/"))
        self.assertFalse(res.ok)
        self.assertIn("SSRF refused", res.error or "")
        self.assertEqual(built, [])


class AlternateIPv4FormTests(unittest.TestCase):
    """inet_aton-style IPv4 spellings must never pass any host check.

    ``ipaddress`` refuses these, and (on Windows) ``getaddrinfo`` fails on
    them -- but proxies and browsers parse them via inet_aton. Before the
    fix, proxy mode's "locally unresolvable -> the proxy resolves" leniency
    waved them through: ``http://2852039166/`` reached a Squid proxy as
    169.254.169.254 (decimal for the cloud metadata service).
    """

    FORMS = (
        "2130706433",  # decimal integer -> 127.0.0.1
        "2852039166",  # decimal integer -> 169.254.169.254 (metadata)
        "127.1",  # two-octet shorthand -> 127.0.0.1
        "0x7f000001",  # hex integer -> 127.0.0.1
        "0177.0.0.1",  # octal first octet -> 127.0.0.1
        "127.0.0.01",  # leading zero (octal-ish parsers) -> 127.0.0.1
        "0x7f.0x0.0x0.0x1",  # hex octets -> 127.0.0.1
        "0",  # -> 0.0.0.0 in browsers/proxies
        "127.0.0.1.",  # trailing-dot literal -> 127.0.0.1
    )

    def test_proxy_preflight_refuses_every_form(self) -> None:
        for host in self.FORMS:
            with self.subTest(host=host), self.assertRaises(SSRFRefused):
                host_public_or_unresolvable(host)

    def test_strict_refuses_every_form(self) -> None:
        for host in self.FORMS:
            with self.subTest(host=host), self.assertRaises(SSRFRefused):
                host_public_strict(host)

    def test_proxy_opener_refuses_before_dial(self) -> None:
        # End-to-end regression for the real bypass: the preflight must
        # raise SSRFRefused, not wave the URL through to the proxy dial.
        opener = build_guarded_opener("http://127.0.0.1:3128")
        for url in ("http://2130706433/", "http://0x7f000001/", "http://127.1/x"):
            with self.subTest(url=url), self.assertRaises(SSRFRefused):
                opener.open(url)

    def test_browser_request_refuses_every_form(self) -> None:
        # Browsers decode these to loopback/metadata before requesting, so
        # the per-request policy must refuse the spelling it is handed.
        for host in self.FORMS:
            with self.subTest(host=host), self.assertRaises(SSRFRefused):
                browser_request_allowed(f"http://{host}/latest/meta-data/")

    def test_canonical_public_literal_still_allowed(self) -> None:
        self.assertEqual(host_public_or_unresolvable("93.184.216.34"), "93.184.216.34")
        self.assertEqual(host_public_strict("93.184.216.34"), "93.184.216.34")

    def test_mixed_labels_take_the_normal_dns_path(self) -> None:
        # "123.public.example" is NOT an inet_aton form (non-integer label),
        # so it must keep resolving via DNS like any other name.
        with patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo):
            self.assertEqual(host_public_strict("123.public.example"), "123.public.example")


class EmbeddedIPv6FormTests(unittest.TestCase):
    """IPv6 spaces that embed an IPv4 address are judged as that IPv4."""

    NONPUBLIC = (
        "64:ff9b::7f00:1",  # NAT64-embedded 127.0.0.1
        "64:ff9b::a9fe:a9fe",  # NAT64-embedded 169.254.169.254 (metadata)
        "64:ff9b:1::1",  # NAT64 local-use prefix
        "2002:7f00:1::",  # 6to4-embedded 127.0.0.1
        "2002:a9fe:a9fe::",  # 6to4-embedded 169.254.169.254
        "::a01:1",  # deprecated IPv4-compatible 10.1.0.1
    )

    def test_embedded_forms_are_nonpublic(self) -> None:
        for raw in self.NONPUBLIC:
            with self.subTest(addr=raw):
                self.assertTrue(ip_is_nonpublic(ipaddress.ip_address(raw)))

    def test_public_addresses_still_pass(self) -> None:
        for raw in ("2606:4700::1111", "2001:4860:4860::8888", "8.8.8.8"):
            with self.subTest(addr=raw):
                self.assertFalse(ip_is_nonpublic(ipaddress.ip_address(raw)))

    def test_resolved_nat64_address_is_refused(self) -> None:
        with patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_getaddrinfo):
            with self.assertRaises(SSRFRefused):
                pick_public_ip("nat64.example")

    def test_strict_refuses_nat64_literal(self) -> None:
        with self.assertRaises(SSRFRefused):
            host_public_strict("64:ff9b::7f00:1")


class ResolverTaxonomyTests(unittest.TestCase):
    """Whatever the platform resolver raises, callers see SSRFRefused."""

    def test_unicode_resolver_error_is_wrapped(self) -> None:
        # POSIX getaddrinfo rejects embedded NULs / undecodable labels with
        # UnicodeError or ValueError; SafeRedirect and the Playwright guard
        # only catch SSRFRefused, so a bare leak would break the taxonomy.
        for exc in (UnicodeError("embedded null byte"), ValueError("bad label")):
            with self.subTest(exc=exc):
                with patch("freegem.ssrf.socket.getaddrinfo", side_effect=exc):
                    with self.assertRaises(SSRFRefused):
                        pick_public_ip("bogus.example")

    def test_proxy_leniency_survives_unicode_resolver_error(self) -> None:
        # The lenient proxy path keeps allowing truly unresolvable names,
        # whatever error type the resolver used to say "no answer".
        for exc in (OSError("no dns"), UnicodeError("embedded null byte")):
            with self.subTest(exc=exc):
                with patch("freegem.ssrf.socket.getaddrinfo", side_effect=exc):
                    self.assertEqual(
                        host_public_or_unresolvable("split.example"), "split.example"
                    )


if __name__ == "__main__":
    unittest.main()

