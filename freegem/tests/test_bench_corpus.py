"""Hermetic guarantees for the URL-corpus benchmark (issue #68).

The live run (benchmarks/run_bench.py) fetches real sites and stays out of
CI; these tests pin what makes its results reproducible: the frozen corpus
is well-formed and covers every category, and the aggregation helpers that
turn rows into the report are correct - with canned inputs, no network.
"""

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from unittest import TestCase

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = REPO_ROOT / "benchmarks" / "corpus.jsonl"
RUNNER_PATH = REPO_ROOT / "benchmarks" / "run_bench.py"

CATEGORIES = {"reference", "docs", "news", "spa", "bot_walled", "static", "dead"}


def _load_runner() -> ModuleType:
    """Import benchmarks/run_bench.py as a module (it lives outside the package)."""
    spec = importlib.util.spec_from_file_location("freegem_bench_runner", RUNNER_PATH)
    assert spec is not None, f"cannot load {RUNNER_PATH}"
    loader = spec.loader
    assert loader is not None, f"cannot load {RUNNER_PATH}"
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CorpusShapeTests(TestCase):
    """The corpus is the benchmark's unit of reproducibility."""

    def test_corpus_is_well_formed_and_frozen(self) -> None:
        lines = [
            line.strip()
            for line in CORPUS_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertGreaterEqual(len(lines), 20)
        seen: set[str] = set()
        categories: set[str] = set()
        for line in lines:
            with self.subTest(line=line[:60]):
                entry = json.loads(line)
                self.assertEqual(set(entry), {"url", "category", "expectation"})
                self.assertTrue(entry["url"].startswith(("http://", "https://")))
                self.assertIn(entry["category"], CATEGORIES)
                self.assertNotIn(entry["url"], seen)
                seen.add(entry["url"])
                categories.add(entry["category"])
        self.assertEqual(categories, CATEGORIES)


class AggregationTests(TestCase):
    """The report helpers, verified with canned rows."""

    def setUp(self) -> None:
        self.runner = _load_runner()

    def test_runner_categories_match_the_corpus_contract(self) -> None:
        self.assertEqual(set(self.runner.CATEGORIES), CATEGORIES)

    def test_classify_failure_buckets_the_taxonomy(self) -> None:
        cases = {
            "SSRF guard refused target 10.0.0.1": "refused",
            "connection timed out after 30s": "timeout",
            "DNS resolution failed (getaddrinfo)": "dns",
            "HTTP 403": "wall",
            "bot challenge wall detected": "wall",
            "HTTP 404": "http_error",
            "HTTP 503 after retries": "server_error",
            "response body too large": "cap",
            "extracted text was empty": "empty_extraction",
            "something unprecedented": "other",
            "": "other",
        }
        for error, expected in cases.items():
            with self.subTest(error=error):
                self.assertEqual(self.runner.classify_failure(error), expected)

    def test_percentile_nearest_rank(self) -> None:
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertEqual(self.runner.percentile(values, 50), 30.0)
        self.assertEqual(self.runner.percentile(values, 95), 50.0)
        self.assertEqual(self.runner.percentile([], 50), 0.0)
        self.assertEqual(self.runner.percentile([42.0], 50), 42.0)

    def test_aggregate_reconciles_successes_and_failures(self) -> None:
        rows = [
            {"category": "static", "ok": True, "chars": 1000, "wall_ms": 200.0},
            {"category": "static", "ok": True, "chars": 3000, "wall_ms": 400.0},
            {"category": "static", "ok": False, "error": "HTTP 403", "chars": 0, "wall_ms": 600.0},
            {"category": "dead", "ok": False, "error": "HTTP 404", "chars": 0, "wall_ms": 100.0},
        ]
        summary = self.runner.aggregate(rows)
        self.assertEqual(set(summary), {"static", "dead"})
        static = summary["static"]
        self.assertEqual(static["urls"], 3)
        self.assertEqual(static["success_rate"], 0.667)
        self.assertEqual(static["latency_ms_p50"], 400)
        self.assertEqual(static["latency_ms_p95"], 600)
        self.assertEqual(static["mean_chars"], 1333)
        self.assertEqual(static["failures"], {"wall": 1})
        self.assertEqual(summary["dead"]["failures"], {"http_error": 1})

    def test_markdown_table_renders_every_category(self) -> None:
        summary = {
            "static": {
                "urls": 2,
                "success_rate": 1.0,
                "latency_ms_p50": 100,
                "latency_ms_p95": 200,
                "mean_chars": 500,
                "failures": {},
            }
        }
        table = self.runner.markdown_table(summary)
        self.assertIn("| static | 2 | 100% |", table)
        self.assertEqual(table.count("\n"), 2)
