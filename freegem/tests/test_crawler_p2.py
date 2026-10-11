"""Unit tests for P2 crawler features:
1. Selenium/Grid adapter
2. External queues (Memory, Redis, RabbitMQ)
3. PostgreSQL and Object Storage sinks
4. Schema-constrained LLM extraction
5. Chunking, relevance, NLP, and telemetry
6. Recrawling & adaptive scheduling
7. Deduplication & search-index sinks
8. MediaWiki, Docs, arXiv, and Feed adapters
9. JS/XHR discovery & best-first scoring
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from freegem.crawler.dedup import Deduplicator, jaccard_similarity
from freegem.crawler.fetch_selenium import SeleniumFetcher, fetch_selenium
from freegem.crawler.intelligence import (
    Telemetry,
    chunk_text,
    extract_keywords,
    select_relevant_chunks,
)
from freegem.crawler.js_discovery import compute_best_first_score, discover_xhr_endpoints
from freegem.crawler.llm_extract import LLMExtractionConfig, LLMExtractor
from freegem.crawler.queue import (
    MemoryQueueAdapter,
    QueueItem,
    RedisQueueAdapter,
)
from freegem.crawler.recrawl import AdaptiveScheduler, RecrawlState
from freegem.crawler.search_index import SearchIndexConfig, SearchIndexSink
from freegem.crawler.sources import (
    DocumentationAdapter,
    FeedAdapter,
)
from freegem.crawler.storage import (
    ObjectStorageConfig,
    ObjectStorageSink,
    PostgresConfig,
    PostgresSink,
)


class TestSeleniumAdapter(unittest.TestCase):
    def test_ssrf_blocked(self) -> None:
        fetcher = SeleniumFetcher()
        outcome = fetcher.fetch("http://127.0.0.1/private")
        self.assertEqual(outcome.kind, "ssrf")

    def test_missing_selenium_package(self) -> None:
        with patch.dict("sys.modules", {"selenium": None}):
            outcome = fetch_selenium("https://example.com")
            self.assertEqual(outcome.kind, "network")
            self.assertIn("selenium package is not installed", outcome.error)


class TestQueueAdapters(unittest.TestCase):
    def test_memory_queue(self) -> None:
        q = MemoryQueueAdapter()
        self.assertEqual(q.len(), 0)
        item = QueueItem(url="https://example.com/page1", depth=1)
        q.push(item)
        self.assertEqual(q.len(), 1)
        popped = q.pop()
        self.assertIsNotNone(popped)
        self.assertEqual(popped.url, "https://example.com/page1")
        self.assertEqual(q.len(), 0)

    def test_redis_queue_mocked(self) -> None:
        mock_redis_module = MagicMock()
        mock_client = MagicMock()
        mock_redis_module.Redis.from_url.return_value = mock_client
        mock_client.rpop.return_value = json.dumps({"url": "https://example.com/redis", "depth": 0, "priority": 5, "payload": None})
        mock_client.llen.return_value = 1

        with patch.dict("sys.modules", {"redis": mock_redis_module}):
            rq = RedisQueueAdapter()
            rq.push(QueueItem(url="https://example.com/redis"))
            self.assertTrue(mock_client.lpush.called)

            popped = rq.pop()
            self.assertIsNotNone(popped)
            self.assertEqual(popped.url, "https://example.com/redis")
            self.assertEqual(rq.len(), 1)


class TestStorageSinks(unittest.TestCase):
    def test_postgres_sink_mocked(self) -> None:
        mock_psycopg2 = MagicMock()
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_psycopg2.connect.return_value = mock_conn
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        with patch.dict("sys.modules", {"psycopg2": mock_psycopg2}):
            sink = PostgresSink(PostgresConfig())
            sink.write({"url": "https://example.com", "title": "Test Page"})
            self.assertTrue(mock_cur.execute.called)

    def test_object_storage_sink_mocked(self) -> None:
        mock_boto3 = MagicMock()
        mock_s3 = MagicMock()
        mock_boto3.client.return_value = mock_s3

        with patch.dict("sys.modules", {"boto3": mock_boto3}):
            sink = ObjectStorageSink(ObjectStorageConfig(bucket="test-bucket"))
            key = sink.write({"url": "https://example.com", "content": "Hello"})
            self.assertTrue(key.startswith("items/"))
            self.assertTrue(mock_s3.put_object.called)


class TestLLMExtractor(unittest.TestCase):
    def test_llm_extractor_custom_fn(self) -> None:
        def fake_completion(prompt: str) -> str:
            return '```json\n[{"name": "Product A", "price": 99.99}]\n```'

        config = LLMExtractionConfig(provider="custom", custom_completion_fn=fake_completion)
        extractor = LLMExtractor(config)

        schema = {
            "type": "object",
            "properties": {"name": {"type": "string"}, "price": {"type": "number"}},
            "required": ["name", "price"],
        }
        items = extractor.extract("Some raw document text...", schema)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "Product A")
        self.assertEqual(items[0]["price"], 99.99)


class TestIntelligenceModule(unittest.TestCase):
    def test_chunking_and_relevance(self) -> None:
        text = "First paragraph about space exploration.\n\nSecond paragraph about Python programming and web scraping."
        chunks = chunk_text(text, chunk_size=30, strategy="paragraph")
        self.assertEqual(len(chunks), 2)

        relevant = select_relevant_chunks(text, query="Python web scraping", top_k=1, chunk_size=30)
        self.assertEqual(len(relevant), 1)
        self.assertIn("Python", relevant[0])

    def test_keywords_and_telemetry(self) -> None:
        text = "Gemini AI model is an advanced AI model developed for research."
        keywords = extract_keywords(text, top_n=3)
        self.assertTrue(any(kw == "model" for kw, _ in keywords))

        tel = Telemetry()
        tel.record_usage("gemini-2.5-flash", 1000, 500)
        self.assertGreater(tel.total_cost_usd, 0.0)


class TestRecrawling(unittest.TestCase):
    def test_adaptive_scheduler(self) -> None:
        scheduler = AdaptiveScheduler(default_interval=86400.0)
        state = RecrawlState(url="https://example.com")

        # Initial check
        changed, state = scheduler.update_and_reschedule(
            state, new_etag="v1", new_last_modified=None, new_content="content v1", now=1000.0
        )
        self.assertFalse(changed)

        # Unchanged check -> interval increases
        changed, state = scheduler.update_and_reschedule(
            state, new_etag="v1", new_last_modified=None, new_content="content v1", now=2000.0
        )
        self.assertFalse(changed)
        self.assertGreater(state.current_interval_sec, 86400.0)

        # Content changed -> interval decreases
        changed, state = scheduler.update_and_reschedule(
            state, new_etag="v2", new_last_modified=None, new_content="content v2 updated", now=3000.0
        )
        self.assertTrue(changed)


class TestDedupAndSearchIndex(unittest.TestCase):
    def test_jaccard_and_simhash_dedup(self) -> None:
        text1 = "The quick brown fox jumps over the lazy dog"
        text2 = "The fast brown fox leaps over the lazy hound"
        sim = jaccard_similarity(text1, text2, k=1)
        self.assertGreater(sim, 0.2)

        dedup = Deduplicator(simhash_threshold=10)
        is_dup1, _ = dedup.is_duplicate("https://e.com/1", text1)
        self.assertFalse(is_dup1)

        is_dup2, match_url = dedup.is_duplicate("https://e.com/2", text1)
        self.assertTrue(is_dup2)
        self.assertEqual(match_url, "https://e.com/1")

    @patch("urllib.request.urlopen")
    def test_search_index_sink(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        sink = SearchIndexSink(SearchIndexConfig(node_url="http://localhost:9200", index_name="test_index"))
        ok = sink.index_document("doc_1", {"title": "Search Test", "url": "https://example.com"})
        self.assertTrue(ok)


class TestSourcesAdapter(unittest.TestCase):
    def test_doc_adapter_sitemap(self) -> None:
        xml = "<urlset><url><loc>https://docs.example.com/page1</loc></url></urlset>"
        urls = DocumentationAdapter.parse_sitemap(xml)
        self.assertIn("https://docs.example.com/page1", urls)

    def test_feed_adapter_rss(self) -> None:
        rss_xml = """<rss version="2.0">
            <channel>
                <item>
                    <title>Post 1</title>
                    <link>https://blog.example.com/1</link>
                    <description>Hello RSS</description>
                </item>
            </channel>
        </rss>"""
        articles = FeedAdapter.parse_feed(rss_xml)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0].title, "Post 1")


class TestJSDiscoveryAndBestFirstScoring(unittest.TestCase):
    def test_xhr_discovery(self) -> None:
        js_code = """
        function loadData() {
            fetch("/api/v1/users");
            axios.get('https://api.example.com/v2/items');
        }
        """
        endpoints = discover_xhr_endpoints(js_code, base_url="https://example.com")
        self.assertIn("https://example.com/api/v1/users", endpoints)
        self.assertIn("https://api.example.com/v2/items", endpoints)

    def test_best_first_scoring(self) -> None:
        score_base = compute_best_first_score("https://example.com/about", depth=0)
        score_deeper = compute_best_first_score("https://example.com/a/b/c", depth=3)
        self.assertGreater(score_base, score_deeper)

        score_kw = compute_best_first_score("https://example.com/docs/api", depth=0, keywords=["api", "docs"])
        self.assertGreater(score_kw, score_base)


if __name__ == "__main__":
    unittest.main()
