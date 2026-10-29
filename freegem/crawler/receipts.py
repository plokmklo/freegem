"""Politeness receipts: per-host evidence that the crawl behaved.

The differentiator nobody else in the ecosystem ships: while other crawlers
treat politeness as a private config knob, freegem emits a receipt -- a
machine-checkable report of what the crawler actually did to each host,
straight out of the frontier database:

* request counts and status histograms (2xx / 3xx / 304 revalidations /
  4xx / 5xx), wall counts (401/403/429) per host;
* retry volume per host, so a struggling origin is visible as data;
* observed inter-fetch intervals (min / median / max) and -- when the
  crawl's ``--delay-ms`` is supplied -- whether the minimum observed
  interval is consistent with the configured per-host delay;
* the honest identity block: exact User-Agent string, the robots stance
  (never consulted: attacker-controlled text -- docs/NON_GOALS.md), and
  the byte caps that bounded every fetch.

The report is a plain dict, serialized canonically (sorted keys), and
sha-256 digested so a receipt can be quoted, compared across runs, or
attached to a publication without trusting anyone's memory. The markdown
render is for humans; the JSON is for pipelines.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from itertools import pairwise
from typing import Any

__all__ = ["build_receipts", "render_markdown"]

WALL_STATUSES = (401, 403, 429)

#: Interval comparisons need tolerance: worker-pool scheduling and the
#: 100ms polite-sleep granularity put real waits slightly under the nominal
#: delay even when the pacer is correct.
_DELAY_EPSILON = 0.9

_ERROR_STATUS_RE = re.compile(r"\bHTTP (\d{3})\b")


def _status_from_error(error: str | None) -> int | None:
    """Wire status from a legacy ``HTTP 403``-style error string ('' -> None)."""
    if not error:
        return None
    match = _ERROR_STATUS_RE.search(error)
    return int(match.group(1)) if match else None


def _connect(db: sqlite3.Connection | str) -> sqlite3.Connection:
    if isinstance(db, sqlite3.Connection):
        # build_receipts accesses rows by column name; a caller-supplied
        # connection without a row factory would crash with tuple indices.
        if db.row_factory is None:
            db.row_factory = sqlite3.Row
        return db
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def build_receipts(
    db: sqlite3.Connection | str,
    *,
    delay_ms: int | None = None,
    user_agent: str = "",
    software: str = "freegem",
    robots_stance: str = "robots.txt never fetched or consulted (attacker-controlled "
    "text; see NON_GOALS). Courtesies: per-host pacing, byte caps, no auth "
    "bypass, no wall cracking.",
    now: float | None = None,
) -> dict[str, Any]:
    """Aggregate the frontier into a per-host politeness report."""
    conn = _connect(db)
    try:
        rows = conn.execute(
            "SELECT url, host, status, http_status, attempts, fetched_at, error "
            "FROM frontier ORDER BY host, fetched_at"
        ).fetchall()
    finally:
        if conn is not db:
            conn.close()

    hosts: dict[str, dict[str, Any]] = {}
    for row in rows:
        host = str(row["host"] or "unknown")
        bucket = hosts.setdefault(
            host,
            {
                "urls": 0,
                "done": 0,
                "http_2xx": 0,
                "http_3xx": 0,
                "not_modified_304": 0,
                "client_error_4xx": 0,
                "server_error_5xx": 0,
                "walls": 0,
                "wall_statuses": {},
                "errors": 0,
                "skipped": 0,
                "pending_or_inflight": 0,
                "retries": 0,
                "interval_sec": {"min": None, "median": None, "max": None},
            },
        )
        bucket["urls"] += 1
        status = str(row["status"] or "")
        http_status = row["http_status"]
        attempts = int(row["attempts"] or 0)
        if attempts > 1:
            bucket["retries"] += attempts - 1
        # Wall detection: prefer the stored wire status (2.12.0+ state files);
        # older files only kept "HTTP 403"-style error strings, so parse those
        # as a fallback rather than reporting zero walls for real history.
        status_int = (
            int(http_status) if http_status is not None else _status_from_error(row["error"])
        )
        if status == "done":
            bucket["done"] += 1
            if status_int == 304:
                bucket["not_modified_304"] += 1
            elif status_int is not None and 200 <= status_int < 300:
                bucket["http_2xx"] += 1
            elif status_int is not None and 300 <= status_int < 400:
                bucket["http_3xx"] += 1
            elif status_int is not None and 400 <= status_int < 500:
                bucket["client_error_4xx"] += 1
                if status_int in WALL_STATUSES:
                    bucket["walls"] += 1
                    key = str(status_int)
                    bucket["wall_statuses"][key] = bucket["wall_statuses"].get(key, 0) + 1
            elif status_int is not None and status_int >= 500:
                bucket["server_error_5xx"] += 1
        elif status == "error":
            bucket["errors"] += 1
            if status_int is not None and 400 <= status_int < 500:
                bucket["client_error_4xx"] += 1
                if status_int in WALL_STATUSES:
                    bucket["walls"] += 1
                    key = str(status_int)
                    bucket["wall_statuses"][key] = bucket["wall_statuses"].get(key, 0) + 1
            elif status_int is not None and status_int >= 500:
                bucket["server_error_5xx"] += 1
        elif status == "skipped":
            bucket["skipped"] += 1
        elif status in ("pending", "inflight"):
            bucket["pending_or_inflight"] += 1

    # Inter-fetch intervals per host, from the done rows' fetch timestamps.
    for host, bucket in hosts.items():
        times = sorted(
            float(r["fetched_at"])
            for r in rows
            if r["host"] == host and r["status"] == "done" and r["fetched_at"] is not None
        )
        if len(times) >= 2:
            gaps = [round(b - a, 3) for a, b in pairwise(times)]
            bucket["interval_sec"] = {
                "min": min(gaps),
                "median": sorted(gaps)[len(gaps) // 2],
                "max": max(gaps),
            }
        if delay_ms is not None and delay_ms >= 0:
            delay_sec = delay_ms / 1000.0
            min_gap = bucket["interval_sec"]["min"]
            if min_gap is None:
                bucket["pacing"] = {
                    "delay_ms": delay_ms,
                    "consistent": True,
                    "note": "fewer than two same-host fetches; nothing to violate",
                }
            else:
                consistent = min_gap >= delay_sec * _DELAY_EPSILON
                bucket["pacing"] = {
                    "delay_ms": delay_ms,
                    "consistent": consistent,
                    "note": "min observed gap vs configured per-host delay "
                    f"(tolerance {_DELAY_EPSILON:.0%})",
                }

    totals: dict[str, Any] = {
        "hosts": len(hosts),
        "urls": sum(h["urls"] for h in hosts.values()),
        "done": sum(h["done"] for h in hosts.values()),
        "not_modified_304": sum(h["not_modified_304"] for h in hosts.values()),
        "walls": sum(h["walls"] for h in hosts.values()),
        "errors": sum(h["errors"] for h in hosts.values()),
        "skipped": sum(h["skipped"] for h in hosts.values()),
        "retries": sum(h["retries"] for h in hosts.values()),
    }
    report: dict[str, Any] = {
        "kind": "freegem-politeness-receipt",
        "version": 1,
        "generated_at": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time())
        ),
        "software": software,
        "user_agent": user_agent,
        "robots_stance": robots_stance,
        "totals": totals,
        "hosts": dict(sorted(hosts.items())),
    }
    if delay_ms is not None:
        report["delay_ms"] = delay_ms
    canonical = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    report["receipt_digest"] = "sha-256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return report


def render_markdown(report: dict[str, Any]) -> str:
    """Human-readable receipt; the JSON stays the canonical form."""
    totals = report["totals"]
    lines = [
        "# Politeness receipt",
        "",
        f"- software: {report['software']}",
        f"- user-agent: {report['user_agent'] or '(not recorded)'}",
        f"- robots: {report['robots_stance']}",
        f"- generated: {report['generated_at']}",
        f"- digest: {report['receipt_digest']}",
        "",
        "## Totals",
        "",
        f"- hosts crawled: {totals['hosts']}",
        f"- urls attempted: {totals['urls']}",
        f"- fetched: {totals['done']} (304 revalidations: {totals['not_modified_304']})",
        f"- wall answers (401/403/429): {totals['walls']}",
        f"- errors: {totals['errors']}, skipped: {totals['skipped']}, retries: {totals['retries']}",
    ]
    if "delay_ms" in report:
        lines.insert(4, f"- per-host delay: {report['delay_ms']} ms")
    lines += ["", "## Per host", ""]
    for host, bucket in report["hosts"].items():
        interval = bucket["interval_sec"]
        gap = interval["min"]
        gap_text = f"{gap:.3f}s min" if gap is not None else "single fetch"
        pacing = bucket.get("pacing")
        pacing_text = ""
        if pacing:
            pacing_text = (
                f"; pacing {'CONSISTENT' if pacing['consistent'] else 'INCONSISTENT'}"
                f" with {pacing['delay_ms']} ms delay"
            )
        lines.append(
            f"- **{host}**: {bucket['done']} done, {bucket['walls']} walls, "
            f"{bucket['errors']} errors, {bucket['skipped']} skipped, "
            f"{bucket['retries']} retries; {gap_text}{pacing_text}"
        )
    return "\n".join(lines)
