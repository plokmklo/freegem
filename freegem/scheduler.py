"""Recurring scheduler: turn recrawl plans into actual re-fetch runs (2.9.0, P3 #5).

The adaptive scheduler (:mod:`freegem.crawler.scheduling`) decides WHEN a URL
is due for a freshness look; this module is the thing that WAKES UP and does
it. It is deliberately boring: a small driver with three integration faces --

* ``run_once(state_path, runner)`` -- one scheduling tick: collect due
  candidates from the crawl state DB, hand each to the caller's runner
  (usually a bounded crawl over those urls), then ``apply_recrawl_plan`` so
  the next due time moves. Pure function of the DB + runner; trivially
  testable and exactly what a cron/systemd-timer invocation needs.
* ``SchedulerLoop`` -- the same tick in a background thread at a fixed
  interval, for long-lived deployments (the server's optional sidecar, a
  docker container, CI). Honest stop semantics: ``stop()`` joins the thread.
* CLI -- ``freegem schedule run --state <db> [--loop [--every-sec N]]`` so a
  crontab line (``*/15 * * * * freegem schedule run --state ...``) or a
  systemd timer needs zero glue code.

The runner boundary matters: this module never fetches by itself. Re-running
a crawl is the crawler's job; the scheduler only decides WHAT is due and
BOOKKEEPING (due times move forward even if the runner failed -- a failed
probe must not spin into a hot loop; the interval applies regardless).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any

from .crawler.frontier import DONE
from .crawler.scheduling import (
    ChangePolicy,
    RecrawlCandidate,
    apply_recrawl_plan,
    recrawl_candidates,
)

__all__ = ["SchedulerLoop", "main", "run_once"]

#: Runner face: given the due candidates, fetch them; returns per-url ok flags.
Runner = Callable[[list[RecrawlCandidate]], dict[str, bool]]


def run_once(
    state_path: str,
    runner: Runner | None = None,
    *,
    policy: ChangePolicy | None = None,
    now: float | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """One tick: due candidates -> optional runner -> persist next due times.

    ``recrawl_candidates`` plans EVERY fetched row (frontier status ``done``,
    what ``Frontier.mark_done`` writes); "due" is this module's call: a row
    is due when its persisted ``next_attempt_at`` has passed (never-planned
    rows -- ``next_attempt_at = 0`` -- are due immediately). Returns a small
    summary (counts only, no page data) suitable for logs.
    """
    now_ts = now if now is not None else time.time()
    conn = sqlite3.connect(state_path)
    conn.row_factory = sqlite3.Row
    try:
        planned = recrawl_candidates(conn, policy=policy, now=now_ts, limit=limit * 2)
        due_rows = dict(
            conn.execute(
                "SELECT url, next_attempt_at FROM frontier WHERE status = ?", (DONE,)
            ).fetchall()
        )
        candidates = [c for c in planned if due_rows.get(c.url, 0.0) <= now_ts][:limit]
        outcomes: dict[str, bool] = {}
        if runner is not None and candidates:
            outcomes = runner(candidates) or {}
        applied = apply_recrawl_plan(conn, candidates)
        return {
            "due": len(candidates),
            "attempted": len(outcomes),
            "ok": sum(1 for v in outcomes.values() if v),
            "applied": applied,
        }
    finally:
        conn.close()


class SchedulerLoop:
    """Run ``run_once`` every ``every_sec`` in a daemon thread."""

    def __init__(
        self,
        state_path: str,
        runner: Runner | None = None,
        *,
        every_sec: float = 900.0,
        policy: ChangePolicy | None = None,
        limit: int = 500,
    ) -> None:
        self.state_path = state_path
        self.runner = runner
        self.every_sec = every_sec
        self.policy = policy
        self.limit = limit
        self.ticks = 0
        self.last_summary: dict[str, Any] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("scheduler loop already running")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="freegem-scheduler", daemon=True)
        self._thread.start()

    def stop(self, join_timeout: float = 10.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=join_timeout)
            if not thread.is_alive():
                self._thread = None
            # else: the tick is wedged past join_timeout. Keep the reference --
            # clearing it here would let a later start() reset _stop and spawn
            # a SECOND tick loop alongside the zombie, unstoppable forever.
            # start() keeps refusing while the zombie is alive.

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.last_summary = run_once(
                    self.state_path, self.runner, policy=self.policy, limit=self.limit
                )
                self.ticks += 1
            except Exception as exc:
                self.last_summary = {"error": f"{type(exc).__name__}: {exc}"}
            elapsed = time.monotonic() - started
            self._stop.wait(timeout=max(0.5, self.every_sec - elapsed))


def main(argv: list[str] | None = None) -> int:
    """``freegem schedule run`` -- cron/systemd-friendly entry point."""
    parser = argparse.ArgumentParser(prog="freegem-schedule")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="one scheduling tick (or a loop)")
    run_p.add_argument("--state", required=True, help="crawl state SQLite file")
    run_p.add_argument("--loop", action="store_true", help="run forever instead of once")
    run_p.add_argument("--every-sec", type=float, default=900.0, help="loop interval (default 900)")
    run_p.add_argument("--limit", type=int, default=500, help="max candidates per tick")
    args = parser.parse_args(argv)

    if not args.loop:
        summary = run_once(args.state, None, limit=args.limit)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    loop = SchedulerLoop(args.state, None, every_sec=args.every_sec, limit=args.limit)
    loop.start()

    import signal

    def _sig(_sig_num: int, _frame: Any) -> None:
        loop.stop()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    try:
        while loop._thread is not None and loop._thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        loop.stop()
    print(json.dumps({"ticks": loop.ticks, "last": loop.last_summary}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
