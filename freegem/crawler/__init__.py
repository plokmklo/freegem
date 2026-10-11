"""The crawler subsystem: a bounded, resumable local crawler.

robots.txt is never fetched and never consulted anywhere in this package:
it is attacker-controlled text from the same origin as the pages being read
and a documented prompt-injection/policy-poisoning vector. Politeness is
per-host pacing; attacks stay out of scope.

Laid out so each concern is one importable module:

- :mod:`.urls`       normalization, scope rules, page budget
- :mod:`.hardening`  redaction, safe XML, DOM caps, resource limits, decoding
- :mod:`.frontier`   the persistent SQLite work list (resume = reopen the file)
- :mod:`.fetch`      one guarded GET with a complete outcome taxonomy
- :mod:`.resilient`  the escalation ladder (headers, backoff, fallbacks)
- :mod:`.distill`    important-only distillation of oversized pages
- :mod:`.extract`    CSS / XPath-subset / JSON-path field extraction + schemas
- :mod:`.output`     JSONL / JSON / CSV writers
- :mod:`.runner`     plan() (no network) and run() (the crawl loop)
- :mod:`.campaign`   bulk fetch campaign across many sites/encodings/states

The CLI lives in ``freegem.cli`` under ``freegem crawl plan|run|resume|status``.
"""

from __future__ import annotations

from .distill import distill_text, extractive_summary
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
from .urls import Budget, Scope, ScopeVerdict, normalize_url, url_key

__all__ = [
    "Budget",
    "Counters",
    "CrawlSpec",
    "ExtractionStats",
    "FetchOutcome",
    "Field",
    "FieldError",
    "Frontier",
    "FrontierItem",
    "FrontierStats",
    "HostPacer",
    "HtmlDom",
    "Node",
    "PlanReport",
    "ResourceLimits",
    "Schema",
    "Scope",
    "ScopeVerdict",
    "UnsafeXMLError",
    "crawl_fetch",
    "decode_body",
    "distill_text",
    "extract_items",
    "extractive_summary",
    "fetch_resilient",
    "load_schema",
    "make_writer",
    "normalize_url",
    "parse_xml",
    "plan",
    "redact_secrets",
    "run",
    "url_key",
]
