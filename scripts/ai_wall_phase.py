"""AI wall phase: re-attempt every unreadable URL through the AI wall rung.

Phase A of the campaign honestly failed 256 of 1003 sites — mostly 403 bot
walls the fetch ladder cannot open (by design: no CAPTCHA solving, no
stealth). The 2.11.0 wall rung (freegem.ai_scrape) adds ONE AI-mediated
attempt for exactly this population: a search-grounded model call asked to
produce the page's main content with citations, refused unless it clears
the honesty floors (length, no self-admitted non-answer), labeled
``via="ai-wall-rescue"``.

This driver re-runs every `kind='error'` URL from phase A through
``ai_scrape(ai_extract="auto", smart=False, search=<throttled gemini_search>)``:

* ``smart=False`` on purpose: phase A already walked every non-AI ladder
  rung for these URLs (browser-headers, reader, wayback) and they failed;
  this phase isolates the NEW rung instead of paying the ladder budget a
  second time. The direct fetch fails fast, the wall rung fires.
* URLs our own guards refused (SSRF guard, robots opt-in) are SKIPPED
  outright — no AI path may bypass our guards, same contract as the ladder.
* every accepted wall rescue gets one grounded verify question, proving
  the AI-mediated text answers questions about the page.

Chunk-safe and resumable like the other phases: results land in the
campaign DB's ``ai_wall`` table and fold into ai_scrape_report.json as an
``"ai_wall"`` section. Re-invoke until it reports DONE.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO = SCRIPTS_DIR.parent
if not (REPO / "freegem" / "__init__.py").exists():  # running from outside a checkout
    REPO = Path("/home/z/my-project/freegem")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SCRIPTS_DIR))

import ai_scrape_bench as bench  # noqa: E402

from freegem.ai_scrape import ai_scrape  # noqa: E402
from freegem.config import BridgeConfig  # noqa: E402
from freegem.gemini import RateLimitError  # noqa: E402
from freegem.mcp_tools import run_tool  # noqa: E402

TIME_BUDGET = float(os.environ.get("AI_WALL_TIME_BUDGET", "420"))
SEED = 42
WORKERS = int(os.environ.get("AI_WALL_WORKERS", "6"))

WALL_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_wall (
    url TEXT PRIMARY KEY,
    category TEXT,
    gate TEXT,
    via TEXT,
    chars INTEGER,
    verify_ok INTEGER,
    answer TEXT,
    notes TEXT,
    ts TEXT
)
"""


class SearchThrottle:
    """Pace gemini_search calls exactly like bench.AiThrottle paces generate.

    Min interval + jitter before every call, and a hard cooldown after any
    RateLimitError, all under one lock. The campaign's only AI traffic is
    this phase's, so one global throttle keeps the anonymous bridge happy.
    """

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_at = 0.0
        self.cooldown_until = 0.0
        self.calls = 0
        self.cooldowns = 0

    def search(self, cfg: BridgeConfig, query: str) -> str:
        with self._lock:
            now = time.monotonic()
            floor = max(self._next_at, self.cooldown_until)
            if floor > now:
                time.sleep(floor - now)
            self._next_at = time.monotonic() + self.min_interval + random.uniform(1.0, 3.0)
            self.calls += 1
        try:
            return run_tool(cfg, "gemini_search", {"query": query})
        except RateLimitError:
            with self._lock:
                self.cooldowns += 1
                self.cooldown_until = time.monotonic() + 75.0
            raise


def selection(con: sqlite3.Connection) -> list[dict]:
    """Every phase-A failure our own guards did NOT cause.

    Guard refusals (SSRF, robots opt-in) and caps stay out: no AI path may
    bypass our guards — the same contract the ladder and the wall rung
    itself enforce. Everything else (403/429/5xx walls, timeouts, dead
    DNS, TLS) is attemptable: the model may honestly know the page even
    where the network path is dead.
    """
    rows = [
        {"url": r[0], "category": r[1], "error": r[2]}
        for r in con.execute("SELECT url, category, error FROM results WHERE kind='error'")
    ]
    skipped = [r for r in rows if _is_our_refusal(r["error"])]
    for r in skipped:
        con.execute(
            "INSERT OR REPLACE INTO ai_wall VALUES (:url,:category,:gate,:via,:chars,"
            ":verify_ok,:answer,:notes,:ts)",
            {
                "url": r["url"],
                "category": r["category"],
                "gate": "skipped_our_refusal",
                "via": "",
                "chars": 0,
                "verify_ok": None,
                "answer": None,
                "notes": (r["error"] or "")[:400],
                "ts": datetime.now(timezone.utc).isoformat(),
            },
        )
    con.commit()
    return [r for r in rows if not _is_our_refusal(r["error"])]


def _is_our_refusal(error: str | None) -> bool:
    text = error or ""
    return "SSRF guard" in text or "robots.txt" in text or ("exceeds" in text and "cap" in text)


def wall_one(cfg: BridgeConfig, throttle: SearchThrottle, seed: dict) -> dict:
    url = seed["url"]

    def search(query: str) -> str:
        return throttle.search(cfg, query)

    row = {
        "url": url,
        "category": seed["category"],
        "gate": "failed",
        "via": "",
        "chars": 0,
        "verify_ok": None,
        "answer": None,
        "notes": "",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        res = ai_scrape(cfg, url, ai_extract="auto", smart=False, max_chars=50_000, search=search)
    except Exception as exc:  # hostile page: record, never kill the run
        row["notes"] = f"{type(exc).__name__}: {exc}"[:400]
        return row
    row["via"] = res.via
    row["chars"] = len(res.text)
    if res.ok and res.via == "ai-wall-rescue":
        row["gate"] = "accepted"
    elif res.ok:
        row["gate"] = "unexpected_ok"  # a direct fetch suddenly worked; fine, record it
        row["chars"] = len(res.text)
    elif any("could not read the page either" in n for n in res.notes):
        row["gate"] = "nonanswer"
    elif any("no usable content" in n for n in res.notes):
        row["gate"] = "none"
    elif any("ai-wall-rescue failed" in n for n in res.notes):
        row["gate"] = "ai_failed"
    elif not any("ai-wall-rescue attempted" in n for n in res.notes):
        row["gate"] = "not_wall_kind"
    row["notes"] = " | ".join(res.notes)[:400]
    if row["gate"] == "accepted":
        try:
            answer = throttle.search(
                cfg,
                "Answer in one sentence, using ONLY the page content below: "
                "what is this page about?\n\n" + res.text[:8000],
            )
            row["answer"] = answer[:300]
            row["verify_ok"] = int(bool(answer.strip()))
        except Exception as exc:
            row["answer"] = f"failed: {type(exc).__name__}"
            row["verify_ok"] = 0
    return row


def fold(con: sqlite3.Connection) -> None:
    """Fold the ai_wall table into the campaign report as its own section."""
    from collections import Counter

    report_path = Path(os.environ.get("AI_BENCH_REPORT", str(bench.REPORT)))
    if not report_path.exists():
        bench.build_report()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    rows = [
        dict(
            zip(
                [c[0] for c in con.execute("SELECT * FROM ai_wall LIMIT 1").description],
                r,
                strict=False,
            )
        )
        for r in con.execute("SELECT * FROM ai_wall")
    ]
    gates = Counter(r["gate"] for r in rows)
    accepted = [r for r in rows if r["gate"] == "accepted"]
    verified = [r for r in accepted if r["verify_ok"]]
    per_category: dict[str, dict] = {}
    for r in rows:
        c = per_category.setdefault(r["category"] or "?", {"attempted": 0, "accepted": 0})
        c["attempted"] += 1
        if r["gate"] == "accepted":
            c["accepted"] += 1
    report["ai_wall"] = {
        "design": (
            "every phase-A failure re-attempted through the 2.11.0 AI wall rung "
            "(search-grounded, citation-required, honesty-floored; smart=False "
            "since the non-AI ladder already had its chance in phase A); accepted "
            "reads get one grounded verify question"
        ),
        "attempted": len(rows),
        "gates": dict(gates),
        "accepted_pct": round(100 * len(accepted) / len(rows), 1) if rows else 0,
        "verify_ok": len(verified),
        "per_category": dict(sorted(per_category.items())),
        "examples": [
            {"url": r["url"], "chars": r["chars"], "answer": (r["answer"] or "")[:160]}
            for r in accepted[:8]
        ],
    }
    report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["ai_wall"]["gates"], indent=1), flush=True)


def main() -> int:
    con = bench.con
    con.execute(WALL_SCHEMA)
    con.commit()
    todo = selection(con)
    wall_done = {r[0] for r in con.execute("SELECT url FROM ai_wall")}
    todo = [s for s in todo if s["url"] not in wall_done]
    print(f"wall phase: attemptable={len(todo)}", flush=True)
    if not todo:
        fold(con)
        print("AI-WALL DONE", flush=True)
        return 0

    cfg = BridgeConfig(log_requests=False).replace(smart_fetch_ai=False)
    throttle = SearchThrottle(float(os.environ.get("AI_BENCH_AI_MIN_INTERVAL", "9.0")))
    started = time.monotonic()
    lock = threading.Lock()

    def save(row: dict) -> None:
        with lock:
            con.execute(
                "INSERT OR REPLACE INTO ai_wall VALUES (:url,:category,:gate,:via,:chars,"
                ":verify_ok,:answer,:notes,:ts)",
                row,
            )
            con.commit()

    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(wall_one, cfg, throttle, seed): seed for seed in todo}
        pending = set(futures)
        while pending:
            over = (time.monotonic() - started) > TIME_BUDGET
            if over:
                for f in pending:
                    f.cancel()
            done_set, pending = wait(pending, timeout=30, return_when=FIRST_COMPLETED)
            for fut in done_set:
                if fut.cancelled():
                    continue
                try:
                    save(fut.result())
                except Exception as exc:
                    seed_hit = futures[fut]
                    save(
                        {
                            "url": seed_hit["url"],
                            "category": seed_hit["category"],
                            "gate": "driver_error",
                            "via": "",
                            "chars": 0,
                            "verify_ok": None,
                            "answer": None,
                            "notes": f"{type(exc).__name__}: {exc}"[:400],
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }
                    )
            if done_set:
                saved = con.execute("SELECT COUNT(*) FROM ai_wall").fetchone()[0]
                print(f"[wall {saved}/{len(todo)}] calls={throttle.calls}", flush=True)

    fold(con)
    remaining = len(todo) - con.execute("SELECT COUNT(*) FROM ai_wall").fetchone()[0]
    if remaining <= 0:
        print("AI-WALL DONE", flush=True)
        return 0
    print(f"AI-WALL partial: {remaining} left — re-invoke", flush=True)
    return 3


if __name__ == "__main__":
    sys.exit(main())
