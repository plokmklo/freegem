"""AI-assisted multi-page research built on FreeGem's citation-verified fetch core.

``freegem.web_research.research`` fetches a bounded set of URLs and produces a
cited answer -- [S1]..[Sn] markers backed by verbatim fetch metadata, with an
optional evidence block whose quotes are checked against the actually-fetched
text. That is the honest foundation; this module adds bounded AI distillation
on top, WITHOUT replacing the citation discipline:

* each fetched page is AI-summarized into a per-source note (via
  :func:`freegem_ai.core.ai_summarize`);
* the notes are joined and the model is asked to answer from them -- the
  answer is still anchored to [S#] labels and the per-URL fetch metadata is
  the source table, never model text;
* fabricated quotes remain marked UNVERIFIED (the same verifier in
  :mod:`freegem.web_research` checks them against fetched text).

The AI passes reuse :meth:`freegem_ai.core.run_ai_pass` so every prompt keeps
the untrusted-content framing, char budget, and ``via`` provenance labeling.
AI views are never written to the shared research cache (which holds verbatim
extractions only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from freegem.config import BridgeConfig
from freegem.gemini import BridgeError
from freegem.web_research import ResearchResult as FGResearchResult
from freegem.web_research import research as fg_research
from freegem.web_scraper import ScrapeResult, scrape_page

from .core import (
    UNTRUSTED_CLOSE,
    UNTRUSTED_OPEN,
    AIResult,
    _truncate_text,
    run_ai_pass,
)

__all__ = [
    "AIExtractResult",
    "AISearchResult",
    "ResearchSourceAI",
    "Summaries",
    "ai_extract",
    "ai_research",
    "ai_search",
]


@dataclass
class ResearchSourceAI:
    """Per-source AI distillation attached to a FreeGem research source."""

    url: str
    title: str
    ai_summary: AIResult | None = None
    ai_extract: AIResult | None = None
    quality: str = "ok"


@dataclass
class Summaries:
    """Bundle of AI results produced during a multi-page research pass."""

    per_source: list[ResearchSourceAI] = field(default_factory=list)
    cross_page: AIResult | None = None
    answer: AIResult | None = None


@dataclass
class AISearchResult:
    """AI-augmented research output: the FreeGem result + AI layers."""

    research: FGResearchResult
    ai: Summaries

    @property
    def sources(self) -> list[ResearchSourceAI]:
        return self.ai.per_source

    def as_dict(self) -> dict[str, Any]:
        return {
            "question": self.research.question,
            "answer": self.research.answer,
            "gemini_used": self.research.gemini_used,
            "ok_pages": self.research.ok_pages,
            "failed_pages": self.research.failed_pages,
            "ai_summary": (self.research.answer if self.research.gemini_used else ""),
            "sources": [
                {
                    "url": s.url,
                    "title": s.title,
                    "ai_summary": s.ai_summary.text if s.ai_summary else None,
                    "ai_extract": s.ai_extract.text if s.ai_extract else None,
                    "quality": s.quality,
                }
                for s in self.sources
            ],
        }


@dataclass
class AIExtractResult:
    """Structured extraction across one or many pages."""

    text: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    via: str = "ai-extract"
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "sources": self.sources,
            "via": self.via,
            "error": self.error,
        }


def _summarize_source(
    cfg: BridgeConfig,
    url: str,
    title: str,
    text: str,
    *,
    model: str | None = None,
    budget_chars: int = 100_000,
    max_summary_chars: int = 2_000,
) -> AIResult:
    """Run a bounded AI summarization over one fetched source's text."""
    from .core import ai_summarize

    text, _truncated = _truncate_text(text, budget_chars)
    if not text.strip():
        return AIResult(
            text="",
            model=cfg.default_model,
            via="ai-summary",
            chars_in=0,
            error="empty source text",
        )
    return ai_summarize(
        cfg,
        text,
        max_summary_chars=max_summary_chars,
        model=model,
        budget_chars=budget_chars,
    )


def _build_source_block(
    idx: int,
    url: str,
    title: str,
    text: str,
    budget_chars: int,
) -> str:
    """Render a per-source [S#] block, truncated to the budget."""
    text, truncated = _truncate_text(text, budget_chars)
    label = title or url
    mark = " [truncated]" if truncated else ""
    return f"[S{idx}] {label} ({url}){mark}\n{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}"


def _fetch_source_text(
    cfg: BridgeConfig,
    url: str,
    *,
    max_chars: int = 20_000,
    cache: Any | None = None,
    refresh: bool = False,
    smart: bool = True,
) -> ScrapeResult:
    """Fetch one URL through FreeGem's guarded scrape path."""
    return scrape_page(
        cfg,
        url,
        max_chars=max_chars,
        smart=smart,
        cache=cache,
        refresh=refresh,
        allow_local=False,
    )


def ai_extract(
    cfg: BridgeConfig,
    question: str,
    urls: list[str],
    *,
    schema: dict[str, str] | None = None,
    max_pages: int = 10,
    max_chars_per_page: int = 20_000,
    budget_chars: int = 100_000,
    model: str | None = None,
    cache: Any | None = None,
    refresh: bool = False,
    smart: bool = True,
) -> AIExtractResult:
    """Fetch pages and extract structured fields grounded in the fetched text.

    When ``schema`` is provided the model is asked to emit JSON with exactly
    those keys; when it is None, extraction is free-form keyed on the
    ``question``. Answers are scoped to [S#]-labeled material built from
    fetched text, and the URL table is rendered from fetch metadata.
    """
    if not question:
        raise ValueError("ai_extract requires a non-empty question")
    if not urls:
        raise ValueError("ai_extract requires at least one URL")

    per_source: list[ResearchSourceAI] = []
    joined_blocks: list[str] = []
    ok_count = 0

    limited_urls = list(urls)[:max_pages]
    for idx, url in enumerate(limited_urls, start=1):
        try:
            page = _fetch_source_text(
                cfg,
                url,
                max_chars=max_chars_per_page,
                cache=cache,
                refresh=refresh,
                smart=smart,
            )
        except Exception as exc:
            per_source.append(
                ResearchSourceAI(
                    url=url,
                    title="",
                    quality="error",
                    ai_summary=AIResult(
                        text="",
                        model=cfg.default_model,
                        via="ai-summary",
                        chars_in=0,
                        error=str(exc),
                    ),
                )
            )
            continue

        text = page.text or ""
        summary = _summarize_source(
            cfg,
            url,
            page.title,
            text,
            model=model,
            budget_chars=budget_chars,
        )
        per_source.append(
            ResearchSourceAI(
                url=url,
                title=page.title,
                ai_summary=summary,
            )
        )
        if text.strip():
            joined_blocks.append(_build_source_block(idx, url, page.title, text, budget_chars))
            ok_count += 1

    if not joined_blocks:
        return AIExtractResult(
            text="",
            sources=[{"url": s.url, "error": "no fetchable text"} for s in per_source],
            error="no sources produced readable text",
        )

    schema_desc = ""
    if schema:
        schema_desc = (
            " Output ONLY a JSON object with exactly these keys:\n"
            + "\n".join(f"- {k} ({v})" for k, v in schema.items())
            + "\nDo not add extra keys or narration."
        )

    extract_prompt = (
        f"{UNTRUSTED_OPEN}\n" + "\n\n".join(joined_blocks) + f"\n{UNTRUSTED_CLOSE}\n\n"
        f"Question: {question}\n"
        + (
            "Extract the requested information from the sources labeled [S#]."
            if schema
            else f"Extract the answer to: {question}"
        )
        + schema_desc
    )

    extract_result = run_ai_pass(
        cfg,
        extract_prompt,
        model_str=model,
        via="ai-extract",
        budget_chars=budget_chars * len(joined_blocks) + 2_000,
    )

    return AIExtractResult(
        text=extract_result.text,
        sources=[
            {"url": s.url, "title": s.title, "summary": s.ai_summary.text if s.ai_summary else None}
            for s in per_source
        ],
        error=extract_result.error,
    )


def ai_search(
    cfg: BridgeConfig,
    question: str,
    urls: list[str],
    *,
    max_pages: int = 8,
    max_chars_per_page: int = 20_000,
    budget_chars: int = 100_000,
    model: str | None = None,
    cache: Any | None = None,
    refresh: bool = False,
    smart: bool = True,
) -> AISearchResult:
    """AI-augmented version of FreeGem's citation-verified research.

    Reuses :func:`freegem.web_research.research` for fetching, citation
    labeling, and quote verification, then adds a bounded AI pass that
    summarizes each source and produces a consolidated AI answer. Both the
    verbatim FreeGem answer and the AI summary are returned.
    """
    if not question:
        raise ValueError("ai_search requires a non-empty question")
    if not urls:
        raise ValueError("ai_search requires at least one URL")

    try:
        fg_result = fg_research(
            cfg,
            question,
            list(urls),
            max_pages=max_pages,
            max_chars_per_page=max_chars_per_page,
            total_budget=budget_chars * max_pages,
            cache=cache,
            refresh=refresh,
            evidence=True,
        )
    except (BridgeError, ValueError) as exc:
        return AISearchResult(
            research=FGResearchResult(
                question=question,
                answer=f"error: {exc}",
            ),
            ai=Summaries(
                cross_page=AIResult(
                    text="",
                    model=cfg.default_model,
                    via="ai-summary",
                    chars_in=0,
                    error=str(exc),
                ),
            ),
        )

    # Per-source AI distillation: re-fetch just the text for summarization.
    # The FreeGem research result carries fetch metadata (URL, title, quality)
    # but not the body text itself; we get the summary text from a scrape.
    per_source: list[ResearchSourceAI] = []
    for s in fg_result.sources:
        if not s.ok:
            per_source.append(
                ResearchSourceAI(
                    url=s.url,
                    title=s.title or "",
                    quality=getattr(s, "quality", "ok"),
                )
            )
            continue
        try:
            page = _fetch_source_text(
                cfg,
                s.url,
                max_chars=max_chars_per_page,
                cache=cache,
                refresh=True,
                smart=smart,
            )
        except Exception as exc:
            page = ScrapeResult(url=s.url, title=s.title or "", text="")
            per_source.append(
                ResearchSourceAI(
                    url=s.url,
                    title=s.title or "",
                    quality=getattr(s, "quality", "ok"),
                    ai_summary=AIResult(
                        text="",
                        model=cfg.default_model,
                        via="ai-summary",
                        chars_in=0,
                        error=str(exc),
                    ),
                )
            )
            continue

        summary = _summarize_source(
            cfg,
            s.url,
            page.title,
            page.text,
            model=model,
            budget_chars=budget_chars,
        )
        per_source.append(
            ResearchSourceAI(
                url=s.url,
                title=page.title or s.title or "",
                ai_summary=summary,
                quality=getattr(s, "quality", "ok"),
            )
        )

    # Cross-page AI synthesis over the joined source texts.
    from .core import ai_summarize

    blocks: list[str] = []
    for idx, s in enumerate(fg_result.sources, start=1):
        if not s.ok:
            continue
        try:
            page = _fetch_source_text(
                cfg,
                s.url,
                max_chars=max_chars_per_page,
                cache=cache,
                refresh=True,
                smart=smart,
            )
        except Exception:
            continue
        if page.text.strip():
            blocks.append(_build_source_block(idx, s.url, page.title, page.text, budget_chars))

    if blocks:
        joined = "\n\n".join(blocks)
        joined, _ = _truncate_text(joined, budget_chars)
        cross = ai_summarize(
            cfg,
            f"{UNTRUSTED_OPEN}\n{joined}\n{UNTRUSTED_CLOSE}\n\n"
            f"Question: {question}\n\nSummarize the findings across all crawled "
            f"pages, focusing on what is most relevant to the query.",
            max_summary_chars=3_000,
            model=model,
            budget_chars=budget_chars,
        )
    else:
        cross = AIResult(
            text="",
            model=cfg.default_model,
            via="ai-summary",
            chars_in=0,
            error="no material to summarize",
        )

    return AISearchResult(
        research=fg_result,
        ai=Summaries(
            per_source=per_source,
            cross_page=cross,
        ),
    )


def ai_research(
    cfg: BridgeConfig,
    question: str,
    urls: list[str],
    *,
    schema: dict[str, str] | None = None,
    max_pages: int = 10,
    max_chars_per_page: int = 20_000,
    budget_chars: int = 100_000,
    model: str | None = None,
    cache: Any | None = None,
    refresh: bool = False,
    smart: bool = True,
) -> AIExtractResult:
    """AI-assisted research: fetch, cite, distill, and extract structured fields.

    This is the high-level entry point: it fetches pages through FreeGem's
    guarded scrape path, then runs AI extraction keyed on ``question`` (or a
    JSON schema when provided). The result carries both the per-source
    summaries and the structured extraction.
    """
    return ai_extract(
        cfg,
        question,
        urls,
        schema=schema,
        max_pages=max_pages,
        max_chars_per_page=max_chars_per_page,
        budget_chars=budget_chars,
        model=model,
        cache=cache,
        refresh=refresh,
        smart=smart,
    )
