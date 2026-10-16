"""Tests for adaptive recrawl scheduling (crawler/scheduling.py)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Generator

import pytest

from freegem.crawler.frontier import DONE, Frontier
from freegem.crawler.scheduling import (
    ChangePolicy,
    RecrawlCandidate,
    adaptive_interval_sec,
    apply_recrawl_plan,
    count_changes,
    record_fetch,
    recrawl_candidates,
)


@pytest.fixture()
def db() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE frontier(
          url TEXT PRIMARY KEY, host TEXT, depth INTEGER DEFAULT 0,
          priority INTEGER DEFAULT 5, status TEXT DEFAULT 'pending',
          attempts INTEGER DEFAULT 0, next_attempt_at REAL DEFAULT 0,
          added_at REAL DEFAULT 0, fetched_at REAL,
          http_status INTEGER, error TEXT,
          etag TEXT, last_modified TEXT, content_hash TEXT
        );
        """
    )
    yield conn
    conn.close()


def _seed_fetched(conn: sqlite3.Connection, url: str, etag: str | None = None) -> None:
    """Seed a row the way the runner leaves it after a successful fetch:
    ``Frontier.mark_done`` writes status 'done' (NOT 'fetched')."""
    conn.execute(
        "INSERT INTO frontier(url, host, status, fetched_at, etag) VALUES(?,?,?,?,?)",
        (url, "example.org", DONE, time.time(), etag),
    )
    conn.commit()


def test_adaptive_interval_unchanged_streak_doubles_to_max() -> None:
    p = ChangePolicy(base_interval_sec=100.0, min_interval_sec=10.0, max_interval_sec=1000.0)
    assert adaptive_interval_sec(changes=0, observations=0, policy=p) == 100.0
    assert adaptive_interval_sec(changes=0, observations=1, policy=p) == 200.0
    assert adaptive_interval_sec(changes=0, observations=3, policy=p) == 800.0
    # capped at max
    assert adaptive_interval_sec(changes=0, observations=30, policy=p) == 1000.0


def test_adaptive_interval_changes_shrink_toward_min() -> None:
    p = ChangePolicy(base_interval_sec=100.0, min_interval_sec=10.0, max_interval_sec=1000.0)
    # every observation changed: freq = 1 -> interval = min
    assert adaptive_interval_sec(changes=5, observations=0, policy=p) == 10.0
    # half changed: halfway between base and min
    v = adaptive_interval_sec(changes=2, observations=2, policy=p)
    assert 10.0 < v < 100.0
    # monotonic: more changes -> never a longer interval
    assert adaptive_interval_sec(changes=4, observations=2, policy=p) <= v


def test_count_changes_collapses_consecutive_identical_hashes() -> None:
    conn = sqlite3.connect(":memory:")
    now = time.time()
    for i, h in enumerate(["a", "a", "a", "b", "b", "c"]):
        record_fetch(conn, "u", content_hash=h, observed_at=now - 600 + i * 100)
    assert count_changes(conn, "u", within_sec=10_000, now=now) == 2
    assert (
        count_changes(conn, "u", within_sec=150, now=now) == 0
    )  # only the last two "b","c" -> wait
    conn.close()


def test_record_fetch_and_candidates_roundtrip(db: sqlite3.Connection) -> None:
    _seed_fetched(db, "https://example.org/a", etag='W/"v1"')
    now = time.time()
    record_fetch(db, "https://example.org/a", content_hash="h1", observed_at=now - 500)
    record_fetch(db, "https://example.org/a", content_hash="h2", observed_at=now - 100)
    cands = recrawl_candidates(db, now=now, limit=10)
    assert len(cands) == 1
    c = cands[0]
    assert c.url == "https://example.org/a"
    assert c.changes == 1
    assert c.validators == {"If-None-Match": 'W/"v1"'}
    assert c.next_attempt_at >= now


def test_candidates_only_cover_done_rows(db: sqlite3.Connection) -> None:
    _seed_fetched(db, "https://example.org/fetched")
    db.execute(
        "INSERT INTO frontier(url, host, status) VALUES(?,?,?)",
        ("https://example.org/pending", "example.org", "pending"),
    )
    db.execute(
        "INSERT INTO frontier(url, host, status) VALUES(?,?,?)",
        ("https://example.org/errored", "example.org", "error"),
    )
    db.commit()
    cands = recrawl_candidates(db, limit=10)
    assert [c.url for c in cands] == ["https://example.org/fetched"]


def test_apply_recrawl_plan_updates_due_time_only_for_done(db: sqlite3.Connection) -> None:
    _seed_fetched(db, "https://example.org/a")
    db.execute(
        "INSERT INTO frontier(url, host, status) VALUES(?,?,?)",
        ("https://example.org/pending", "example.org", "pending"),
    )
    db.commit()
    plan = [
        RecrawlCandidate("https://example.org/a", 12345.0, 600.0, 0, 0),
        RecrawlCandidate("https://example.org/pending", 99999.0, 600.0, 0, 0),
    ]
    touched = apply_recrawl_plan(db, plan)
    assert touched == 1
    row = db.execute(
        "SELECT next_attempt_at, attempts, status FROM frontier WHERE url = ?",
        ("https://example.org/a",),
    ).fetchone()
    assert row[0] == 12345.0
    assert row[1] == 0  # attempts untouched
    other = db.execute(
        "SELECT next_attempt_at FROM frontier WHERE url = 'https://example.org/pending'"
    ).fetchone()
    assert other[0] != 99999.0


def test_limit_bounds_candidate_scan(db: sqlite3.Connection) -> None:
    for i in range(12):
        _seed_fetched(db, f"https://example.org/p{i}")
    assert len(recrawl_candidates(db, limit=5)) == 5


def test_recrawl_candidates_see_rows_written_by_frontier_mark_done(tmp_path) -> None:
    """Regression (issue #42): the recrawl pipeline queried status 'fetched',
    but ``Frontier.mark_done`` writes 'done' -- so on REAL crawl databases
    ``recrawl_candidates`` always returned [] and adaptive recrawl never
    fired. This test writes through the Frontier itself, the way the runner
    does, and the plan must see the row and be able to apply to it."""
    state = str(tmp_path / "state.db")
    frontier = Frontier(state)
    try:
        frontier.push("https://example.org/a", host="example.org")
        claimed = frontier.claim(1)
        assert claimed, "frontier claim failed"
        frontier.mark_done("https://example.org/a", http_status=200, etag='W/"v1"')
    finally:
        frontier.close()

    conn = sqlite3.connect(state)
    try:
        assert conn.execute("SELECT DISTINCT status FROM frontier").fetchall() == [("done",)]
        cands = recrawl_candidates(conn, limit=10)
        assert [c.url for c in cands] == ["https://example.org/a"]
        assert cands[0].validators == {"If-None-Match": 'W/"v1"'}
        assert apply_recrawl_plan(conn, cands) == 1
        stored = conn.execute(
            "SELECT next_attempt_at FROM frontier WHERE url = 'https://example.org/a'"
        ).fetchone()[0]
        assert stored == cands[0].next_attempt_at
    finally:
        conn.close()
