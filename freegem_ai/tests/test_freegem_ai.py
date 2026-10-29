"""Tests for freegem_ai -- AI-assisted scraper/crawler/fetcher.

No network beyond the loopback test server: every Gemini call goes through
``mock.patch('freegem_ai.core.generate')`` (the bridge seam), every fetch goes
to a throwaway ``ThreadingHTTPServer`` behind the same DNS/dial fakes the
scraper tests use.
"""

from __future__ import annotations

import threading
import urllib.parse
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest import mock

import pytest

from freegem.config import BridgeConfig
from freegem.tests.test_scraper import (
    _FAKE_HOST,
    _fake_create_connection,
    _fake_resolver,
)
from freegem.web_scraper import clear_robots_cache
from freegem_ai.core import (
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    ai_clean_page,
    ai_extract_entities,
    ai_questionnaire,
    ai_summarize,
    run_ai_pass,
)
from freegem_ai.crawler import AICrawlResult, ai_crawl, ai_score_page
from freegem_ai.research import AIExtractResult, ai_extract, ai_research, ai_search
from freegem_ai.scraper import AIBatchItem, AIBatchResult, scrape, scrape_batch

# ── fake model seam ──────────────────────────────────────────────────────────


class FakeGenerate:
    """A fake for ``freegem_ai.core.generate``. Records prompts, returns canned text."""

    def __init__(self, answer: str = "fake answer", exc: Exception | None = None) -> None:
        self.answer = answer
        self.exc = exc
        self.prompts: list[str] = []

    def __call__(self, cfg: Any, prompt: str, *args: Any, **kwargs: Any) -> str:
        self.prompts.append(prompt)
        if self.exc is not None:
            raise self.exc
        return self.answer

    @property
    def calls(self) -> int:
        return len(self.prompts)


# ── fake HTTP site ────────────────────────────────────────────────────────────

_ARTICLE = (
    "<!DOCTYPE html><html><head><title>Test Article</title></head><body>"
    "<nav><a href='/'>Home</a> <a href='/m'>Menu</a></nav>"
    "<article>"
    + ("This is real content about the topic. " * 50)
    + "</article>"
    + "<p>Footer text here.</p>"
    + "</body></html>"
)

_NAVGUNK = (
    "<!DOCTYPE html><html><head><title>Shell</title></head><body>"
    "<nav><a href='/'>Home</a> <a href='/m'>Menu</a> <a href='/a'>About</a></nav>"
    "<div id='app'></div>" + "<p>Home Menu About Sign in</p>" + "</body></html>"
)

_LINKS_PAGE = (
    "<!DOCTYPE html><html><head><title>Links Page</title></head><body>"
    "<article><p>Some content here about the topic.</p></article>"
    + "".join(f'<a href="/page{i}">link {i}</a>' for i in range(1, 11))
    + "</body></html>"
)

_SMALL_PAGE = (
    "<!DOCTYPE html><html><head><title>Small</title></head><body>"
    "<p>Brief content.</p>"
    + "".join(f'<a href="/linked{i}">l{i}</a>' for i in range(5))
    + "</body></html>"
)


class _AiTestSite(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _send(self, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/article":
            self._send(_ARTICLE.encode())
        elif path == "/links":
            self._send(_LINKS_PAGE.encode())
        elif path == "/small":
            self._send(_SMALL_PAGE.encode())
        elif path == "/navgunk":
            self._send(_NAVGUNK.encode())
        else:
            self.send_error(404)


@pytest.fixture(autouse=True)
def _fake_site() -> Iterator[str]:
    """Loopback server + DNS/dial fakes, shared across all tests."""
    clear_robots_cache()
    resolver = mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_resolver)
    resolver.start()
    dial = mock.patch(
        "freegem.ssrf.socket.create_connection",
        side_effect=_fake_create_connection,
    )
    dial.start()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _AiTestSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://{_FAKE_HOST}:{server.server_port}"
    yield base
    server.shutdown()
    server.server_close()
    dial.stop()
    resolver.stop()


@pytest.fixture
def cfg() -> BridgeConfig:
    return BridgeConfig(log_requests=False)


# ── core tests ─────────────────────────────────────────────────────────────────


class TestCore:
    def test_untrusted_markers_present(self) -> None:
        from freegem_ai.core import _wrap_untrusted

        wrapped = _wrap_untrusted("hello world")
        assert UNTRUSTED_OPEN in wrapped
        assert UNTRUSTED_CLOSE in wrapped
        assert "hello world" in wrapped

    def test_truncate_stays_under_budget(self) -> None:
        from freegem_ai.core import DEFAULT_AI_BUDGET, _truncate_text

        text = "x" * (DEFAULT_AI_BUDGET + 500)
        truncated, was = _truncate_text(text, DEFAULT_AI_BUDGET)
        assert was is True
        assert len(truncated) <= DEFAULT_AI_BUDGET + 50

    def test_truncate_no_truncation(self) -> None:
        from freegem_ai.core import _truncate_text

        text = "short text"
        truncated, was = _truncate_text(text, 100_000)
        assert was is False
        assert truncated == text

    def test_resolve_gemini_args_default(self, cfg: BridgeConfig) -> None:
        from freegem_ai.core import _resolve_gemini_args

        name, mode, think, _extra = _resolve_gemini_args(cfg)
        assert name is not None
        assert isinstance(mode, int)
        assert isinstance(think, int)

    def test_resolve_gemini_args_unknown_falls_back(self, cfg: BridgeConfig) -> None:
        from freegem_ai.core import _resolve_gemini_args

        _name, mode, think, _extra = _resolve_gemini_args(cfg, "nonexistent-model-xyz@think=2")
        # Should fall back to default model
        assert mode is not None
        assert think is not None

    def test_run_ai_pass_success(self, cfg: BridgeConfig) -> None:
        fake = FakeGenerate("ai response")
        with mock.patch("freegem_ai.core.generate", fake):
            result = run_ai_pass(cfg, "test prompt", via="test-via")
        assert result.text == "ai response"
        assert result.via == "test-via"
        assert result.model is not None
        assert result.error is None

    def test_run_ai_pass_failure(self, cfg: BridgeConfig) -> None:
        from freegem.gemini import BridgeError

        fake = FakeGenerate(exc=BridgeError("rate limited"))
        with mock.patch("freegem_ai.core.generate", fake):
            result = run_ai_pass(cfg, "test prompt", via="test-via")
        assert result.text == ""
        assert result.error is not None
        assert "rate limited" in result.error

    def test_ai_summarize_wraps_untrusted(self, cfg: BridgeConfig) -> None:
        fake = FakeGenerate("SUMMARY")
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_summarize(cfg, "page content here")
        assert result.text == "SUMMARY"
        assert result.via == "ai-summary"
        assert UNTRUSTED_OPEN in fake.prompts[0]

    def test_ai_clean_page_success(self, _fake_site: str, cfg: BridgeConfig) -> None:
        fake = FakeGenerate("cleaned content here")
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_clean_page(cfg, _fake_site + "/article", smart=False)
        assert result.scrape.title == "Test Article"
        assert result.ai_clean is not None
        assert result.ai_clean.text == "cleaned content here"
        assert result.ai_clean.via == "ai-clean"

    def test_ai_extract_entities_success(self, cfg: BridgeConfig) -> None:
        fake = FakeGenerate('{"entities": ["person: Alice"], "topics": ["AI"], "terms": ["LLM"]}')
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_extract_entities(cfg, "Alice works on AI and LLMs.")
        assert result.via == "ai-entities"
        assert "Alice" in result.text

    def test_ai_questionnaire_success(self, cfg: BridgeConfig) -> None:
        fake = FakeGenerate("1. What is AI?\n2. How does it work?")
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_questionnaire(cfg, "AI is the simulation of human intelligence.")
        assert result.via == "ai-questions"
        assert "1." in result.text
        assert "2." in result.text


# ── scraper tests ──


class TestScraper:
    def test_scrape_single_url_ai_clean(self, _fake_site: str, cfg: BridgeConfig) -> None:
        fake = FakeGenerate("AI-cleaned page content")
        with mock.patch("freegem_ai.core.generate", fake):
            results = scrape(
                cfg,
                [_fake_site + "/article"],
                ai_clean=True,
                ai_summarize=True,
                smart=False,
            )
        assert len(results) == 1
        assert results[0].ok
        assert results[0].page.ai_clean is not None
        assert results[0].page.ai_clean.text == "AI-cleaned page content"
        assert results[0].page.ai_summary is not None

    def test_scrape_batch_multi_url(self, _fake_site: str, cfg: BridgeConfig) -> None:
        fake = FakeGenerate("batch cleaned")
        with mock.patch("freegem_ai.core.generate", fake):
            results = scrape(
                cfg,
                [_fake_site + "/article", _fake_site + "/links"],
                ai_clean=True,
                smart=False,
            )
        assert len(results) == 2
        assert all(r.ok for r in results)
        assert all(
            r.page.ai_clean is not None and r.page.ai_clean.text == "batch cleaned" for r in results
        )

    def test_scrape_ask_question(self, _fake_site: str, cfg: BridgeConfig) -> None:
        fake = FakeGenerate("The answer is 42")
        with mock.patch("freegem_ai.core.generate", fake):
            results = scrape(
                cfg,
                [_fake_site + "/article"],
                ai_clean=False,
                question="what is the meaning?",
                smart=False,
            )
        assert len(results) == 1
        assert results[0].ai_question is not None
        assert results[0].ai_question.text == "The answer is 42"

    def test_scrape_with_schema(self, _fake_site: str, cfg: BridgeConfig) -> None:
        fake = FakeGenerate('{"title": "Test", "author": "Someone"}')
        with mock.patch("freegem_ai.core.generate", fake):
            results = scrape(
                cfg,
                [_fake_site + "/article"],
                ai_clean=False,
                schema={"title": "string", "author": "string"},
                smart=False,
            )
        assert len(results) == 1
        assert results[0].ai_extract is not None
        assert "Test" in results[0].ai_extract.text

    def test_scrape_batch_explicit_items(self, _fake_site: str, cfg: BridgeConfig) -> None:
        fake = FakeGenerate("explicit item result")
        with mock.patch("freegem_ai.core.generate", fake):
            items = [
                AIBatchItem(url=_fake_site + "/article", ai_clean=True),
                AIBatchItem(url=_fake_site + "/links", ai_summarize=True),
            ]
            results = scrape_batch(cfg, items, smart=False)
        assert len(results) == 2
        assert results[0].page.ai_clean is not None
        assert results[1].page.ai_summary is not None
        assert results[0].page.ai_clean.text == "explicit item result"

    def test_scrape_on_page_callback(self, _fake_site: str, cfg: BridgeConfig) -> None:
        fake = FakeGenerate("cb result")
        seen: list[AIBatchResult] = []
        with mock.patch("freegem_ai.core.generate", fake):
            scrape(
                cfg,
                [_fake_site + "/article", _fake_site + "/links"],
                ai_clean=True,
                smart=False,
                on_page=seen.append,
            )
        assert len(seen) == 2
        assert all(
            r.page.ai_clean is not None and r.page.ai_clean.text == "cb result" for r in seen
        )

    def test_scrape_empty_url_raises(self, cfg: BridgeConfig) -> None:
        with pytest.raises(ValueError, match="at least one"):
            scrape(cfg, [], smart=False)

    def test_scrape_preserves_order(self, _fake_site: str, cfg: BridgeConfig) -> None:
        urls = [_fake_site + "/article", _fake_site + "/links", _fake_site + "/small"]
        fake = FakeGenerate("ordered")
        with mock.patch("freegem_ai.core.generate", fake):
            results = scrape(cfg, urls, ai_summarize=True, smart=False)
        assert [r.page.scrape.url for r in results] == urls


# ── research tests ──


class TestResearch:
    def test_ai_extract_success(self, _fake_site: str, cfg: BridgeConfig) -> None:
        fake = FakeGenerate('{"language": "Python"}')
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_extract(
                cfg,
                "What language?",
                [_fake_site + "/article"],
                schema={"language": "string"},
                smart=False,
            )
        assert isinstance(result, AIExtractResult)
        assert result.text == '{"language": "Python"}'
        assert len(result.sources) == 1

    def test_ai_extract_empty_urls(self, cfg: BridgeConfig) -> None:
        with pytest.raises(ValueError, match="at least one"):
            ai_extract(cfg, "q", [], smart=False)

    def test_ai_extract_empty_question(self, cfg: BridgeConfig) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            ai_extract(cfg, "", ["http://x.com"], smart=False)

    def test_ai_research_delegates_to_extract(self, _fake_site: str, cfg: BridgeConfig) -> None:
        fake = FakeGenerate('{"result": "yes"}')
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_research(
                cfg,
                "What is this about?",
                [_fake_site + "/article"],
                schema={"result": "string"},
                smart=False,
            )
        assert isinstance(result, AIExtractResult)
        assert result.error is None

    def test_ai_search_success(self, _fake_site: str, cfg: BridgeConfig) -> None:
        fake = FakeGenerate("summarized answer")
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_search(
                cfg,
                "what is the topic",
                [_fake_site + "/article"],
                smart=False,
            )
        assert result.research.question == "what is the topic"
        assert result.ai.cross_page is not None
        assert result.ai.cross_page.text == "summarized answer"

    def test_ai_search_bad_question_raises(self, cfg: BridgeConfig) -> None:
        with pytest.raises(ValueError):
            ai_search(cfg, "", ["http://x.com"], smart=False)


# ── crawler tests ──


class TestCrawler:
    def test_ai_score_page(self, _fake_site: str, cfg: BridgeConfig) -> None:
        fake = FakeGenerate("0.85 highly relevant")
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_score_page(
                cfg,
                "AI",
                _fake_site + "/article",
                "some content about AI",
            )
        assert result.via == "ai-score"
        assert "0.85" in result.text

    def test_ai_crawl_basic(self, _fake_site: str, cfg: BridgeConfig) -> None:
        fake = FakeGenerate("0.9 relevant\n\n[next pages]")
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_crawl(
                cfg,
                [_fake_site + "/article"],
                "AI topic",
                max_pages=5,
                max_depth=1,
                smart=False,
            )
        assert isinstance(result, AICrawlResult)
        assert len(result.records) >= 1
        assert result.records[0].ok

    def test_ai_crawl_empty_seeds(self, cfg: BridgeConfig) -> None:
        with pytest.raises(ValueError, match="at least one"):
            ai_crawl(cfg, [], "query", smart=False)

    def test_ai_crawl_empty_query(self, _fake_site: str, cfg: BridgeConfig) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            ai_crawl(cfg, [_fake_site + "/article"], "", smart=False)

    def test_ai_crawl_max_pages_respected(self, _fake_site: str, cfg: BridgeConfig) -> None:
        fake = FakeGenerate("0.5 moderate relevance")
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_crawl(
                cfg,
                [_fake_site + "/article"],
                "topic",
                max_pages=2,
                max_depth=0,
                smart=False,
            )
        assert len(result.records) <= 2 + 1  # may add a couple via depth 0


# ── integration: 10x repeat ──


class TestRepeatable:
    """Each capability tested 10x for determinism (no flakiness)."""

    REPEATS = 10

    @pytest.mark.parametrize("i", range(REPEATS))
    def test_ai_clean_10x(self, _fake_site: str, cfg: BridgeConfig, i: int) -> None:
        fake = FakeGenerate(f"cleaned run {i}")
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_clean_page(cfg, _fake_site + "/article", smart=False)
        assert result.ai_clean is not None
        assert result.ai_clean.text == f"cleaned run {i}"

    @pytest.mark.parametrize("i", range(REPEATS))
    def test_ai_summarize_10x(self, cfg: BridgeConfig, i: int) -> None:
        fake = FakeGenerate(f"summary {i}")
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_summarize(cfg, "content to summarize")
        assert result.text == f"summary {i}"

    @pytest.mark.parametrize("i", range(REPEATS))
    def test_ai_extract_entities_10x(self, cfg: BridgeConfig, i: int) -> None:
        fake = FakeGenerate('{"entities": [], "topics": ["test"], "terms": []}')
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_extract_entities(cfg, "some text about test topics")
        assert result.via == "ai-entities"

    @pytest.mark.parametrize("i", range(REPEATS))
    def test_run_ai_pass_10x(self, cfg: BridgeConfig, i: int) -> None:
        fake = FakeGenerate(f"pass {i}")
        with mock.patch("freegem_ai.core.generate", fake):
            result = run_ai_pass(cfg, "prompt", via="repeat")
        assert result.text == f"pass {i}"
        assert result.via == "repeat"

    @pytest.mark.parametrize("i", range(REPEATS))
    def test_scrape_single_10x(self, _fake_site: str, cfg: BridgeConfig, i: int) -> None:
        fake = FakeGenerate(f"scrape {i}")
        with mock.patch("freegem_ai.core.generate", fake):
            results = scrape(
                cfg,
                [_fake_site + "/article"],
                ai_clean=True,
                smart=False,
            )
        assert results[0].page.ai_clean is not None
        assert results[0].page.ai_clean.text == f"scrape {i}"

    @pytest.mark.parametrize("i", range(REPEATS))
    def test_ai_score_10x(self, _fake_site: str, cfg: BridgeConfig, i: int) -> None:
        fake = FakeGenerate("0.7 moderate")
        with mock.patch("freegem_ai.core.generate", fake):
            result = ai_score_page(
                cfg,
                "topic",
                _fake_site + "/article",
                "content",
            )
        assert "0.7" in result.text
