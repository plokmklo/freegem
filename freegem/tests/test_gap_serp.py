"""Edge-case tests for the SERP providers (freegem.serp).

The happy paths live in test_serp.py; these cover the DDG redirect-URL
decode quirks, Brave's count cap and key handling, parser degenerate
inputs, and the guarded-opener wiring behind ``cfg``.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from freegem.config import BridgeConfig
from freegem.serp import (
    BraveSearchProvider,
    DuckDuckGoHTMLProvider,
    ProviderNotConfigured,
    SearchHit,
    _ddg_real_url,
    _opener,
    _parse_brave,
    _parse_ddg,
    get_provider,
)


class DdgRealUrlTests(unittest.TestCase):
    def test_relative_l_wrapper(self) -> None:
        # parse_qs inside _ddg_real_url already percent-decodes the target.
        self.assertEqual(
            _ddg_real_url("/l/?uddg=https%3A%2F%2Fex.com%2Fpage&rut=abc"),
            "https://ex.com/page",
        )

    def test_scheme_relative_l_wrapper(self) -> None:
        self.assertEqual(
            _ddg_real_url("//duckduckgo.com/l/?uddg=https%3A%2F%2Fex.com"),
            "https://ex.com",
        )

    def test_absolute_url_with_uddg(self) -> None:
        self.assertEqual(
            _ddg_real_url("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fex.com"),
            "https://ex.com",
        )

    def test_plain_http_passthrough(self) -> None:
        self.assertEqual(
            _ddg_real_url("https://direct.example.com/page"),
            "https://direct.example.com/page",
        )

    def test_non_http_scheme_dropped(self) -> None:
        self.assertEqual(_ddg_real_url("javascript:void(0)"), "")
        self.assertEqual(_ddg_real_url("/search?q=cats"), "")


class DdgParserEdgeTests(unittest.TestCase):
    def test_invalid_href_skipped_remaining_kept(self) -> None:
        html = (
            '<a class="result__a" href="javascript:void(0)">Bad</a>'
            '<a class="result__a" href="https://ok.example.com/">Good</a>'
        )
        hits = _parse_ddg(html, 10)
        self.assertEqual([h.url for h in hits], ["https://ok.example.com/"])
        # position reflects the SERP slot, so the survivor keeps rank 2
        self.assertEqual(hits[0].position, 2)

    def test_missing_snippet_is_empty_string(self) -> None:
        html = (
            '<a class="result__a" href="https://a.example.com/">Title A</a>'
        )
        hits = _parse_ddg(html, 10)
        self.assertEqual(hits[0].snippet, "")

    def test_zero_max_results_falls_back_to_ten(self) -> None:
        # ``max_results or 10`` treats 0 as "unset", a deliberate quirk.
        html = (
            '<a class="result__a" href="https://a.example.com/">A</a>'
            '<a class="result__a" href="https://b.example.com/">B</a>'
        )
        self.assertEqual(len(_parse_ddg(html, 0)), 2)

    def test_unencoded_uddg_is_decoded(self) -> None:
        html = (
            '<a class="result__a" href="/l/?uddg=https://ex.com/a b">T</a>'
        )
        hits = _parse_ddg(html, 10)
        self.assertEqual(hits[0].url, "https://ex.com/a b")


class BraveEdgeTests(unittest.TestCase):
    def test_whitespace_only_key_refuses(self) -> None:
        # __init__ strips, so "   " is as unconfigured as "".
        with self.assertRaises(ProviderNotConfigured):
            BraveSearchProvider(api_key="   ").search("q")

    def test_count_capped_at_twenty(self) -> None:
        provider = BraveSearchProvider(api_key="tok")
        response = mock.MagicMock()
        response.read.return_value = b'{"web": {"results": []}}'
        response.__enter__.return_value = response
        with mock.patch("freegem.serp.urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            provider.search("q", max_results=50)
        self.assertIn("count=20", build.return_value.open.call_args[0][0].full_url)

    def test_zero_max_results_counts_as_ten(self) -> None:
        provider = BraveSearchProvider(api_key="tok")
        response = mock.MagicMock()
        response.read.return_value = b'{"web": {"results": []}}'
        response.__enter__.return_value = response
        with mock.patch("freegem.serp.urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            provider.search("q", max_results=0)
        self.assertIn("count=10", build.return_value.open.call_args[0][0].full_url)

    def test_parser_tolerates_degenerate_payloads(self) -> None:
        self.assertEqual(_parse_brave({}, 10), [])
        self.assertEqual(_parse_brave({"web": {}}, 10), [])
        self.assertEqual(_parse_brave({"web": {"results": None}}, 10), [])

    def test_searchhit_is_plain_record(self) -> None:
        hit = SearchHit(title="t", url="u", snippet="s", position=1, source="brave")
        self.assertEqual((hit.title, hit.url, hit.snippet), ("t", "u", "s"))


class OpenerAndRegistryTests(unittest.TestCase):
    def test_opener_without_config_is_plain(self) -> None:
        with mock.patch("freegem.serp.urllib.request.build_opener") as build:
            build.return_value = object()
            opener = _opener(None)
        build.assert_called_once_with()
        self.assertIs(opener, build.return_value)

    def test_opener_with_config_is_guarded(self) -> None:
        cfg = BridgeConfig(guard=True)
        sentinel = object()
        with mock.patch("freegem.serp.build_guarded_opener") as guarded:
            guarded.return_value = sentinel
            self.assertIs(_opener(cfg), sentinel)
        guarded.assert_called_once_with(cfg.proxy, None)

    def test_brave_without_cfg_has_empty_key(self) -> None:
        provider = get_provider("brave")
        self.assertEqual(provider.api_key, "")  # type: ignore[attr-defined]
        with self.assertRaises(ProviderNotConfigured):
            provider.search("q")

    def test_ddg_with_config_uses_guarded_opener(self) -> None:
        provider = DuckDuckGoHTMLProvider(cfg=BridgeConfig(guard=True))
        response = mock.MagicMock()
        response.read.return_value = b"<html></html>"
        response.__enter__.return_value = response
        with mock.patch("freegem.serp.build_guarded_opener") as guarded:
            guarded.return_value.open.return_value = response
            self.assertEqual(provider.search("anything"), [])
        guarded.assert_called_once()


if __name__ == "__main__":
    unittest.main()
