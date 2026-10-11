"""v3 retry driver: shuffled failure re-attempts, persisted to the main DB.

The campaign CLI's --retry-failed re-walks failures in ORDER BY url, so a
killed foreground chunk always re-processes the same alphabetical prefix.
This driver shuffles, works through the whole failure set, and persists each
completed URL back into the same state DB the campaign uses (same columns),
so repeated foreground calls converge no matter where they are interrupted.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import random
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from freegem.config import load_config  # noqa: E402
from freegem.crawler.hardening import ResourceLimits  # noqa: E402
from freegem.crawler.resilient import HostPacer, fetch_resilient  # noqa: E402

DB = "/home/z/my-project/download/campaign/campaign_state.db"


def main() -> int:
    random.seed()
    con = sqlite3.connect(DB, check_same_thread=False)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT url, category FROM results WHERE error IS NOT NULL ORDER BY url"
    ).fetchall()
    urls = [(str(r["url"]), str(r["category"])) for r in rows]
    random.shuffle(urls)
    print(f"pending failures: {len(urls)}", flush=True)

    limits = ResourceLimits(timeout_sec=30.0, max_bytes=100 * 1024 * 1024, max_retries=2)
    pacer = HostPacer(default_delay_sec=1.0)
    jar: http.cookiejar.CookieJar = http.cookiejar.CookieJar()
    lock = threading.Lock()
    cfg = load_config(None)
    spill_dir = "/home/z/my-project/download/campaign/spill"
    os.makedirs(spill_dir, exist_ok=True)
    done = {"n": 0, "ok": 0}

    def work(item: tuple[str, str]) -> None:
        url, category = item
        try:
            outcome = fetch_resilient(cfg, url, limits=limits, pacer=pacer, spill_dir=spill_dir, cookie_jar=jar)
        except Exception as exc:  # never let one URL kill the worker
            print(json.dumps({"event": "driver_error", "url": url, "error": str(exc)[:120]}), flush=True)
            return
        with lock, con:
            con.execute(
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
        with lock:
            done["n"] += 1
            if outcome.ok:
                done["ok"] += 1
            print(
                json.dumps(
                    {
                        "event": "fetched",
                        "url": url,
                        "kind": outcome.kind,
                        "status": outcome.status,
                        "fallback": outcome.fallback,
                        "attempts": outcome.attempts,
                        "ms": outcome.elapsed_ms,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(work, item) for item in urls]
            for fut in futures:
                fut.result()
    except KeyboardInterrupt:
        print("interrupted: completed URLs are persisted", flush=True)
    print(json.dumps({"completed": done["n"], "recovered_ok": done["ok"]}), flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
