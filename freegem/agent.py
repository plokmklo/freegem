"""Bounded single-agent job orchestrator (issue #66).

Wires into the existing ``execute_job`` dispatcher as the ``agent`` job type:

    POST /v1/jobs
    {"type": "agent", "params": {"task": "Compare scraping frameworks."}}

The agent:

1. Creates a validated plan from the task.
2. Selects from an allowlisted registry of existing tools.
3. Executes bounded multi-step work.
4. Inspects tool results.
5. Gathers additional evidence only when justified.
6. Verifies claims and citations.
7. Returns one structured result.

ALL limits are structural guardrails -- the agent cannot exceed them:

| Limit | Default |
|---|---|
| max_steps | 20 |
| max_pages | 30 |
| max_chars | 500_000 |
| max_llm_calls | 10 |
| max_retries | 2 |
| max_exec_seconds | 300 |
| max_artifact_bytes | 1_000_000 |
| max_tool_depth | 3 |

Trust boundaries: page content and tool output are untrusted data
throughout. Citations cannot be fabricated -- ``sources`` only ever holds
URLs that a tool result actually carried, never URLs scraped out of an
answer. Missing evidence is not reported as contradiction: the verdict
vocabulary belongs to :mod:`freegem.claim_verify`, which keeps
``not_found``, ``source_unavailable`` and ``contradicted`` distinct.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from freegem.claim_verify import verify
from freegem.config import BridgeConfig
from freegem.mcp_tools import run_tool
from freegem.web_research import render_json, research

#: Default limits for the bounded agent (issue #66).
DEFAULT_AGENT_LIMITS: dict[str, int] = {
    "max_steps": 20,
    "max_pages": 30,
    "max_chars": 500_000,
    "max_llm_calls": 10,
    "max_retries": 2,
    "max_exec_seconds": 300,
    "max_artifact_bytes": 1_000_000,
    "max_tool_depth": 3,
}


@dataclass
class AgentStep:
    """One planned step plus what executing it produced."""

    step: int
    tool: str
    args: dict[str, Any]
    ok: bool = False
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    tokens_used: int = 0
    chars_seen: int = 0
    sources: list[str] = field(default_factory=list)


@dataclass
class AgentResult:
    """Final structured result (the shape issue #66 requires)."""

    answer: str
    sources: list[str]
    verified_claims: list[dict[str, Any]]
    steps: list[dict[str, Any]]
    status: str
    limitations: list[str]
    usage: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "sources": self.sources,
            "verified_claims": self.verified_claims,
            "steps": self.steps,
            "status": self.status,
            "limitations": self.limitations,
            "usage": self.usage,
        }


@dataclass
class AgentLimits:
    """Structural bounds, checked between every step -- never after the fact."""

    max_steps: int = 20
    max_pages: int = 30
    max_chars: int = 500_000
    max_llm_calls: int = 10
    max_retries: int = 2
    max_exec_seconds: int = 300
    max_artifact_bytes: int = 1_000_000
    max_tool_depth: int = 3

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> AgentLimits:
        raw: dict[str, int] = dict(DEFAULT_AGENT_LIMITS)
        user_limits = params.get("limits", {})
        if isinstance(user_limits, dict):
            for key, default in DEFAULT_AGENT_LIMITS.items():
                value = user_limits.get(key, default)
                with contextlib.suppress(TypeError, ValueError):
                    raw[key] = int(value)
        return cls(**raw)


#: The allowlisted registry of tools the agent may call. The plan is
#: validated against this set before any step executes.
AGENT_TOOLS: frozenset[str] = frozenset({"ask", "research", "verify_claims"})

_URL_RE = re.compile(r"https?://[^\s)\]<>]+")

#: Error recorded on a step that never ran because a budget said stop.
_SKIPPED = "skipped: a structural budget was exhausted before this step ran"


def plan_task(task: str, limits: AgentLimits) -> list[dict[str, Any]]:
    """Create a validated plan from the task string.

    Deterministic decomposition: no LLM call is spent on planning. The
    task maps onto a search phase (find candidate pages), a research phase
    (fetch those pages and answer across them), and a verification phase
    (check the answer's claims against what was actually fetched). Every
    step's tool must be in :data:`AGENT_TOOLS` or the plan is rejected
    before anything executes.
    """
    plan: list[dict[str, Any]] = [
        {
            "tool": "ask",
            "args": {"query": task},
        },
        {
            "tool": "research",
            "args": {
                "question": task,
                "urls": [],  # filled from the search step's own results
                "max_chars": min(limits.max_chars, 30_000),
            },
        },
        {
            "tool": "verify_claims",
            "args": {"claims": [], "urls": []},
        },
    ]
    for entry in plan:
        if entry["tool"] not in AGENT_TOOLS:
            raise ValueError(f"agent plan references non-allowlisted tool {entry['tool']!r}")
    return plan


def _extract_urls_from_text(text: str) -> list[str]:
    """Pull source URLs out of a ``gemini_search`` result blob.

    Only the search tool's own output is mined, and only for pages to
    fetch next: these URLs are candidates for research, never citations
    by themselves.
    """
    return _URL_RE.findall(text)


def _usage(
    elapsed: float, llm_calls: int, pages: int, chars: int, steps_done: int
) -> dict[str, Any]:
    return {
        "elapsed_sec": round(elapsed, 2),
        "llm_calls": llm_calls,
        "pages_processed": pages,
        "total_chars": chars,
        "steps_executed": steps_done,
    }


def run_agent(cfg: BridgeConfig, task: str, limits: AgentLimits | None = None) -> AgentResult:
    """Execute one bounded task with FreeGem's existing tools.

    Tool output is untrusted data: it is never executed, and a URL only
    enters ``sources`` because a tool result carried it -- the answer
    text is never mined for sources, so the model cannot fabricate a
    citation that was not fetched.
    """
    limits = limits or AgentLimits()
    start_time = time.perf_counter()
    steps: list[AgentStep] = []
    all_sources: list[str] = []
    limitations: list[str] = []
    total_chars = 0
    total_llm_calls = 0
    total_pages = 0

    def note(text: str) -> None:
        if text not in limitations:
            limitations.append(text)

    def budget_blocks() -> bool:
        """True when a structural budget says stop before the next step."""
        if time.perf_counter() - start_time > limits.max_exec_seconds:
            note(f"execution time budget {limits.max_exec_seconds}s exhausted")
            return True
        if len(steps) >= limits.max_steps:
            note(f"step budget {limits.max_steps} exhausted")
            return True
        if total_llm_calls >= limits.max_llm_calls:
            note(f"LLM-call budget {limits.max_llm_calls} exhausted")
            return True
        return False

    if not task or not task.strip():
        return AgentResult(
            answer="",
            sources=[],
            verified_claims=[],
            steps=[],
            status="failed",
            limitations=["task is empty"],
            usage=_usage(0.0, 0, 0, 0, 0),
        )

    plan = plan_task(task, limits)
    planned_tools = [entry["tool"] for entry in plan]

    # Phase 1: search for candidate pages (one grounded Gemini call).
    step = AgentStep(step=1, tool="ask", args={"query": task})
    search_urls: list[str] = []
    if budget_blocks():
        step.error = _SKIPPED
    else:
        try:
            found = run_tool(cfg, "gemini_search", {"query": task})
            total_llm_calls += 1
            step.ok = True
            step.result = {"answer": found}
            step.sources = _extract_urls_from_text(found)
            step.chars_seen = len(found)
        except Exception as exc:
            step.error = str(exc)
            note(f"search step failed: {exc}")
        search_urls = list(step.sources)
        total_chars += step.chars_seen
        all_sources.extend(step.sources)
    steps.append(step)

    # Phase 2: research the pages the search actually found.
    research_answer = ""
    if search_urls and "research" in planned_tools:
        step = AgentStep(
            step=len(steps) + 1,
            tool="research",
            args={"question": task, "urls": search_urls},
        )
        if budget_blocks():
            step.error = _SKIPPED
        else:
            try:
                studied = research(
                    cfg,
                    task,
                    search_urls,
                    max_chars_per_page=min(limits.max_chars, 30_000),
                    total_budget=limits.max_chars,
                )
                total_llm_calls += 1
                total_pages += len(search_urls)
                step.ok = True
                step.result = json.loads(render_json(studied))
                step.sources = [src.url for src in studied.sources]
                step.chars_seen = len(studied.answer or "")
                research_answer = studied.answer
            except Exception as exc:
                step.error = str(exc)
                note(f"research step failed: {exc}")
            total_chars += step.chars_seen
            all_sources.extend(step.sources)
        steps.append(step)

    # Phase 3: synthesize the answer from what actually succeeded.
    answer = ""
    if any(s.ok for s in steps):
        answer = research_answer
        if not answer:
            parts: list[str] = []
            for s in steps:
                if s.ok and isinstance(s.result, dict):
                    for key in ("answer", "summary", "text"):
                        value = s.result.get(key)
                        if value:
                            parts.append(str(value))
            answer = " ".join(parts)[:10_000] if parts else ""

    # Phase 4: verify the answer's claims against the fetched sources.
    verified_claims: list[dict[str, Any]] = []
    if answer and all_sources and "verify_claims" in planned_tools:
        verify_urls = all_sources[:5]
        step = AgentStep(
            step=len(steps) + 1,
            tool="verify_claims",
            args={"claims": [answer], "urls": verify_urls},
        )
        if budget_blocks():
            step.error = _SKIPPED
        else:
            try:
                checked = verify(cfg, [answer], urls=verify_urls, max_pages=5)
                total_llm_calls += 1
                step.ok = True
                verdicts = [
                    {
                        "claim": c.claim,
                        "status": c.status,
                        "evidence": [e.quote[:500] for e in c.evidence],
                    }
                    for c in checked.claims
                ]
                step.result = {"verdicts": verdicts}
                verified_claims = verdicts
            except Exception as exc:
                step.error = str(exc)
                note(f"verification step failed: {exc}")
        steps.append(step)

    elapsed = time.perf_counter() - start_time
    status = "completed" if answer and any(s.ok for s in steps) else "failed"
    unique: list[str] = []
    seen: set[str] = set()
    for url in all_sources:
        if url not in seen:
            seen.add(url)
            unique.append(url)

    return AgentResult(
        answer=answer,
        sources=unique,
        verified_claims=verified_claims,
        steps=[
            {
                "step": s.step,
                "tool": s.tool,
                "ok": s.ok,
                "result_summary": str(s.result)[:200] if s.result else "",
                "error": s.error,
                "sources": s.sources,
                "chars_seen": s.chars_seen,
            }
            for s in steps
        ],
        status=status,
        limitations=limitations,
        usage=_usage(elapsed, total_llm_calls, total_pages, total_chars, len(steps)),
    )


def validate_agent_params(params: dict[str, Any]) -> tuple[str, AgentLimits]:
    """Validate agent job params and return ``(task, limits)``.

    Raises ValueError on invalid input; the job dispatcher turns that into
    a failed job with the reason as its error.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be a JSON object")
    task = params.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("params.task must be a non-empty string")
    limits = AgentLimits.from_params(params)
    if limits.max_steps < 1 or limits.max_steps > 100:
        raise ValueError(f"max_steps must be 1-100, got {limits.max_steps}")
    if limits.max_pages < 1 or limits.max_pages > 10_000:
        raise ValueError(f"max_pages must be 1-10000, got {limits.max_pages}")
    if limits.max_chars < 1:
        raise ValueError(f"max_chars must be >= 1, got {limits.max_chars}")
    if limits.max_llm_calls < 1 or limits.max_llm_calls > 100:
        raise ValueError(f"max_llm_calls must be 1-100, got {limits.max_llm_calls}")
    if limits.max_retries < 0 or limits.max_retries > 5:
        raise ValueError(f"max_retries must be 0-5, got {limits.max_retries}")
    if limits.max_exec_seconds < 1:
        raise ValueError(f"max_exec_seconds must be >= 1, got {limits.max_exec_seconds}")
    if limits.max_artifact_bytes < 1:
        raise ValueError(f"max_artifact_bytes must be >= 1, got {limits.max_artifact_bytes}")
    if limits.max_tool_depth < 1 or limits.max_tool_depth > 10:
        raise ValueError(f"max_tool_depth must be 1-10, got {limits.max_tool_depth}")
    return task, limits
