#!/usr/bin/env python3
"""Aggregate the live-1000 chunk results into the committed report.

Reads chunk_*.jsonl, classifies failures with the same taxonomy as
benchmarks/run_bench.py, and writes report.json, failures.jsonl,
thin.jsonl and summary.md under the output directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

THIN_CHARS = 200


def classify_failure(error: str) -> str:
    """Same heuristic buckets as benchmarks/run_bench.py."""
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
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = round(pct / 100 * (len(ordered) - 1))
    return ordered[rank]


def domain_of(url: str) -> str:
    if url.startswith("https://"):
        return url[8:].split("/")[0]
    if url.startswith("http://"):
        return url[7:].split("/")[0]
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description="aggregate live-1000 chunk results")
    parser.add_argument("--chunks", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    rows = []
    chunk_files = sorted(args.chunks.glob("chunk_*.jsonl"))
    for path in chunk_files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                rows.append(record)

    ok_rows = [row for row in rows if row.get("ok")]
    fail_rows = [row for row in rows if not row.get("ok")]
    buckets: dict[str, list[dict]] = {}
    for row in fail_rows:
        buckets.setdefault(classify_failure(str(row.get("error", ""))), []).append(row)
    ordered_buckets = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    via_counts: dict[str, int] = {}
    for row in ok_rows:
        via = str(row.get("via") or "?")
        via_counts[via] = via_counts.get(via, 0) + 1
    chars = [float(row.get("chars", 0) or 0) for row in ok_rows]
    thin = [row for row in ok_rows if float(row.get("chars", 0) or 0) < THIN_CHARS]

    report = {
        "total": len(rows),
        "ok": len(ok_rows),
        "failed": len(fail_rows),
        "success_rate": round(len(ok_rows) / len(rows), 4) if rows else 0.0,
        "chunk_files": [path.name for path in chunk_files],
        "via_counts": dict(sorted(via_counts.items(), key=lambda item: -item[1])),
        "chars": {
            "mean": round(sum(chars) / len(chars)) if chars else 0,
            "p50": round(percentile(chars, 50)),
            "p90": round(percentile(chars, 90)),
            "max": round(max(chars)) if chars else 0,
        },
        "thin_ok_lt_200_chars": len(thin),
        "failure_buckets": {name: len(items) for name, items in ordered_buckets},
    }
    (args.out / "report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )

    with open(args.out / "failures.jsonl", "w", encoding="utf-8") as fh:
        for name, items in ordered_buckets:
            for row in items:
                fh.write(
                    json.dumps(
                        {
                            "bucket": name,
                            "url": row.get("url", ""),
                            "error": str(row.get("error", ""))[:300],
                            "via": row.get("via", ""),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    with open(args.out / "thin.jsonl", "w", encoding="utf-8") as fh:
        for row in thin:
            fh.write(
                json.dumps(
                    {
                        "url": row.get("url", ""),
                        "chars": row.get("chars", 0),
                        "via": row.get("via", ""),
                        "excerpt": str(row.get("excerpt", ""))[:200],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    lines = [
        "# Live 1000-site scrape - aggregated results",
        "",
        f"- urls attempted: **{len(rows)}**",
        f"- ok: **{len(ok_rows)}** ({report['success_rate']:.1%})",
        f"- failed: **{len(fail_rows)}**",
        f"- thin extractions (ok but < {THIN_CHARS} chars): **{len(thin)}**",
        "",
        "## Failures by bucket",
        "",
        "| bucket | count | example domains |",
        "| --- | --- | --- |",
    ]
    for name, items in ordered_buckets:
        domains = []
        for row in items:
            domain = domain_of(str(row.get("url", "")))
            if domain and domain not in domains:
                domains.append(domain)
        lines.append(f"| {name} | {len(items)} | {', '.join(domains[:5])} |")
    lines += ["", "## Successes by via", "", "| via | count |", "| --- | --- |"]
    for via, count in report["via_counts"].items():
        lines.append(f"| {via} | {count} |")
    lines += [
        "",
        "## Extracted chars (ok rows)",
        "",
        f"mean {report['chars']['mean']}, p50 {report['chars']['p50']}, "
        f"p90 {report['chars']['p90']}, max {report['chars']['max']}",
        "",
        "## Sample errors per bucket",
        "",
    ]
    for name, items in ordered_buckets:
        lines.append(f"### {name} ({len(items)})")
        lines.append("")
        shown = 0
        seen_errors = set()
        for row in items:
            error = " ".join(str(row.get("error", "")).split())[:160]
            if error in seen_errors:
                continue
            seen_errors.add(error)
            lines.append(f"- `{error}` ({row.get('url', '')})")
            shown += 1
            if shown >= 5:
                break
        lines.append("")
    (args.out / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"aggregated {len(rows)} rows from {len(chunk_files)} chunk files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
