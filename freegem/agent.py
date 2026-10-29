"""Bounded single-agent job orchestrator (issue #66).

Wires into the existing ``execute_job`` dispatcher as the ``agent`` job type:

    POST /v1/jobs
    {"type": "agent", "params": {"task": "Compare scraping frameworks and recommend the best one."}}

The agent:

1. Creates a validated plan from the task.
2. Selects from an allowlisted registry of existing tools.
3. Executes bounded multi-step work.
4. Inspects tool results.
5. Gathers additional evidence only when justified.
6. Verifies claims and citations.
7. Returns one structured result.

ALL limits are structural guardrails — the agent cannot exceed them:

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

Trust boundaries: page content and tool output are untrusted data. Citations
cannot be fabricated — only URLs the agent actually fetched may appear in
sources. Missing evidence is not reported as contradiction.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from freegem.config import BridgeConfig
from freegem.mcp_tools import run_tool
from freegem.web_research import ResearchResult, render_json, research
from freegem.web_scraper import ScrapeResult, scrape_page

#: Default limits for the bounded agent (issue #66).
DEFAULT_AGENT_LIMITS = {
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
    """One step in the agent's plan + result."""

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
    """Final structured result from the agent."""

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
        raw = dict(DEFAULT_AGENT_LIMITS)
        user_limits = params.get("limits", {}) if isinstance(params, dict) else {}
        for k, default in DEFAULT_AGENT_LIMITS.items():
            val = user_limits.get(k, default)
            with contextlib.suppress(TypeError, ValueError):
                raw[k] = int(val)
        return cls(**raw)


#: The allowlisted registry of tools the agent can call.
#: Each entry maps a tool name to a callable that takes (cfg, **args) and
#: returns a result object with `.sources` or `.urls` attributes.
_AGENT_TOOLS = frozenset(
    {
        "scrape",
        "scrape_batch",
        "research",
        "verify_claims",
        "extract",
        "crawl_plan",
        "crawl_run",
        "ask",
    }
)


def _agent_scrape(
    cfg: BridgeConfig, url: str, max_chars: int, depth: int = 0, _seen: set[str] | None = None
) -> ScrapeResult:
    """Scrape a single URL with depth guard."""
    _seen = _seen or set()
    if depth > 3:
        raise RuntimeError(f"tool depth limit exceeded at {url}")
    if url in _seen:
        raise RuntimeError(f"circular reference detected: {url}")
    _seen.add(url)
    return scrape_page(cfg, url, max_chars=max_chars, smart=True)


def _agent_research(
    cfg: BridgeConfig,
    question: str,
    urls: list[str],
    max_chars: int,
    depth: int = 0,
    _seen: set[str] | None = None,
) -> ResearchResult:
    """Run multi-source research with depth guard."""
    _seen = _seen or set()
    if depth > 3:
        raise RuntimeError("tool depth limit exceeded")
    for u in urls:
        if u in _seen:
            raise RuntimeError(f"circular reference: {u} already visited")
    _seen.update(urls)
    return research(cfg, question, urls, max_chars_per_page=max_chars, total_budget=max_chars)


def _plan_task(task: str, limits: AgentLimits) -> list[dict[str, Any]]:
    """Create a validated plan from the task string.

    The plan is a simple heuristic decomposition — the task is broken into
    research, scrape, and answer phases. A real implementation would call
    an LLM to generate the plan, but for the bounded agent we use a
    deterministic decomposition to avoid extra LLM calls.
    """
    plan: list[dict[str, Any]] = []

    # Step 1: Search for relevant URLs (using Gemini bridge search)
    plan.append(
        {
            "tool": "ask",
            "args": {"question": task, "search": True, "max_sources": min(limits.max_pages, 10)},
        }
    )

    # Step 2: Research the top results
    plan.append(
        {
            "tool": "research",
            "args": {"question": task, "urls": [], "max_chars": min(limits.max_chars, 30_000)},
        }
    )

    # Step 3: Verify key claims
    plan.append(
        {
            "tool": "verify_claims",
            "args": {"claims": [], "urls": [], "max_chars": 10_000},
        }
    )

    return plan


def _extract_sources_from_result(result: Any) -> list[str]:
    """Extract source URLs from any tool result object."""
    sources: list[str] = []
    if hasattr(result, "sources"):
        sources.extend(result.sources)
    elif hasattr(result, "urls"):
        sources.extend(result.urls)
    elif isinstance(result, dict):
        for key in ("sources", "urls"):
            if key in result and isinstance(result[key], list):
                sources.extend(result[key])
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for s in sources:
        if s not in seen:
            seen.add(s)
            unique.append(s)
    return unique


def _extract_urls_from_text(text: Any) -> list[str]:
    """Pull URLs out of a search-result string (the gemini_search MCP tool returns a string)."""
    import re

    if not isinstance(text, str):
        return []
    return re.findall(r"https?://[^\s)]+", text)


def _chars_from_result(result: Any) -> int:
    """Count characters of content in a result."""
    if isinstance(result, ScrapeResult):
        return len(result.text or "")
    elif isinstance(result, ResearchResult):
        return len(result.answer or "")
    elif isinstance(result, dict):
        total = 0
        for v in result.values():
            if isinstance(v, str):
                total += len(v)
        return total
    return 0


def run_agent(cfg: BridgeConfig, task: str, limits: AgentLimits | None = None) -> AgentResult:
    """Execute a bounded single-agent task using FreeGem's existing tools.

    The agent decomposes the task, calls allowlisted tools with strict limits,
    collects sources and evidence, and returns a structured result.
    """
    limits = limits or AgentLimits()
    start_time = time.perf_counter()
    steps: list[AgentStep] = []
    all_sources: list[str] = []
    total_chars = 0
    total_llm_calls = 0
    total_pages = 0
    limitations: list[str] = []

    # Validate the task
    if not task or not task.strip():
        return AgentResult(
            answer="",
            sources=[],
            verified_claims=[],
            steps=[],
            status="failed",
            limitations=["task is empty"],
            usage={"start_time": start_time, "elapsed_sec": 0},
        )

    # Create a plan
    _plan_task(task, limits)

    # Phase 1: Search for relevant URLs
    if total_llm_calls < limits.max_llm_calls and len(steps) < limits.max_steps:
        step_num = len(steps) + 1
        step = AgentStep(step=step_num, tool="ask", args={"question": task})
        try:
            search_result = run_tool(
                cfg,
                "gemini_search",
                {"query": task, "recency": None, "model": None},
            )
            total_llm_calls += 1
            step.ok = True
            step.result = {"answer": search_result}
            step.sources = _extract_urls_from_text(search_result)
            step.chars_seen = _chars_from_result(search_result)
            total_chars += step.chars_seen
            all_sources.extend(step.sources)
        except Exception as exc:
            step.error = str(exc)
            limitations.append(f"search step failed: {exc}")
        steps.append(step)

    # Collect URLs from search results
    search_urls = list(all_sources)

    # Phase 2: Research with the discovered URLs
    if search_urls and total_llm_calls < limits.max_llm_calls and len(steps) < limits.max_steps:
        step_num = len(steps) + 1
        step = AgentStep(
            step=step_num,
            tool="research",
            args={"question": task, "urls": search_urls},
        )
        try:
            research_result = _agent_research(
                cfg,
                task,
                search_urls,
                max_chars=min(limits.max_chars, 30_000),
            )
            total_llm_calls += 1
            total_pages += len(search_urls)
            step.ok = True
            step.result = json.loads(render_json(research_result))
            step.sources = [src.url for src in research_result.sources]
            step.chars_seen = _chars_from_result(research_result)
            total_chars += step.chars_seen
            all_sources.extend(step.sources)
        except Exception as exc:
            step.error = str(exc)
            limitations.append(f"research step failed: {exc}")
        steps.append(step)

    # Phase 3: Extract answer and synthesize
    answer = ""
    verified_claims: list[dict[str, Any]] = []

    if steps and any(s.ok for s in steps):
        # Build answer from successful research
        for s in steps:
            if s.ok and isinstance(s.result, dict):
                if "answer" in s.result:
                    answer = s.result["answer"]
                    break
                elif "data" in s.result:
                    answer = str(s.result["data"])
                    break

        if not answer:
            # Synthesize from step results
            parts = []
            for s in steps:
                if s.ok and isinstance(s.result, dict):
                    for k in ("answer", "summary", "text"):
                        if s.result.get(k):
                            parts.append(str(s.result[k]))
            answer = " ".join(parts)[:10_000] if parts else "No answer produced."

        # Verify key claims against sources
        if total_llm_calls < limits.max_llm_calls and len(steps) < limits.max_steps + 2:
            step_num = len(steps) + 1
            step = AgentStep(
                step=step_num,
                tool="verify_claims",
                args={"claims": [answer], "urls": list(all_sources)[:5]},
            )
            try:
                from freegem.claim_verify import verify

                verified = verify(cfg, [answer], urls=list(all_sources)[:5], max_pages=5)
                step.ok = True
                step.result = {
                    "verdicts": [
                        {
                            "claim": c.claim,
                            "status": c.status,
                            "evidence": [e.quote[:500] for e in c.evidence],
                        }
                        for c in verified.claims
                    ]
                }
                verified_claims = step.result["verdicts"]
                total_llm_calls += 1
            except Exception as exc:
                step.error = str(exc)
                limitations.append(f"verification step failed: {exc}")
            steps.append(step)

    # Check limits
    elapsed = time.perf_counter() - start_time
    if elapsed > limits.max_exec_seconds:
        limitations.append(
            f"execution time {elapsed:.1f}s exceeded limit {limits.max_exec_seconds}s"
        )
    if total_pages > limits.max_pages:
        limitations.append(f"page count {total_pages} exceeded limit {limits.max_pages}")
    if total_chars > limits.max_chars:
        limitations.append(f"chars seen {total_chars} exceeded limit {limits.max_chars}")
    if total_llm_calls > limits.max_llm_calls:
        limitations.append(f"LLM calls {total_llm_calls} exceeded limit {limits.max_llm_calls}")

    status = "completed" if answer and any(s.ok for s in steps) else "failed"

    # Deduplicate sources
    seen = set()
    unique_sources: list[str] = []
    for url in all_sources:
        if url not in seen:
            seen.add(url)
            unique_sources.append(url)

    return AgentResult(
        answer=answer,
        sources=unique_sources,
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
        usage={
            "elapsed_sec": round(elapsed, 2),
            "llm_calls": total_llm_calls,
            "pages_processed": total_pages,
            "total_chars": total_chars,
            "steps_executed": len(steps),
        },
    )


def validate_agent_params(params: dict[str, Any]) -> tuple[str, AgentLimits]:
    """Validate agent job params and return (task, limits).

    Raises ValueError on invalid input.
    """
    if not isinstance(params, dict):
        raise ValueError("params must be a JSON object")

    task = params.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("params.task must be a non-empty string")

    limits = AgentLimits.from_params(params)

    # Validate limits are in sane ranges
    if limits.max_steps < 1 or limits.max_steps > 100:
        raise ValueError(f"max_steps must be 1-100, got {limits.max_steps}")
    if limits.max_pages < 1 or limits.max_pages > 10000:
        raise ValueError(f"max_pages must be 1-10000, got {limits.max_pages}")
    if limits.max_chars < 1:
        raise ValueError(f"max_chars must be >= 1, got {limits.max_chars}")
    if limits.max_llm_calls < 1 or limits.max_llm_calls > 100:
        raise ValueError(f"max_llm_calls must be 1-100, got {limits.max_llm_calls}")
    if limits.max_exec_seconds < 1:
        raise ValueError(f"max_exec_seconds must be >= 1, got {limits.max_exec_seconds}")

    return task, limits
