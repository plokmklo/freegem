"""AI-assisted smart scraping: a gated LLM layer over the extraction pipeline.

``scrape_page`` reads a page the honest way -- fetch, extract readable text
with deterministic heuristics. That fails on a known class of pages: pages
whose extractor output is navigation chrome, script soup, or empty. The
smart-fetch ladder already rescues REFUSED pages (403/429/5xx); nothing
rescued the page that answers HTTP 200 but extracts badly.

This module is that missing layer, available ON REQUEST only (``freegem
scrape URL --ai`` or the library's :func:`ai_scrape`):

1. the ordinary scrape runs first (smart-fetch ladder included);
2. its text is graded by the deterministic
   :func:`freegem.web_scraper.extraction_quality`;
3. only when the grade is poor (``nav_only`` / ``empty``) -- or the caller
   forces ``ai_extract="always"`` -- does the LLM see the page, and its job
   is tightly scoped: return the page's MAIN READABLE CONTENT, in the
   page's own words. No summarization, no commentary, no invention;
4. the rescue is GATED: the model's text must grade strictly better than
   the heuristic text (or be longer at equal grade) or it is discarded and
   the heuristic result ships with an honest note;
5. optionally, structured fields via :mod:`freegem.llm_extract`
   (schema-checked, repair-once) and/or a grounded answer to one caller
   question.

The AI never replaces the deterministic pipeline when that pipeline worked:
in ``auto`` mode a page graded ``ok`` costs zero model calls. "Assisted"
means assist, not substitute.

Trust boundaries
----------------

The prompt carries page content wrapped in the same untrusted-content
markers the ``--ask`` path uses, with literal markers in the page DEFUSED
first (:func:`web_scraper._defuse_untrusted_markers`), so a page cannot use
its own text to escape the "everything below is data" frame. The model's
output is untrusted too: it is graded before it ships, and it never becomes
a cache entry.

Provenance and honesty
----------------------

``via`` reports how the SHIPPED text was obtained (``"html"``,
``"mediawiki-api"``, ladder paths, or ``"ai-extract"``). ``notes`` carries
one line per AI event -- attempted, accepted, rejected, rate-limited,
errored -- so a caller (and a human) can always tell what the model did and
did not do. ``ai_used`` is true only when the model contributed to the
shipped result. AI views are NEVER written to the shared research cache
(the cache holds heuristic full-page extractions, the #21 contract); AI
views live in the returned object only.

Memory
------

``$FREEGEM_CACHE_DIR/ai_scrape_memory.json`` (default
``~/.cache/freegem/``) remembers, per host, the last outcome -- whether the
AI rung rescued that host's page or the heuristic pipeline managed alone --
so repeated runs can say "this host usually needs the AI rung". The note is
report-only: the heuristic always runs first, because it is free,
deterministic, and often right.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .llm_extract import (
    BridgeProvider,
    ExtractionError,
    ExtractionProvider,
    ExtractionSchema,
    extract_structured,
)
from .web_scraper import (
    _MIN_CONTENT_CHARS,
    _SCRAPE_MANY_MAX,
    _UNTRUSTED_CLOSE,
    _UNTRUSTED_OPEN,
    DEFAULT_MAX_CHARS,
    ScrapeError,
    _clamp_batch_concurrency,
    _defuse_untrusted_markers,
    _host_locks_for,
    extraction_quality,
    scrape_page,
)

__all__ = [
    "AI_EXTRACT_MODES",
    "AiMemory",
    "AiScrapeResult",
    "ai_scrape",
    "ai_scrape_many",
    "default_ai_memory_path",
    "default_complete",
    "schema_from_json",
]

#: The caller's choices for the content-rescue rung. ``auto`` rescues only
#: pages the deterministic grader scored below ``ok``; ``always`` tries the
#: rescue on every page (the gate still decides); ``never`` disables the
#: rung entirely (a question/schema still works, grounded on the heuristic
#: text).
AI_EXTRACT_MODES = ("auto", "always", "never")

#: Grade ranks used by the rescue gate. ``empty`` < ``nav_only`` < ``ok``:
#: a rescue must land on a STRICTLY better grade, or on the same grade with
#: meaningfully more text, to displace the heuristic result.
_QUALITY_RANK = {"empty": 0, "nav_only": 1, "ok": 2}

#: Characters of page text handed to the model, in either direction
#: (rescue or question). A bound, not a target: the model works from the
#: same text the caller would have seen, so a page whose content sits past
#: the bound is honestly out of reach rather than silently half-read.
_AI_CONTEXT_CHARS = 60_000

#: A rescue at or below this length is "the model shrugged": rejected
#: without spending the gate's time.
_MIN_RESCUE_CHARS = 120

#: How much longer an equal-grade rescue must be to win the gate. A 1%
#: edge is noise; 10% more text at the same grade is a real recovery.
_EQUAL_GRADE_GAIN = 1.10

#: The model's way of saying "there is no recoverable content here".
_SENTINEL_NONE = "NONE"

Complete = Callable[[str], str]


def default_complete(cfg: BridgeConfig) -> Complete:
    """The zero-config AI seam: freegem's own Gemini bridge.

    Anonymous when no session is configured, cookie-authenticated when one
    is. Tests inject their own seam instead -- nothing in this module talks
    to the network except through this callable.
    """
    return BridgeProvider(cfg).complete


def default_ai_memory_path() -> Path:
    """``$FREEGEM_CACHE_DIR`` if set, else ``~/.cache/freegem/ai_scrape_memory.json``."""
    base = os.environ.get("FREEGEM_CACHE_DIR")
    root = Path(base) if base else Path.home() / ".cache" / "freegem"
    return root / "ai_scrape_memory.json"


class AiMemory:
    """Per-host record of which pipeline last produced a readable page.

    Same shape and discipline as the smart-fetch :class:`StrategyMemory`:
    one JSON object mapping host -> strategy name, an instance lock around
    read-modify-write, and a tolerant load (corrupt or absent files degrade
    to an empty memory; they never crash a scrape).
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_ai_memory_path()
        self._lock = threading.Lock()
        self._data: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self._data = {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}

    def get(self, host: str) -> str | None:
        return self._data.get(host)

    def remember(self, host: str, strategy: str) -> None:
        with self._lock:
            if self._data.get(host) == strategy:
                return
            self._data[host] = strategy
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            try:
                tmp.write_text(
                    json.dumps(self._data, ensure_ascii=False, indent=1, sort_keys=True),
                    encoding="utf-8",
                )
                tmp.replace(self.path)
            except OSError:
                pass  # memory is advisory; a full disk must not fail a scrape

    def __len__(self) -> int:
        return len(self._data)


@dataclass
class AiScrapeResult:
    """What one AI-assisted scrape produced, with full provenance.

    ``text``/``via``/``title`` describe the shipped page text exactly as
    :class:`~freegem.web_scraper.ScrapeResult` does. The AI-specific fields
    are additive: ``quality`` grades the shipped text, ``ai_used`` says
    whether the model contributed, ``notes`` carries one line per AI event,
    ``answer`` holds a grounded answer when a ``query`` was asked, and
    ``fields`` holds schema-validated structured data when a schema was
    given. ``ok=False`` means the underlying fetch failed; ``error`` says
    why.
    """

    url: str
    ok: bool = True
    title: str = ""
    text: str = ""
    via: str = "html"
    quality: str = "empty"
    ai_used: bool = False
    notes: list[str] = field(default_factory=list)
    answer: str | None = None
    fields: dict[str, Any] | None = None
    field_problems: list[str] = field(default_factory=list)
    truncated: bool = False
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "ok": self.ok,
            "title": self.title,
            "text": self.text,
            "via": self.via,
            "quality": self.quality,
            "ai_used": self.ai_used,
            "notes": list(self.notes),
            "answer": self.answer,
            "fields": self.fields,
            "field_problems": list(self.field_problems),
            "truncated": self.truncated,
            "error": self.error,
        }


def _context_block(text: str, notes: list[str]) -> str:
    """Cap, defuse, and frame page text as untrusted data."""
    text = _defuse_untrusted_markers(text)
    if len(text) > _AI_CONTEXT_CHARS:
        notes.append(f"page context truncated to {_AI_CONTEXT_CHARS} chars for the model")
        text = text[:_AI_CONTEXT_CHARS]
    return f"{_UNTRUSTED_OPEN}\n{text}\n{_UNTRUSTED_CLOSE}"


def _rescue_prompt(title: str, text: str, notes: list[str]) -> str:
    return (
        "You are the extraction-rescue rung of an automated web reader. The "
        "page below was fetched successfully but its deterministic extractor "
        "produced poor text (navigation chrome, scripts, or nothing useful).\n"
        "Return ONLY the page's main readable content, in the page's own "
        "words, as plain text. Do not summarize. Do not comment. Do not add "
        "anything the page does not say. If there is no recoverable page "
        f"content in the text below, return exactly {_SENTINEL_NONE} and "
        "nothing else.\n"
        f"\nTitle: {title}\n\n{_context_block(text, notes)}"
    )


def _question_prompt(question: str, title: str, text: str, notes: list[str]) -> str:
    return (
        "Answer the question below using ONLY the web page content provided. "
        "If the page does not contain the answer, say so plainly. Be concise.\n"
        f"\nQuestion: {question}\n\nTitle: {title}\n\n{_context_block(text, notes)}"
    )


def _gate_rescue(original: str, original_quality: str, rescued: str) -> bool:
    """Decide whether the model's text displaces the heuristic text.

    The gate is deliberately conservative. A strictly better grade wins
    outright. At an equal grade the rescue needs BOTH a substantial
    heuristic original (below ``_MIN_CONTENT_CHARS`` the original is a
    stub, and "more junk than junk" is not a recovery) AND 10% more text.
    Anything shorter than the rescue floor loses outright. A tie -- same
    grade, similar length -- means the model just paraphrased the
    extractor, and paraphrase must never be presented as the better read.
    """
    stripped = rescued.strip()
    if len(stripped) < _MIN_RESCUE_CHARS:
        return False
    rank_new = _QUALITY_RANK.get(extraction_quality(stripped), 0)
    rank_old = _QUALITY_RANK.get(original_quality, 0)
    if rank_new > rank_old:
        return True
    return bool(
        rank_new == rank_old
        and len(original) >= _MIN_CONTENT_CHARS
        and len(stripped) > len(original) * _EQUAL_GRADE_GAIN
    )


def _wrap_provider(complete: Complete) -> ExtractionProvider:
    """Adapt the raw completion seam to llm_extract's provider protocol."""

    class _SeamProvider:
        def complete(self, prompt: str) -> str:
            return complete(prompt)

    provider: ExtractionProvider = _SeamProvider()
    return provider


def schema_from_json(raw: str) -> ExtractionSchema:
    """Build an :class:`ExtractionSchema` from the CLI's ``--schema`` JSON.

    Accepts ``{"name": "type"}`` shorthand and a per-field object form
    ``{"name": {"type": "string", "description": "...", "enum": [...],
    "required": false}}``. Raises ``ValueError`` with a one-line reason on
    any malformed input (the CLI turns that into exit code 2).
    """
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"--schema is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise ValueError("--schema must be a non-empty JSON object mapping field names to types")
    from .llm_extract import FieldSpec

    fields = []
    for name, spec in data.items():
        if not isinstance(name, str) or not name:
            raise ValueError("--schema field names must be non-empty strings")
        if isinstance(spec, str):
            fields.append(FieldSpec(name=name, type=spec, required=False))
        elif isinstance(spec, dict):
            ftype = str(spec.get("type", "string"))
            enum = tuple(str(v) for v in spec.get("enum", ()))
            if enum and "type" not in spec:
                # {"status": {"enum": ["a","b"]}} is enum shorthand; an
                # explicit "type" key always wins.
                ftype = "enum"
            fields.append(
                FieldSpec(
                    name=name,
                    type=ftype,
                    description=str(spec.get("description", "")),
                    enum=enum,
                    required=bool(spec.get("required", False)),
                )
            )
        else:
            raise ValueError(f"--schema field {name!r} must be a type string or an object")
    return ExtractionSchema(name="cli", fields=tuple(fields))


def ai_scrape(
    cfg: BridgeConfig,
    url: str,
    *,
    query: str | None = None,
    schema: ExtractionSchema | None = None,
    ai_extract: str = "auto",
    max_chars: int = DEFAULT_MAX_CHARS,
    smart: bool = True,
    cache: Any | None = None,
    refresh: bool = False,
    ignore_robots: bool = True,
    allow_local: bool = False,
    complete: Complete | None = None,
    memory: AiMemory | None = None,
    progress: Any | None = None,
) -> AiScrapeResult:
    """Fetch one page with the deterministic pipeline, then assist with AI.

    The fetch, guards, ladder, and cache semantics are exactly
    :func:`freegem.web_scraper.scrape_page`'s. On top of that:

    ``ai_extract`` governs the content-rescue rung (see ``AI_EXTRACT_MODES``).
    ``query`` asks the model one question grounded on the shipped text
    (answer lands in ``result.answer``; never in the shared cache).
    ``schema`` extracts structured fields through
    :func:`freegem.llm_extract.extract_structured` (result in
    ``result.fields``; ``field_problems`` carries whatever the validator
    had to forgive).

    ``complete`` is the AI seam (default: the Gemini bridge). ``memory`` is
    the per-host AiMemory (default: a fresh one on the default path, only
    when a rescue actually happens). ``allow_local`` forwards to
    ``scrape_page`` (#50): local targets stay a CLI-only, operator-typed
    choice. Fetch failures raise nothing: the result carries ``ok=False``
    and ``error``.
    """
    if ai_extract not in AI_EXTRACT_MODES:
        raise ValueError(f"ai_extract must be one of {AI_EXTRACT_MODES}, got {ai_extract!r}")
    notes: list[str] = []
    host = urllib.parse.urlsplit(url).hostname or ""
    if memory is not None and ai_extract == "auto" and memory.get(host) == "ai-extract":
        notes.append("host's last read needed the AI rescue rung")
    try:
        page = scrape_page(
            cfg,
            url,
            max_chars=max_chars,
            ignore_robots=ignore_robots,
            smart=smart,
            cache=cache,
            refresh=refresh,
            allow_local=allow_local,
            progress=progress,
        )
    except ScrapeError as exc:
        return AiScrapeResult(url=url, ok=False, error=str(exc), via="none")

    result = AiScrapeResult(
        url=url,
        ok=True,
        title=page.title,
        text=page.text,
        via=page.via,
        truncated=page.truncated,
    )
    quality = extraction_quality(page.text)
    result.quality = quality

    if complete is None:
        complete = default_complete(cfg)

    # ── rung 1: content rescue ────────────────────────────────────────────
    rescue_won = False
    needs_rescue = ai_extract == "always" or (ai_extract == "auto" and quality != "ok")
    if needs_rescue:
        notes.append(f"ai-extract attempted (heuristic grade: {quality})")
        prompt = _rescue_prompt(page.title, page.text, notes)
        try:
            rescued = complete(prompt).strip()
        except Exception as exc:  # BridgeError, RateLimitError, URLError, ...
            notes.append(f"ai-extract failed: {type(exc).__name__}: {exc}")
        else:
            if not rescued or rescued.upper().startswith(_SENTINEL_NONE):
                notes.append("ai-extract: model found no recoverable content")
            elif _gate_rescue(page.text, quality, rescued):
                result.text = rescued
                result.via = "ai-extract"
                result.quality = extraction_quality(rescued)
                result.ai_used = True
                rescue_won = True
                notes.append(f"ai-extract accepted ({len(rescued)} chars, grade {result.quality})")
            else:
                notes.append(
                    f"ai-extract rejected: model text did not improve on "
                    f"the {quality} heuristic extraction"
                )
    # ── rung 2: grounded question ─────────────────────────────────────────
    if query:
        try:
            result.answer = complete(_question_prompt(query, result.title, result.text, notes))
            result.ai_used = True
            if not rescue_won:
                notes.append("answered via ai (grounded on the heuristic text)")
        except Exception as exc:
            notes.append(f"ai-answer failed: {type(exc).__name__}: {exc}")
    # ── rung 3: schema-constrained fields ─────────────────────────────────
    if schema is not None:
        try:
            fields, problems = extract_structured(schema, result.text, _wrap_provider(complete))
            result.fields = fields
            result.field_problems = problems
            result.ai_used = True
            if not rescue_won:
                notes.append("fields extracted via ai")
        except ExtractionError as exc:
            notes.append(f"ai-schema failed: {exc}")
    # ── memory (advisory, never fatal) ────────────────────────────────────
    if memory is not None:
        with contextlib.suppress(OSError):
            memory.remember(host, "ai-extract" if rescue_won else "heuristic")
    result.notes = notes
    return result


def ai_scrape_many(
    cfg: BridgeConfig,
    urls: list[str],
    *,
    concurrency: int = 4,
    per_host_delay: float = 0.2,
    **kwargs: Any,
) -> list[AiScrapeResult]:
    """Fetch several URLs through a bounded pool, one :class:`AiScrapeResult` per URL.

    The same pool discipline as :func:`web_scraper.scrape_many`: worker
    count clamped to the ``batch_max_concurrency`` config ceiling, per-host
    locks serializing same-origin fetches, results in caller order,
    per-URL failures recorded instead of killing the batch. Keyword
    arguments forward to :func:`ai_scrape` (``query``, ``schema``,
    ``ai_extract``, ``memory``, ...).
    """
    if len(urls) > _SCRAPE_MANY_MAX:
        raise ValueError(
            f"batch ai-scrape is capped at {_SCRAPE_MANY_MAX} URLs per run; got {len(urls)}"
        )
    concurrency = _clamp_batch_concurrency(cfg, concurrency)
    locks = _host_locks_for(urls)
    results: list[AiScrapeResult | None] = [None] * len(urls)

    def work(url: str) -> AiScrapeResult:
        lock = locks.get(urllib.parse.urlsplit(url).hostname or "")
        guard: Any = lock if lock is not None else contextlib.nullcontext()
        with guard:
            if per_host_delay > 0:
                time.sleep(per_host_delay)
            try:
                return ai_scrape(cfg, url, **kwargs)
            except Exception as exc:  # a hostile page must not kill the batch
                return AiScrapeResult(url=url, ok=False, error=f"{type(exc).__name__}: {exc}")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(work, url): i for i, url in enumerate(urls)}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()
    return [r for r in results if r is not None]
