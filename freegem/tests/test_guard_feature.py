"""Tests for the guard feature: validation is opt-in, not the default.

User directive 2026-10-17: a plain ``scrape`` should just fetch and hand text
to the caller (the AI). The SSRF guard is a FEATURE (``--guard`` / config
``guard: true``), forced ON for the network-facing server/MCP surfaces.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from freegem.config import BridgeConfig
from freegem.web_scraper import ScrapeError, scrape_page


class GuardDefaultTests(unittest.TestCase):
    def test_default_is_off(self) -> None:
        self.assertFalse(BridgeConfig().guard)

    def test_replace_flips_it(self) -> None:
        self.assertTrue(BridgeConfig().replace(guard=True).guard)

    def test_bool_field_is_coerced_from_config_string(self) -> None:
        # "guard": "true" in a hand-written JSON config must become a bool.
        from freegem.config import _BOOL_FIELDS

        self.assertIn("guard", _BOOL_FIELDS)


class UnguardedScrapeTests(unittest.TestCase):
    """Default path: no SSRF pre-validation -- fetch, fail honestly."""

    def test_public_host_is_not_ssrf_refused_by_default(self) -> None:
        # Guard off: even if dialing eventually fails (no network in tests),
        # the failure must be connection, never an SSRF pre-refusal.
        cfg = BridgeConfig(log_requests=False)
        with mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=_public_resolver):
            with self.assertRaises(ScrapeError) as ctx:
                scrape_page(cfg, "http://public.example/x")
        self.assertNotIn("SSRF guard", str(ctx.exception))

    def test_local_file_still_works_when_guard_off(self) -> None:
        # Local files bypass the network path entirely (#50) in both modes.
        with tempfile.TemporaryDirectory() as td:
            page = os.path.join(td, "g.html")
            with open(page, "w", encoding="utf-8") as fh:
                fh.write("<html><body><p>GUARD-FEATURE-MARKER</p></body></html>")
            out = scrape_page(BridgeConfig(log_requests=False), page, allow_local=True)
            self.assertIn("GUARD-FEATURE-MARKER", out.text)


def _public_resolver(host: str, *args: object, **kwargs: object) -> list[tuple]:
    """Return a verified-public IP for any host (mocks DNS; dial will fail)."""
    return [(2, 1, 6, "", ("93.184.216.34", 0))]


class GuardedScrapeTests(unittest.TestCase):
    """--guard / guard=True restores the strict pre-validation."""

    def test_loopback_refused_when_guard_on(self) -> None:
        cfg = BridgeConfig(log_requests=False).replace(guard=True)
        with self.assertRaises(ScrapeError) as ctx:
            scrape_page(cfg, "http://127.0.0.1:9/x")
        self.assertIn("SSRF guard", str(ctx.exception))

    def test_metadata_ip_refused_when_guard_on(self) -> None:
        cfg = BridgeConfig(log_requests=False).replace(guard=True)
        with self.assertRaises(ScrapeError) as ctx:
            scrape_page(cfg, "http://169.254.169.254/latest/meta-data/")
        self.assertIn("SSRF guard", str(ctx.exception))

    def test_scheme_is_refused_when_guard_on(self) -> None:
        # ftp: url_allowed dies before any network attempt.
        cfg = BridgeConfig(log_requests=False).replace(guard=True)
        with self.assertRaises(ScrapeError) as ctx:
            scrape_page(cfg, "ftp://host.example/x")
        self.assertIn("SSRF guard", str(ctx.exception))


class LocalFileIndependentOfGuardTests(unittest.TestCase):
    """Local files are not a network fetch: they work in BOTH modes (#50)."""

    def test_local_path_with_guard_off(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            page = os.path.join(td, "guard-probe-off.html")
            with open(page, "w", encoding="utf-8") as fh:
                fh.write("<html><body><p>GUARD-FEATURE-MARKER-OFF</p></body></html>")
            out = scrape_page(BridgeConfig(log_requests=False), page, allow_local=True)
            self.assertIn("GUARD-FEATURE-MARKER-OFF", out.text)

    def test_local_path_with_guard_on(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            page = os.path.join(td, "guard-probe-on.html")
            with open(page, "w", encoding="utf-8") as fh:
                fh.write("<html><body><p>GUARD-FEATURE-MARKER-ON</p></body></html>")
            cfg = BridgeConfig(log_requests=False).replace(guard=True)
            out = scrape_page(cfg, page, allow_local=True)
            self.assertIn("GUARD-FEATURE-MARKER-ON", out.text)


class CliWiringTests(unittest.TestCase):
    def test_scrape_parser_has_guard_flag(self) -> None:
        from freegem.cli import _build_parser

        args = _build_parser().parse_args(["scrape", "http://example.com", "--guard"])
        self.assertTrue(args.guard)

    def test_scrape_parser_guard_defaults_off(self) -> None:
        from freegem.cli import _build_parser

        args = _build_parser().parse_args(["scrape", "http://example.com"])
        self.assertFalse(args.guard)


if __name__ == "__main__":
    unittest.main()
