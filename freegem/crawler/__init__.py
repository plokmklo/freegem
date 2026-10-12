"""The crawler subsystem: a bounded, resumable local crawler with production-scale adapters.

robots.txt is never fetched and never consulted anywhere in this package:
it is attacker-controlled text from the same origin as the pages being read
and a documented prompt-injection/policy-poisoning vector. Politeness is
per-host pacing; attacks stay out of scope.

Laid out so each concern is one importable module:

- :mod:`.urls`            normalization, scope rules, page budget
- :mod:`.hardening`       redaction, safe XML, DOM caps, resource limits, decoding
- :mod:`.frontier`        the persistent SQLite work list (resume = reopen the file)
- :mod:`.fetch`           one guarded GET with a complete outcome taxonomy
- :mod:`.resilient`       the escalation ladder (headers, backoff, fallbacks)
- :mod:`.distill`         important-only distillation of oversized pages
- :mod:`.extract`         CSS / XPath-subset / JSON-path field extraction + schemas
- :mod:`.output`          JSONL / JSON / CSV writers
- :mod:`.runner`          plan() (no network) and run() (the crawl loop)
- :mod:`.campaign`        bulk fetch campaign across many sites/encodings/states
- :mod:`.scheduling`      content-change recrawling and adaptive scheduling
- :mod:`.sources`         MediaWiki, arXiv, feed, and docs source adapters
- :mod:`.discovery`       JavaScript/XHR discovery and best-first scoring

Production-scale companions live one level up so the generic pieces stay
importable without the crawler:

- :mod:`freegem.dedup`          64-bit simhash + jaccard near-duplicate filter
- :mod:`freegem.search_sinks`   SQLite FTS5 / JSONL / null search-index sinks
- :mod:`freegem.text_pipeline`  chunking, relevance selection, NLP, telemetry
- :mod:`freegem.llm_extract`    schema-constrained, provider-neutral extraction
- :mod:`freegem.queues`         memory / sqlite / redis / amqp work queues
- :mod:`freegem.stores`         SQLite / PostgreSQL documents, file / S3 blobs
- :mod:`freegem.selenium_fetcher` optional Selenium/Grid rendering adapter
"""

from __future__ import annotations

from .discovery import BestFirstPrioritizer, ScoreWeights, discover_js_endpoints, score_url
from .distill import DISTILL_THRESHOLD_BYTES, distill_text, extractive_summary
from .extract import ExtractionStats, Field, FieldError, Schema, extract_items
from .fetch import FetchOutcome, crawl_fetch
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
from .output import make_writer
from .resilient import HostPacer, fetch_resilient
from .runner import Counters, CrawlSpec, PlanReport, load_schema, plan, run
from .scheduling import (
    ChangePolicy,
    RecrawlCandidate,
    adaptive_interval_sec,
    apply_recrawl_plan,
    count_changes,
    record_fetch,
    recrawl_candidates,
)
from .sources import (
    ArxivSource,
    DocsSource,
    FeedSource,
    MediaWikiSource,
    SourceItem,
    detect_source,
)
from .urls import Budget, Scope, ScopeVerdict, normalize_url, url_key

__all__ = [
    "DISTILL_THRESHOLD_BYTES",
    "ArxivSource",
    "BestFirstPrioritizer",
    "Budget",
    "ChangePolicy",
    "Counters",
    "CrawlSpec",
    "DocsSource",
    "ExtractionStats",
    "FeedSource",
    "FetchOutcome",
    "Field",
    "FieldError",
    "Frontier",
    "FrontierItem",
    "FrontierStats",
    "HostPacer",
    "HtmlDom",
    "MediaWikiSource",
    "Node",
    "PlanReport",
    "RecrawlCandidate",
    "ResourceLimits",
    "Schema",
    "Scope",
    "ScopeVerdict",
    "ScoreWeights",
    "SourceItem",
    "UnsafeXMLError",
    "adaptive_interval_sec",
    "apply_recrawl_plan",
    "count_changes",
    "crawl_fetch",
    "decode_body",
    "detect_source",
    "discover_js_endpoints",
    "distill_text",
    "extract_items",
    "extractive_summary",
    "fetch_resilient",
    "load_schema",
    "make_writer",
    "normalize_url",
    "parse_xml",
    "plan",
    "record_fetch",
    "recrawl_candidates",
    "redact_secrets",
    "run",
    "score_url",
    "url_key",
]
