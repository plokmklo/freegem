"""Content-change recrawling and adaptive scheduling (2.8.0).

The crawler stores ``content_hash``/``etag``/``last_modified`` per fetched
URL but, before this module, never used them to decide WHEN to look again.
Recrawling everything hourly wastes the budget on pages that never change;
recrawling nothing lets stale copies silently rot.

The policy here is deliberately boring and inspectable:

* every fetched URL gets a per-URL change history (``content_history`` table
  in the SAME SQLite file as the frontier -- one file, one human-inspectable
  artifact);
* ``adaptive_interval_sec()`` is a pure function: the more often a page has
  ACTUALLY changed within the observation horizon, the shorter the next
  interval, and pages observed unchanged decay toward ``max_interval_sec``
  (exponentially, by doubling -- the classic Nutch/Polybot heuristic,
  re-derived here because it is ten lines);
* conditional validation first: a recrawl sends ``If-None-Match`` /
  ``If-Modified-Since`` from the stored validators, so an unchanged page
  costs a 304 and no body bytes;
* ``apply_recrawl_plan()`` mutates ONLY ``next_attempt_at``/``priority`` of
  done rows (``Frontier.DONE`` -- what ``Frontier.mark_done`` writes when a
  fetch succeeds) -- it never re-enqueues into the live pending queue
  behind the caller's back, and never touches rows still mid-retry.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from .frontier import DONE

__all__ = [
    "ChangePolicy",
    "adaptive_interval_sec",
    "apply_recrawl_plan",
    "record_fetch",
    "recrawl_candidates",
]

#: Same file as the frontier: one crawl artifact, inspectable with sqlite3.
_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS content_history(
  url           TEXT NOT NULL,
  observed_at   REAL NOT NULL,
  content_hash  TEXT,
  etag          TEXT,
  last_modified TEXT,
  http_status   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_history_url ON content_history(url, observed_at);
"""


@dataclass(frozen=True)
class ChangePolicy:
    """Bounds of the adaptive schedule. All values are seconds."""

    base_interval_sec: float = 3600.0
    min_interval_sec: float = 300.0
    max_interval_sec: float = 7 * 24 * 3600.0
    #: A URL with more distinct-change observations than this inside the
    #: horizon is treated as "changes constantly" and pinned to min.
    horizon_sec: float = 30 * 24 * 3600.0


def ensure_history_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_HISTORY_SCHEMA)
    conn.commit()


def record_fetch(
    conn: sqlite3.Connection,
    url: str,
    *,
    content_hash: str | None,
    etag: str | None = None,
    last_modified: str | None = None,
    http_status: int | None = None,
    observed_at: float | None = None,
) -> None:
    """Append one observation to the URL's change history.

    Cheap by design (one INSERT): the crawler calls it for every fetched
    page. Callers own the transaction if they need batching; by default the
    insert commits alone so a crash never loses the history it already paid
    bandwidth for.
    """
    ensure_history_schema(conn)
    conn.execute(
        "INSERT INTO content_history(url, observed_at, content_hash, etag, last_modified, http_status)"
        " VALUES(?,?,?,?,?,?)",
        (
            url,
            observed_at if observed_at is not None else time.time(),
            content_hash,
            etag,
            last_modified,
            http_status,
        ),
    )
    conn.commit()


def count_changes(
    conn: sqlite3.Connection,
    url: str,
    *,
    within_sec: float,
    now: float | None = None,
) -> int:
    """How many times the content actually CHANGED inside the horizon.

    Consecutive identical hashes collapse: a page fetched ten times with the
    same hash has changed zero times, not nine.
    """
    now_ts = now if now is not None else time.time()
    rows = conn.execute(
        "SELECT content_hash FROM content_history"
        " WHERE url = ? AND observed_at >= ? ORDER BY observed_at ASC",
        (url, now_ts - within_sec),
    ).fetchall()
    changes = 0
    prev: str | None = None
    for (h,) in rows:
        if prev is not None and h is not None and h != prev:
            changes += 1
        if h is not None:
            prev = h
    return changes


def adaptive_interval_sec(
    *,
    changes: int,
    observations: int,
    policy: ChangePolicy | None = None,
) -> float:
    """Next interval for a URL, from its observed change rate.

    - never observed to change (or zero observations): start at base and
      DOUBLE toward max on each unchanged recheck -- the caller passes
      ``observations`` = consecutive unchanged count for that case;
    - changed at least once recently: shrink proportionally to the change
      frequency, floor at min.

    The shape is deliberately monotonic and bounded: no page can be starved
    below min or hammer above max, and the same observation always produces
    the same interval (testable, explainable, no clock dependencies).
    """
    p = policy or ChangePolicy()
    if observations <= 0 and changes <= 0:
        return p.base_interval_sec
    if changes <= 0:
        # unchanged streak: base * 2^streak, capped at max
        return min(p.base_interval_sec * (2 ** min(observations, 30)), p.max_interval_sec)
    # changed within horizon: interpolate min..base by change frequency
    freq = changes / max(1, observations + changes)
    interval = p.base_interval_sec - (p.base_interval_sec - p.min_interval_sec) * min(1.0, freq)
    return max(p.min_interval_sec, min(interval, p.base_interval_sec))


@dataclass
class RecrawlCandidate:
    """One URL the scheduler moved (or would move) to its next due time."""

    url: str
    next_attempt_at: float
    interval_sec: float
    changes: int
    observations: int
    validators: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "next_attempt_at": self.next_attempt_at,
            "interval_sec": round(self.interval_sec, 1),
            "changes": self.changes,
            "observations": self.observations,
            "validators": self.validators,
        }


def recrawl_candidates(
    conn: sqlite3.Connection,
    *,
    policy: ChangePolicy | None = None,
    now: float | None = None,
    limit: int = 500,
) -> list[RecrawlCandidate]:
    """Fetched URLs due for a freshness decision, with their planned interval.

    Reads ``frontier`` (successfully fetched rows -- status ``done``, the
    value :meth:`freegem.crawler.frontier.Frontier.mark_done` writes -- carry
    etag/last_modified/content_hash) and ``content_history`` (change counts).
    Does NOT mutate anything -- the caller inspects the plan, then calls
    :func:`apply_recrawl_plan`.
    """
    p = policy or ChangePolicy()
    now_ts = now if now is not None else time.time()
    ensure_history_schema(conn)
    rows = conn.execute(
        "SELECT url, etag, last_modified, content_hash FROM frontier"
        " WHERE status = ? ORDER BY fetched_at ASC LIMIT ?",
        (DONE, limit),
    ).fetchall()
    out: list[RecrawlCandidate] = []
    for url, etag, last_modified, _current_hash in rows:
        changes = count_changes(conn, url, within_sec=p.horizon_sec, now=now_ts)
        total = conn.execute(
            "SELECT COUNT(*) FROM content_history WHERE url = ?", (url,)
        ).fetchone()[0]
        interval = adaptive_interval_sec(changes=changes, observations=max(0, total - 1), policy=p)
        validators: dict[str, str] = {}
        if etag:
            validators["If-None-Match"] = etag
        if last_modified:
            validators["If-Modified-Since"] = last_modified
        out.append(
            RecrawlCandidate(
                url=url,
                next_attempt_at=now_ts + interval,
                interval_sec=interval,
                changes=changes,
                observations=max(0, total - 1),
                validators=validators,
            )
        )
    return out


def apply_recrawl_plan(
    conn: sqlite3.Connection,
    candidates: list[RecrawlCandidate],
) -> int:
    """Persist the planned due-times for done rows (``Frontier.DONE``).

    ``attempts`` is left alone (a freshness probe is not a failure and must
    not poison the retry budget). The caller fetches when the row comes due
    and calls :func:`record_fetch` with the outcome -- that is the only way
    history grows, so the plan and the observations can never disagree.
    Returns how many rows were updated.
    """
    touched = 0
    for c in candidates:
        cur = conn.execute(
            "UPDATE frontier SET next_attempt_at = ? WHERE url = ? AND status = ?",
            (c.next_attempt_at, c.url, DONE),
        )
        touched += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    conn.commit()
    return touched
