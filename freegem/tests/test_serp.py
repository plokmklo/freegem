"""Tests for the SERP provider integrations (freegem.serp)."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from freegem.serp import (
    BraveSearchProvider,
    DuckDuckGoHTMLProvider,
    ProviderNotConfigured,
    _parse_brave,
    _parse_ddg,
    get_provider,
    provider_names,
)

DDG_HTML = """
<html><body>
<div class="result results_links">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.example.com%2Fguide&amp;rut=abc">First <b>Result</b></a>
  </h2>
  <a class="result__snippet" href="#">The first snippet with <b>bold</b> text inside.</a>
</div>
<div class="result">
  <a rel="nofollow" class="result__a" href="https://direct.example.com/page">Direct Link Result</a>
  <a class="result__snippet" href="#">Snippet for the direct link.</a>
</div>
</body></html>
"""

BRAVE_JSON = {
    "web": {
        "results": [
            {"title": "Brave One", "url": "https://one.example.com/", "description": "First brave hit."},
            {"title": "Brave Two", "url": "https://two.example.com/", "description": "Second hit\nwith newline."},
            {"title": "No URL", "url": "", "description": "dropped"},
        ]
    }
}


class DuckDuckGoTests(unittest.TestCase):
    def test_parse_realistic_markup(self) -> None:
        parsed = _parse_ddg(DDG_HTML, 10)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0].url, "https://docs.example.com/guide")
        self.assertEqual(parsed[0].title, "First Result")
        self.assertEqual(parsed[0].snippet, "The first snippet with bold text inside.")
        self.assertEqual(parsed[0].position, 1)
        self.assertEqual(parsed[0].source, "duckduckgo")
        self.assertEqual(parsed[1].url, "https://direct.example.com/page")

    def test_max_results_and_empty(self) -> None:
        self.assertEqual(len(_parse_ddg(DDG_HTML, 1)), 1)
        self.assertEqual(_parse_ddg("<html></html>", 10), [])

    def test_network_call_mocked(self) -> None:
        provider = DuckDuckGoHTMLProvider()
        response = mock.MagicMock()
        response.read.return_value = DDG_HTML.encode()
        response.__enter__.return_value = response
        with mock.patch("freegem.serp.urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            hits = provider.search("structured extraction", max_results=5)
        self.assertEqual(hits[0].url, "https://docs.example.com/guide")
        request = build.return_value.open.call_args[0][0]
        self.assertIn(b"q=structured+extraction", request.data)  # POST body
        self.assertEqual(request.full_url, "https://html.duckduckgo.com/html/")


class BraveTests(unittest.TestCase):
    def test_refuses_without_key(self) -> None:
        with self.assertRaises(ProviderNotConfigured):
            BraveSearchProvider(api_key="").search("anything")

    def test_parse_and_header(self) -> None:
        provider = BraveSearchProvider(api_key="tok-123")
        response = mock.MagicMock()
        response.read.return_value = json.dumps(BRAVE_JSON).encode()
        response.__enter__.return_value = response
        with mock.patch("freegem.serp.urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            hits = provider.search("brave test", max_results=10)
        self.assertEqual(len(hits), 2)  # the url-less entry is dropped
        self.assertEqual(hits[0].url, "https://one.example.com/")
        self.assertEqual(hits[1].snippet, "Second hit with newline.")
        request = build.return_value.open.call_args[0][0]
        self.assertEqual(request.headers.get("X-subscription-token"), "tok-123")
        self.assertIn("count=10", request.full_url)

    def test_brave_parser_drops_malformed_entries(self) -> None:
        hits = _parse_brave({"web": {"results": [{"title": "x"}]}}, 10)
        self.assertEqual(hits, [])


class BoundaryTests(unittest.TestCase):
    def test_registry_explicit_optin(self) -> None:
        self.assertEqual(provider_names(), ["duckduckgo", "brave"])
        self.assertIsInstance(get_provider("duckduckgo"), DuckDuckGoHTMLProvider)
        with self.assertRaises(ValueError):
            get_provider("google")  # deliberately absent: ToS, keying, boundary

    def test_brave_key_flows_from_config(self) -> None:
        from freegem.config import BridgeConfig

        cfg = BridgeConfig.from_dict({"brave_api_key": "cfg-tok"})
        provider = get_provider("brave", cfg)
        self.assertEqual(provider.api_key, "cfg-tok")  # type: ignore[attr-defined]

    def test_empty_query_is_local_noop(self) -> None:
        with mock.patch("freegem.serp.urllib.request.build_opener") as build:
            self.assertEqual(BraveSearchProvider(api_key="k").search("   "), [])
            self.assertEqual(DuckDuckGoHTMLProvider().search("  "), [])
        build.assert_not_called()  # boundary: no query, no network


if __name__ == "__main__":
    unittest.main()
