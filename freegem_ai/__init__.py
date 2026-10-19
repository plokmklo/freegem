"""AI-assisted web scraper, crawler, and fetcher built on FreeGem.

freegem_ai wraps FreeGem's guarded fetch core (``freegem.web_scraper``,
``freegem.web_research``, ``freegem.crawler``) and adds AI-powered processing
on top through the Gemini bridge (``freegem.gemini.generate``):

* **AI cleanup** -- Gemini distills pages to their main content, dropping
  nav/cookie boilerplate (``ai_clean`` / ``freegem ai clean``).
* **AI summarization** -- per-page or multi-page summaries with key points.
* **AI extraction** -- schema-constrained field extraction from one or many pages.
* **AI Q&A** -- answer questions grounded in fetched pages, with verified quotes.
* **AI entity extraction** -- named entities, topics, sentiment per page.
* **AI crawl intelligence** -- distill large pages, summarize crawl results,
  discover relevant links from page content, score pages against a query.

The module reuses FreeGem's fetch machinery wholesale -- every URL goes
through the same SSRF guard, smart-fetch ladder, cache, and body caps. The
AI passes are bounded (configurable token budget) and clearly labeled:
AI-mediated output is never presented as a verbatim fetch, fabricated
quotes are marked UNVERIFIED (same pattern as FreeGem's research layer),
and a model answer that says it could not read a page is recorded as an
honest failure.

Usage:

    from freegem_ai import scrape, ai_research, ai_search, ai_extract

    cfg = load_config()
    # AI-cleaned scrape
    results = scrape(cfg, ["https://example.com/article"], ai_clean=True)
    # Multi-page research with AI extraction
    result = ai_research(cfg, "Compare Rust and Go for web backends",
                         ["https://example.com/rust", "https://example.com/go"],
                         schema={"language": "string", "use_case": "string"})
    # AI-powered crawl
    from freegem_ai.crawler import ai_crawl
    results = ai_crawl(cfg, ["https://docs.example.com/"], "article", 50)
"""

from __future__ import annotations

__version__ = "1.0.0"

# Core data types and AI helpers
from .core import (
    DEFAULT_AI_BUDGET,
    DEFAULT_AI_MAX_OUTPUT_CHARS,
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    AIError,
    AIPageResult,
    AIResult,
    _truncate_text,
    _wrap_untrusted,
    ai_clean_page,
    ai_extract_entities,
    ai_questionnaire,
    ai_summarize,
    run_ai_pass,
)

# AI-powered crawl intelligence
from .crawler import (
    AICrawlRecord,
    AICrawlResult,
    ai_crawl,
    ai_crawl_discover_links,
    ai_score_page,
)

# AI-assisted research
from .research import (
    AIExtractResult,
    AISearchResult,
    ResearchSourceAI,
    Summaries,
    ai_extract,
    ai_research,
    ai_search,
)

# High-level scraper API
from .scraper import (
    AIBatchItem,
    AIBatchResult,
    ai_ask_page,
    scrape,
    scrape_batch,
)

__all__ = [
    "DEFAULT_AI_BUDGET",
    "DEFAULT_AI_MAX_OUTPUT_CHARS",
    "UNTRUSTED_CLOSE",
    "UNTRUSTED_OPEN",
    # scraper
    "AIBatchItem",
    "AIBatchResult",
    # crawler
    "AICrawlRecord",
    "AICrawlResult",
    # core
    "AIError",
    # research
    "AIExtractResult",
    "AIPageResult",
    "AIResult",
    "AISearchResult",
    "ResearchSourceAI",
    "Summaries",
    "_truncate_text",
    "_wrap_untrusted",
    "ai_ask_page",
    "ai_clean_page",
    "ai_crawl",
    "ai_crawl_discover_links",
    "ai_extract",
    "ai_extract_entities",
    "ai_questionnaire",
    "ai_research",
    "ai_score_page",
    "ai_search",
    "ai_summarize",
    "run_ai_pass",
    "scrape",
    "scrape_batch",
]
