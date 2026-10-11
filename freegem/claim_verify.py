"""Evidence-backed claim verification: statuses per claim, quotes per status.

``freegem verify`` (CLI) and ``web_verify`` (MCP) land here. The layer above
:mod:`web_research`: research answers a question, verify judges a LIST of
claims against fetched sources and returns, per claim, exactly one of:

* ``supported``            -- the sources contain clear verbatim support
* ``partially_supported``  -- verbatim support exists, but covers only part
* ``contradicted``         -- a source statement conflicts with the claim
* ``not_found``            -- the sources do not settle the claim either way
* ``source_unavailable``   -- the sources that would settle it failed to load

The last two are the point. "The source contradicts the claim" and "the
source did not contain enough evidence" are different findings, and a fetch
failure is never evidence of falsity.

Design contract (inherits every guard from research/scrape):

* sources go through the SAME guarded, robots-aware, budget-capped fetch
  machinery as ``research`` -- SSRF pinning, robots.txt, size caps, cache;
* every status quote is checked verbatim (whitespace/case-insensitive)
  against the text this run actually fetched, and per-quote verification
  flags travel with the output -- an unverifiable quote is flagged, never
  silently deleted, so a hallucination looks different from a matcher miss;
* statuses the model returns are VALIDATED against the vocabulary; a
  "supported" verdict with no quotable evidence is machine-downgraded to
  ``not_found`` with a note -- the schema never just trusts the model;
* when every source is dead, no model call happens at all: every claim is
  ``source_unavailable`` by construction;
* when Gemini is unavailable the verdicts are NOT automated: claims come
  back ``not_found`` (or ``source_unavailable`` with no material) with an
  explicit note, plus the raw material -- never a fabricated judgment;
* Location labels ("Introduction", "§ History", "para 7") are computed
  deterministically by finding the quote inside sectioned source text --
  the model never asserts where evidence lives;
* URL discovery (optional) uses ONLY supported search APIs, robots-checked
  like every other fetch: the Wikimedia API portal for Wikimedia wikis, the
  site's own OpenSearch API for other MediaWiki hosts. No crawling, no
  link extraction, no browser rendering -- discovered URLs are a handful of
  article pages that then go through the normal guarded fetch.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .config import BridgeConfig
from .gemini import BridgeError, log
from .metrics import RunMetrics
from .web_research import (
    _INJECTION_RULES,
    DEFAULT_CACHE_TTL,
    DEFAULT_MAX_CHARS_PER_PAGE,
    DEFAULT_MAX_PAGES,
    DEFAULT_TOTAL_BUDGET,
    ResearchCache,
    ResearchSource,
    _collect_sources,
    _dedupe_boilerplate,
    _gemini_complete,
    _iso,
    _normalize_for_match,
    _normalize_urls,
    _sources_table,
)

__all__ = [
    "HARD_MAX_CLAIMS",
    "STATUSES",
    "STATUS_CONTRADICTED",
    "STATUS_LABELS",
    "STATUS_NOT_FOUND",
    "STATUS_PARTIAL",
    "STATUS_SUPPORTED",
    "STATUS_UNAVAILABLE",
    "ClaimResult",
    "Evidence",
    "VerifyError",
    "VerifyResult",
    "discover_urls",
    "render_json",
    "render_markdown",
    "render_text",
    "run_verify_tool",
    "verify",
    "verify_cli",
]

# ─── statuses ─────────────────────────────────────────────────────────────────

STATUS_SUPPORTED = "supported"
STATUS_PARTIAL = "partially_supported"
STATUS_CONTRADICTED = "contradicted"
STATUS_NOT_FOUND = "not_found"
STATUS_UNAVAILABLE = "source_unavailable"

#: The closed vocabulary. Anything else the model writes is repaired.
STATUSES = (
    STATUS_SUPPORTED,
    STATUS_PARTIAL,
    STATUS_CONTRADICTED,
    STATUS_NOT_FOUND,
    STATUS_UNAVAILABLE,
)

#: Human-facing labels used by the text/markdown renderers.
STATUS_LABELS = {
    STATUS_SUPPORTED: "Supported",
    STATUS_PARTIAL: "Partially supported",
    STATUS_CONTRADICTED: "Contradicted",
    STATUS_NOT_FOUND: "Not found",
    STATUS_UNAVAILABLE: "Source unavailable",
}

#: Statuses that MUST carry at least one verbatim quote to be honest.
_EVIDENCE_REQUIRED = (STATUS_SUPPORTED, STATUS_PARTIAL, STATUS_CONTRADICTED)


class VerifyError(BridgeError):
    """A caller mistake: no claims, no URLs, discovery refused."""


# ─── limits ───────────────────────────────────────────────────────────────────

#: Ceiling on claims per verify call. Verifying is one model call over all
#: claims; beyond eight the per-claim evidence quality degrades and the
#: prompt stops being bounded.
HARD_MAX_CLAIMS = 8

#: Hard cap on total material characters fed to the model (post-slice).
_VERIFY_MATERIAL_BUDGET = 48_000

#: Evidence caps, mirroring the research evidence block.
_EVIDENCE_MAX_QUOTES = 4
_EVIDENCE_MAX_QUOTE_CHARS = 400

#: Sections considered per claim for the advisory hints.
_HINTS_PER_CLAIM = 4

#: URL discovery: how many results one search may return.
DEFAULT_DISCOVERY_LIMIT = 3
_DISCOVERY_LIMIT_CAP = 5

_MAX_SECTIONS_PER_PAGE = 400


# ─── result types ─────────────────────────────────────────────────────────────


@dataclass
class Evidence:
    """One verbatim quote offered for a claim, and whether it holds.

    ``location`` is computed HERE, by finding the quote inside the cited
    source's sectioned text -- never taken from the model. It is ``None``
    for unverified quotes: a quote that is not in the text has no honest
    location.
    """

    quote: str
    source_marker: str
    verified: bool = False
    location: str | None = None
    retrieved_at: str | None = None


@dataclass
class ClaimResult:
    """The verdict for one input claim, plus its machine-checked evidence."""

    number: int
    claim: str
    status: str = STATUS_NOT_FOUND
    evidence: list[Evidence] = field(default_factory=list)
    #: Machine repairs and downgrades ("status downgraded: no verifiable
    #: quote"), so a changed verdict is always explainable in the output.
    note: str | None = None

    @property
    def all_verified(self) -> bool:
        return bool(self.evidence) and all(e.verified for e in self.evidence)


@dataclass
class VerifyResult:
    """Everything one verify call produced, plus fetch and run metadata."""

    claims: list[ClaimResult] = field(default_factory=list)
    sources: list[ResearchSource] = field(default_factory=list)
    gemini_used: bool = False
    #: Present when URL discovery ran: engine, wiki host, counts.
    discovery: dict[str, Any] | None = None
    metrics: RunMetrics = field(default_factory=RunMetrics)
    #: Untrusted-wrapped raw extracts, set ONLY when Gemini was unavailable
    #: so the output still shows what would have been judged. None means
    #: the model path ran (or nothing was fetched).
    raw_material: str | None = None

    @property
    def ok_pages(self) -> int:
        return sum(1 for s in self.sources if s.ok)

    @property
    def failed_pages(self) -> int:
        return sum(1 for s in self.sources if not s.ok and not s.skipped)

    @property
    def cache_hits(self) -> int:
        return sum(1 for s in self.sources if s.from_cache)

    def status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for claim in self.claims:
            counts[claim.status] = counts.get(claim.status, 0) + 1
        return counts


# ─── section splitting and relevance ─────────────────────────────────────────


@dataclass(frozen=True)
class _Section:
    label: str
    text: str


_MEDIAWIKI_HEADING = re.compile(r"^=+ (.+?) =+\s*$", re.MULTILINE)


def _split_sections(text: str, via: str) -> list[_Section]:
    """Split one source's text into labeled sections.

    MediaWiki plain-text extracts keep ``== Heading ==`` markup, so they
    split cleanly on heading lines; the lead section is labeled
    "Introduction" (MediaWiki's own name for it). Extracted HTML text has
    no heading structure left, so it splits into blank-line-separated
    blocks ("para 1", "para 2", ...), chunked when a page has no blank
    lines at all. Location labels come from whatever split THIS produced,
    which is honest about the fidelity available per source type.
    """
    if via == "mediawiki-api":
        matches = list(_MEDIAWIKI_HEADING.finditer(text))
        if not matches:
            return [_Section("Introduction", text)] if text.strip() else []
        sections: list[_Section] = []
        lead = text[: matches[0].start()].strip()
        if lead:
            sections.append(_Section("Introduction", lead))
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[match.end() : end].strip()
            if body:
                sections.append(_Section(match.group(1).strip(), body))
        return sections[:_MAX_SECTIONS_PER_PAGE]

    # HTML/text extraction: paragraphs are blank-line blocks; a page with
    # none (single blob) is chunked at line boundaries so locations still
    # point somewhere useful.
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) <= 1 and text.strip():
        blocks = []
        current: list[str] = []
        size = 0
        for line in text.splitlines():
            current.append(line)
            size += len(line)
            if size >= 600:
                blocks.append("\n".join(current))
                current, size = [], 0
        if current:
            blocks.append("\n".join(current))
    return [_Section(f"para {i}", b) for i, b in enumerate(blocks, 1)][:_MAX_SECTIONS_PER_PAGE]


_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "by",
        "with",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "he",
        "she",
        "his",
        "her",
        "they",
        "their",
        "them",
        "you",
        "we",
        "our",
        "your",
        "not",
        "no",
        "did",
        "does",
        "do",
        "had",
        "has",
        "have",
        "but",
        "or",
        "and",
        "if",
        "then",
        "than",
        "so",
        "such",
        "can",
        "could",
        "may",
        "might",
        "will",
        "would",
        "shall",
        "should",
        "must",
        "also",
        "into",
        "over",
        "under",
        "between",
        "during",
        "before",
        "after",
        "above",
        "below",
        "when",
        "where",
        "who",
        "whom",
        "which",
        "what",
        "how",
        "why",
        "s",
        "he",
        "d",
        "ll",
        "re",
        "ve",
        "t",
    ]
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _claim_tokens(claim: str) -> list[str]:
    return [t for t in _WORD_RE.findall(claim.lower()) if t not in _STOPWORDS]


def _score_sections(
    sections: list[_Section], tokens: list[str]
) -> list[tuple[float, int, _Section]]:
    """Distinct-keyword coverage per section, best first.

    Score is the fraction of the claim's distinct keywords the section
    contains; ties break toward the earlier section (encyclopedic leads
    summarize first). Sections with zero overlap never come back: the
    caller falls back to the lead instead of pretending relevance.
    """
    if not tokens:
        return []
    wanted = set(tokens)
    scored: list[tuple[float, int, _Section]] = []
    for position, section in enumerate(sections):
        found = wanted.intersection(_WORD_RE.findall(section.text.lower()))
        if found:
            scored.append((len(found) / len(wanted), position, section))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored


def _relevant_section_labels(
    material: list[tuple[ResearchSource, str]], claim: str, per_claim: int = _HINTS_PER_CLAIM
) -> list[tuple[int, str]]:
    """``[(source_number, label), ...]`` for the prompt's advisory hints."""
    tokens = _claim_tokens(claim)
    if not tokens:
        return []
    hits: list[tuple[float, int, int, str]] = []
    for source_number, (source, text) in enumerate(material, 1):
        sections = _split_sections(text, source.via)
        for score, position, section in _score_sections(sections, tokens):
            hits.append((score, source_number, position, section.label))
    hits.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [(number, label) for _, number, _, label in hits[:per_claim]]


def _locate_quote(quote: str, source: ResearchSource, text: str) -> str | None:
    """The section label containing ``quote``, or None when absent.

    Matching is the same whitespace/case-insensitive normalization quote
    verification uses, so a quote that verifies always locates (unless the
    source has no sections at all -- an empty text cannot hold a location).
    """
    needle = _normalize_for_match(quote)
    if len(needle) < 8:
        return None  # too short to point anywhere honestly
    for section in _split_sections(text, source.via):
        if needle in _normalize_for_match(section.text):
            return section.label
    return None


# ─── URL discovery (supported APIs only, robots-checked) ──────────────────────


def _wiki_project_lang(host: str) -> tuple[str, str] | None:
    """``en.wikipedia.org`` -> ``("wikipedia", "en")`` for the API portal.

    Only recognized Wikimedia families route to the portal; ``en.m.``
    mirrors fold to their language. Anything else is not a portal wiki.
    """
    from .web_scraper import _MEDIAWIKI_SUFFIXES

    host = host.lower().strip()
    for suffix in _MEDIAWIKI_SUFFIXES:
        if host.endswith(suffix):
            labels = [p for p in host[: -len(suffix)].split(".") if p and p != "m"]
            lang = labels[-1] if labels else ""
            if not lang:
                return None
            # ".wikipedia.org" -> "wikipedia" for the portal path
            return suffix.strip(".").removesuffix(".org"), lang
    return None


def _portal_search_url(project: str, lang: str, query: str, limit: int) -> str:
    return (
        "https://api.wikimedia.org/core/v1/"
        f"{project}/{lang}/search/page?" + urllib.parse.urlencode({"q": query, "limit": limit})
    )


def _opensearch_url(host: str, query: str, limit: int) -> str:
    return f"https://{host}/w/api.php?" + urllib.parse.urlencode(
        {
            "action": "opensearch",
            "format": "json",
            "search": query,
            "limit": limit,
            "redirects": "resolve",
        }
    )


def _check_search_robots(cfg: BridgeConfig, search_url: str, host: str) -> None:
    from .web_scraper import ScrapeError, _check_robots

    try:
        _check_robots(cfg, search_url)
    except ScrapeError as exc:
        raise ScrapeError(
            f"URL discovery is disallowed by robots.txt for {host!r} ({exc}); "
            "supply URLs explicitly instead of discovery"
        ) from exc


def _portal_discover(
    cfg: BridgeConfig, host: str, project: str, lang: str, query: str, limit: int
) -> list[dict[str, Any]]:
    """Search through the Wikimedia API portal (api.wikimedia.org).

    The per-wiki search APIs under ``/w/`` are disallowed by Wikimedia's
    robots.txt for generic agents; the portal host serves the same
    official index without that disallow, so discovery stays robots-clean
    instead of working around a rule.
    """
    from .web_scraper import ScrapeError, _decode_body, _fetch, _read_capped

    search_url = _portal_search_url(project, lang, query, limit)
    _check_search_robots(cfg, search_url, host)
    try:
        response = _fetch(cfg, search_url)
        data = json.loads(_decode_body(_read_capped(response, 1_000_000), ""))
    except ScrapeError as exc:
        raise ScrapeError(f"Wikimedia search failed for {query!r}: {exc}") from exc
    except ValueError as exc:
        raise ScrapeError(f"Wikimedia search returned unusable data: {exc}") from exc
    pages = data.get("pages") if isinstance(data, dict) else None
    if not isinstance(pages, list):
        raise ScrapeError("Wikimedia search response had no pages list")
    found: list[dict[str, Any]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        key, title = page.get("key"), page.get("title")
        if not isinstance(key, str) or not key:
            continue
        found.append(
            {
                "url": f"https://{host}/wiki/{urllib.parse.quote(key)}",
                "title": title if isinstance(title, str) else key,
                "via": "wikimedia-search",
            }
        )
        if len(found) >= limit:
            break
    return found


def _opensearch_discover(
    cfg: BridgeConfig, host: str, query: str, limit: int
) -> list[dict[str, Any]]:
    """Search a non-Wikimedia MediaWiki host's own OpenSearch API.

    v1 JSON shape (a stable 4-element array) is requested on purpose: the
    formatversion=2 object shape has drifted across MediaWiki releases.
    robots.txt is honored exactly as for any other fetch.
    """
    from .web_scraper import ScrapeError, _decode_body, _fetch, _read_capped

    search_url = _opensearch_url(host, query, limit)
    _check_search_robots(cfg, search_url, host)
    try:
        response = _fetch(cfg, search_url)
        data = json.loads(_decode_body(_read_capped(response, 1_000_000), ""))
    except ScrapeError as exc:
        raise ScrapeError(f"OpenSearch failed on {host!r} for {query!r}: {exc}") from exc
    except ValueError as exc:
        raise ScrapeError(f"OpenSearch on {host!r} returned unusable data: {exc}") from exc
    if not isinstance(data, list) or len(data) < 2 or not isinstance(data[1], list):
        raise ScrapeError(f"OpenSearch on {host!r} returned an unexpected shape")
    titles = [t for t in data[1] if isinstance(t, str) and t.strip()]
    urls = data[3] if len(data) > 3 and isinstance(data[3], list) else []
    found: list[dict[str, Any]] = []
    for index, title in enumerate(titles[:limit]):
        url = ""
        if index < len(urls) and isinstance(urls[index], str) and urls[index].startswith("http"):
            url = urls[index]
        else:
            url = f"https://{host}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
        found.append({"url": url, "title": title, "via": "mediawiki-opensearch"})
    return found


def discover_urls(
    cfg: BridgeConfig,
    query: str,
    *,
    limit: int = DEFAULT_DISCOVERY_LIMIT,
    wiki_host: str | None = None,
) -> list[dict[str, Any]]:
    """Find candidate article URLs for ``query`` via a supported search API.

    Wikimedia wikis go through the api.wikimedia.org portal; other
    MediaWiki hosts use their own OpenSearch endpoint. Every request is
    robots-checked like any other fetch -- if a site refuses its search
    API to us, discovery refuses too and says to supply URLs explicitly.
    This is ONE bounded API call, never a crawl: no link extraction, no
    following results-of-results.
    """
    query = (query or "").strip()
    if not query:
        raise VerifyError("discovery requires a non-empty query")
    limit = max(1, min(int(limit), _DISCOVERY_LIMIT_CAP))
    host = (wiki_host or cfg.discover_wiki or "en.wikipedia.org").strip().lower()
    portal = _wiki_project_lang(host)
    if portal is not None:
        project, lang = portal
        return _portal_discover(cfg, host, project, lang, query, limit)
    return _opensearch_discover(cfg, host, query, limit)


# ─── material fitting ─────────────────────────────────────────────────────────


def _fit_material(
    material: list[tuple[ResearchSource, str]], claims: list[str]
) -> list[tuple[ResearchSource, str]]:
    """Cap total prompt material, preferring pages relevant to the claims.

    At or under budget everything passes through untouched. Over budget,
    pages are ranked by distinct claim-keyword coverage and sliced in that
    order until the budget is spent -- relevance, not list position,
    decides which page survives a cut. Verification later runs against the
    FULL fetched text regardless of this slice, so trimming the prompt
    never hides a verifiable quote.
    """
    total = sum(len(text) for _, text in material)
    if total <= _VERIFY_MATERIAL_BUDGET or not material:
        return material
    tokens: set[str] = set()
    for claim in claims:
        tokens.update(_claim_tokens(claim))
    scored: list[tuple[float, int, ResearchSource, str]] = []
    for position, (source, text) in enumerate(material):
        found = tokens.intersection(_WORD_RE.findall(text.lower()))
        scored.append((len(found) / len(tokens) if tokens else 0.0, position, source, text))
    scored.sort(key=lambda item: (-item[0], item[1]))
    kept: list[tuple[ResearchSource, str]] = []
    remaining = _VERIFY_MATERIAL_BUDGET
    for _, _, source, text in scored:
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining].rstrip()
        kept.append((source, text))
        remaining -= len(text)
    order = {source.url: i for i, (source, _) in enumerate(material)}
    kept.sort(key=lambda item: order.get(item[0].url, len(order)))
    return kept


# ─── prompt ───────────────────────────────────────────────────────────────────


_CLAIMS_FENCE_RE = re.compile(r"```claims\s*\n(.*?)\n```", re.DOTALL)


def _source_block(index: int, source: ResearchSource, text: str) -> str:
    from .web_research import _source_header, _wrap_untrusted

    return f"{_source_header(index, source)}\n{_wrap_untrusted(index, text)}"


def _verify_prompt(
    claims: list[str],
    material: list[tuple[ResearchSource, str]],
    hints: dict[int, list[tuple[int, str]]],
) -> str:
    """One call judges every claim. Hints are advisory; full material ships."""
    claim_lines = "\n".join(f"[C{i}] {claim}" for i, claim in enumerate(claims, 1))
    hint_lines: list[str] = []
    for number in range(1, len(claims) + 1):
        parts = [f'[S{n}] "{label}"' for n, label in hints.get(number, [])]
        hint_lines.append(f"C{number}: {', '.join(parts)}" if parts else f"C{number}: (none)")
    blocks = [f"{_source_block(i, source, text)}" for i, (source, text) in enumerate(material, 1)]
    return (
        "You are a claim-verification engine. Below are numbered web sources "
        "([S1], [S2], ...) and numbered claims ([C1], [C2], ...). For EACH claim, "
        "judge it ONLY against the source material and answer with exactly one status:\n"
        "- supported: the material contains clear verbatim support for the whole claim\n"
        "- partially_supported: verbatim support exists, but covers only part of the claim\n"
        "- contradicted: the material contains a clear verbatim statement that conflicts "
        "with the claim\n"
        "- not_found: the material does not settle the claim either way\n"
        "- source_unavailable: the sources that would settle the claim failed to load "
        "or carried no content; a failed fetch is never evidence that a claim is false\n\n"
        f"{_INJECTION_RULES}\n"
        "Relevant-section hints below come from a deterministic keyword match and are "
        "advisory only: quote whatever passage actually supports or contradicts the "
        "claim, wherever it appears.\n"
        + "\n".join(f"Hints {line}" for line in hint_lines)
        + "\n\nRespond with EXACTLY ONE fenced block, nothing after it:\n"
        "```claims\n"
        '[{"claim_number": 1, "status": "supported", '
        '"evidence": [{"source": 1, "quotes": ["<verbatim quote>"]}]}]\n'
        "```\n"
        "- One entry per claim, ascending claim_number.\n"
        "- supported / partially_supported / contradicted REQUIRE at least one verbatim "
        "quote (at most 30 words) copied EXACTLY from the cited source's material. "
        "contradicted must quote the passage that conflicts with the claim.\n"
        "- not_found and source_unavailable take an empty evidence list.\n"
        "- A claim the sources do not settle is not_found, never a guess.\n\n"
        f"Claims:\n{claim_lines}\n\n" + "\n\n".join(blocks)
    )


# ─── parsing and validation ───────────────────────────────────────────────────


def _parse_claims_block(answer: str, claims: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Extract and structurally validate the ```claims block.

    Returns ``(entries, repairs)``. ``entries`` holds one well-formed dict
    per input claim (missing claims are NOT added here -- the caller turns
    absences into not_found). Repairs record every schema violation found,
    so the output can show what the model got wrong instead of hiding it.
    """
    match = _CLAIMS_FENCE_RE.search(answer)
    repairs: list[str] = []
    if match is None:
        return [], ["model returned no ```claims block"]
    try:
        data = json.loads(match.group(1).strip())
    except ValueError:
        return [], ["```claims block was not valid JSON"]
    if not isinstance(data, list):
        return [], ["```claims block was not a JSON list"]
    entries: dict[int, dict[str, Any]] = {}
    for item in data:
        if not isinstance(item, dict):
            repairs.append("ignored a non-object entry")
            continue
        number = item.get("claim_number")
        if not isinstance(number, int) or isinstance(number, bool):
            repairs.append("ignored an entry without a numeric claim_number")
            continue
        if not 1 <= number <= len(claims):
            repairs.append(f"ignored an entry for out-of-range claim {number}")
            continue
        if number in entries:
            repairs.append(f"ignored a duplicate entry for claim {number}")
            continue
        status = item.get("status")
        if status not in STATUSES:
            repairs.append(f"claim {number}: unknown status {status!r} -> not_found")
            status = STATUS_NOT_FOUND
        evidence = item.get("evidence")
        evidence = evidence if isinstance(evidence, list) else []
        clean: list[dict[str, Any]] = []
        for entry in evidence[:_EVIDENCE_MAX_QUOTES]:
            if not isinstance(entry, dict):
                continue
            source = entry.get("source")
            quotes = entry.get("quotes")
            if not isinstance(source, int) or isinstance(source, bool) or source < 1:
                continue
            if not isinstance(quotes, list):
                continue
            kept = [q.strip() for q in quotes if isinstance(q, str) and q.strip()]
            kept = [q[:_EVIDENCE_MAX_QUOTE_CHARS] for q in kept[:_EVIDENCE_MAX_QUOTES]]
            if not kept:
                continue
            clean.append({"source": source, "quotes": kept})
        if status in _EVIDENCE_REQUIRED and not clean:
            repairs.append(f"claim {number}: {status} requires verbatim evidence -> not_found")
            status = STATUS_NOT_FOUND
        entries[number] = {"claim_number": number, "status": status, "evidence": clean}
    return list(entries.values()), repairs


def _build_claim_results(
    entries: list[dict[str, Any]],
    repairs: list[str],
    claims: list[str],
    material: list[tuple[ResearchSource, str]],
) -> list[ClaimResult]:
    """Turn validated entries into ClaimResults with verified, located quotes.

    Every quote is checked against the text this run actually fetched
    (whitespace/case-insensitive); verified quotes get a deterministic
    Location. Unverified quotes stay and carry the UNVERIFIED flag --
    deleting them would make a model hallucination indistinguishable from
    a matcher miss.
    """
    pages = {index: text for index, (_, text) in enumerate(material, 1)}
    by_number = {entry["claim_number"]: entry for entry in entries}
    results: list[ClaimResult] = []
    for number, claim_text in enumerate(claims, 1):
        entry = by_number.get(number)
        result = ClaimResult(number=number, claim=claim_text)
        if entry is None:
            result.status = STATUS_NOT_FOUND
            result.note = "no verdict was returned for this claim"
            results.append(result)
            continue
        result.status = entry["status"]
        evidences: list[Evidence] = []
        for evidence_entry in entry["evidence"]:
            source_number = evidence_entry["source"]
            if source_number not in pages:
                result.note = _join_notes(
                    result.note, f"evidence citing unloaded [S{source_number}] was dropped"
                )
                continue
            source, text = material[source_number - 1]
            marker = f"S{source_number}"
            fetched_at = source.fetched_at
            for quote in evidence_entry["quotes"]:
                verified = _normalize_for_match(quote) in _normalize_for_match(text)
                evidences.append(
                    Evidence(
                        quote=quote,
                        source_marker=marker,
                        verified=verified,
                        location=_locate_quote(quote, source, text) if verified else None,
                        retrieved_at=_date_only(fetched_at),
                    )
                )
        result.evidence = evidences
        # The evidence rule must hold AFTER drops too: a supported /
        # partially_supported / contradicted verdict whose every quote was
        # dropped (unloaded sources) is no longer a supported verdict.
        if result.status in _EVIDENCE_REQUIRED and not result.evidence:
            downgraded = result.status
            result.status = STATUS_NOT_FOUND
            result.note = _join_notes(
                result.note, f"{downgraded} downgraded: no verifiable quote remained"
            )
        results.append(result)
    notes = "; ".join(repairs) if repairs else None
    if notes:
        for result in results:
            result.note = _join_notes(result.note, notes)
    return results


def _join_notes(left: str | None, right: str) -> str:
    return f"{left}; {right}" if left else right


def _date_only(ts: float | None) -> str | None:
    if ts is None:
        return None
    return (_iso(ts) or "")[:10] or None


# ─── the pipeline ─────────────────────────────────────────────────────────────


def _normalize_claims(raw: list[str]) -> list[str]:
    claims: list[str] = []
    for item in raw:
        claim = str(item).strip()
        if claim and claim not in claims:
            claims.append(claim)
    if not claims:
        raise VerifyError("verify requires at least one non-empty claim")
    if len(claims) > HARD_MAX_CLAIMS:
        raise VerifyError(
            f"verify accepts at most {HARD_MAX_CLAIMS} claims per call "
            f"(got {len(claims)}); split the batch"
        )
    return claims


def verify(
    cfg: BridgeConfig,
    claims: list[str],
    urls: list[str] | None = None,
    *,
    discover: bool = False,
    discovery_query: str | None = None,
    discovery_limit: int = DEFAULT_DISCOVERY_LIMIT,
    wiki_host: str | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_chars_per_page: int = DEFAULT_MAX_CHARS_PER_PAGE,
    total_budget: int = DEFAULT_TOTAL_BUDGET,
    cache: ResearchCache | None = None,
    refresh: bool = False,
    concurrency: int | None = None,
) -> VerifyResult:
    """Verify claims against fetched sources; never raises for dead sources.

    Raises for caller mistakes only (empty claims, no URLs with discovery
    off, discovery refused). One model call judges all claims; the model's
    statuses are schema-validated, its quotes machine-checked verbatim,
    and its locations computed by finding each quote in the fetched text.
    """
    started_metrics = RunMetrics()
    claims = _normalize_claims(claims)

    discovered_info: dict[str, Any] | None = None
    with started_metrics.stage("discover"):
        if urls:
            normalized = _normalize_urls([str(u) for u in urls])
        elif discover:
            query = (discovery_query or " ".join(claims)).strip()
            host = (wiki_host or cfg.discover_wiki or "en.wikipedia.org").lower()
            try:
                found = discover_urls(cfg, query, limit=discovery_limit, wiki_host=wiki_host)
            except BridgeError as exc:
                raise VerifyError(str(exc)) from exc
            normalized = [item["url"] for item in found]
            discovered_info = {
                "used": True,
                "wiki": host,
                "query": query,
                "found": len(normalized),
                "urls": normalized,
            }
            if not normalized:
                raise VerifyError(
                    f"discovery found no sources for {query!r} on {host}; supply URLs explicitly"
                )
        else:
            raise VerifyError("verify needs URLs or discovery: pass urls=[...] or discover=True")
    if not normalized:
        raise VerifyError("verify requires at least one URL")

    with started_metrics.stage("fetch"):
        sources, material = _collect_sources(
            cfg,
            normalized,
            max_pages=max_pages,
            max_chars_per_page=max_chars_per_page,
            total_budget=total_budget,
            cache=cache,
            refresh=refresh,
            concurrency=cfg.research_concurrency if concurrency is None else concurrency,
        )
    started_metrics.bump("fetch_ok", sum(1 for s in sources if s.ok))
    started_metrics.bump("fetch_failed", sum(1 for s in sources if not s.ok and not s.skipped))
    started_metrics.bump("cache_hits", sum(1 for s in sources if s.from_cache))

    if material:
        with started_metrics.stage("dedupe"):
            deduped = _dedupe_boilerplate([text for _, text in material])
            material = [(source, text) for (source, _), text in zip(material, deduped, strict=True)]

    gemini_used = False
    claim_results: list[ClaimResult] = []
    fallback_material: list[tuple[ResearchSource, str]] = []
    if not material:
        # No source carried text: nothing can be judged, and no model call
        # can change that. source_unavailable for every claim, by design.
        for number, claim_text in enumerate(claims, 1):
            claim_results.append(
                ClaimResult(
                    number=number,
                    claim=claim_text,
                    status=STATUS_UNAVAILABLE,
                    note="no loadable source carried any text",
                )
            )
    else:
        with started_metrics.stage("generate"):
            prompt_material = _fit_material(material, claims)
            hints = {
                number: _relevant_section_labels(prompt_material, claim)
                for number, claim in enumerate(claims, 1)
            }
            try:
                answer = _gemini_complete(
                    cfg, _verify_prompt(claims, prompt_material, hints)
                ).strip()
                gemini_used = True
            except BridgeError as exc:
                log(cfg, f"verify falling back to unjudged statuses: {exc}")
                for number, claim_text in enumerate(claims, 1):
                    claim_results.append(
                        ClaimResult(
                            number=number,
                            claim=claim_text,
                            status=STATUS_NOT_FOUND,
                            note=(
                                "gemini unavailable; no automated verdict was made "
                                "(raw material follows in the text output)"
                            ),
                        )
                    )
                answer = ""
                fallback_material = prompt_material
        if gemini_used:
            with started_metrics.stage("parse"):
                entries, repairs = _parse_claims_block(answer, claims)
                claim_results = _build_claim_results(entries, repairs, claims, material)
            started_metrics.bump("schema_repairs", len(repairs))
        elif not claim_results:
            # defensive: the generate stage always fills claim_results
            claim_results = [
                ClaimResult(number=n, claim=c, status=STATUS_NOT_FOUND)
                for n, c in enumerate(claims, 1)
            ]

    offered = sum(len(c.evidence) for c in claim_results)
    verified_count = sum(1 for c in claim_results for e in c.evidence if e.verified)
    started_metrics.bump("evidence_offered", offered)
    started_metrics.bump("evidence_verified", verified_count)

    return VerifyResult(
        claims=claim_results,
        sources=sources,
        gemini_used=gemini_used,
        discovery=discovered_info,
        metrics=started_metrics,
        raw_material=_raw_material_block(fallback_material) if fallback_material else None,
    )


# ─── rendering ────────────────────────────────────────────────────────────────


def _evidence_lines(claim: ClaimResult, *, fancy: bool) -> list[str]:
    """One block per evidence item, in the documented output shape.

    plain (text mode):
        Evidence:
        "quote" (verified|UNVERIFIED)

        Source: [S1]
        Location: Introduction
        Retrieved: 2026-10-11

    fancy (markdown mode) uses curly quotes and a blockquote. Location is
    omitted for unverified quotes: there is no honest place to point at.
    """
    if not claim.evidence:
        return []
    multiple = len(claim.evidence) > 1
    lines: list[str] = []
    for index, item in enumerate(claim.evidence, 1):
        header = f"Evidence {index}/{len(claim.evidence)}:" if multiple else "Evidence:"
        lines.append(header)
        mark = "verified" if item.verified else "UNVERIFIED"
        if fancy:
            open_q, close_q = "\u201c", "\u201d"
            if item.verified:
                lines.append(f"> {open_q}{item.quote}{close_q} \u2014 [{item.source_marker}]")
            else:
                lines.append(
                    f"> {open_q}{item.quote}{close_q} \u2014 [{item.source_marker}] "
                    "\u2717 not found verbatim in the source"
                )
        else:
            lines.append(f'"{item.quote}" ({mark})')
        lines.append("")
        lines.append(f"Source: [{item.source_marker}]")
        if item.verified and item.location:
            lines.append(f"Location: {item.location}")
        lines.append(f"Retrieved: {item.retrieved_at or 'unknown'}")
        if index < len(claim.evidence):
            lines.append("")
    return lines


def _meta_line(result: VerifyResult) -> str:
    counts = result.status_counts()
    summary = ", ".join(
        f"{count} {STATUS_LABELS[status].lower()}"
        for status, count in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    parts = [f"{len(result.claims)} claims ({summary})"]
    parts.append(f"{result.ok_pages}/{len(result.sources)} pages")
    if result.failed_pages:
        parts.append(f"{result.failed_pages} failed")
    if result.cache_hits:
        parts.append(f"{result.cache_hits} cached")
    parts.append("gemini" if result.gemini_used else "unjudged (gemini unavailable)")
    parts.append(f"{result.metrics.elapsed_sec:.1f}s")
    line = "verify: " + ", ".join(parts)
    if result.discovery:
        line += f" | discovered via {result.discovery['wiki']} ({result.discovery['found']} urls)"
    return f"{line} [req {result.metrics.request_id}]"


_UNSET = object()


def render_text(result: VerifyResult, *, raw_material: Any = _UNSET) -> str:
    """Plain-text rendering: claims, evidence blocks, sources, metadata.

    ``raw_material`` (untrusted-wrapped extracts shown when Gemini was
    unavailable) defaults to what the result carries; pass None to force it
    off, or a string to override.
    """
    if raw_material is _UNSET:
        raw_material = result.raw_material
    lines: list[str] = []
    for claim in result.claims:
        lines.append(f"Claim: {claim.claim}")
        lines.append(f"Status: {STATUS_LABELS[claim.status]}")
        if claim.note:
            lines.append(f"Note: {claim.note}")
        evidence = _evidence_lines(claim, fancy=False)
        if evidence:
            lines.append("")
            lines.extend(evidence)
        lines.append("")
    if raw_material:
        lines += [raw_material, ""]
    lines += [_sources_table(result.sources), "", _meta_line(result)]
    return "\n".join(lines).strip()


def render_markdown(result: VerifyResult, *, raw_material: Any = _UNSET) -> str:
    """Markdown rendering: one section per claim, linked source table."""
    if raw_material is _UNSET:
        raw_material = result.raw_material
    lines: list[str] = []
    for claim in result.claims:
        lines.append(f"### C{claim.number}. {claim.claim}")
        lines.append("")
        lines.append(f"**Status: {STATUS_LABELS[claim.status]}**")
        if claim.note:
            lines.append("")
            lines.append(f"*Note: {claim.note}*")
        evidence = _evidence_lines(claim, fancy=True)
        if evidence:
            lines.append("")
            lines.extend(evidence)
        lines.append("")
    if raw_material:
        lines += [raw_material, ""]
    lines += ["## Sources", ""]
    index = 0
    for source in result.sources:
        if not source.ok:
            continue
        index += 1
        title = source.title or source.url
        lines.append(f"{index}. [{title}]({source.url}) `S{index}`")
    fetched = [s for s in result.sources if s.ok and s.fetched_at is not None]
    if fetched:
        stamps = ", ".join(f"S{i} {_iso(s.fetched_at)}" for i, s in enumerate(fetched, 1))
        lines.append("")
        lines.append(f"Fetched: {stamps}")
    lines += ["", f"*{_meta_line(result)}*"]
    return "\n".join(lines).strip()


def render_json(result: VerifyResult, *, raw_material: Any = _UNSET) -> str:
    """JSON rendering with the full documented schema (see README)."""
    from . import __version__

    if raw_material is _UNSET:
        raw_material = result.raw_material
    claims_payload: list[dict[str, Any]] = []
    for claim in result.claims:
        claims_payload.append(
            {
                "number": claim.number,
                "claim": claim.claim,
                "status": claim.status,
                "status_label": STATUS_LABELS[claim.status],
                "evidence": [
                    {
                        "quote": item.quote,
                        "source": item.source_marker,
                        "verified": item.verified,
                        "location": item.location,
                        "retrieved_at": item.retrieved_at,
                    }
                    for item in claim.evidence
                ],
                "all_verified": claim.all_verified,
                "note": claim.note,
            }
        )
    index = 0
    sources_payload: list[dict[str, Any]] = []
    for source in result.sources:
        entry: dict[str, Any] = {
            "url": source.url,
            "ok": source.ok,
            "title": source.title,
            "error": source.error,
            "truncated": source.truncated,
            "chars": source.chars,
            "from_cache": source.from_cache,
            "skipped": source.skipped,
            "via": source.via,
            "quality": source.quality,
            "fetched_at": _iso(source.fetched_at),
            "last_modified": source.last_modified,
        }
        if source.ok:
            index += 1
            entry["index"] = index
            entry["marker"] = f"S{index}"
        sources_payload.append(entry)
    offered = sum(len(c.evidence) for c in result.claims)
    verified = sum(1 for c in result.claims for e in c.evidence if e.verified)
    meta: dict[str, Any] = {
        "claims_total": len(result.claims),
        "statuses": result.status_counts(),
        "evidence_offered": offered,
        "evidence_verified": verified,
        "urls_requested": len(result.sources),
        "pages_ok": result.ok_pages,
        "pages_failed": result.failed_pages,
        "cache_hits": result.cache_hits,
        "chars_fetched": sum(s.chars for s in result.sources),
        "gemini_used": result.gemini_used,
        "elapsed_sec": round(result.metrics.elapsed_sec, 3),
        "version": __version__,
        "metrics": result.metrics.snapshot(),
    }
    if result.discovery:
        meta["discovery"] = result.discovery
    payload: dict[str, Any] = {
        "claims": claims_payload,
        "sources": sources_payload,
        "meta": meta,
    }
    if raw_material:
        payload["raw_material"] = raw_material
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _raw_material_block(material: list[tuple[ResearchSource, str]]) -> str:
    from .web_research import _material_blocks

    return "\n\n".join(_material_blocks("", material))


# ─── entry points ─────────────────────────────────────────────────────────────


def verify_cli(
    cfg: BridgeConfig,
    *,
    claims: list[str],
    urls: list[str] | None = None,
    discover: bool = False,
    discovery_query: str | None = None,
    discovery_limit: int = DEFAULT_DISCOVERY_LIMIT,
    wiki_host: str | None = None,
    fmt: str = "text",
    max_pages: int = DEFAULT_MAX_PAGES,
    max_chars_per_page: int = DEFAULT_MAX_CHARS_PER_PAGE,
    total_budget: int = DEFAULT_TOTAL_BUDGET,
    cache_ttl: int = DEFAULT_CACHE_TTL,
    no_cache: bool = False,
    refresh: bool = False,
    fetch_concurrency: int | None = None,
) -> str:
    """One-shot claim verification for the CLI: renders in the requested format."""
    cache = None if no_cache else ResearchCache(ResearchCache.default_path(), cache_ttl)
    result = verify(
        cfg,
        claims,
        urls,
        discover=discover,
        discovery_query=discovery_query,
        discovery_limit=discovery_limit,
        wiki_host=wiki_host,
        max_pages=max_pages,
        max_chars_per_page=max_chars_per_page,
        total_budget=total_budget,
        cache=cache,
        refresh=refresh,
        concurrency=fetch_concurrency,
    )
    if fmt == "json":
        return render_json(result)
    if fmt == "markdown":
        return render_markdown(result)
    return render_text(result)


def run_verify_tool(cfg: BridgeConfig, arguments: dict[str, Any]) -> str:
    """MCP ``web_verify`` dispatch. Text output with statuses and evidence."""
    raw_claims = arguments.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims:
        raise ValueError("web_verify requires a non-empty 'claims' list")
    claims = [str(c).strip() for c in raw_claims if str(c).strip()]

    raw_urls = arguments.get("urls")
    urls: list[str] | None = None
    if isinstance(raw_urls, list) and raw_urls:
        urls = [str(u).strip() for u in raw_urls if str(u).strip()] or None

    def _int(name: str, default: int, low: int, high: int) -> int:
        try:
            value = int(arguments.get(name) or default)
        except (TypeError, ValueError):
            value = default
        return max(low, min(value, high))

    max_pages = _int("max_pages", DEFAULT_MAX_PAGES, 1, 10)
    max_chars = _int(
        "max_chars_per_page",
        DEFAULT_MAX_CHARS_PER_PAGE,
        500,
        50_000,
    )
    fetch_concurrency = _int(
        "fetch_concurrency", int(getattr(cfg, "research_concurrency", 3)), 1, 8
    )
    discovery_limit = _int("discovery_limit", DEFAULT_DISCOVERY_LIMIT, 1, _DISCOVERY_LIMIT_CAP)
    discover = arguments.get("discover") is True
    discovery_query = str(arguments.get("discovery_query") or "").strip() or None
    wiki_host = str(arguments.get("wiki_host") or "").strip() or None

    if not urls and not discover:
        raise ValueError("web_verify needs 'urls' (a non-empty list) or 'discover': true")

    return verify_cli(
        cfg,
        claims=claims,
        urls=urls,
        discover=discover,
        discovery_query=discovery_query,
        discovery_limit=discovery_limit,
        wiki_host=wiki_host,
        fmt="text",
        max_pages=max_pages,
        max_chars_per_page=max_chars,
        fetch_concurrency=fetch_concurrency,
    )
