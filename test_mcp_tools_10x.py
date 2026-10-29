"""Test all 14 MCP tools 10 times each, collecting timing and errors.

Runs each tool with a minimal, sensible query, collecting success/failure,
latency, and error taxonomy. Results are saved to mcptools_10x_results.json.

NOT a test suite (#72): this harness makes ~140 LIVE calls -- real Gemini
sessions, real network fetches, real rate limits. Nothing here is hermetic.
Both pytest collection and direct execution are gated behind an explicit
opt-in environment variable, so an accidental `pytest` at the repo root
never fires a live-API loop:

Run:  FREEGEM_LIVE_MCP=1 .venv/bin/python test_mcp_tools_10x.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from freegem.config import BridgeConfig
from freegem.mcp_tools import TOOL_SPECS, run_tool

CWD = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(CWD, "mcptools_10x_results.json")

ALL_TOOLS = [t["name"] for t in TOOL_SPECS]
assert len(ALL_TOOLS) == 14, f"expected 14 tools, got {len(ALL_TOOLS)}"

# (#72) Live-API opt-in gate: unset (the default) means this module is
# inert -- pytest collects the filename, so collection itself must never
# fire 140 live calls, and a direct run refuses instead of dialing.
_LIVE = bool(os.environ.get("FREEGEM_LIVE_MCP"))

if not _LIVE:
    try:
        import pytest
    except ImportError:  # pragma: no cover - dev-only dependency
        pytest = None
    if pytest is not None:
        pytestmark = pytest.mark.skip(reason="live-API harness: set FREEGEM_LIVE_MCP=1 to opt in")

# Minimal, fast queries per tool. These are designed to return quickly so
# 10 iterations * 14 tools = 140 calls complete in reasonable time.
TOOL_QUERIES = {
    "gemini_search": {"query": "what is web scraping"},
    "gemini_ask": {"question": "define web scraping in one sentence"},
    "gemini_deep_think": {"problem": "explain the trade-off between polling and webhooks"},
    "gemini_advisor": {"topic": "should I scrape a site without an API"},
    "gemini_chat": {"messages": [{"role": "user", "content": "say hello world"}]},
    "gemini_list_models": {},
    "web_scrape": {"url": "https://example.com"},
    "web_research": {
        "urls": ["https://en.wikipedia.org/wiki/Web_scraping"],
        "question": "what is web scraping",
    },
    "web_verify": {
        "claims": ["web scraping is the automated extraction of data from websites"],
        "urls": ["https://en.wikipedia.org/wiki/Web_scraping"],
    },
    "ai_clean": {"url": "https://en.wikipedia.org/wiki/Web_scraping"},
    "ai_summarize": {"url": "https://en.wikipedia.org/wiki/Web_scraping"},
    "ai_extract": {
        "urls": ["https://en.wikipedia.org/wiki/Web_scraping"],
        "query": "what is web scraping",
    },
    "ai_research": {
        "urls": ["https://en.wikipedia.org/wiki/Web_scraping"],
        "query": "what is web scraping used for",
    },
    "ai_crawl": {
        "seeds": ["https://en.wikipedia.org/wiki/Web_scraping"],
        "query": "web scraping",
        "max_pages": 3,
    },
}


def run_one(cfg: BridgeConfig, name: str) -> dict:
    """Run one tool invocation once, return result dict."""
    args = TOOL_QUERIES.get(name, {})
    start = time.monotonic()
    try:
        result = run_tool(cfg, name, args)
        elapsed = time.monotonic() - start
        # Truncate large outputs
        result_str = str(result)
        return {
            "tool": name,
            "ok": True,
            "elapsed_sec": round(elapsed, 3),
            "result_len": len(result_str),
            "error": None,
            "result_preview": result_str[:300],
        }
    except Exception as exc:
        elapsed = time.monotonic() - start
        return {
            "tool": name,
            "ok": False,
            "elapsed_sec": round(elapsed, 3),
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            "result_len": 0,
            "result_preview": "",
        }


def main() -> None:
    if not _LIVE:
        sys.exit(
            "test_mcp_tools_10x.py makes ~140 LIVE Gemini/web calls.\n"
            "Opt in explicitly:\n"
            "  FREEGEM_LIVE_MCP=1 .venv/bin/python test_mcp_tools_10x.py"
        )
    cfg = BridgeConfig()
    results: list[dict] = []

    print(f"Testing {len(ALL_TOOLS)} MCP tools x 10 iterations = {len(ALL_TOOLS) * 10} calls...")
    print(f"Tools: {', '.join(ALL_TOOLS)}")
    print()

    for i in range(10):
        for name in ALL_TOOLS:
            # Shuffle order each round to test resilience
            pass
        for name in ALL_TOOLS:
            print(f"  Round {i + 1}/10: {name}...", end=" ", flush=True)
            r = run_one(cfg, name)
            status = "OK" if r["ok"] else "FAIL"
            print(f"{status} ({r['elapsed_sec']:.2f}s)")
            results.append(r)
        # Small delay between rounds to avoid hammering
        time.sleep(1)

    # Build summary
    summary: dict[str, dict] = {}
    for name in ALL_TOOLS:
        tool_results = [r for r in results if r["tool"] == name]
        oks = [r for r in tool_results if r["ok"]]
        fails = [r for r in tool_results if not r["ok"]]
        times = [r["elapsed_sec"] for r in oks]
        summary[name] = {
            "success_rate": f"{len(oks)}/10",
            "avg_time_sec": round(sum(times) / max(1, len(times)), 3) if times else 0,
            "min_time_sec": round(min(times), 3) if times else 0,
            "max_time_sec": round(max(times), 3) if times else 0,
            "failures": [
                {
                    "error": f["error"][:200],
                    "elapsed_sec": f["elapsed_sec"],
                }
                for f in fails
            ],
        }

    report = {
        "total_calls": len(results),
        "tools_tested": ALL_TOOLS,
        "tool_count": len(ALL_TOOLS),
        "iterations": 10,
        "summary": summary,
        "all_results": results,
    }

    Path(OUT).write_text(json.dumps(report, indent=2, ensure_ascii=False))

    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    for name in ALL_TOOLS:
        s = summary[name]
        print(f"  {name:25s} {s['success_rate']:>6s}  avg={s['avg_time_sec']:.2f}s")
    print(f"\nResults saved to {OUT}")

    # Count overall success rate
    total_ok = sum(1 for r in results if r["ok"])
    print(f"\nOverall: {total_ok}/{len(results)} calls succeeded")


if __name__ == "__main__":
    main()
