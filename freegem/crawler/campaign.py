"""Bulk fetch campaign: many sites, many encodings, many failure modes.

The campaign answers a question a single scrape cannot: "how does the fetch
stack actually behave across a thousand sites -- old and new hand-written
HTML, news portals, PHP forums, Java application servers, Shift-JIS and GBK
pages, bot-walled CDNs?" The point is the FAILURE distribution, not the
green checkmarks: the report's blocked/timeout/network buckets are what
drives fetch upgrades (header sets, retry policy, decoding).

It is a library first and a CLI second::

    python -m freegem.crawler.campaign run --seeds seeds.jsonl \\
        --report report.json --concurrency 8 --limit 1000

Seeds are JSONL: ``{"url": "...", "category": "news", "note": "..."}``.
State is a SQLite file (``--state``), so a killed campaign resumes where it
stopped and the report is reproducible from the recorded outcomes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..config import BridgeConfig
from ..web_scraper import RobotsCache
from .fetch import FetchOutcome, crawl_fetch
from .hardening import ResourceLimits

__all__ = ["CampaignResult", "load_seeds", "run_campaign"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS results(
  url        TEXT PRIMARY KEY,
  category   TEXT NOT NULL DEFAULT '',
  kind       TEXT NOT NULL,
  status     INTEGER,
  ms         INTEGER NOT NULL DEFAULT 0,
  bytes      INTEGER NOT NULL DEFAULT 0,
  error      TEXT,
  attempted_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS queue(
  url      TEXT PRIMARY KEY,
  category TEXT NOT NULL DEFAULT '',
  note     TEXT
);
"""


@dataclass
class CampaignResult:
    """Aggregated outcome of one campaign run."""

    attempted: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)
    blocked_samples: list[dict[str, Any]] = field(default_factory=list)
    error_samples: list[dict[str, Any]] = field(default_factory=list)
    total_bytes: int = 0
    total_ms: int = 0

    def record(self, url: str, category: str, outcome: FetchOutcome) -> None:
        self.attempted += 1
        self.by_kind[outcome.kind] = self.by_kind.get(outcome.kind, 0) + 1
        if outcome.status is not None:
            key = str(outcome.status)
            self.by_status[key] = self.by_status.get(key, 0) + 1
        bucket = self.by_category.setdefault(category, {})
        bucket[outcome.kind] = bucket.get(outcome.kind, 0) + 1
        self.total_bytes += len(outcome.body or b"")
        self.total_ms += outcome.elapsed_ms
        if outcome.blocked and len(self.blocked_samples) < 25:
            self.blocked_samples.append(
                {
                    "url": url,
                    "status": outcome.status,
                    "kind": outcome.kind,
                    "server": outcome.headers.get("server", ""),
                }
            )
        if outcome.kind in ("network", "timeout", "ssrf") and len(self.error_samples) < 25:
            self.error_samples.append(
                {"url": url, "kind": outcome.kind, "error": (outcome.error or "")[:120]}
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "by_kind": dict(sorted(self.by_kind.items(), key=lambda kv: -kv[1])),
            "by_status": dict(sorted(self.by_status.items())),
            "by_category": self.by_category,
            "blocked_samples": self.blocked_samples,
            "error_samples": self.error_samples,
            "total_bytes": self.total_bytes,
            "avg_ms": (self.total_ms // self.attempted) if self.attempted else 0,
        }


def load_seeds(path: str) -> list[dict[str, str]]:
    """Read JSONL seeds: one ``{"url":..., "category":...}`` per line."""
    seeds: list[dict[str, str]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            url = str(data.get("url", "")).strip()
            if url:
                seeds.append({"url": url, "category": str(data.get("category", "uncategorized"))})
    return seeds


def run_campaign(
    cfg: BridgeConfig,
    seeds: list[dict[str, str]],
    *,
    state_path: str,
    report_path: str = "",
    concurrency: int = 8,
    limit: int = 1000,
    timeout_sec: float = 12.0,
    robots: bool = True,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> CampaignResult:
    """Fetch every seed once (resume-aware), persist outcomes, aggregate."""
    limits = ResourceLimits(timeout_sec=timeout_sec, max_bytes=2 * 1024 * 1024)
    robots_cache = RobotsCache() if robots else None
    db = sqlite3.connect(state_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    with db:
        db.executescript(_SCHEMA)

    queue = seeds[:limit]
    with db:
        for seed in queue:
            db.execute(
                "INSERT OR IGNORE INTO queue(url, category) VALUES(?,?)",
                (seed["url"], seed.get("category", "uncategorized")),
            )

    result = CampaignResult()
    lock = threading.Lock()
    cancel = threading.Event()

    def work(row: sqlite3.Row) -> None:
        if cancel.is_set():
            return
        url = str(row["url"])
        category = str(row["category"])
        outcome = crawl_fetch(cfg, url, limits=limits, robots=robots_cache)
        with lock:
            result.record(url, category, outcome)
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO results"
                    "(url, category, kind, status, ms, bytes, error, attempted_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        url,
                        category,
                        outcome.kind,
                        outcome.status,
                        outcome.elapsed_ms,
                        len(outcome.body or b""),
                        outcome.error,
                        time.time(),
                    ),
                )
            if on_event is not None:
                on_event({"event": "fetched", **outcome.summary(), "category": category})

    rows = db.execute("SELECT url, category FROM queue ORDER BY url").fetchall()
    try:
        with ThreadPoolExecutor(max_workers=max(1, min(concurrency, 16))) as pool:
            for future in [pool.submit(work, row) for row in rows]:
                future.result()
    except KeyboardInterrupt:  # graceful: keep what finished, state is on disk
        cancel.set()
    finally:
        db.close()

    if report_path:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(result.as_dict(), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    return result


def _cli(argv: list[str] | None = None) -> int:
    """Standalone entry: ``python -m freegem.crawler.campaign run ...``."""
    import argparse

    parser = argparse.ArgumentParser(prog="freegem-campaign")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="fetch every seed once and report")
    run_p.add_argument("--seeds", required=True, help="JSONL seeds file")
    run_p.add_argument("--state", required=True, help="SQLite state file")
    run_p.add_argument("--report", default="", help="write the JSON report here")
    run_p.add_argument("--concurrency", type=int, default=8)
    run_p.add_argument("--limit", type=int, default=1000)
    run_p.add_argument("--timeout", type=float, default=12.0)
    run_p.add_argument("--no-robots", action="store_true")
    args = parser.parse_args(argv)

    from ..config import load_config

    cfg = load_config(None)
    seeds = load_seeds(args.seeds)

    def on_event(payload: dict[str, Any]) -> None:
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    result = run_campaign(
        cfg,
        seeds,
        state_path=args.state,
        report_path=args.report,
        concurrency=args.concurrency,
        limit=args.limit,
        timeout_sec=args.timeout,
        robots=not args.no_robots,
        on_event=on_event,
    )
    summary = result.as_dict()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
