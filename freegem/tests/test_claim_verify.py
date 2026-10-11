"""Tests for evidence-backed claim verification.

The fetch and model seams are stubbed exactly like test_research.py:
``freegem.web_scraper.scrape_page`` for pages and
``freegem.claim_verify._gemini_complete`` for the model reply. What these
tests pin down is the layer that is new here: the closed status vocabulary
and its machine-enforced repairs, verbatim quote verification against the
text actually fetched, deterministic Location mapping inside sectioned
sources, robots-correct URL discovery through supported search APIs, the
source_unavailable / gemini-down fallbacks, and the documented output
shapes (Claim/Status/Evidence/Source/Location/Retrieved).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import typing
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from freegem.claim_verify import (
    HARD_MAX_CLAIMS,
    STATUS_CONTRADICTED,
    STATUS_NOT_FOUND,
    STATUS_PARTIAL,
    STATUS_SUPPORTED,
    STATUS_UNAVAILABLE,
    VerifyError,
    discover_urls,
    render_json,
    render_markdown,
    render_text,
    verify,
)
from freegem.config import BridgeConfig
from freegem.gemini import BridgeError
from freegem.mcp_tools import TOOL_SPECS, run_tool
from freegem.web_research import ResearchSource
from freegem.web_scraper import ScrapeError, ScrapeResult

FIXTURES = Path(__file__).parent / "fixtures" / "verify"

ALEXANDER_TEXT = (FIXTURES / "alexander_source.txt").read_text(encoding="utf-8")

CLAIMS = [
    "Alexander became king of Macedon in 336 BC.",
    "Alexander died in Babylon in 323 BC.",
    "Alexander died in 330 BC.",
    "Alexander had a sister named Cleopatra.",
    "Alexander's empire stretched from Greece to India and he ruled it until his death.",
]

GOOD_ANSWER = (FIXTURES / "gemini_claims_ok.txt").read_text(encoding="utf-8")

WIKI_URL = "https://en.wikipedia.org/wiki/Alexander_the_Great"


def _page(url: str, title: str, text: str, via: str = "mediawiki-api") -> ScrapeResult:
    return ScrapeResult(url=url, title=title, text=text, links=[], via=via)


def _fetcher(pages: dict[str, ScrapeResult], failures: set[str] | None = None) -> Any:
    def fake(cfg: BridgeConfig, url: str, **kwargs: Any) -> ScrapeResult:
        if failures and url in failures:
            raise ScrapeError(f"refused {url!r}")
        if url not in pages:
            raise ScrapeError(f"no fake page for {url!r}")
        return pages[url]

    return fake


def _gemini(answer: str, *, fail: bool = False) -> Any:
    def fake(cfg: BridgeConfig, prompt: str) -> str:
        if fail:
            raise BridgeError("gemini down")
        # The prompt must keep every guard the research layer enforces.
        assert "UNTRUSTED WEB CONTENT" in prompt
        assert "[S1]" in prompt
        return answer

    return fake


def _run_verify(
    *,
    pages: dict[str, ScrapeResult] | None = None,
    failures: set[str] | None = None,
    answer: str = GOOD_ANSWER,
    gemini_fail: bool = False,
    claims: list[str] | None = None,
    urls: list[str] | None = None,
    **kwargs: Any,
) -> Any:
    pages = (
        pages
        if pages is not None
        else {WIKI_URL: _page(WIKI_URL, "Alexander the Great", ALEXANDER_TEXT)}
    )
    with (
        mock.patch("freegem.web_scraper.scrape_page", _fetcher(pages, failures)),
        mock.patch("freegem.claim_verify._gemini_complete", _gemini(answer, fail=gemini_fail)),
    ):
        return verify(
            BridgeConfig(),
            claims if claims is not None else CLAIMS,
            urls if urls is not None else [WIKI_URL],
            **kwargs,
        )


class _TmpDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="freegem-verify-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class StatusPipelineTests(_TmpDirTestCase):
    """The happy path: statuses, verified quotes, locations, retrieval dates."""

    result: typing.ClassVar[Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = _run_verify()

    def test_statuses_match_the_fixture_contract(self) -> None:
        expected = [
            STATUS_SUPPORTED,
            STATUS_SUPPORTED,
            STATUS_CONTRADICTED,
            STATUS_NOT_FOUND,
            STATUS_PARTIAL,
        ]
        self.assertEqual([c.status for c in self.result.claims], expected)

    def test_every_offered_quote_verifies(self) -> None:
        for claim in self.result.claims:
            for evidence in claim.evidence:
                self.assertTrue(evidence.verified, evidence.quote)

    def test_locations_come_from_the_source_sections(self) -> None:
        first, second, third, _fourth, fifth = self.result.claims
        self.assertEqual(first.evidence[0].location, "Introduction")
        self.assertEqual(second.evidence[0].location, "Death and legacy")
        self.assertEqual(third.evidence[0].location, "Death and legacy")
        self.assertEqual(fifth.evidence[0].location, "Introduction")

    def test_contradiction_carries_the_conflicting_passage(self) -> None:
        third = self.result.claims[2]
        self.assertIn("323 BC", third.evidence[0].quote)

    def test_not_found_has_no_evidence(self) -> None:
        self.assertEqual(self.result.claims[3].evidence, [])

    def test_retrieved_dates_present(self) -> None:
        for claim in self.result.claims:
            for evidence in claim.evidence:
                self.assertRegex(evidence.retrieved_at or "", r"^\d{4}-\d{2}-\d{2}$")

    def test_gemini_was_used(self) -> None:
        self.assertTrue(self.result.gemini_used)

    def test_metrics_recorded_stages(self) -> None:
        snapshot = self.result.metrics.snapshot()
        for stage in ("fetch", "generate", "parse"):
            self.assertIn(stage, snapshot["stages"])
        self.assertEqual(snapshot["counters"]["evidence_offered"], 4)
        self.assertEqual(snapshot["counters"]["evidence_verified"], 4)
        self.assertEqual(snapshot["counters"]["schema_repairs"], 0)


class SchemaRepairTests(_TmpDirTestCase):
    """The model layer lies or garbles; the deterministic layer repairs."""

    def test_repairs_fixture(self) -> None:
        result = _run_verify(
            claims=CLAIMS[:4],
            answer=(FIXTURES / "gemini_claims_repairs.txt").read_text(encoding="utf-8"),
        )
        by_number = {c.number: c for c in result.claims}
        # unknown status -> not_found
        self.assertEqual(by_number[1].status, STATUS_NOT_FOUND)
        self.assertIn("unknown status", by_number[1].note or "")
        # supported with no evidence -> not_found
        self.assertEqual(by_number[2].status, STATUS_NOT_FOUND)
        self.assertIn("requires verbatim evidence", by_number[2].note or "")
        # duplicate: first entry wins, later contradicted ignored
        self.assertEqual(by_number[1].status, STATUS_NOT_FOUND)
        # out-of-range claim number and missing claim_number ignored
        self.assertEqual(result.metrics.snapshot()["counters"]["schema_repairs"], 5)

    def test_missing_verdict_becomes_not_found(self) -> None:
        answer = '```claims\n[{"claim_number": 1, "status": "supported", "evidence": [{"source": 1, "quotes": ["He succeeded his father, Philip II, to the throne in 336 BC, at the age of 20"]}]}]\n```'
        result = _run_verify(claims=CLAIMS[:2], answer=answer)
        self.assertEqual(result.claims[0].status, STATUS_SUPPORTED)
        self.assertEqual(result.claims[1].status, STATUS_NOT_FOUND)
        self.assertIn("no verdict", result.claims[1].note or "")

    def test_no_claims_block_at_all(self) -> None:
        result = _run_verify(claims=CLAIMS[:1], answer="I refuse to answer in that format.")
        self.assertEqual(result.claims[0].status, STATUS_NOT_FOUND)
        self.assertIn("no ```claims block", result.claims[0].note or "")

    def test_evidence_from_unloaded_source_is_dropped(self) -> None:
        answer = (
            '```claims\n[{"claim_number": 1, "status": "supported", '
            '"evidence": [{"source": 2, "quotes": ["whatever"]}]}]\n```'
        )
        result = _run_verify(claims=CLAIMS[:1], answer=answer)
        self.assertEqual(result.claims[0].status, STATUS_NOT_FOUND)
        self.assertIn("unloaded [S2]", result.claims[0].note or "")
        self.assertEqual(result.claims[0].evidence, [])

    def test_hallucinated_quote_is_flagged_not_deleted(self) -> None:
        answer = (
            '```claims\n[{"claim_number": 1, "status": "supported", '
            '"evidence": [{"source": 1, "quotes": ["Alexander was born in Athens in 400 BC"]}]}]\n```'
        )
        result = _run_verify(claims=CLAIMS[:1], answer=answer)
        claim = result.claims[0]
        self.assertEqual(len(claim.evidence), 1)
        self.assertFalse(claim.evidence[0].verified)
        self.assertIsNone(claim.evidence[0].location)  # no honest location
        self.assertFalse(claim.all_verified)

    def test_rewrapped_quote_still_verifies(self) -> None:
        # Whitespace-insensitive matching: the model rewrapped a line break.
        answer = (
            '```claims\n[{"claim_number": 1, "status": "supported", "evidence": '
            '[{"source": 1, "quotes": ["He succeeded his father, Philip II, to the\\nthrone in 336 BC, at the age of 20"]}]}]\n```'
        )
        result = _run_verify(claims=CLAIMS[:1], answer=answer)
        self.assertTrue(result.claims[0].evidence[0].verified)


class FallbackTests(_TmpDirTestCase):
    """Dead sources and a dead model never fabricate verdicts."""

    def test_all_sources_dead_is_source_unavailable_without_model_call(self) -> None:
        with (
            mock.patch("freegem.web_scraper.scrape_page", _fetcher({}, failures={WIKI_URL})),
            mock.patch("freegem.claim_verify._gemini_complete") as gemini,
        ):
            result = verify(BridgeConfig(), CLAIMS[:1], [WIKI_URL])
        gemini.assert_not_called()
        self.assertEqual(result.claims[0].status, STATUS_UNAVAILABLE)
        self.assertIn("no loadable source", result.claims[0].note or "")
        self.assertFalse(result.gemini_used)

    def test_gemini_down_leaves_verdicts_unjudged(self) -> None:
        result = _run_verify(gemini_fail=True)
        self.assertFalse(result.gemini_used)
        for claim in result.claims:
            self.assertEqual(claim.status, STATUS_NOT_FOUND)
            self.assertIn("gemini unavailable", claim.note or "")

    def test_gemini_down_raw_material_is_untrusted_marked(self) -> None:
        result = _run_verify(gemini_fail=True)
        self.assertIsNotNone(result.raw_material)
        assert result.raw_material
        self.assertIn("UNTRUSTED WEB CONTENT", result.raw_material)
        self.assertIn(ALEXANDER_TEXT[:40].strip(), result.raw_material)


class CallerMistakeTests(_TmpDirTestCase):
    def test_empty_claims_rejected(self) -> None:
        with self.assertRaises(VerifyError):
            _run_verify(claims=["", "  "])

    def test_too_many_claims_rejected(self) -> None:
        with self.assertRaises(VerifyError):
            _run_verify(claims=[f"claim {n}" for n in range(HARD_MAX_CLAIMS + 1)])

    def test_no_urls_and_no_discovery_rejected(self) -> None:
        with self.assertRaises(VerifyError):
            _run_verify(claims=CLAIMS[:1], urls=[])

    def test_duplicate_claims_deduped(self) -> None:
        result = _run_verify(claims=["A claim.", "A claim.", "Another."])
        self.assertEqual([c.claim for c in result.claims], ["A claim.", "Another."])


class DiscoveryTests(_TmpDirTestCase):
    """URL discovery: supported APIs only, robots-checked, never a crawl."""

    def _fake_response(self, payload: bytes) -> Any:
        from freegem.tests.test_claim_verify import _FakeResponse

        return _FakeResponse(payload)

    def test_portal_search_url_shape(self) -> None:
        from freegem.claim_verify import _portal_search_url

        url = _portal_search_url("wikipedia", "en", "Alexander the Great", 3)
        self.assertTrue(
            url.startswith("https://api.wikimedia.org/core/v1/wikipedia/en/search/page?")
        )
        self.assertIn("q=Alexander+the+Great", url)
        self.assertIn("limit=3", url)

    def test_wiki_project_lang(self) -> None:
        from freegem.claim_verify import _wiki_project_lang

        self.assertEqual(_wiki_project_lang("en.wikipedia.org"), ("wikipedia", "en"))
        self.assertEqual(_wiki_project_lang("en.m.wikipedia.org"), ("wikipedia", "en"))
        self.assertEqual(_wiki_project_lang("de.wiktionary.org"), ("wiktionary", "de"))
        self.assertIsNone(_wiki_project_lang("example.com"))

    def test_portal_discovery_builds_article_urls(self) -> None:
        payload = json.dumps(
            {
                "pages": [
                    {"key": "Alexander_the_Great", "title": "Alexander the Great"},
                    {"key": "Philip_II_of_Macedon", "title": "Philip II of Macedon"},
                ]
            }
        ).encode()
        with (
            mock.patch("freegem.web_scraper._check_robots", lambda cfg, url: None),
            mock.patch(
                "freegem.web_scraper._fetch",
                lambda cfg, url, timeout=30: self._fake_response(payload),
            ),
        ):
            found = discover_urls(BridgeConfig(), "Alexander the Great", limit=2)
        self.assertEqual(
            found,
            [
                {
                    "url": "https://en.wikipedia.org/wiki/Alexander_the_Great",
                    "title": "Alexander the Great",
                    "via": "wikimedia-search",
                },
                {
                    "url": "https://en.wikipedia.org/wiki/Philip_II_of_Macedon",
                    "title": "Philip II of Macedon",
                    "via": "wikimedia-search",
                },
            ],
        )

    def test_opensearch_discovery_for_non_wikimedia_hosts(self) -> None:
        payload = json.dumps(
            [
                "dune",
                ["Dune (novel)", "Dune (film)"],
                ["desc1", "desc2"],
                [
                    "https://wiki.example.org/wiki/Dune_(novel)",
                    "https://wiki.example.org/wiki/Dune_(film)",
                ],
            ]
        ).encode()
        cfg = BridgeConfig().replace(discover_wiki="wiki.example.org")
        with (
            mock.patch("freegem.web_scraper._check_robots", lambda cfg2, url: None),
            mock.patch(
                "freegem.web_scraper._fetch",
                lambda cfg2, url, timeout=30: self._fake_response(payload),
            ),
        ):
            found = discover_urls(cfg, "dune", limit=2)
        self.assertEqual(
            [item["url"] for item in found],
            [
                "https://wiki.example.org/wiki/Dune_(novel)",
                "https://wiki.example.org/wiki/Dune_(film)",
            ],
        )
        self.assertEqual({item["via"] for item in found}, {"mediawiki-opensearch"})

    def test_robots_refusal_blocks_discovery(self) -> None:
        def refuse(cfg: BridgeConfig, url: str) -> None:
            raise ScrapeError(f"robots.txt disallows {url!r}")

        with mock.patch("freegem.web_scraper._check_robots", refuse):
            with self.assertRaises(ScrapeError) as ctx:
                discover_urls(BridgeConfig(), "Alexander")
        self.assertIn("robots.txt", str(ctx.exception))
        self.assertIn("supply URLs explicitly", str(ctx.exception))

    def test_discovery_limit_is_capped(self) -> None:
        payload = json.dumps(
            {"pages": [{"key": f"Page_{n}", "title": f"Page {n}"} for n in range(1, 11)]}
        ).encode()
        with (
            mock.patch("freegem.web_scraper._check_robots", lambda cfg, url: None),
            mock.patch(
                "freegem.web_scraper._fetch",
                lambda cfg, url, timeout=30: self._fake_response(payload),
            ),
        ):
            found = discover_urls(BridgeConfig(), "Alexander", limit=99)
        self.assertEqual(len(found), 5)  # _DISCOVERY_LIMIT_CAP

    def test_verify_with_discovery_end_to_end(self) -> None:
        payload = json.dumps(
            {"pages": [{"key": "Alexander_the_Great", "title": "Alexander the Great"}]}
        ).encode()

        def fake_fetch(cfg: BridgeConfig, url: str, timeout: float = 30) -> Any:
            return self._fake_response(payload)

        with (
            mock.patch("freegem.web_scraper._check_robots", lambda cfg, url: None),
            mock.patch("freegem.web_scraper._fetch", fake_fetch),
            mock.patch(
                "freegem.web_scraper.scrape_page",
                _fetcher({WIKI_URL: _page(WIKI_URL, "Alexander the Great", ALEXANDER_TEXT)}),
            ),
            mock.patch("freegem.claim_verify._gemini_complete", _gemini(GOOD_ANSWER)),
        ):
            result = verify(
                BridgeConfig(),
                CLAIMS[:1],
                None,
                discover=True,
                discovery_query="Alexander the Great",
                wiki_host="en.wikipedia.org",
            )
        self.assertIsNotNone(result.discovery)
        assert result.discovery
        self.assertEqual(result.discovery["found"], 1)
        self.assertEqual(result.discovery["wiki"], "en.wikipedia.org")
        self.assertEqual(result.claims[0].status, STATUS_SUPPORTED)


class SectionAndLocationTests(_TmpDirTestCase):
    """Deterministic splitting, scoring, and quote location."""

    def test_mediawiki_lead_is_introduction(self) -> None:
        from freegem.claim_verify import _split_sections

        sections = _split_sections(ALEXANDER_TEXT, "mediawiki-api")
        self.assertEqual(sections[0].label, "Introduction")
        self.assertIn("commonly known as Alexander the Great", sections[0].text)

    def test_mediawiki_headings_become_sections(self) -> None:
        from freegem.claim_verify import _split_sections

        labels = [s.label for s in _split_sections(ALEXANDER_TEXT, "mediawiki-api")]
        self.assertIn("Early life", labels)
        self.assertIn("Death and legacy", labels)
        self.assertIn("Conquest of the Persian Empire", labels)

    def test_html_text_splits_into_paragraphs(self) -> None:
        from freegem.claim_verify import _split_sections

        sections = _split_sections("one\n\ntwo\n\nthree", "html")
        self.assertEqual([s.label for s in sections], ["para 1", "para 2", "para 3"])

    def test_html_blob_without_blank_lines_is_chunked(self) -> None:
        from freegem.claim_verify import _split_sections

        blob = "\n".join(f"line {n} " + "x" * 60 for n in range(30))
        sections = _split_sections(blob, "html")
        self.assertGreater(len(sections), 1)
        self.assertTrue(sections[0].label.startswith("para "))

    def test_scoring_prefers_keyword_rich_sections(self) -> None:
        from freegem.claim_verify import _relevant_section_labels

        labels = _relevant_section_labels(
            [(ResearchSource(url=WIKI_URL, via="mediawiki-api"), ALEXANDER_TEXT)],
            "Alexander died in Babylon",
        )
        self.assertTrue(labels)
        number, label = labels[0]
        self.assertEqual(number, 1)
        self.assertEqual(label, "Death and legacy")

    def test_zero_overlap_returns_no_hints(self) -> None:
        from freegem.claim_verify import _relevant_section_labels

        labels = _relevant_section_labels(
            [(ResearchSource(url=WIKI_URL, via="mediawiki-api"), ALEXANDER_TEXT)],
            "quantum chromodynamics gauge symmetry",
        )
        self.assertEqual(labels, [])

    def test_location_falls_back_to_none_for_unfindable_quotes(self) -> None:
        from freegem.claim_verify import _locate_quote

        source = ResearchSource(url=WIKI_URL, via="mediawiki-api")
        self.assertIsNone(_locate_quote("not in the text at all", source, ALEXANDER_TEXT))
        self.assertIsNone(_locate_quote("tiny", source, ALEXANDER_TEXT))  # too short


class RenderTests(_TmpDirTestCase):
    """The documented output shapes, exactly."""

    result: typing.ClassVar[Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = _run_verify()

    def test_text_shape_matches_the_spec(self) -> None:
        text = render_text(self.result)
        self.assertIn("Claim: Alexander became king of Macedon in 336 BC.", text)
        self.assertIn("Status: Supported", text)
        self.assertIn("Evidence:", text)
        self.assertIn(
            '"He succeeded his father, Philip II, to the throne in 336 BC, at the age of 20" (verified)',
            text,
        )
        self.assertIn("Source: [S1]", text)
        self.assertIn("Location: Introduction", text)
        self.assertRegex(text, r"Retrieved: \d{4}-\d{2}-\d{2}")
        self.assertIn("Status: Contradicted", text)
        self.assertIn("Status: Not found", text)
        self.assertIn("Sources:", text)
        self.assertIn("[req ", text)

    def test_markdown_shape(self) -> None:
        md = render_markdown(self.result)
        self.assertIn("### C1. Alexander became king of Macedon in 336 BC.", md)
        self.assertIn("**Status: Supported**", md)
        self.assertIn("> \u201c", md)
        self.assertIn("## Sources", md)

    def test_json_schema(self) -> None:
        payload = json.loads(render_json(self.result))
        self.assertEqual(payload["meta"]["claims_total"], 5)
        self.assertEqual(
            payload["meta"]["statuses"],
            {"supported": 2, "contradicted": 1, "not_found": 1, "partially_supported": 1},
        )
        self.assertEqual(payload["meta"]["evidence_offered"], 4)
        self.assertEqual(payload["meta"]["evidence_verified"], 4)
        first = payload["claims"][0]
        self.assertEqual(first["status"], "supported")
        self.assertEqual(first["status_label"], "Supported")
        evidence = first["evidence"][0]
        self.assertTrue(evidence["verified"])
        self.assertEqual(evidence["location"], "Introduction")
        self.assertEqual(evidence["source"], "S1")
        self.assertIn("request_id", payload["meta"]["metrics"])
        self.assertIn("stages", payload["meta"]["metrics"])
        self.assertIn("memory_rss_kb", payload["meta"]["metrics"])
        source_entry = payload["sources"][0]
        self.assertEqual(source_entry["marker"], "S1")
        self.assertEqual(source_entry["via"], "mediawiki-api")

    def test_json_includes_raw_material_when_gemini_down(self) -> None:
        result = _run_verify(gemini_fail=True)
        payload = json.loads(render_json(result))
        self.assertIn("raw_material", payload)
        self.assertIn("UNTRUSTED WEB CONTENT", payload["raw_material"])


class McpAndCliTests(_TmpDirTestCase):
    def test_tool_spec_registered(self) -> None:
        names = {spec["name"] for spec in TOOL_SPECS}
        self.assertIn("web_verify", names)

    def test_dispatch_reaches_verify(self) -> None:
        pages = {WIKI_URL: _page(WIKI_URL, "Alexander the Great", ALEXANDER_TEXT)}
        with (
            mock.patch.dict(
                "os.environ", {"FREEGEM_CACHE_DIR": str(self.tmp / "cache")}, clear=False
            ),
            mock.patch("freegem.web_scraper.scrape_page", _fetcher(pages)),
            mock.patch("freegem.claim_verify._gemini_complete", _gemini(GOOD_ANSWER)),
        ):
            output = run_tool(
                BridgeConfig(),
                "web_verify",
                {"claims": CLAIMS, "urls": [WIKI_URL]},
            )
        self.assertIn("Claim: Alexander became king of Macedon in 336 BC.", output)
        self.assertIn("Status: Supported", output)

    def test_dispatch_requires_urls_or_discover(self) -> None:
        with self.assertRaises(ValueError):
            run_tool(BridgeConfig(), "web_verify", {"claims": ["c"]})

    def test_dispatch_requires_claims(self) -> None:
        with self.assertRaises(ValueError):
            run_tool(BridgeConfig(), "web_verify", {"urls": [WIKI_URL]})

    def test_dispatch_discovery_flag(self) -> None:
        payload = json.dumps(
            {"pages": [{"key": "Alexander_the_Great", "title": "Alexander the Great"}]}
        ).encode()

        def fake_fetch(cfg: BridgeConfig, url: str, timeout: float = 30) -> Any:
            return _FakeResponse(payload)

        pages = {WIKI_URL: _page(WIKI_URL, "Alexander the Great", ALEXANDER_TEXT)}
        with (
            mock.patch.dict(
                "os.environ", {"FREEGEM_CACHE_DIR": str(self.tmp / "cache")}, clear=False
            ),
            mock.patch("freegem.web_scraper._check_robots", lambda cfg, url: None),
            mock.patch("freegem.web_scraper._fetch", fake_fetch),
            mock.patch("freegem.web_scraper.scrape_page", _fetcher(pages)),
            mock.patch("freegem.claim_verify._gemini_complete", _gemini(GOOD_ANSWER)),
        ):
            output = run_tool(
                BridgeConfig(),
                "web_verify",
                {"claims": CLAIMS[:1], "discover": True, "discovery_query": "Alexander the Great"},
            )
        self.assertIn("discovered via en.wikipedia.org (1 urls)", output)


class _FakeResponse:
    """A urllib-shaped response: read(n) yields up to n bytes, then stops."""

    headers: typing.ClassVar[dict[str, str]] = {"Content-Type": "application/json"}

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._pos = 0

    def read(self, n: int = -1) -> bytes:
        if self._pos >= len(self._payload):
            return b""
        if n is None or n < 0:
            chunk = self._payload[self._pos :]
            self._pos = len(self._payload)
            return chunk
        chunk = self._payload[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk


class MaterialBudgetTests(_TmpDirTestCase):
    def test_fit_material_keeps_everything_when_under_budget(self) -> None:
        from freegem.claim_verify import _fit_material

        material = [(ResearchSource(url="http://a"), "short text")]
        self.assertEqual(_fit_material(material, ["claim"]), material)

    def test_fit_material_prefers_relevant_pages_over_budget(self) -> None:
        from freegem.claim_verify import _fit_material

        filler = "unrelated filler word " * 40_000
        # Relevant text is bigger than the budget, so it consumes all of it
        # and the zero-overlap filler page is dropped entirely.
        relevant = "Alexander king of Macedon " * 4_000
        material = [
            (ResearchSource(url="http://filler"), filler),
            (ResearchSource(url="http://relevant"), relevant),
        ]
        kept = _fit_material(material, ["Who was the king of Macedon?"])
        self.assertEqual([source.url for source, _ in kept], ["http://relevant"])
        # And the kept text is sliced to the budget, not the full page.
        self.assertLessEqual(sum(len(t) for _, t in kept), 48_000)

    def test_fit_material_never_hides_quotes_from_verification(self) -> None:
        # The slice only bounds the PROMPT; verification runs on the full
        # material. Pin that asymmetry by checking verify() against a page
        # whose relevant section sits beyond the per-page cut.
        huge = ALEXANDER_TEXT + "\n\n" + "irrelevant tail " * 30_000
        pages = {WIKI_URL: _page(WIKI_URL, "Alexander the Great", huge)}
        result = _run_verify(pages=pages, claims=CLAIMS[:2])
        for claim in result.claims:
            for evidence in claim.evidence:
                self.assertTrue(evidence.verified)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
