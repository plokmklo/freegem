#!/usr/bin/env python3
"""Reproducible URL-corpus benchmark for freegem's fetch layer (issue #68).

Runs a frozen corpus of real URLs through the shipped "freegem scrape"
surface and reports the four numbers a developer needs to judge the engine:
success rate, extraction quality, latency, and failure categories - per
corpus category.

Deliberately NOT part of the pytest suite: it fetches live sites, so it runs
on a developer machine (or the daily canary), not in hermetic CI. The corpus
shape and the aggregation helpers are pinned hermetically by
freegem/tests/test_bench_corpus.py.

Usage::

    python benchmarks/run_bench.py                      # full corpus
    python benchmarks/run_bench.py --limit 5           # first 5 URLs
    python benchmarks/run_bench.py --repeat 3           # 3 passes, medians
    python benchmarks/run_bench.py --no-smart           # direct fetch only
    python benchmarks/run_bench.py --out report.json    # report location

Latency is wall-clock per URL minus a measured "freegem --version"
process-start baseline, so the numbers approximate in-process fetch time.
A short gap between fetches keeps the run polite; robots.txt is not
consulted (same stance as the scraper).
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = REPO_ROOT / "benchmarks" / "corpus.jsonl"
DEFAULT_OUT = REPO_ROOT / "benchmarks" / "report.json"
FETCH_GAP_SEC = 0.5
CATEGORIES = ("reference", "docs", "news", "spa", "bot_walled", "static", "dead")


def load_corpus(path: Path) -> list[dict[str, str]]:
    """Read the frozen corpus; file order is kept so runs are comparable."""
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(json.loads(line))
    return entries


def classify_failure(error: str) -> str:
    """Bucket a scrape error string into the failure taxonomy (heuristic).

    Unknown shapes land in "other", so a report always reconciles:
    successes + every failure category == corpus size.
    """
    text = (error or "").lower()
    if "ssrf" in text or "refus" in text:
        return "refused"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "dns" in text or "getaddrinfo" in text:
        return "dns"
    if "too large" in text or "cap " in text:
        return "cap"
    if "403" in text or "401" in text or "429" in text or "challenge" in text or "bot" in text:
        return "wall"
    if "http 4" in text or "404" in text:
        return "http_error"
    if "http 5" in text or "502" in text or "503" in text or "504" in text:
        return "server_error"
    if "empty" in text or "nav" in text or "no text" in text:
        return "empty_extraction"
    return "other"


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = round(pct / 100 * (len(ordered) - 1))
    return ordered[rank]


def mean_chars(rows: list[dict]) -> float:
    """Mean extracted characters across a category's rows."""
    if not rows:
        return 0.0
    total = sum(float(row.get("chars", 0)) for row in rows)
    return round(total / len(rows))


def scrape_url(url: str, smart: bool) -> tuple[dict, float]:
    """One URL through the shipped CLI; returns (record, wall_seconds)."""
    command = [
        "freegem",
        "scrape",
        url,
        "--format",
        "jsonl",
        "--no-cache",
        "--max-chars",
        "50000",
    ]
    if not smart:
        command.append("--no-smart")
    started = time.perf_counter()
    proc = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    elapsed = time.perf_counter() - started
    stdout = proc.stdout.strip()
    record: dict = {}
    if stdout:
        try:
            parsed = json.loads(stdout.splitlines()[0])
        except json.JSONDecodeError:
            parsed = {}
        if isinstance(parsed, dict):
            record = parsed
    record["returncode"] = proc.returncode
    if not record.get("ok") and not record.get("error"):
        record["error"] = proc.stderr.strip()[:300] or f"exit {proc.returncode}"
    return record, elapsed


def aggregate(rows: list[dict]) -> dict[str, dict]:
    """Per-category success / latency / quality / failure summaries."""
    by_category: dict[str, list[dict]] = {}
    for row in rows:
        by_category.setdefault(str(row.get("category", "?")), []).append(row)
    summary: dict[str, dict] = {}
    for category, cat_rows in sorted(by_category.items()):
        ok_rows = [row for row in cat_rows if row.get("ok")]
        latencies = [float(row.get("wall_ms", 0.0)) for row in cat_rows]
        failures: dict[str, int] = {}
        for row in cat_rows:
            if not row.get("ok"):
                key = classify_failure(str(row.get("error", "")))
                failures[key] = failures.get(key, 0) + 1
        summary[category] = {
            "urls": len(cat_rows),
            "success_rate": round(len(ok_rows) / len(cat_rows), 3) if cat_rows else 0.0,
            "latency_ms_p50": round(percentile(latencies, 50)),
            "latency_ms_p95": round(percentile(latencies, 95)),
            "mean_chars": mean_chars(cat_rows),
            "failures": failures,
        }
    return summary


def markdown_table(summary: dict[str, dict]) -> str:
    """One glanceable table for the terminal."""
    lines = [
        "| category | urls | success | p50 ms | p95 ms | mean chars | failures |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for category, stats in summary.items():
        lines.append(
            f"| {category} | {stats['urls']} | {stats['success_rate']:.0%} | "
            f"{stats['latency_ms_p50']} | {stats['latency_ms_p95']} | "
            f"{stats['mean_chars']} | {json.dumps(stats['failures'])} |"
        )
    return "\n".join(lines)


def measure_baseline() -> float:
    """Process-start overhead: one "freegem --version" call."""
    started = time.perf_counter()
    subprocess.run(["freegem", "--version"], capture_output=True, text=True, check=False)
    return time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser(description="freegem URL-corpus benchmark (issue #68)")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--limit", type=int, default=0, help="only the first N URLs")
    parser.add_argument("--repeat", type=int, default=1, help="passes per URL; medians")
    parser.add_argument("--no-smart", action="store_true", help="direct fetch only")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    corpus = load_corpus(args.corpus)
    if args.limit:
        corpus = corpus[: args.limit]

    print(f"corpus: {len(corpus)} URLs from {args.corpus}")
    print(f"smart-fetch ladder: {'off (--no-smart)' if args.no_smart else 'on'}")

    baseline = measure_baseline()
    print(f"baseline process overhead: {baseline * 1000:.0f} ms")

    rows: list[dict] = []
    for index, entry in enumerate(corpus, start=1):
        latencies = []
        record: dict = {}
        for _ in range(max(1, args.repeat)):
            record, elapsed = scrape_url(entry["url"], smart=not args.no_smart)
            latencies.append(elapsed)
        row = dict(record)
        row["category"] = entry["category"]
        row["expectation"] = entry.get("expectation", "")
        row["wall_ms"] = max(0.0, (statistics.median(latencies) - baseline) * 1000)
        rows.append(row)
        mark = "ok  " if record.get("ok") else "FAIL"
        print(
            f"[{mark}] ({index}/{len(corpus)}) {entry['category']:<10} "
            f"{entry['url']:<55} via={record.get('via', '-')}"
        )
        time.sleep(FETCH_GAP_SEC)

    summary = aggregate(rows)
    report = {
        "corpus": str(args.corpus),
        "urls": len(rows),
        "repeat": max(1, args.repeat),
        "smart": not args.no_smart,
        "baseline_ms": round(baseline * 1000),
        "by_category": summary,
        "rows": rows,
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {args.out}\n")
    print(markdown_table(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
