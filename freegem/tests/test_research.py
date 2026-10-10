"""Tests for multi-source research: aggregation, cache, budgets, citations.

The network surface (SSRF guard, robots, caps, pinning) is fully covered by
test_scraper.py; research sits ON TOP of that machinery, so these tests stub
``scrape_page`` (the fetch seam) and ``_gemini_complete`` (the model seam) and
verify the layer that is actually new here: multi-page aggregation, the local
fetch cache, per-page and total budgets, [S#] citation bookkeeping, untrusted
markers in every prompt, and the deterministic fallback when Gemini is down.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock

from freegem.config import BridgeConfig
from freegem.gemini import BridgeError
from freegem.mcp_tools import TOOL_SPECS, run_tool
from freegem.web_research import (
    DEFAULT_MAX_PAGES,
    HARD_MAX_PAGES,
    ResearchCache,
    ResearchResult,
    render_json,
    render_markdown,
    render_text,
    research,
    research_cli,
)
from freegem.web_scraper import ScrapeError, ScrapeResult


def _page(url: str, title: str, text: str, truncated: bool = False) -> ScrapeResult:
    return ScrapeResult(url=url, title=title, text=text, truncated=truncated, links=[])


def _fetcher(pages: dict[str, ScrapeResult], failures: set[str] | None = None) -> Any:
    """A scrape_page stand-in keyed by URL; listed URLs raise ScrapeError."""

    def fake(cfg: BridgeConfig, url: str, **kwargs: Any) -> ScrapeResult:
        if failures and url in failures:
            raise ScrapeError(f"refused {url!r}")
        if url not in pages:
            raise ScrapeError(f"no fake page for {url!r}")
        return pages[url]

    return fake


class _TmpDirTestCase(unittest.TestCase):
    """A per-test temp directory, cleaned up afterwards."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="freegem-research-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class ResearchCacheTests(_TmpDirTestCase):
    """The JSON file cache: roundtrip, TTL, corruption, eviction."""

    def test_roundtrip(self) -> None:
        cache = ResearchCache(self.tmp / "cache.json")
        cache.put("http://a.test/", "Title A", "body text")
        got = cache.get("http://a.test/")
        assert got is not None
        self.assertEqual(got["title"], "Title A")
        self.assertEqual(got["text"], "body text")

    def test_miss_on_empty_cache(self) -> None:
        self.assertIsNone(ResearchCache(self.tmp / "cache.json").get("http://a.test/"))

    def test_ttl_expiry_is_a_miss(self) -> None:
        path = self.tmp / "cache.json"
        cache = ResearchCache(path, ttl=0)
        cache.put("http://a.test/", "t", "x")
        self.assertIsNone(ResearchCache(path, ttl=0).get("http://a.test/"))
        # The same entry is fresh for a longer-TTL cache.
        self.assertIsNotNone(ResearchCache(path, ttl=3600).get("http://a.test/"))

    def test_corrupt_file_is_a_miss(self) -> None:
        path = self.tmp / "cache.json"
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(ResearchCache(path).get("http://a.test/"))

    def test_tampered_text_is_a_miss(self) -> None:
        path = self.tmp / "cache.json"
        cache = ResearchCache(path)
        cache.put("http://a.test/", "t", "original body")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["http://a.test/"]["text"] = "injected body"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(cache.get("http://a.test/"))

    def test_eviction_keeps_newest(self) -> None:
        cache = ResearchCache(self.tmp / "cache.json", ttl=99999)
        for i in range(300):
            cache.put(f"http://a.test/{i}", "t", f"body {i}")
        got = cache.get("http://a.test/299")
        assert got is not None
        self.assertEqual(got["text"], "body 299")
        # The oldest entries are gone.
        self.assertIsNone(cache.get("http://a.test/0"))

    def test_write_failure_reports_via_on_error(self) -> None:
        # A directory where the file should be: writes fail with OSError.
        blocker = self.tmp / "blocked.json"
        blocker.mkdir()
        notes: list[str] = []
        cache = ResearchCache(blocker, on_error=notes.append)
        cache.put("http://a.test/", "t", "x")
        self.assertTrue(notes and "cache write failed" in notes[0])


class NormalizeTests(unittest.TestCase):
    def test_fragments_and_duplicates(self) -> None:
        from freegem.web_research import _normalize_urls

        self.assertEqual(
            _normalize_urls(["http://a/x#frag", "http://a/x", "", "http://b/", "http://a/x#other"]),
            ["http://a/x", "http://b/"],
        )


class CollectTests(_TmpDirTestCase):
    """Fetching, budgets, and cache interaction via the scrape_page seam."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg = BridgeConfig(log_requests=False)

    def _run(self, urls: list[str], **kwargs: Any) -> Any:
        from freegem.web_research import _collect_sources

        kwargs.setdefault("max_pages", DEFAULT_MAX_PAGES)
        kwargs.setdefault("max_chars_per_page", 12000)
        kwargs.setdefault("total_budget", 80000)
        kwargs.setdefault("cache", None)
        kwargs.setdefault("refresh", False)
        return _collect_sources(self.cfg, urls, **kwargs)

    def test_two_pages_collected_in_order(self) -> None:
        pages = {
            "http://a.test/1": _page("http://a.test/1", "One", "content one"),
            "http://a.test/2": _page("http://a.test/2", "Two", "content two"),
        }
        with mock.patch("freegem.web_scraper.scrape_page", _fetcher(pages)):
            sources, material = self._run(["http://a.test/1", "http://a.test/2"])
        self.assertTrue(all(s.ok for s in sources))
        self.assertEqual([s.title for s in sources], ["One", "Two"])
        self.assertEqual([text for _, text in material], ["content one", "content two"])

    def test_failed_source_recorded_run_continues(self) -> None:
        pages = {"http://a.test/2": _page("http://a.test/2", "Two", "content two")}
        with mock.patch(
            "freegem.web_scraper.scrape_page", _fetcher(pages, failures={"http://a.test/1"})
        ):
            sources, material = self._run(["http://a.test/1", "http://a.test/2"])
        self.assertFalse(sources[0].ok)
        assert sources[0].error is not None
        self.assertIn("refused", sources[0].error)
        self.assertTrue(sources[1].ok)
        self.assertEqual(len(material), 1)

    def test_max_pages_cap_skips_without_fetching(self) -> None:
        calls: list[str] = []
        pages = {f"http://a.test/{i}": _page(f"http://a.test/{i}", str(i), "x") for i in range(3)}

        def spying(cfg: BridgeConfig, url: str, **kw: Any) -> ScrapeResult:
            calls.append(url)
            return pages[url]

        with mock.patch("freegem.web_scraper.scrape_page", spying):
            sources, _ = self._run([f"http://a.test/{i}" for i in range(3)], max_pages=2)
        self.assertEqual(calls, ["http://a.test/0", "http://a.test/1"])
        self.assertEqual(len([s for s in sources if s.skipped == "max_pages"]), 1)
        self.assertEqual(len([s for s in sources if s.ok]), 2)

    def test_total_budget_skips_without_fetching(self) -> None:
        calls: list[str] = []

        def spying(cfg: BridgeConfig, url: str, **kw: Any) -> ScrapeResult:
            calls.append(url)
            return _page(url, url, "y" * 1000)

        with mock.patch("freegem.web_scraper.scrape_page", spying):
            sources, _ = self._run(
                ["http://a.test/1", "http://a.test/2", "http://a.test/3"],
                max_chars_per_page=1000,
                total_budget=1500,
            )
        # Page 1 eats 1000; page 2 gets the remaining 500; page 3 is over budget.
        self.assertEqual(calls, ["http://a.test/1", "http://a.test/2"])
        skipped = [s for s in sources if s.skipped == "total_budget"]
        self.assertEqual([s.url for s in skipped], ["http://a.test/3"])

    def test_cache_hit_skips_the_network(self) -> None:
        cache = ResearchCache(self.tmp / "cache.json", ttl=9999)
        cache.put("http://a.test/1", "Cached", "cached body")
        fetch = mock.Mock(side_effect=AssertionError("must not fetch on a cache hit"))
        with mock.patch("freegem.web_scraper.scrape_page", fetch):
            sources, material = self._run(["http://a.test/1"], cache=cache)
        self.assertTrue(sources[0].from_cache)
        self.assertEqual(sources[0].title, "Cached")
        self.assertEqual([text for _, text in material], ["cached body"])
        fetch.assert_not_called()

    def test_cache_miss_populates_and_second_run_hits(self) -> None:
        cache = ResearchCache(self.tmp / "cache.json", ttl=9999)
        pages = {"http://a.test/1": _page("http://a.test/1", "One", "fresh body")}
        fetcher = _fetcher(pages)
        with mock.patch("freegem.web_scraper.scrape_page", fetcher):
            self._run(["http://a.test/1"], cache=cache)
            first = mock.Mock(side_effect=fetcher)
            with mock.patch("freegem.web_scraper.scrape_page", first):
                sources, _ = self._run(["http://a.test/1"], cache=cache)
        self.assertTrue(sources[0].from_cache)
        first.assert_not_called()

    def test_refresh_refetches_and_rewrites_cache(self) -> None:
        cache = ResearchCache(self.tmp / "cache.json", ttl=9999)
        cache.put("http://a.test/1", "Old", "old body")
        pages = {"http://a.test/1": _page("http://a.test/1", "New", "new body")}
        with mock.patch("freegem.web_scraper.scrape_page", _fetcher(pages)):
            sources, material = self._run(["http://a.test/1"], cache=cache, refresh=True)
        self.assertFalse(sources[0].from_cache)
        self.assertEqual([text for _, text in material], ["new body"])
        got = cache.get("http://a.test/1")
        assert got is not None
        self.assertEqual(got["title"], "New")


class ResearchFlowTests(_TmpDirTestCase):
    """End-to-end research() with both seams stubbed."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg = BridgeConfig(log_requests=False)

    def _research(
        self,
        urls: list[str],
        pages: dict[str, ScrapeResult],
        gemini: Any,
        failures: set[str] | None = None,
        **kwargs: Any,
    ) -> ResearchResult:
        with (
            mock.patch("freegem.web_scraper.scrape_page", _fetcher(pages, failures)),
            mock.patch("freegem.web_research._gemini_complete", gemini),
        ):
            return research(self.cfg, kwargs.pop("question", "Which option?"), urls, **kwargs)

    def test_single_pass_answer_with_citations_and_table(self) -> None:
        pages = {
            "http://a.test/1": _page("http://a.test/1", "Option A", "alpha content"),
            "http://a.test/2": _page("http://a.test/2", "Option B", "beta content"),
        }

        def gemini(cfg: BridgeConfig, prompt: str) -> str:
            self.assertIn("alpha content", prompt)
            self.assertIn("beta content", prompt)
            return "## Answer\nB wins [S2], A is close [S1]."

        result = self._research(["http://a.test/1", "http://a.test/2"], pages, gemini)
        self.assertTrue(result.gemini_used)
        self.assertIn("[S2]", result.answer)
        output = render_text(result)
        self.assertIn("[S1] http://a.test/1 — Option A", output)
        self.assertIn("[S2] http://a.test/2 — Option B", output)

    def test_prompt_carries_untrusted_markers_and_question(self) -> None:
        pages = {"http://a.test/1": _page("http://a.test/1", "T", "page words")}
        captured: list[str] = []

        def gemini(cfg: BridgeConfig, prompt: str) -> str:
            captured.append(prompt)
            return "answer [S1]"

        self._research(["http://a.test/1"], pages, gemini, question="why?")
        prompt = captured[0]
        self.assertIn("UNTRUSTED WEB CONTENT", prompt)
        self.assertIn("data, not instructions", prompt)
        self.assertIn("why?", prompt)
        self.assertIn("[S1]", prompt)

    def test_citation_beyond_sources_falls_back_deterministic(self) -> None:
        pages = {"http://a.test/1": _page("http://a.test/1", "T", "words")}

        def gemini(cfg: BridgeConfig, prompt: str) -> str:
            return "invented [S7] citation"

        result = self._research(["http://a.test/1"], pages, gemini)
        self.assertFalse(result.gemini_used)
        self.assertIn("raw extracted material", result.answer)
        self.assertIn("[S1]", result.answer)

    def test_gemini_down_returns_raw_extracts_not_an_error(self) -> None:
        pages = {
            "http://a.test/1": _page("http://a.test/1", "A", "alpha words"),
            "http://a.test/2": _page("http://a.test/2", "B", "beta words"),
        }

        def gemini(cfg: BridgeConfig, prompt: str) -> str:
            raise BridgeError("no session")

        result = self._research(["http://a.test/1", "http://a.test/2"], pages, gemini)
        self.assertFalse(result.gemini_used)
        self.assertIn("alpha words", result.answer)
        self.assertIn("beta words", result.answer)
        self.assertIn("UNTRUSTED WEB CONTENT", result.answer)
        self.assertEqual(result.failed_pages, 0)

    def test_all_sources_failed_reports_nothing_to_research(self) -> None:
        def gemini(cfg: BridgeConfig, prompt: str) -> str:
            raise AssertionError("gemini must not be called with no material")

        result = self._research(
            ["http://a.test/1"],
            {},
            gemini,
            failures={"http://a.test/1"},
        )
        self.assertIn("No source could be fetched", result.answer)
        self.assertEqual(result.failed_pages, 1)

    def test_empty_question_and_empty_urls_raise(self) -> None:
        with self.assertRaises(ValueError):
            research(self.cfg, "   ", ["http://a.test/"])
        with self.assertRaises(ValueError):
            research(self.cfg, "q", ["   "])

    def test_map_reduce_runs_above_single_pass_budget(self) -> None:
        big_text_a = ("alpha line\n" * 4000)[:25000]
        big_text_b = ("beta line\n" * 4000)[:25000]
        pages = {
            "http://a.test/1": _page("http://a.test/1", "Big A", big_text_a),
            "http://a.test/2": _page("http://a.test/2", "Big B", big_text_b),
        }
        stages: list[str] = []

        def gemini(cfg: BridgeConfig, prompt: str) -> str:
            stages.append("map" if "At most 10 bullets" in prompt else "reduce")
            return "a relevant point [S1]" if stages[-1] == "map" else "## Answer\nfinal [S1] [S2]"

        with mock.patch("freegem.web_research._MAP_CHUNK_CHARS", 20000):
            result = self._research(["http://a.test/1", "http://a.test/2"], pages, gemini)
        self.assertEqual(stages.count("map"), 4)  # 2 pages x 2 chunks
        self.assertEqual(stages.count("reduce"), 1)
        self.assertTrue(result.gemini_used)

    def test_map_reduce_survives_dead_chunks(self) -> None:
        big_text = "gamma line\n" * 6000  # > one chunk
        pages = {"http://a.test/1": _page("http://a.test/1", "Big", big_text)}
        calls: list[int] = []

        def gemini(cfg: BridgeConfig, prompt: str) -> str:
            calls.append(1)
            if len(calls) == 1:
                raise BridgeError("chunk died")
            return "## Answer\nsaved [S1]"

        with mock.patch("freegem.web_research._MAP_CHUNK_CHARS", 1000):
            result = self._research(["http://a.test/1"], pages, gemini)
        self.assertTrue(result.gemini_used)
        self.assertIn("saved [S1]", result.answer)

    def test_boilerplate_deduplicated_across_pages(self) -> None:
        shared = "Sign in to continue to the best experience of our site\n"
        pages = {
            "http://a.test/1": _page("http://a.test/1", "A", shared + "unique alpha"),
            "http://a.test/2": _page("http://a.test/2", "B", shared + "unique beta"),
        }
        captured: list[str] = []

        def gemini(cfg: BridgeConfig, prompt: str) -> str:
            captured.append(prompt)
            return "answer [S1] [S2]"

        self._research(["http://a.test/1", "http://a.test/2"], pages, gemini)
        prompt = captured[0]
        self.assertEqual(prompt.count("Sign in to continue"), 1)
        self.assertIn("unique alpha", prompt)
        self.assertIn("unique beta", prompt)

    def test_page_level_truncation_flagged_in_metadata(self) -> None:
        pages = {"http://a.test/1": _page("http://a.test/1", "T", "z" * 500, truncated=True)}

        def gemini(cfg: BridgeConfig, prompt: str) -> str:
            return "a [S1]"

        result = self._research(["http://a.test/1"], pages, gemini)
        self.assertTrue(result.sources[0].truncated)
        self.assertIn("truncated", render_text(result))


class RendererTests(_TmpDirTestCase):
    def _sample_result(self) -> ResearchResult:
        from freegem.web_research import ResearchSource

        return ResearchResult(
            question="Which option?",
            answer="B wins [S2].",
            sources=[
                ResearchSource(url="http://a.test/1", ok=True, title="Option A", chars=100),
                ResearchSource(
                    url="http://a.test/2",
                    ok=True,
                    title="Option B",
                    chars=200,
                    truncated=True,
                    from_cache=True,
                ),
                ResearchSource(url="http://a.test/3", ok=False, error="robots.txt disallows"),
            ],
            gemini_used=True,
            chars_fetched=300,
            elapsed_sec=1.25,
        )

    def test_text_rendering(self) -> None:
        out = render_text(self._sample_result())
        self.assertIn("B wins [S2].", out)
        self.assertIn("[S1] http://a.test/1 — Option A", out)
        self.assertIn("(truncated, cached)", out)
        self.assertIn("2/3 pages", out)
        self.assertIn("1 failed", out)

    def test_markdown_rendering_links_sources(self) -> None:
        out = render_markdown(self._sample_result())
        self.assertIn("[Option A](http://a.test/1)", out)
        self.assertIn("`S2`", out)

    def test_json_schema(self) -> None:
        payload = json.loads(render_json(self._sample_result()))
        self.assertEqual(payload["question"], "Which option?")
        self.assertTrue(payload["gemini_used"])
        self.assertEqual(payload["answer"], "B wins [S2].")
        self.assertEqual(len(payload["sources"]), 3)
        first, second, third = payload["sources"]
        self.assertEqual(first["marker"], "S1")
        self.assertEqual(second["marker"], "S2")
        self.assertNotIn("marker", third)
        self.assertEqual(third["error"], "robots.txt disallows")
        meta = payload["meta"]
        self.assertEqual(meta["urls_requested"], 3)
        self.assertEqual(meta["pages_ok"], 2)
        self.assertEqual(meta["pages_failed"], 1)
        self.assertEqual(meta["cache_hits"], 1)  # source 2 was fetched from cache
        self.assertEqual(meta["chars_fetched"], 300)
        self.assertTrue(meta["version"])


class MCPResearchTests(_TmpDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = BridgeConfig(log_requests=False)

    def test_tool_spec_registered(self) -> None:
        names = {spec["name"] for spec in TOOL_SPECS}
        self.assertIn("web_research", names)

    def test_dispatch_reaches_research(self) -> None:
        pages = {"http://a.test/1": _page("http://a.test/1", "T", "words")}

        def gemini(cfg: BridgeConfig, prompt: str) -> str:
            return "answer [S1]"

        with (
            mock.patch("freegem.web_scraper.scrape_page", _fetcher(pages)),
            mock.patch("freegem.web_research._gemini_complete", gemini),
        ):
            out = run_tool(
                self.cfg,
                "web_research",
                {"urls": ["http://a.test/1"], "question": "what?"},
            )
        self.assertIn("answer [S1]", out)
        self.assertIn("[S1] http://a.test/1", out)

    def test_dispatch_validates_arguments(self) -> None:
        with self.assertRaises(ValueError):
            run_tool(self.cfg, "web_research", {"question": "q"})  # no urls
        with self.assertRaises(ValueError):
            run_tool(self.cfg, "web_research", {"urls": ["http://a.test/"]})  # no question

    def test_max_pages_clamped_to_hard_cap(self) -> None:
        seen: dict[str, Any] = {}

        def fake_research(cfg: BridgeConfig, question: str, urls: list[str], **kw: Any) -> Any:
            seen.update(kw)
            return ResearchResult(question=question, answer="x", sources=[])

        with mock.patch("freegem.web_research.research", fake_research):
            run_tool(
                self.cfg,
                "web_research",
                {"urls": ["http://a.test/1"], "question": "q", "max_pages": 99},
            )
        self.assertEqual(seen["max_pages"], HARD_MAX_PAGES)


class CLIResearchTests(_TmpDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg = BridgeConfig(log_requests=False)

    def test_parser_accepts_research_shape(self) -> None:
        from freegem.cli import _build_parser

        args = _build_parser().parse_args(
            [
                "research",
                "http://a.test/1",
                "http://a.test/2",
                "--query",
                "Which?",
                "--format",
                "json",
            ]
        )
        self.assertEqual(args.urls, ["http://a.test/1", "http://a.test/2"])
        self.assertEqual(args.query, "Which?")
        self.assertEqual(args.format, "json")
        self.assertFalse(args.no_cache)

    def test_main_dispatch_and_output(self) -> None:
        from freegem.cli import main

        def fake_research_cli(cfg: BridgeConfig, **kwargs: Any) -> str:
            self.assertEqual(kwargs["query"], "Which option?")
            self.assertTrue(kwargs["no_cache"])
            return "cited answer [S1]\n\nSources:\n[S1] http://a.test/1"

        with (
            mock.patch("freegem.web_research.research_cli", fake_research_cli),
            mock.patch.dict("os.environ", {"FREEGEM_CACHE_DIR": str(self.tmp / "c")}, clear=False),
        ):
            buf = StringIO()
            with redirect_stdout(buf):
                rc = main(
                    [
                        "research",
                        "http://a.test/1",
                        "--query",
                        "Which option?",
                        "--no-cache",
                    ]
                )
        self.assertEqual(rc, 0)
        self.assertIn("cited answer [S1]", buf.getvalue())

    def test_main_error_path_returns_one(self) -> None:
        from freegem.cli import main

        def fake_research_cli(cfg: BridgeConfig, **kwargs: Any) -> str:
            raise ValueError("research requires a non-empty question")

        with mock.patch("freegem.web_research.research_cli", fake_research_cli):
            rc = main(["research", "http://a.test/1", "--query", "q", "--no-cache"])
        self.assertEqual(rc, 1)

    def test_research_cli_text_format_uses_default_cache_path_env(self) -> None:
        # research_cli builds its cache from FREEGEM_CACHE_DIR; point it at the
        # temp dir so the test never touches the real home directory.
        pages = {"http://a.test/1": _page("http://a.test/1", "T", "words")}

        def gemini(cfg: BridgeConfig, prompt: str) -> str:
            return "answer [S1]"

        env = {"FREEGEM_CACHE_DIR": str(self.tmp / "c")}
        with (
            mock.patch.dict("os.environ", env, clear=False),
            mock.patch("freegem.web_scraper.scrape_page", _fetcher(pages)),
            mock.patch("freegem.web_research._gemini_complete", gemini),
        ):
            out = research_cli(self.cfg, urls=["http://a.test/1"], query="q", no_cache=False)
        self.assertIn("answer [S1]", out)
        self.assertTrue((self.tmp / "c" / "research_cache.json").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
