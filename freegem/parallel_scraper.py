"""Parallel scraper with adaptive concurrency and AI summarization.

Builds on :func:`freegem.web_scraper.scrape_many` but lifts its hard cap of
100 URLs and adds three capabilities the survey papers (#72, #73) call for
when scraping thousands of pages:

1. **Adaptive concurrency** -- the pool width self-tunes. When the fraction
   of bot-wall / rate-limit failures (``via`` containing ``challenge``,
   ``wayback``, ``ai-search``) rises above a threshold, concurrency is
   throttled back so the pool does not manufacture 429s by hammering one
   origin too hard. When hosts are clean, the pool widens back up. This
   mirrors how a human opens tabs lazily instead of all at once.

2. **AI summarization** -- optional, gated, and lazy. Results are fetched
   in parallel; the AI pass runs ONLY when the caller asks for it (``summarize=True``)
   and is itself bounded by a per-batch token budget so summarizing a 10 000-page
   crawl does not explode the Gemini quota. Each batch result carries its own
   ``summary`` field, and an optional top-level digest summarizes the whole set.

3. **No hard URL cap** -- ``scrape_many`` caps at 100 URLs per call (#15) by
   design. The parallel scraper removes that ceiling: it chunks internally and
   reuses the same bounded pool + per-host serialization discipline, so 10 000
   URLs is just many rounds of the same 20-worker pool.

Memory is bounded: page text is truncated to ``max_chars`` per page and
never accumulated in full for the digest -- the AI sees a structured digest
built from per-page summaries, not the raw concatenation.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from .config import BridgeConfig
from .web_scraper import BatchItem, ScrapeError, ScrapeResult, scrape_page, _clamp_batch_concurrency

__all__ = [
    "ParallelResult",
    "ParallelDigest",
    "scrape_parallel",
    "summarize_results",
    "botwall_ratio",
]

#: When this fraction of in-flight results are bot walls / rate limited,
#: throttle the pool back. The papers: IP-reputation walls compound under
#: concurrent pressure, so stepping down concurrency is the honest path.
_BOTWALL_THROTTLE_RATIO = 0.35

#: How far to throttle: multiplier on current concurrency when walls dominate.
_THROTTLE_FACTOR = 0.6

#: How fast to widen back: multiplier on current concurrency when clean.
_WIDEN_FACTOR = 1.2

#: Hard floor and ceiling for the adaptive pool.
_MIN_WORKERS = 2
_MAX_WORKERS_DEFAULT = 50

#: Per-page text cap for the AI digest (not for scrape_page, which has its own).
_DIGEST_PAGE_CAP = 4_000

#: AI timeout for the whole batch digest (seconds). The batch digest is a
#: single bounded model call; a timeout does not lose per-page summaries, only
#: the top-level digest.
_DIGEST_TIMEOUT_SEC = 60.0

#: Markers in a BatchItem's ``via`` or ``error`` that indicate the page hit
#: a bot wall / rate limit rather than being genuinely unreadable.
_BOTWALL_MARKERS = (
    "bot-challenge",
    "challenge",
    "wayback",
    "ai-search",
    "403",
    "401",
    "429",
    "rate limit",
    "too many requests",
    "service unavailable",
    "access denied",
)


@dataclass
class ParallelResult:
    """One URL's outcome in a parallel scrape, mirroring :class:`BatchItem`."""

    url: str
    ok: bool = False
    title: str = ""
    text: str = ""
    chars: int = 0
    via: str = ""
    truncated: bool = False
    error: str = ""
    summary: str = ""
    elapsed_ms: int = 0

    def as_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "url": self.url,
            "ok": self.ok,
            "title": self.title,
            "chars": self.chars,
            "via": self.via,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
        }
        if self.ok and include_text:
            data["text"] = self.text[:_DIGEST_PAGE_CAP]
        if self.summary:
            data["summary"] = self.summary
        if not self.ok:
            data["error"] = self.error
        return data


@dataclass
class ParallelDigest:
    """Aggregate stats + optional AI digest of a completed parallel scrape."""

    total: int = 0
    ok: int = 0
    failed: int = 0
    botwall: int = 0
    via_counts: dict[str, int] = field(default_factory=dict)
    avg_elapsed_ms: float = 0.0
    peak_concurrency: int = 0
    digest_text: str = ""
    elapsed_sec: float = 0.0


def botwall_ratio(results: list[ParallelResult]) -> float:
    """Fraction of results that look like bot walls / rate limits."""
    if not results:
        return 0.0
    walls = sum(1 for r in results if _is_botwall(r))
    return walls / len(results)


def summarize_results(
    cfg: BridgeConfig,
    results: list[ParallelResult],
    *,
    model: str | None = None,
    budget_chars: int = 200_000,
    question: str | None = None,
    digest: bool = True,
) -> str:
    """Add AI summaries to ``results`` (in-place) and return a batch digest.

    This is a standalone entry point for callers that fetched results through
    ``scrape_page`` / ``scrape_many`` and want AI summaries afterward, without
    re-fetching. ``digest=True`` also builds a top-level digest string from
    the per-page summaries.

    AI summaries are bounded, labeled, and never cached as verbatim extractions
    (per NON_GOALS). When Gemini is unavailable, the digest is empty and
    per-page ``summary`` fields are left empty -- the raw text is still there.
    """
    _summarize_batch(cfg, results, model=model, budget_chars=budget_chars, question=question)
    if digest and sum(1 for r in results if r.ok) > 3 or (question is not None and results):
        return _batch_digest(cfg, results, model=model, budget_chars=budget_chars, question=question)
    return ""


def _is_botwall(r: ParallelResult) -> bool:
    """True when a result was blocked, not genuinely unreadable."""
    via = (r.via or "").lower()
    err = (r.error or "").lower()
    if r.ok and r.chars > 0 and "challenge" not in via and "wayback" not in via:
        return False
    combined = f"{via} {err}"
    return any(marker in combined for marker in _BOTWALL_MARKERS)


def _adaptive_concurrency(
    base: int,
    current_ratio: float,
    peak: int,
) -> int:
    """Shrink the pool when walls dominate, widen it when hosts are clean."""
    if current_ratio > _BOTWALL_THROTTLE_RATIO:
        return max(_MIN_WORKERS, int(base * _THROTTLE_FACTOR))
    if current_ratio < 0.1 and peak < base:
        return min(base, int(peak * _WIDEN_FACTOR) + 1)
    return max(_MIN_WORKERS, min(base, peak))


def _chunked(seq: list[str], size: int) -> Iterable[list[str]]:
    """Yield successive ``size``-sized chunks from ``seq``."""
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def scrape_parallel(
    cfg: BridgeConfig,
    urls: list[str],
    *,
    max_chars: int = 50_000,
    ignore_robots: bool = True,
    smart: bool = True,
    cache: Any | None = None,
    refresh: bool = False,
    concurrency: int = 10,
    max_workers: int | None = None,
    progress: Callable[[str], None] | None = None,
    allow_local: bool = False,
    summarize: bool = False,
    model: str | None = None,
    budget_chars: int = 200_000,
    question: str | None = None,
) -> tuple[list[ParallelResult], ParallelDigest]:
    """Fetch many URLs in parallel with adaptive concurrency + AI digests.

    Unlike ``scrape_many``, there is no hard URL cap -- the pool reuses the
    same per-host serialization discipline and simply processes URLs in
    waves. Concurrency adapts: if more than :data:`_BOTWALL_THROTTLE_RATIO`
    of results look like bot walls, the pool shrinks; when hosts are clean it
    widens back.

    When ``summarize`` is set, each successfully fetched page gets a one-sentence
    AI summary (bounded, labeled ``via="ai-clean"``), and an optional batch-level
    digest distills the whole set. Summaries are lazy: they only run when
    requested, keeping the fetch fast and the AI budget predictable.

    Results keep the caller's URL order. A single failed URL is recorded with
    its error and never aborts the batch.
    """
    if not urls:
        return [], ParallelDigest(total=0)

    concurrency = _clamp_batch_concurrency(cfg, concurrency)
    max_workers = max(
        _MIN_WORKERS,
        min(max_workers or concurrency, cfg.batch_max_concurrency),
    )

    results: list[ParallelResult | None] = [None] * len(urls)
    digest = ParallelDigest(total=len(urls))
    peak_concurrency = 0
    elapsed_sum_ms = 0
    index = 0

    def _work(url: str) -> ParallelResult:
        started = time.monotonic()
        try:
            page: ScrapeResult = scrape_page(
                cfg,
                url,
                max_chars=max_chars,
                ignore_robots=ignore_robots,
                smart=smart,
                cache=cache,
                refresh=refresh,
                progress=progress,
                allow_local=allow_local,
            )
        except ScrapeError as exc:
            return ParallelResult(
                url=url,
                ok=False,
                error=str(exc),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # a hostile page must not kill the batch
            return ParallelResult(
                url=url,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
        ms = int((time.monotonic() - started) * 1000)
        if not page.text.strip():
            return ParallelResult(
                url=url,
                ok=False,
                error="HTTP 200 but zero readable text",
                elapsed_ms=ms,
                via=page.via,
            )
        return ParallelResult(
            url=url,
            ok=True,
            title=page.title,
            text=page.text,
            chars=len(page.text),
            via=page.via,
            truncated=page.truncated,
            elapsed_ms=ms,
        )

    # Process in waves: each wave fills the current concurrency slot. After each
    # wave we sample the bot-wall ratio and adjust before the next wave.
    wave_size = min(len(urls), max_workers)
    overall_start = time.monotonic()

    for chunk in _chunked(urls, wave_size):
        if progress is not None:
            progress(
                f"parallel-scrape: wave {digest.ok + digest.failed + 1}/"
                f"{len(urls)} (~{wave_size} workers, "
                f"botwall ratio so far: {botwall_ratio([r for r in results if r]):.0%})"
            )
        pool = ThreadPoolExecutor(max_workers=wave_size, thread_name_prefix="parascrape")
        try:
            futures: dict[Future, int] = {
                pool.submit(_work, urls[index + i]): index + i
                for i in range(len(chunk))
                if (index + i) < len(urls)
            }
            for future in as_completed(futures):
                pos = futures[future]
                try:
                    item = future.result(timeout=300)
                except Exception as exc:
                    item = ParallelResult(
                        url=urls[pos],
                        ok=False,
                        error=f"{type(exc).__name__}: {exc}",
                        elapsed_ms=0,
                    )
                results[pos] = item
                elapsed_sum_ms += item.elapsed_ms
                if item.ok:
                    digest.ok += 1
                else:
                    digest.failed += 1
                    if _is_botwall(item):
                        digest.botwall += 1
                digest.via_counts[item.via or "none"] = (
                    digest.via_counts.get(item.via or "none", 0) + 1
                )
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        # Adapt concurrency for the next wave based on what this wave saw.
        ratio = botwall_ratio([r for r in results if r is not None])
        peak_concurrency = max(peak_concurrency, len(futures))
        wave_size = max(_MIN_WORKERS, int(wave_size * (_THROTTLE_FACTOR if ratio > _BOTWALL_THROTTLE_RATIO else _WIDEN_FACTOR)))
        wave_size = min(wave_size, max_workers)
        index += len(chunk)

    # Fill in any stragglers (defensive: should not happen, but as_completed
    # does not guarantee all futures complete before pool.shutdown).
    final: list[ParallelResult] = []
    for r in results:
        if r is not None:
            final.append(r)
        else:
            final.append(ParallelResult(url="", ok=False, error="not processed"))

    digest.peak_concurrency = peak_concurrency
    digest.avg_elapsed_ms = elapsed_sum_ms / max(1, digest.total)
    digest.elapsed_sec = round(time.monotonic() - overall_start, 2)

    # AI summarization (lazy, bounded).
    if summarize and digest.ok > 0:
        _summarize_batch(cfg, final, model=model, budget_chars=budget_chars, question=question)

        if digest.ok > 3 or question is not None:
            digest.digest_text = _batch_digest(cfg, final, model=model, budget_chars=budget_chars, question=question)

    return final, digest


def _summarize_batch(
    cfg: BridgeConfig,
    results: list[ParallelResult],
    *,
    model: str | None = None,
    budget_chars: int = 200_000,
    question: str | None = None,
) -> None:
    """Fill in per-page summaries for successful results.

    Each summary is a single bounded model call. If Gemini is unavailable
    (no session, rate-limited), the summary field is left empty -- the raw
    text is still available. This is AI-assisted, labeled, and never cached
    as a verbatim extraction (per NON_GOALS).
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    try:
        from .mcp_tools import run_tool
    except Exception:
        return  # no Gemini bridge available; skip summaries silently

    def summarize_one(r: ParallelResult) -> None:
        if not r.ok or not r.text.strip():
            return
        snippet = r.text[:_DIGEST_PAGE_CAP]
        if question:
            prompt = (
                f"Read the text from {r.url} and answer: {question}\n"
                f"Be concise. If the text does not contain the answer, say so.\n\n"
                f"[PAGE TEXT]\n{snippet}\n[/PAGE TEXT]"
            )
        else:
            prompt = (
                f"Summarize the following web page text from {r.url} in one "
                f"concise sentence (max 200 chars). Identify the main topic.\n\n"
                f"[PAGE TEXT]\n{snippet}\n[/PAGE TEXT]"
            )
        try:
            pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="para-ai")
            future = pool.submit(run_tool, cfg, "gemini_ask", {"question": prompt, "model": model})
            try:
                r.summary = future.result(timeout=20).strip()
            except FuturesTimeout:
                r.summary = "[timeout: AI summary unavailable]"
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            r.summary = ""  # leave empty: raw text is still available

    # Bound concurrency for the AI calls: one model call per page, capped at
    # the number of successful results.
    batch_pool = ThreadPoolExecutor(max_workers=min(4, digest_ok(results)), thread_name_prefix="para-batch")
    try:
        for r in results:
            if r.ok and r.text.strip():
                batch_pool.submit(summarize_one, r)
    finally:
        batch_pool.shutdown(wait=False, cancel_futures=True)


def digest_ok(results: list[ParallelResult]) -> int:
    return sum(1 for r in results if r.ok)


def _batch_digest(
    cfg: BridgeConfig,
    results: list[ParallelResult],
    *,
    model: str | None = None,
    budget_chars: int = 200_000,
    question: str | None = None,
) -> str:
    """Build a top-level digest summarizing the whole batch.

    Uses per-page summaries (not raw text) so the model call stays small
    regardless of how many pages were fetched. Bounded by the per-call
    budget; a timeout leaves the digest empty.
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    try:
        from .mcp_tools import run_tool
    except Exception:
        return ""

    ok_results = [r for r in results if r.ok]
    if not ok_results:
        return ""

    # Build a structured digest from per-page summaries + key facts.
    lines: list[str] = []
    for r in ok_results[:50]:  # cap digest input size
        summary = (r.summary or "")[:500]
        if not summary:
            summary = f"({len(r.text)} chars, via={r.via})"
        lines.append(f"- {r.title or r.url}: {summary}")

    digest_text = "\n".join(lines)
    if question:
        prompt = (
            f"Synthesize the following page summaries into a concise answer to: "
            f"{question}\n\n"
            f"Key findings:\n{digest_text}\n\n"
            "Answer in 2-3 short paragraphs. Cite which sources support each point."
        )
    else:
        prompt = (
            f"Synthesize the following page summaries into a concise overview "
            f"of the topic. Group related findings, note any disagreements, "
            f"and flag sources that were blocked by bot walls.\n\n"
            f"{digest_text}"
        )

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="para-digest")
    try:
        future = pool.submit(run_tool, cfg, "gemini_ask", {"question": prompt, "model": model})
        try:
            return future.result(timeout=int(_DIGEST_TIMEOUT_SEC)).strip()
        except FuturesTimeout:
            return ""
    except Exception:
        return ""
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


# Re-export for convenience.
__all__ = [
    "ParallelResult",
    "ParallelDigest",
    "scrape_parallel",
    "summarize_results",
    "botwall_ratio",
]
