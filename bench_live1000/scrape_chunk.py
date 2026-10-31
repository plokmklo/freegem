#!/usr/bin/env python3
"""Scrape one chunk of the live-1000 corpus through the shipped freegem CLI.

Slices urls.json to this job's chunk, then drives the URLs through
`freegem scrape --format jsonl --no-cache` in sub-batches (BATCH URLs per
invocation, --concurrency parallel fetches). Page text is stripped from
the records (only a 200-char excerpt is kept) so the committed results
stay small; failures are recorded per URL, never fatal for the chunk.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time

BATCH = 25
CONCURRENCY = 8
SUBPROCESS_TIMEOUT_SEC = 1500
EXCERPT_CHARS = 200


def run_batch(urls: list[str]) -> tuple[list[dict], float]:
    """One freegem scrape invocation covering up to BATCH urls."""
    command = [
        "freegem",
        "scrape",
        *urls,
        "--format",
        "jsonl",
        "--no-cache",
        "--max-chars",
        "50000",
        "--concurrency",
        str(CONCURRENCY),
    ]
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SEC, check=False
        )
    except subprocess.TimeoutExpired:
        wall = time.perf_counter() - started
        return [
            {"url": url, "ok": False, "error": "runner: subprocess timeout", "excerpt": ""}
            for url in urls
        ], wall
    wall = time.perf_counter() - started
    records: dict[str, dict] = {}
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        url = str(record.get("url") or "")
        if not url:
            continue
        text = record.pop("text", "") or ""
        record.setdefault("chars", len(text))
        record["excerpt"] = " ".join(str(text)[:EXCERPT_CHARS].split())
        records[url] = record
    missing_error = proc.stderr.strip()[:300] or f"runner: no record, exit {proc.returncode}"
    out = []
    for url in urls:
        record = records.get(url)
        if record is None:
            record = {"url": url, "ok": False, "error": missing_error, "excerpt": ""}
        record.setdefault("excerpt", "")
        out.append(record)
    return out, wall


def main() -> int:
    parser = argparse.ArgumentParser(description="scrape one live-1000 chunk")
    parser.add_argument("--list", default="urls.json")
    parser.add_argument("--chunk", type=int, required=True)
    parser.add_argument("--chunks", type=int, required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    with open(args.list, encoding="utf-8") as fh:
        urls = json.load(fh)["urls"]
    size = (len(urls) + args.chunks - 1) // args.chunks
    mine = urls[args.chunk * size : (args.chunk + 1) * size]
    print(f"chunk {args.chunk}: {len(mine)} urls, batches of {BATCH}", flush=True)

    ok_count = 0
    with open(args.out, "w", encoding="utf-8") as fh:
        for start in range(0, len(mine), BATCH):
            batch = mine[start : start + BATCH]
            records, wall = run_batch(batch)
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            ok_now = sum(1 for record in records if record.get("ok"))
            ok_count += ok_now
            print(
                f"batch {start // BATCH + 1}: {ok_now}/{len(records)} ok in {wall:.1f}s",
                flush=True,
            )
    print(f"chunk {args.chunk} done: {ok_count}/{len(mine)} ok", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
