"""The persistent local frontier: a SQLite-backed work list for the crawler.

Why SQLite: the frontier must survive a crash, a Ctrl-C, and a machine
reboot (checkpoints, resume); it must answer "what is due next, oldest
priority first" cheaply; and it must be inspectable by a human with
``sqlite3 crawl.db`` when a crawl looks stuck. A JSON file satisfies none of
that; a server DB is against the project's zero-dependency rule.

Durability contract:

* every state change commits in its own transaction -- the state file on
  disk is ALWAYS a valid checkpoint, there is no separate checkpoint step
  to forget;
* ``checkpoint()`` additionally truncates the WAL so the file can be copied;
* resume = open the same file again. Nothing else.

Politeness lives here too: ``host_ready``/``touch_host`` track the last
fetch time per host so the runner can wait ``delay`` between hits on the
same host even with concurrent workers.
"""

from __future__ import annotations

import contextlib
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

__all__ = ["Frontier", "FrontierItem", "FrontierStats"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS frontier(
  url             TEXT PRIMARY KEY,
  host            TEXT NOT NULL,
  depth           INTEGER NOT NULL DEFAULT 0,
  priority        INTEGER NOT NULL DEFAULT 5,
  status          TEXT NOT NULL DEFAULT 'pending',
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  added_at        REAL NOT NULL DEFAULT 0,
  fetched_at      REAL,
  http_status     INTEGER,
  error           TEXT,
  etag            TEXT,
  last_modified   TEXT,
  content_hash    TEXT
);
CREATE INDEX IF NOT EXISTS idx_frontier_due
  ON frontier(status, priority, next_attempt_at, added_at);
CREATE INDEX IF NOT EXISTS idx_frontier_host ON frontier(host, status);
CREATE TABLE IF NOT EXISTS host_state(
  host        TEXT PRIMARY KEY,
  last_fetch  REAL NOT NULL DEFAULT 0,
  ok_count    INTEGER NOT NULL DEFAULT 0,
  err_count   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS crawl_meta(
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

#: status values, for tests and tooling
PENDING = "pending"
INFLIGHT = "inflight"
DONE = "done"
ERROR = "error"
SKIPPED = "skipped"

_TERMINAL = (DONE, ERROR, SKIPPED)


@dataclass(frozen=True)
class FrontierItem:
    """One claimed URL handed to a worker."""

    url: str
    host: str
    depth: int
    priority: int
    attempts: int
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True)
class FrontierStats:
    """Counts by status plus the top failing hosts."""

    by_status: dict[str, int]
    top_hosts: list[tuple[str, int]]
    queued: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "by_status": dict(self.by_status),
            "queued": self.queued,
            "top_hosts": [[host, n] for host, n in self.top_hosts],
        }


class Frontier:
    """Thread-safe persistent crawl frontier."""

    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;" + _SCHEMA)
            self._db.commit()

    # -- writing -------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def checkpoint(self) -> None:
        """Durably fold the WAL into the main file so the DB can be copied."""
        with self._lock:
            self._db.commit()
            # TRUNCATE can fail on locked DBs; the checkpoint attempt is best-effort.
            with contextlib.suppress(sqlite3.DatabaseError):
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO crawl_meta(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._db.commit()

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM crawl_meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def push(
        self,
        url: str,
        *,
        host: str,
        depth: int = 0,
        priority: int = 5,
    ) -> bool:
        """Add a URL; returns False when it was already known (dedup)."""
        now = time.time()
        with self._lock:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO frontier"
                "(url, host, depth, priority, status, added_at, next_attempt_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (url, host, depth, priority, PENDING, now, now),
            )
            self._db.commit()
            return cursor.rowcount > 0

    def claim(self, limit: int, *, now: float | None = None) -> list[FrontierItem]:
        """Atomically move up to ``limit`` due pending URLs to inflight."""
        stamp = time.time() if now is None else now
        with self._lock:
            rows = self._db.execute(
                "SELECT url, host, depth, priority, attempts, etag, last_modified"
                " FROM frontier"
                " WHERE status=? AND next_attempt_at<=?"
                " ORDER BY priority ASC, added_at ASC LIMIT ?",
                (PENDING, stamp, limit),
            ).fetchall()
            claimed: list[FrontierItem] = []
            for row in rows:
                self._db.execute(
                    "UPDATE frontier SET status=? WHERE url=? AND status=?",
                    (INFLIGHT, row["url"], PENDING),
                )
                claimed.append(
                    FrontierItem(
                        url=row["url"],
                        host=row["host"],
                        depth=int(row["depth"]),
                        priority=int(row["priority"]),
                        attempts=int(row["attempts"]),
                        etag=row["etag"],
                        last_modified=row["last_modified"],
                    )
                )
            self._db.commit()
            return claimed

    def mark_done(
        self,
        url: str,
        *,
        http_status: int | None,
        etag: str | None = None,
        last_modified: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE frontier SET status=?, fetched_at=?, http_status=?, etag=?,"
                " last_modified=?, content_hash=?, error=NULL WHERE url=?",
                (DONE, time.time(), http_status, etag, last_modified, content_hash, url),
            )
            self._db.execute(
                "UPDATE host_state SET ok_count=ok_count+1 WHERE host="
                "(SELECT host FROM frontier WHERE url=?)",
                (url,),
            )
            self._db.commit()

    def mark_error(
        self,
        url: str,
        error: str,
        *,
        retry: bool,
        max_retries: int = 2,
        backoff_sec: float = 5.0,
        retry_after: float | None = None,
    ) -> bool:
        """Record a failure; schedule a retry when allowed.

        Returns True when the URL will be retried. Backoff is exponential in
        the attempt count with jitter, so a herd of failures does not line up
        to hammer the same host again on the same second. When the server
        sent ``Retry-After`` (429/503), that value IS the delay -- the site
        told us when it can take us back, and ignoring it is how polite
        crawlers get upgraded to banned crawlers.
        """
        with self._lock:
            row = self._db.execute("SELECT attempts FROM frontier WHERE url=?", (url,)).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 1
            will_retry = retry and attempts <= max_retries
            if will_retry:
                if retry_after is not None:
                    delay = max(retry_after, backoff_sec * 0.5)
                else:
                    delay = backoff_sec * (2 ** (attempts - 1)) * (0.5 + random.random())
                self._db.execute(
                    "UPDATE frontier SET status=?, attempts=?, error=?, next_attempt_at=?"
                    " WHERE url=?",
                    (PENDING, attempts, error[:500], time.time() + delay, url),
                )
            else:
                self._db.execute(
                    "UPDATE frontier SET status=?, attempts=?, error=?, fetched_at=? WHERE url=?",
                    (ERROR, attempts, error[:500], time.time(), url),
                )
            self._db.execute(
                "UPDATE host_state SET err_count=err_count+1 WHERE host="
                "(SELECT host FROM frontier WHERE url=?)",
                (url,),
            )
            self._db.commit()
            return will_retry

    def mark_skipped(self, url: str, reason: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE frontier SET status=?, error=?, fetched_at=? WHERE url=?",
                (SKIPPED, reason[:500], time.time(), url),
            )
            self._db.commit()

    # -- host politeness -------------------------------------------------------

    def host_ready(self, host: str, *, delay_sec: float, now: float | None = None) -> float:
        """Seconds to wait before the next fetch on ``host`` (0 = go now)."""
        stamp = time.time() if now is None else now
        with self._lock:
            row = self._db.execute(
                "SELECT last_fetch FROM host_state WHERE host=?", (host,)
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT OR IGNORE INTO host_state(host, last_fetch) VALUES(?,0)",
                    (host,),
                )
                self._db.commit()
                return 0.0
            elapsed = stamp - float(row["last_fetch"])
        return max(0.0, delay_sec - elapsed)

    def touch_host(self, host: str, *, now: float | None = None) -> None:
        stamp = time.time() if now is None else now
        with self._lock:
            self._db.execute(
                "INSERT INTO host_state(host, last_fetch) VALUES(?,?)"
                " ON CONFLICT(host) DO UPDATE SET last_fetch=excluded.last_fetch",
                (host, stamp),
            )
            self._db.commit()

    # -- reading ----------------------------------------------------------------

    def stats(self) -> FrontierStats:
        with self._lock:
            rows = self._db.execute(
                "SELECT status, COUNT(*) AS n FROM frontier GROUP BY status"
            ).fetchall()
            hosts = self._db.execute(
                "SELECT host, err_count FROM host_state WHERE err_count>0"
                " ORDER BY err_count DESC LIMIT 10"
            ).fetchall()
            queued = self._db.execute(
                "SELECT COUNT(*) AS n FROM frontier WHERE status=?", (PENDING,)
            ).fetchone()
        return FrontierStats(
            by_status={str(r["status"]): int(r["n"]) for r in rows},
            top_hosts=[(str(r["host"]), int(r["err_count"])) for r in hosts],
            queued=int(queued["n"]) if queued else 0,
        )

    def pending_urls(self, limit: int = 50) -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT url FROM frontier WHERE status=?"
                " ORDER BY priority ASC, added_at ASC LIMIT ?",
                (PENDING, limit),
            ).fetchall()
        return [str(r["url"]) for r in rows]

    def inflight_count(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM frontier WHERE status=?", (INFLIGHT,)
            ).fetchone()
        return int(row["n"]) if row else 0

    def abandon_inflight(self) -> int:
        """Return inflight rows to pending (crash/restart recovery)."""
        with self._lock:
            cursor = self._db.execute(
                "UPDATE frontier SET status=? WHERE status=?", (PENDING, INFLIGHT)
            )
            self._db.commit()
            return int(cursor.rowcount)

    def seen(self, url: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM frontier WHERE url=?", (url,)).fetchone()
        return row is not None
