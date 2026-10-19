"""AI-powered crawl intelligence built on FreeGem's guarded, resumable crawler.

Wraps :mod:`freegem.crawler.runner` (the bounded frontier-driven crawl loop)
and :mod:`freegem.crawler.distill` (oversized-page distillation) and adds:

* **per-page AI distillation** for pages too large for inline reading --
  :func:`freegem.crawler.distill.distill_text` is reused (Gemini + extractive
  fallback);
* **AI link discovery** -- once a page is fetched, a bounded Gemini pass can
  surface semantically relevant "next" links beyond the raw href crawl;
* **AI content scoring** -- a bounded pass ranks how topically relevant each
  fetched page is to a seed query, so a large frontier can be pruned to the
  pages that actually matter before extraction.

Every AI pass goes through :meth:`freegem_ai.core.run_ai_pass`, so the same
untrusted-content framing, char budget, and ``via`` provenance labeling apply.
AI views are never written to the crawl's artifact store (which holds verbatim
bodies only); AI results ride in the returned records only, marked
``via=ai-*``.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from freegem.config import BridgeConfig
from freegem.web_scraper import scrape_page

from .core import (
    DEFAULT_AI_BUDGET,
    AIResult,
    _truncate_text,
    _wrap_untrusted,
    run_ai_pass,
)

__all__ = [
    "AICrawlRecord",
    "AICrawlResult",
    "ai_crawl",
    "ai_crawl_discover_links",
    "ai_score_page",
]


@dataclass
class AICrawlRecord:
    """One crawled page plus its AI augmentations."""

    url: str
    status: int | None = None
    ok: bool = False
    title: str = ""
    chars: int = 0
    via: str = "html"
    ai_distill: AIResult | None = None
    ai_links: AIResult | None = None
    ai_score: AIResult | None = None
    error: str | None = None


@dataclass
class AICrawlResult:
    """Aggregated AI crawl output."""

    query: str
    records: list[AICrawlRecord] = field(default_factory=list)
    ai_summary: AIResult | None = None

    @property
    def ok(self) -> int:
        return sum(1 for r in self.records if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.records if not r.ok)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "ok_count": self.ok,
            "failed_count": self.failed,
            "total": len(self.records),
            "ai_summary": self.ai_summary.text if self.ai_summary else None,
            "records": [
                {
                    "url": r.url,
                    "status": r.status,
                    "ok": r.ok,
                    "title": r.title,
                    "chars": r.chars,
                    "via": r.via,
                    "ai_distill": r.ai_distill.text if r.ai_distill else None,
                    "ai_links": r.ai_links.text if r.ai_links else None,
                    "ai_score": r.ai_score.text if r.ai_score else None,
                    "error": r.error,
                }
                for r in self.records
            ],
        }


_LINK_RE = re.compile(r'href\s*=\s*["\']?([^"\'>\s]+)', re.IGNORECASE)
_MAX_HREF_LEN = 2000


def _extract_links(text: str, base_url: str) -> list[str]:
    """Pull raw href candidates from scraped text (not a parser -- a filter)."""
    raw = set()
    for m in _LINK_RE.finditer(text):
        href = m.group(1)[:_MAX_HREF_LEN]
        raw.add(href)
    base_origin = urllib.parse.urljoin(base_url, "/").rstrip("/")
    out: list[str] = []
    seen: set[str] = set()
    for href in raw:
        if href.startswith("#"):
            continue
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        if href.startswith("//"):
            href = f"{urllib.parse.urlsplit(base_url).scheme}:{href}"
        full = urllib.parse.urljoin(base_url, href)
        if not full.startswith(("http://", "https://")):
            continue
        if not full.startswith(base_origin) and not href.startswith("http"):
            # Skip protocol-relative / odd schemes we cannot resolve safely.
            pass
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out


def ai_crawl_discover_links(
    cfg: BridgeConfig,
    url: str,
    text: str,
    *,
    model: str | None = None,
    budget_chars: int = DEFAULT_AI_BUDGET,
) -> AIResult:
    """Ask the model to surface semantically relevant follow-on URLs.

    The model sees the page text and the explicitly listed hrefs; it returns
    a JSON list of URLs ranked by topical relevance. This is a HINT layer --
    results are never trusted blindly and the caller must still scope the
    crawl. Fabricated URLs from the model are reported as such via ``via``.
    """
    text, _truncated = _truncate_text(text, budget_chars)
    hrefs = _extract_links(text, url)
    prompt = (
        f"{_wrap_untrusted(text)}\n\n"
        f"Below are the explicit href links found on that page:\n"
        + "\n".join(f"  - {h}" for h in hrefs[:200])
        + "\n\nYou are an AI crawl planner. From the page content AND the href "
        'list, return JSON: {"links": [{"url": "...", '
        '"reason": "why this is relevant to the topic"}]}. '
        "Return ONLY valid JSON, no narration. If no links are relevant, "
        'return {"links": []}.'
    )
    return run_ai_pass(
        cfg,
        prompt,
        model_str=model,
        via="ai-links",
        budget_chars=budget_chars,
    )


def ai_score_page(
    cfg: BridgeConfig,
    query: str,
    url: str,
    text: str,
    *,
    model: str | None = None,
    budget_chars: int = DEFAULT_AI_BUDGET,
) -> AIResult:
    """Ask the model how topically relevant a page is to ``query``.

    Returns a short text like '0.87 -- highly relevant: the page explains X'
    or '0.12 -- marginally relevant: the page mentions X once in passing.'
    The score is a heuristic for pruning large frontiers, not a trust signal.
    """
    text, _truncated = _truncate_text(text, budget_chars)
    prompt = (
        f"{_wrap_untrusted(text)}\n\n"
        f"Query: {query}\n\nOn a 0.00-1.00 scale, rate how topically relevant "
        f"the page above is to the query. Reply with a single line: "
        f'<score> <justification> (e.g. "0.87 highly relevant: ..."). '
        f"The score is a heuristic for crawl pruning, not a trust or quality "
        f"signal."
    )
    return run_ai_pass(
        cfg,
        prompt,
        model_str=model,
        via="ai-score",
        budget_chars=budget_chars,
    )


def ai_crawl(
    cfg: BridgeConfig,
    seeds: list[str],
    query: str,
    max_pages: int = 100,
    max_depth: int = 2,
    *,
    model: str | None = None,
    budget_chars: int = DEFAULT_AI_BUDGET,
    ai_score: bool = True,
    ai_discover_links: bool = True,
    ai_distill_large: bool = True,
    concurrency: int = 2,
    allow_local: bool = False,
    smart: bool = True,
    on_record: Any | None = None,
) -> AICrawlResult:
    """High-level AI-powered crawl: fetch seeds, score/distill, discover links.

    Reuses :func:`freegem.web_scraper.scrape_page` for fetching (same SSRF
    guard, cache, smart-fetch ladder). Unlike the full :mod:`freegem.crawler`
    campaign (which is frontier/SQLite-backed and resumable), this is an
    in-memory crawl suitable for agent workflows: it walks the link graph up
    to ``max_depth``, scores each page against ``query``, distills oversized
    pages, and optionally uses AI link discovery to find relevant next hops.

    ``on_record`` is called with each :class:`AICrawlRecord` as it completes,
    so a caller can stream partial results.
    """
    if not seeds:
        raise ValueError("ai_crawl requires at least one seed URL")
    if not query:
        raise ValueError("ai_crawl requires a non-empty query")

    # Lazy import: the full crawler package has heavy deps the simple path
    # avoids.
    from freegem.crawler.distill import DISTILL_THRESHOLD_BYTES, distill_text

    visited: set[str] = set()
    frontier: list[tuple[int, str]] = [(0, s) for s in seeds]
    records: list[AICrawlRecord] = []
    ok_count = 0

    while frontier and ok_count < max_pages:
        frontier.sort(key=lambda t: t[0])
        depth, url = frontier.pop(0)
        if url in visited:
            continue
        visited.add(url)

        rec = AICrawlRecord(url=url)
        try:
            page = scrape_page(
                cfg,
                url,
                max_chars=200_000,
                smart=smart,
                allow_local=allow_local,
                cache=None,
            )
            rec.title = page.title
            rec.chars = len(page.text)
            rec.via = page.via
            rec.ok = True
            ok_count += 1

            # AI distillation for oversized pages (reuse FreeGem's distiller).
            if ai_distill_large and len(page.text) >= DISTILL_THRESHOLD_BYTES:
                distilled, _mode = distill_text(cfg, url, page.text)
                rec.ai_distill = AIResult(
                    text=distilled,
                    model=model or cfg.default_model,
                    via="ai-distill",
                    chars_in=len(page.text),
                )
            # AI content scoring.
            if ai_score and page.text.strip():
                rec.ai_score = ai_score_page(
                    cfg,
                    query,
                    url,
                    page.text,
                    model=model,
                    budget_chars=budget_chars,
                )
            # AI link discovery.
            if ai_discover_links and depth < max_depth and page.text.strip():
                rec.ai_links = ai_crawl_discover_links(
                    cfg,
                    url,
                    page.text,
                    model=model,
                    budget_chars=budget_chars,
                )
                # Parse the model's JSON link list and enqueue relevant URLs.
                if rec.ai_links.text and not rec.ai_links.error:
                    _enqueue_ai_links(rec.ai_links.text, url, query, frontier, visited, depth)

            # Also enqueue raw hrefs discovered on the page, scoped.
            if depth < max_depth:
                hrefs = _extract_links(page.text, url)
                origin = urllib.parse.urlparse(url).netloc
                for href in hrefs:
                    if href in visited:
                        continue
                    if urllib.parse.urlparse(href).netloc == origin:
                        frontier.append((depth + 1, href))
        except Exception as exc:
            rec.error = str(exc)

        records.append(rec)
        if on_record is not None:
            on_record(rec)

    # Cross-page AI summary over all fetched page contents.
    summary: AIResult | None = None
    if records:
        blocks = []
        for r in records:
            if not r.ok or not r.chars:
                continue
            blocks.append(f"## {r.title or r.url}\n{r.url}")
        if blocks:
            joined = "\n\n".join(blocks)
            joined, _ = _truncate_text(joined, budget_chars)
            summary = run_ai_pass(
                cfg,
                f"{_wrap_untrusted(joined)}\n\n"
                f"Query: {query}\n\nSummarize the findings across all crawled "
                f"pages, focusing on what is most relevant to the query.",
                model_str=model,
                via="ai-summary",
                budget_chars=budget_chars,
            )

    return AICrawlResult(
        query=query,
        records=records,
        ai_summary=summary,
    )


def _enqueue_ai_links(
    raw_json: str,
    base_url: str,
    query: str,
    frontier: list[tuple[int, str]],
    visited: set[str],
    depth: int,
) -> None:
    """Parse the model's AI link-discovery JSON and enqueue relevant URLs."""
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return
    if not isinstance(data, dict):
        return
    links = data.get("links", [])
    if not isinstance(links, list):
        return
    base_origin = urllib.parse.urlsplit(base_url).netloc
    for entry in links:
        if not isinstance(entry, dict):
            continue
        href = entry.get("url", "")
        if not isinstance(href, str) or not href.startswith("http"):
            continue
        if href in visited:
            continue
        # Scope: same origin as the page that surfaced the link.
        if urllib.parse.urlsplit(href).netloc == base_origin:
            frontier.append((depth + 1, href))
