"""The crawler subsystem: a bounded, robots-aware, resumable local crawler.

Laid out so each concern is one importable module:

- :mod:`.urls`       normalization, scope rules, page budget
- :mod:`.hardening`  redaction, safe XML, DOM caps, resource limits, decoding
- :mod:`.frontier`   the persistent SQLite work list (resume = reopen the file)
- :mod:`.fetch`      one guarded GET with a complete outcome taxonomy
- :mod:`.extract`    CSS / XPath-subset / JSON-path field extraction + schemas
- :mod:`.output`     JSONL / JSON / CSV writers
- :mod:`.runner`     plan() (no network) and run() (the crawl loop)
- :mod:`.campaign`   bulk fetch campaign across many sites/encodings/states

The CLI lives in ``freegem.cli`` under ``freegem crawl plan|run|resume|status``.
"""

from __future__ import annotations

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
    "extract_items",
    "load_schema",
    "make_writer",
    "normalize_url",
    "parse_xml",
    "plan",
    "redact_secrets",
    "run",
    "url_key",
]
