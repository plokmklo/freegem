"""Honest scope boundaries: what FreeGem does NOT promise.

Every entry here is a deliberate non-goal or an inherent limit of reading
the live web. Stating them plainly keeps expectations aligned with reality:
FreeGem is a best-effort local reader of sites reachable from your machine
-- not a magic bullet, not an evasion tool, and not a hosted service.

The list is surfaced three ways so nobody can miss it:

- ``GET /health`` returns the short labels under ``non_guarantees``.
- ``GET /v1/about`` returns the full label/assumption/reality detail.
- ``docs/NON_GOALS.md`` and the README "Limitations" section carry the
  human-readable expansion.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["NON_GOALS", "NonGoal", "non_guarantee_labels"]


@dataclass(frozen=True)
class NonGoal:
    """One explicit non-promise.

    ``label`` is the short API-facing name. ``assumption`` is what a new
    user commonly assumes FreeGem provides. ``reality`` is what FreeGem
    actually does instead.
    """

    label: str
    assumption: str
    reality: str


NON_GOALS: tuple[NonGoal, ...] = (
    NonGoal(
        label="No guaranteed scraping SLA",
        assumption=(
            "Every URL succeeds with complete, fresh, accurate content, on any site, every time."
        ),
        reality=(
            "Scraping success, availability, completeness, freshness, and "
            "accuracy on arbitrary sites are best-effort. Sites go down, "
            "move, truncate, serve broken markup, and change without "
            "notice. Jobs and tools report what they actually got -- "
            "including partial content and hard failures -- so callers can "
            "decide, but nothing is guaranteed."
        ),
    ),
    NonGoal(
        label="No CAPTCHA solving, anti-bot bypass, stealth, or fingerprint evasion",
        assumption=(
            "A headless crawler that slips past CAPTCHAs, bot walls, and "
            "bot-detection fingerprints."
        ),
        reality=(
            "FreeGem fetches politely, like a normal client: honest user "
            "agent, per-host rate limits, no CAPTCHA solving, no stealth "
            "modes, no fingerprint evasion. That is a design stance, not a "
            "missing feature -- if a site hard-blocks you, FreeGem reports "
            "the failure instead of attacking the site."
        ),
    ),
    NonGoal(
        label="No free proxy pool or managed browser fleet",
        assumption="Bundled rotating proxies or a hosted browser cluster.",
        reality=(
            "Requests originate from your machine and your IPs. There is no "
            "proxy pool, no managed browser fleet, and no bundled egress "
            "infrastructure; if you need proxies, you configure and pay for "
            "them yourself."
        ),
    ),
    NonGoal(
        label="No proprietary web index or built-in SERP",
        assumption="A built-in search engine over the crawled web.",
        reality=(
            "FreeGem reads pages; it does not ship a web index or a SERP. "
            "What it does ship are the building blocks -- the crawler, "
            "simhash deduplication, and search-index sinks (SQLite FTS5, "
            "JSONL tf-idf) -- so you can implement and own your own index."
        ),
    ),
    NonGoal(
        label="No unlimited crawling or unlimited autoscaling",
        assumption="Crawl forever, at any concurrency, scaling without bounds.",
        reality=(
            "Concurrency, queue depth, request-body and response-body caps, "
            "and per-host politeness delays are deliberate guardrails that "
            "protect your machine and the target sites. They are "
            "configurable, not removable. Scaling out means running more "
            "workers on infrastructure you provide."
        ),
    ),
    NonGoal(
        label="No universal JavaScript, login, WebSocket, CAPTCHA, PDF, DOCX, or media support",
        assumption="One tool that renders, logs into, and parses everything.",
        reality=(
            "Adapters cover common cases: Playwright/Selenium render "
            "JavaScript on a best-effort basis, session profiles carry "
            "cookies you supply, PDF/DOCX adapters parse common documents, "
            "source adapters handle MediaWiki/arXiv/feeds. WebSockets, "
            "CAPTCHA flows, interactive logins, and exotic media formats "
            "are out of scope."
        ),
    ),
    NonGoal(
        label="No deterministic or always-correct LLM extraction",
        assumption="AI extraction that is right every time.",
        reality=(
            "LLM extraction and summarization are probabilistic. Schema "
            "constraints, validation with repair, and complaint-driven "
            "retries improve quality; they do not make the output "
            "deterministic or correct. Validate results before you depend "
            "on them."
        ),
    ),
    NonGoal(
        label="No hosted queues, databases, dashboards, proxies, or billing-free infrastructure",
        assumption="A managed service with free hosting included.",
        reality=(
            "FreeGem is a local tool. Redis/RabbitMQ queues, PostgreSQL "
            "stores, S3/object storage, dashboards, and proxies are "
            "adapters you point at infrastructure you run and pay for. "
            "Nothing hosted, nothing billed, nothing bundled."
        ),
    ),
)


def non_guarantee_labels() -> list[str]:
    """Short labels only -- what ``GET /health`` returns."""

    return [goal.label for goal in NON_GOALS]
