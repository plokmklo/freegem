"""MediaWiki fallback-extracts honesty (#67): request shape and budgets.

The `prop=extracts` fallback deliberately sends NO explicit `exchars`.
The TextExtracts extension truncates an extract only when `exchars` or
`exsentences` is requested (ApiQueryExtracts::truncate() is a no-op
without them), and `exchars` is hard-capped at 1200 by the extension --
an explicit budget would CUT a long article's extract from the complete
plain text to ~1200 characters instead of bounding a bigger one. This
module pins that contract hermetically:

* the fallback request carries the bounded parameters it does send and no
  `exchars`;
* a long fallback extract arrives whole -- upstream never truncates it;
* freegem's caller `max_chars` still cuts AFTER the full extraction, and
  `truncated=True` on a `via="mediawiki-api"` result can only be freegem's
  own cut, never a silent upstream cap;
* the full-parse path answers first when it can, and the fallback catches
  what parse cannot.
"""

from __future__ import annotations

import json
import unittest
import urllib.parse
from typing import Any
from unittest import mock

from freegem.config import BridgeConfig
from freegem.web_scraper import ScrapeResult, format_page, scrape_page

_ARTICLE = "https://en.wikipedia.org/wiki/Alexander_the_Great"


class _FakeHeaders(dict):
    """dict is already a headers-like mapping for these tests."""


class _FakeResponse:
    """A read()-able response speaking the Action API's JSON dialect."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = _FakeHeaders(headers or {})

    def read(self, size: int = -1) -> bytes:
        out, self._body = self._body, b""
        return out


def _parse_body(html: str, title: str = "Alexander the Great") -> bytes:
    return json.dumps({"parse": {"title": title, "text": html}}).encode()


def _extract_body(extract: str, title: str = "Alexander the Great") -> bytes:
    return json.dumps({"query": {"pages": [{"title": title, "extract": extract}]}}).encode()


def _long_extract() -> str:
    """Well past any exchars cap the extension could impose (~44k chars)."""
    return "Alexander III of Macedon conquered the known world. " * 850


class MediaWikiFallbackBudgetTests(unittest.TestCase):
    """The extracts fallback: full upstream text, freegem cuts afterwards."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(guard=True, log_requests=False)

    def _scrape(self, responses: list[Any], **kwargs: Any) -> tuple[ScrapeResult, Any]:
        with (
            mock.patch("freegem.web_scraper._check_robots"),
            mock.patch("freegem.web_scraper._fetch", side_effect=responses) as fetch,
        ):
            result = scrape_page(self.cfg, _ARTICLE, **kwargs)
        return result, fetch

    def _fallback_responses(self) -> list[Any]:
        return [_FakeResponse(b""), _FakeResponse(_extract_body(_long_extract()))]

    def test_fallback_request_carries_no_exchars(self) -> None:
        """The budget-free request is the bounded one (#67): parse answers
        nothing usable, the fallback runs, and it must not ask for exchars."""
        result, fetch = self._scrape(
            [_FakeResponse(b"not json at all"), _FakeResponse(_extract_body("text " * 50))]
        )
        self.assertEqual(result.via, "mediawiki-api")
        self.assertEqual(len(fetch.call_args_list), 2)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(fetch.call_args_list[1].args[1]).query)
        self.assertNotIn("exchars", params)
        self.assertNotIn("exsentences", params)
        self.assertEqual(params["prop"], ["extracts"])
        self.assertEqual(params["exlimit"], ["1"])
        self.assertEqual(params["explaintext"], ["1"])
        self.assertEqual(params["titles"], ["Alexander_the_Great"])
        self.assertEqual(params["formatversion"], ["2"])

    def test_long_fallback_extract_arrives_whole(self) -> None:
        """No upstream cap applies: the complete extract survives, tail and
        all -- an exchars=1200 request would have cut it to a stub."""
        result, _ = self._scrape(self._fallback_responses())
        self.assertEqual(result.via, "mediawiki-api")
        self.assertFalse(result.truncated, "upstream returned the complete extract")
        self.assertGreater(len(result.text), 40_000)
        self.assertTrue(result.text.startswith("Alexander III of Macedon"))
        self.assertTrue(result.text.endswith("world."))

    def test_caller_max_chars_cuts_after_the_full_extraction(self) -> None:
        result, _ = self._scrape(self._fallback_responses(), max_chars=500)
        self.assertEqual(result.via, "mediawiki-api")
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.text), 500)
        self.assertIn("[content truncated at the max_chars limit]", format_page(result))

    def test_truncation_is_distinguishable_by_construction(self) -> None:
        """(#67) Upstream cannot truncate a budget-free extract, so a
        truncated flag on a mediawiki-api result is ALWAYS freegem's own
        caller-budget cut -- the two sources never mix on this path."""
        whole, _ = self._scrape(self._fallback_responses())
        self.assertFalse(whole.truncated)
        cut, _ = self._scrape(self._fallback_responses(), max_chars=500)
        self.assertTrue(cut.truncated)

    def test_full_parse_success_skips_the_fallback(self) -> None:
        long_html = (
            '<div class="mw-parser-output"><p>' + ("Real content sentence. " * 500) + "</p></div>"
        )
        result, fetch = self._scrape([_FakeResponse(_parse_body(long_html))])
        self.assertEqual(result.via, "mediawiki-api-parse")
        self.assertEqual(len(fetch.call_args_list), 1)
        self.assertIn("action=parse", fetch.call_args_list[0].args[1])
        self.assertGreater(len(result.text), 10_000)

    def test_full_parse_stub_falls_back_to_extracts(self) -> None:
        """Parse answered but with a stub: the fallback still runs (#67's
        full-parse-failure regression)."""
        result, fetch = self._scrape(
            [
                _FakeResponse(_parse_body("<p>too short</p>")),
                _FakeResponse(_extract_body("real extract text " * 20)),
            ]
        )
        self.assertEqual(result.via, "mediawiki-api")
        self.assertEqual(len(fetch.call_args_list), 2)
        self.assertIn("real extract text", result.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
