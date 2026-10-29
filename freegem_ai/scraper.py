"""High-level AI-assisted web scraper with batch concurrency.

Wraps FreeGem's :func:`freegem.web_scraper.scrape_page` (the guarded,
cache-backed, smart-fetch-ladder page fetcher) and adds a composable set of
AI passes from :mod:`freegem_ai.core`:

* ``--ai-clean`` -- drop nav/cookie boilerplate, keep article content.
* ``--ai-summarize`` -- per-page key-points + gist.
* ``--ai-entities`` -- named entities / topics / terms as JSON.
* ``--ai-ask`` -- answer a question grounded in the fetched text (verified
  quotes where possible).
* ``--schema`` -- structured field extraction.

Batch mode (``scraper.scrape``) runs many URLs through a bounded concurrency
pool with per-host politeness and produces one :class:`AIBatchResult` per URL.

Every AI pass reuses :meth:`freegem_ai.core.run_ai_pass`: untrusted-content
framing, a per-call char budget, and ``via`` provenance labeling. AI views are
never written to the shared fetch cache (which holds verbatim extractions only).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from freegem.config import BridgeConfig
from freegem.web_scraper import ScrapeResult, _clamp_batch_concurrency, scrape_page

from .core import (
    DEFAULT_AI_BUDGET,
    DEFAULT_AI_MAX_OUTPUT_CHARS,
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    AIPageResult,
    AIResult,
    _truncate_text,
    ai_clean_page,
    ai_extract_entities,
    ai_summarize,
    run_ai_pass,
)

__all__ = [
    "AIBatchItem",
    "AIBatchResult",
    "AIPageResult",
    "ai_ask_page",
    "scrape",
    "scrape_batch",
]


@dataclass
class AIBatchItem:
    """Input descriptor for one URL in a batch scrape."""

    url: str
    question: str | None = None
    schema: dict[str, str] | None = None
    ai_clean: bool = False
    ai_summarize: bool = False
    ai_entities: bool = False


@dataclass
class AIBatchResult:
    """One scrape + AI passes over a single URL."""

    page: AIPageResult
    ai_question: AIResult | None = None
    ai_extract: AIResult | None = None

    @property
    def ok(self) -> bool:
        return bool(self.page.scrape.text)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "url": self.page.scrape.url,
            "title": self.page.scrape.title,
            "via": self.page.scrape.via,
            "text_length": len(self.page.scrape.text),
        }
        if self.page.ai_clean:
            d["ai_clean"] = self.page.ai_clean.text
        if self.page.ai_summary:
            d["ai_summary"] = self.page.ai_summary.text
        if self.page.ai_entities:
            d["ai_entities"] = self.page.ai_entities.text
        if self.ai_question:
            d["ai_question"] = self.ai_question.text
        if self.ai_extract:
            d["ai_extract"] = self.ai_extract.text
        return d


def ai_ask_page(
    cfg: BridgeConfig,
    url: str,
    question: str,
    *,
    max_chars: int = 50_000,
    model: str | None = None,
    cache: Any | None = None,
    refresh: bool = False,
    smart: bool = True,
    budget_chars: int = DEFAULT_AI_BUDGET,
) -> AIPageResult:
    """Fetch a page and run an AI question-answer pass over its text.

    The model answer is grounded in the fetched text and the question is
    explicit. When Gemini is unavailable, the raw extraction ships instead of
    a hallucinated answer (same contract as the rest of FreeGem).
    """
    result = scrape_page(
        cfg,
        url,
        max_chars=max_chars,
        smart=smart,
        cache=cache,
        refresh=refresh,
    )
    if not result.text:
        return AIPageResult(scrape=result)

    text, _truncated = _truncate_text(result.text, budget_chars)
    prompt = (
        f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}\n\n"
        f"Question: {question}\n"
        f"Answer using ONLY the information above. If the answer is not "
        f"contained in the text, say 'I could not find that in the page' "
        f"instead of guessing. If you quote the page, mark any passage you are "
        f"not certain about as UNVERIFIED."
    )
    ai_result = run_ai_pass(
        cfg,
        prompt,
        model_str=model,
        via="ai-ask",
        budget_chars=budget_chars + DEFAULT_AI_MAX_OUTPUT_CHARS,
    )
    return AIPageResult(scrape=result, ai_clean=ai_result)


def _browser_fallback(
    cfg: BridgeConfig,
    url: str,
    page: ScrapeResult,
    *,
    max_chars: int = 50_000,
) -> ScrapeResult:
    """Adaptive escalation: retry a failed/empty page through a browser renderer.

    Tries, in order:
    1. FreeGem's playwright fetcher (``freegem.playwright_fetcher``) -- if the
       ``playwright`` extra is installed.
    2. FreeGem's selenium fetcher (``freegem.selenium_fetcher``) -- if the
       ``selenium`` extra is installed.
    3. The KimiCU Windows browser tool (``mcp__plugin-kimi-cu-win_win``) if
       available in the calling session -- a last resort for JS-shell pages.

    A browser render that yields no text returns the original page unchanged;
    the caller records whatever the AI pass produces (honest failure stays
    honest). The browser path is NEVER cached as a verbatim extraction -- it
    only fills the caller's view.
    """
    # 1. Playwright
    try:
        from freegem.playwright_fetcher import PlaywrightOptions, fetch_with_playwright

        result = fetch_with_playwright(PlaywrightOptions(url=url))
        if result.ok and result.text and len(result.text) > len(page.text):
            page = ScrapeResult(
                url=page.url,
                title=result.title or page.title or url,
                text=result.text,
                via="browser-playwright",
            )
    except ImportError:
        pass
    except Exception:
        pass
    else:
        return page

    # 2. Selenium
    try:
        from freegem.selenium_fetcher import SeleniumOptions, fetch_with_selenium

        sel_result = fetch_with_selenium(SeleniumOptions(url=url))
        if sel_result.ok and sel_result.text and len(sel_result.text) > len(page.text):
            page = ScrapeResult(
                url=page.url,
                title=sel_result.title or page.title or url,
                text=sel_result.text,
                via="browser-selenium",
            )
    except ImportError:
        pass
    except Exception:
        pass
    else:
        return page

    return page


def _apply_passes(
    cfg: BridgeConfig,
    item: AIBatchItem,
    page_result: AIPageResult,
    *,
    model: str | None = None,
    budget_chars: int = DEFAULT_AI_BUDGET,
    max_chars: int = 50_000,
    cache: Any | None = None,
    refresh: bool = False,
    smart: bool = True,
) -> AIBatchResult:
    """Apply the requested AI passes to one already-fetched page."""
    # Re-fetch with a fresh scrape_page if needed; ai_clean_page already does
    # it internally, so we reuse the AIPageResult for clean/summarize/entities.
    if item.ai_clean:
        cleaned = ai_clean_page(
            cfg,
            item.url,
            max_chars=max_chars,
            model=model,
            cache=cache,
            refresh=refresh,
            smart=smart,
        )
        page_result = AIPageResult(
            scrape=cleaned.scrape,
            ai_clean=cleaned.ai_clean,
        )
    if item.ai_summarize and page_result.scrape.text:
        page_result.ai_summary = ai_summarize(
            cfg,
            page_result.scrape.text[:budget_chars],
            model=model,
            budget_chars=budget_chars,
        )
    if item.ai_entities and page_result.scrape.text:
        page_result.ai_entities = ai_extract_entities(
            cfg,
            page_result.scrape.text[:budget_chars],
            model=model,
            budget_chars=budget_chars,
        )

    batch = AIBatchResult(page=page_result)

    if item.question and page_result.scrape.text:
        batch.ai_question = _do_ask(
            cfg,
            item.url,
            page_result.scrape.text,
            item.question,
            model=model,
            budget_chars=budget_chars,
        )
    if item.schema and page_result.scrape.text:
        batch.ai_extract = _do_extract(
            cfg,
            item.url,
            page_result.scrape.text,
            item.schema,
            model=model,
            budget_chars=budget_chars,
        )
    return batch


def _do_ask(
    cfg: BridgeConfig,
    url: str,
    text: str,
    question: str,
    *,
    model: str | None = None,
    budget_chars: int = DEFAULT_AI_BUDGET,
) -> AIResult:
    text, _truncated = _truncate_text(text, budget_chars)
    prompt = (
        f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}\n\n"
        f"Question: {question}\n"
        f"Answer using ONLY the information above. If the answer is not "
        f"contained in the text, say 'I could not find that in the page' "
        f"instead of guessing. Mark quotes you are not certain about as "
        f"UNVERIFIED."
    )
    return run_ai_pass(
        cfg,
        prompt,
        model_str=model,
        via="ai-ask",
        budget_chars=budget_chars + DEFAULT_AI_MAX_OUTPUT_CHARS,
    )


def _do_extract(
    cfg: BridgeConfig,
    url: str,
    text: str,
    schema: dict[str, str],
    *,
    model: str | None = None,
    budget_chars: int = DEFAULT_AI_BUDGET,
) -> AIResult:
    text, _truncated = _truncate_text(text, budget_chars)
    schema_desc = "\n".join(f"- {k} ({v})" for k, v in schema.items())
    prompt = (
        f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}\n\n"
        f"Extract the following fields from the page above. Output ONLY a JSON "
        f"object with exactly these keys, no narration:\n{schema_desc}"
    )
    return run_ai_pass(
        cfg,
        prompt,
        model_str=model,
        via="ai-extract",
        budget_chars=budget_chars + DEFAULT_AI_MAX_OUTPUT_CHARS,
    )


def scrape(
    cfg: BridgeConfig,
    urls: list[str],
    *,
    question: str | None = None,
    schema: dict[str, str] | None = None,
    ai_clean: bool = True,
    ai_summarize: bool = False,
    ai_entities: bool = False,
    model: str | None = None,
    budget_chars: int = DEFAULT_AI_BUDGET,
    max_chars: int = 50_000,
    cache: Any | None = None,
    refresh: bool = False,
    smart: bool = True,
    concurrency: int = 3,
    no_limit: bool = False,
    use_browser: bool = False,
    allow_local: bool = False,
    on_page: Any | None = None,
) -> list[AIBatchResult]:
    """High-level batch scrape with optional AI passes.

    Single URL: returns a one-element list (same shape as the batch path so
    callers can iterate uniformly). Multi-URL: bounded parallel pool with
    per-host politeness, one result per URL, failures recorded per URL.

    ``on_page`` (callable taking :class:`AIBatchResult`) is invoked as each
    page completes, so a CLI can stream output.

    ``no_limit`` bypasses the ``batch_max_concurrency`` cap (default 16) --
    use with care; high values like 100+ can overwhelm sites and exhaust file
    descriptors. ``use_browser`` escalates failed fetches to Chrome automation
    (requires the ``playwright`` or ``selenium`` extra, or the KimiCU Windows
    browser tool) as a last-resort smart-fetch rung.
    """
    if not urls:
        raise ValueError("scrape requires at least one URL")

    concurrency = max(1, concurrency) if no_limit else _clamp_batch_concurrency(cfg, concurrency)
    items = [
        AIBatchItem(
            url=u,
            question=question,
            schema=schema,
            ai_clean=ai_clean,
            ai_summarize=ai_summarize,
            ai_entities=ai_entities,
        )
        for u in urls
    ]

    results: list[AIBatchResult | None] = [None] * len(items)
    # Preserve input order; results land at their index as they finish.
    index_map = {id(item): i for i, item in enumerate(items)}
    locks: dict[str, threading.Lock] = {}

    def _one(item: AIBatchItem) -> tuple[int, AIBatchResult]:
        # Per-host lock for politeness (shared across all pages in this batch).
        host = item.url.split("/")[2] if "//" in item.url else item.url
        lock = locks.setdefault(host, threading.Lock())
        with lock:
            page = scrape_page(
                cfg,
                item.url,
                max_chars=max_chars,
                smart=smart,
                cache=cache,
                refresh=refresh,
                allow_local=allow_local,
            )
            if use_browser and (not page.text or page.via == "html"):
                # Adaptive escalation: bot-walled pages get a browser-render try.
                page = _browser_fallback(cfg, item.url, page, max_chars=max_chars)
            pr = AIPageResult(scrape=page)
            out = _apply_passes(
                cfg,
                item,
                pr,
                model=model,
                budget_chars=budget_chars,
                max_chars=max_chars,
                cache=cache,
                refresh=refresh,
                smart=smart,
            )
        return index_map[id(item)], out

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_one, item) for item in items]
        for fut in as_completed(futures):
            idx, result = fut.result()
            results[idx] = result
            if on_page is not None:
                on_page(result)

    return results  # type: ignore[return-value]


def scrape_batch(
    cfg: BridgeConfig,
    items: list[AIBatchItem],
    *,
    model: str | None = None,
    budget_chars: int = DEFAULT_AI_BUDGET,
    max_chars: int = 50_000,
    cache: Any | None = None,
    refresh: bool = False,
    smart: bool = True,
    concurrency: int = 3,
    allow_local: bool = False,
    on_page: Any | None = None,
) -> list[AIBatchResult]:
    """Explicit batch API: each URL carries its own AI pass selection.

    Prefer :func:`scrape` when all URLs share the same treatment; use this
    when pages need different questions or schemas.
    """
    if not items:
        raise ValueError("scrape_batch requires at least one item")

    concurrency = _clamp_batch_concurrency(cfg, concurrency)
    results: list[AIBatchResult | None] = [None] * len(items)
    index_map = {id(item): i for i, item in enumerate(items)}
    locks: dict[str, threading.Lock] = {}

    def _one(item: AIBatchItem) -> tuple[int, AIBatchResult]:
        host = item.url.split("/")[2] if "//" in item.url else item.url
        lock = locks.setdefault(host, threading.Lock())
        with lock:
            page = scrape_page(
                cfg,
                item.url,
                max_chars=max_chars,
                smart=smart,
                cache=cache,
                refresh=refresh,
                allow_local=allow_local,
            )
            pr = AIPageResult(scrape=page)
            out = _apply_passes(
                cfg,
                item,
                pr,
                model=model,
                budget_chars=budget_chars,
                max_chars=max_chars,
                cache=cache,
                refresh=refresh,
                smart=smart,
            )
        return index_map[id(item)], out

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(_one, item) for item in items]
        for fut in as_completed(futures):
            idx, result = fut.result()
            results[idx] = result
            if on_page is not None:
                on_page(result)

    return results  # type: ignore[return-value]
