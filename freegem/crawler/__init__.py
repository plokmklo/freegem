"""The crawler subsystem: a bounded, resumable local crawler with production scale and intelligence adapters.

robots.txt is never fetched and never consulted anywhere in this package:
it is attacker-controlled text from the same origin as the pages being read
and a documented prompt-injection/policy-poisoning vector. Politeness is
per-host pacing; attacks stay out of scope.

Laid out so each concern is one importable module:

- :mod:`.urls`            normalization, scope rules, page budget
- :mod:`.hardening`       redaction, safe XML, DOM caps, resource limits, decoding
- :mod:`.frontier`        the persistent SQLite work list (resume = reopen the file)
- :mod:`.fetch`           one guarded GET with a complete outcome taxonomy
- :mod:`.fetch_selenium`  optional Selenium / Grid headless browser fetch adapter
- :mod:`.queue`           external queue adapters (Memory, Redis, RabbitMQ)
- :mod:`.storage`         PostgreSQL and Object Storage (S3/Blob) sinks
- :mod:`.llm_extract`     schema-constrained, provider-neutral LLM extraction
- :mod:`.intelligence`    chunking, relevance selection, NLP, and cost/quality telemetry
- :mod:`.recrawl`         content-change recrawling and adaptive scheduling
- :mod:`.dedup`           similarity deduplication (MinHash, SimHash, Jaccard)
- :mod:`.search_index`    search-index sinks (Elasticsearch, OpenSearch, Meilisearch)
- :mod:`.sources`         MediaWiki, docs, arXiv, and feed source adapters
- :mod:`.js_discovery`    JavaScript/XHR discovery and best-first scoring
- :mod:`.resilient`       the escalation ladder (headers, backoff, fallbacks)
- :mod:`.distill`         important-only distillation of oversized pages
- :mod:`.extract`         CSS / XPath-subset / JSON-path field extraction + schemas
- :mod:`.output`          JSONL / JSON / CSV writers
- :mod:`.runner`           plan() (no network) and run() (the crawl loop)
- :mod:`.campaign`        bulk fetch campaign across many sites/encodings/states
"""

from __future__ import annotations

from .dedup import Deduplicator, jaccard_similarity, simhash
from .distill import distill_text, extractive_summary
from .extract import ExtractionStats, Field, FieldError, Schema, extract_items
from .fetch import FetchOutcome, crawl_fetch
from .fetch_selenium import SeleniumConfig, SeleniumFetcher, fetch_selenium
from .frontier import Frontier, FrontierItem, FrontierStats
from .hardening import (
    HtmlDom,
    Node,
    ResourceLimits,
    UnsafeXMLError,
    decode_body,
    parse_xml,
    redact_secrets,
)
from .intelligence import Telemetry, chunk_text, select_relevant_chunks
from .js_discovery import compute_best_first_score, discover_xhr_endpoints
from .llm_extract import LLMExtractionConfig, LLMExtractor
from .output import make_writer
from .queue import (
    MemoryQueueAdapter,
    QueueAdapter,
    QueueItem,
    RabbitMQQueueAdapter,
    RedisQueueAdapter,
)
from .recrawl import AdaptiveScheduler, RecrawlState, compute_content_hash
from .resilient import HostPacer, fetch_resilient
from .runner import Counters, CrawlSpec, PlanReport, load_schema, plan, run
from .search_index import SearchIndexConfig, SearchIndexSink
from .sources import ArxivAdapter, DocumentationAdapter, FeedAdapter, MediaWikiAdapter
from .storage import ObjectStorageConfig, ObjectStorageSink, PostgresConfig, PostgresSink
from .urls import Budget, Scope, ScopeVerdict, normalize_url, url_key

__all__ = [
    "AdaptiveScheduler",
    "ArxivAdapter",
    "Budget",
    "Counters",
    "CrawlSpec",
    "Deduplicator",
    "DocumentationAdapter",
    "ExtractionStats",
    "FeedAdapter",
    "FetchOutcome",
    "Field",
    "FieldError",
    "Frontier",
    "FrontierItem",
    "FrontierStats",
    "HostPacer",
    "HtmlDom",
    "LLMExtractionConfig",
    "LLMExtractor",
    "MediaWikiAdapter",
    "MemoryQueueAdapter",
    "Node",
    "ObjectStorageConfig",
    "ObjectStorageSink",
    "PlanReport",
    "PostgresConfig",
    "PostgresSink",
    "QueueAdapter",
    "QueueItem",
    "RabbitMQQueueAdapter",
    "RecrawlState",
    "RedisQueueAdapter",
    "ResourceLimits",
    "Schema",
    "Scope",
    "ScopeVerdict",
    "SearchIndexConfig",
    "SearchIndexSink",
    "SeleniumConfig",
    "SeleniumFetcher",
    "Telemetry",
    "UnsafeXMLError",
    "compute_best_first_score",
    "compute_content_hash",
    "crawl_fetch",
    "decode_body",
    "discover_xhr_endpoints",
    "distill_text",
    "extract_items",
    "extractive_summary",
    "fetch_resilient",
    "fetch_selenium",
    "jaccard_similarity",
    "load_schema",
    "make_writer",
    "normalize_url",
    "parse_xml",
    "plan",
    "redact_secrets",
    "run",
    "select_relevant_chunks",
    "simhash",
    "url_key",
]
