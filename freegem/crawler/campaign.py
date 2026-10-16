"""Bulk fetch campaign: many sites, many encodings, many failure modes.

The campaign answers a question a single scrape cannot: "how does the fetch
stack actually behave across a thousand sites -- old and new hand-written
HTML, news portals, PHP forums, Java application servers, Shift-JIS and GBK
pages, bot-walled CDNs?" The point is the FAILURE distribution, not the
green checkmarks: the report's blocked/timeout/network buckets are what
drives fetch upgrades (header profiles, retry policy, decoding).

Policy (v3): NO robots.txt -- the campaign never fetches or consults one.
robots.txt is attacker-controlled text from the same origin as the pages; it
is a documented prompt-injection vector, and a published page is published
to be read. Courtesies: per-host pacing (a real delay between same-host
requests, extended on 429) and no attacking -- no auth bypass, no wall
cracking. Every seed goes through the resilient ladder (browser-grade
headers, origin-granted cookies via one shared cookie jar, backoff,
origin-root fallback for dead paths and path-specific 5xx, scheme/host
swaps, DNS-over-HTTPS when the local resolver cannot answer, last-resort
TLS relax for broken certificates, and a final wayback-machine read of the
page's public snapshot when the live origin still refuses -- those outcomes
are flagged via="wayback").

It is a library first and a CLI second::

    python -m freegem.crawler.campaign run --seeds seeds.jsonl \\
        --report report.json --concurrency 8 --limit 1000

    python -m freegem.crawler.campaign run --seeds seeds.jsonl \\
        --state state.db --retry-failed --distill

Seeds are JSONL: ``{"url": "...", "category": "news", "note": "..."}``.
State is a SQLite file (``--state``), so a killed campaign resumes where it
stopped and the report is reproducible from the recorded outcomes.
``--retry-failed`` re-runs only the seeds whose last outcome was not ``ok``
(the upgrade path: fix the fetcher, re-try exactly what it missed).
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..config import BridgeConfig
from .distill import DISTILL_THRESHOLD_BYTES, distill_text
from .fetch import FetchOutcome
from .hardening import ResourceLimits
from .resilient import HostPacer, fetch_resilient

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
CREATE TABLE IF NOT EXISTS distilled(
  url      TEXT PRIMARY KEY,
  mode     TEXT NOT NULL,
  chars    INTEGER NOT NULL DEFAULT 0,
  text     TEXT NOT NULL DEFAULT ''
);
"""

_MIGRATIONS = (
    "ALTER TABLE results ADD COLUMN via TEXT NOT NULL DEFAULT 'origin'",
    "ALTER TABLE results ADD COLUMN fallback TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE results ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1",
)


@dataclass
class CampaignResult:
    """Aggregated outcome of one campaign run."""

    attempted: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    by_status: dict[str, int] = field(default_factory=dict)
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)
    by_fallback: dict[str, int] = field(default_factory=dict)
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
        if outcome.fallback:
            self.by_fallback[outcome.fallback] = self.by_fallback.get(outcome.fallback, 0) + 1
        self.total_bytes += outcome.body_size or len(outcome.body or b"")
        self.total_ms += outcome.elapsed_ms
        if outcome.blocked and len(self.blocked_samples) < 25:
            self.blocked_samples.append(
                {
                    "url": url,
                    "status": outcome.status,
                    "kind": outcome.kind,
                    "fallback": outcome.fallback,
                    "attempts": outcome.attempts,
                    "server": outcome.headers.get("server", ""),
                }
            )
        if outcome.kind in ("network", "timeout", "ssrf") and len(self.error_samples) < 25:
            self.error_samples.append(
                {"url": url, "kind": outcome.kind, "error": (outcome.error or "")[:120]}
            )

    def readable(self) -> int:
        """Seeds whose content was actually obtained (``ok`` kind)."""
        return self.by_kind.get("ok", 0)

    def as_dict(self) -> dict[str, Any]:
        attempted = self.attempted or 1
        return {
            "attempted": self.attempted,
            "readable": self.readable(),
            "readable_pct": round(100.0 * self.readable() / attempted, 2),
            "by_kind": dict(sorted(self.by_kind.items(), key=lambda kv: -kv[1])),
            "by_status": dict(sorted(self.by_status.items())),
            "by_category": self.by_category,
            "by_fallback": dict(sorted(self.by_fallback.items(), key=lambda kv: -kv[1])),
            "blocked_samples": self.blocked_samples,
            "error_samples": self.error_samples,
            "total_bytes": self.total_bytes,
            "avg_ms": (self.total_ms // attempted) if self.attempted else 0,
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


def _migrate(db: sqlite3.Connection) -> None:
    """Add v2 columns when upgrading a v1 state file (idempotent)."""
    for statement in _MIGRATIONS:
        try:
            with db:
                db.execute(statement)
        except sqlite3.OperationalError:
            pass  # column already exists


def _persist(
    db: sqlite3.Connection,
    lock: threading.Lock,
    url: str,
    category: str,
    outcome: FetchOutcome,
) -> None:
    with lock, db:
        db.execute(
            "INSERT OR REPLACE INTO results"
            "(url, category, kind, status, ms, bytes, error, attempted_at,"
            " via, fallback, attempts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                url,
                category,
                outcome.kind,
                outcome.status,
                outcome.elapsed_ms,
                outcome.body_size or len(outcome.body or b""),
                outcome.error,
                time.time(),
                outcome.via,
                outcome.fallback,
                outcome.attempts,
            ),
        )


def _distill_if_large(
    cfg: BridgeConfig,
    db: sqlite3.Connection,
    lock: threading.Lock,
    url: str,
    outcome: FetchOutcome,
    counters: dict[str, int],
) -> None:
    """Distill pages at/above the size threshold; store beside the results."""
    size = outcome.body_size or len(outcome.body or b"")
    if size < DISTILL_THRESHOLD_BYTES:
        return
    text = outcome.text()
    distilled, mode = distill_text(cfg, url, text, content_type=outcome.content_type)
    counters["distilled"] = counters.get("distilled", 0) + 1
    counters[f"distill_{mode}"] = counters.get(f"distill_{mode}", 0) + 1
    with lock, db:
        db.execute(
            "INSERT OR REPLACE INTO distilled(url, mode, chars, text) VALUES(?,?,?,?)",
            (url, mode, len(distilled), distilled),
        )


def run_campaign(
    cfg: BridgeConfig,
    seeds: list[dict[str, str]],
    *,
    state_path: str,
    report_path: str = "",
    concurrency: int = 8,
    limit: int = 1000,
    timeout_sec: float = 20.0,
    host_delay_sec: float = 1.0,
    retry_failed: bool = False,
    distill: bool = False,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> CampaignResult:
    """Fetch seeds (resume-aware) through the resilient ladder, persist
    outcomes, aggregate. ``retry_failed`` re-attempts only rows whose last
    outcome was not ``ok``; ``distill`` summarizes oversized pages (Gemini
    when a session exists, extractive otherwise) into the ``distilled``
    table. There is deliberately NO robots.txt step."""
    limits = ResourceLimits(timeout_sec=timeout_sec, max_bytes=100 * 1024 * 1024)
    spill_dir = os.path.join(os.path.dirname(os.path.abspath(state_path)) or ".", "spill")
    pacer = HostPacer(default_delay_sec=host_delay_sec)
    db = sqlite3.connect(state_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    with db:
        db.executescript(_SCHEMA)
    _migrate(db)

    queue = seeds[:limit]
    with db:
        for seed in queue:
            db.execute(
                "INSERT OR IGNORE INTO queue(url, category) VALUES(?,?)",
                (seed["url"], seed.get("category", "uncategorized")),
            )

    result = CampaignResult()
    distill_counters: dict[str, int] = {}
    # RLock, not Lock: work() holds this while calling _distill_if_large,
    # whose distilled-row write re-acquires the same lock (issue #41 — a plain
    # Lock self-deadlocked on the first page at/above the distill threshold).
    lock = threading.RLock()
    cancel = threading.Event()
    # One shared jar: cookies an origin itself grants (anonymous entitlement
    # tokens, consent marks) ride along on retries and later seeds, exactly
    # as a browser's jar would. http.cookiejar is internally locked.
    jar: http.cookiejar.CookieJar = http.cookiejar.CookieJar()

    def work(row: sqlite3.Row) -> None:
        if cancel.is_set():
            return
        url = str(row["url"])
        category = str(row["category"])
        outcome = fetch_resilient(
            cfg, url, limits=limits, pacer=pacer, spill_dir=spill_dir, cookie_jar=jar
        )
        _persist(db, lock, url, category, outcome)
        with lock:
            result.record(url, category, outcome)
            if distill:
                _distill_if_large(cfg, db, lock, url, outcome, distill_counters)
        if on_event is not None:
            on_event({"event": "fetched", **outcome.summary(), "category": category})

    if retry_failed:
        rows = db.execute(
            "SELECT url, category FROM results WHERE kind != 'ok' ORDER BY url"
        ).fetchall()
    else:
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
        payload = result.as_dict()
        if distill:
            payload["distill"] = dict(sorted(distill_counters.items()))
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
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
    run_p.add_argument("--timeout", type=float, default=20.0)
    run_p.add_argument(
        "--host-delay",
        type=float,
        default=1.0,
        help="minimum seconds between requests to the same host",
    )
    run_p.add_argument(
        "--retry-failed",
        action="store_true",
        help="re-attempt only seeds whose last outcome was not ok",
    )
    run_p.add_argument(
        "--distill",
        action="store_true",
        help="distill oversized pages (Gemini, extractive fallback)",
    )
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
        host_delay_sec=args.host_delay,
        retry_failed=args.retry_failed,
        distill=args.distill,
        on_event=on_event,
    )
    summary = result.as_dict()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
