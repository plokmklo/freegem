"""AI-deep phase: exercise the AI-assisted rungs where they actually apply.

Phase A of the campaign (ai_scrape_bench.py) ran 1003 sites in auto mode;
most of its 256 unreadable pages are REFUSALS (403 bot walls), where no
extraction — AI or not — has anything to read. The AI rung's real target
population is pages that ANSWER but extract badly, plus a control sample
of pages that extract well (to measure how often the gate correctly
refuses to replace good heuristic text).

This pass:
  1. every `nav_only`-graded readable page from phase A -> ai_extract=
     "always" (the model tries a rescue; the gate decides), and every
     ACCEPTED rescue gets one grounded verify question;
  2. a deterministic random sample of 40 `ok`-graded pages -> always-mode
     too (expect the gate to reject most — that IS the honesty result);
  3. everything lands in the ai_deep table (resumable) and folds into
     ai_scrape_report.json as an "ai_deep" section.

All model calls share the campaign's global throttle (min interval +
jitter + 429 cooldown). Chunk-safe: re-invoke until it reports DONE.
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

DB_PATH = bench.DB
TIME_BUDGET = float(os.environ.get("AI_DEEP_TIME_BUDGET", "420"))
OK_SAMPLE = 40
SEED = 42

DEEP_SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_deep (
    url TEXT PRIMARY KEY,
    source TEXT,
    gate TEXT,
    via TEXT,
    quality TEXT,
    chars_before INTEGER,
    chars_after INTEGER,
    verify_ok INTEGER,
    answer TEXT,
    notes TEXT,
    ts TEXT
)
"""


def selection(con: sqlite3.Connection) -> tuple[list[dict], list[dict]]:
    nav_only = [
        {"url": r[0], "chars": r[1], "source": "nav_only"}
        for r in con.execute(
            "SELECT url, chars FROM results WHERE kind='ok' AND quality='nav_only'"
        )
    ]
    ok_urls = [
        r[0] for r in con.execute("SELECT url FROM results WHERE kind='ok' AND quality='ok'")
    ]
    sample = sorted(random.Random(SEED).sample(ok_urls, min(OK_SAMPLE, len(ok_urls))))
    ok_rows = [{"url": u, "chars": 0, "source": "ok_sample"} for u in sample]
    return nav_only, ok_rows


def main() -> int:
    con = bench.con
    con.execute(DEEP_SCHEMA)
    con.commit()
    nav_only, ok_sample = selection(con)
    deep_done = {r[0] for r in con.execute("SELECT url FROM ai_deep")}
    todo = [s for grp in (nav_only, ok_sample) for s in grp if s["url"] not in deep_done]
    print(
        f"deep phase: nav_only={len(nav_only)} ok_sample={len(ok_sample)} todo={len(todo)}",
        flush=True,
    )
    if not todo:
        fold(con)
        print("AI-DEEP DONE", flush=True)
        return 0

    cfg = BridgeConfig(log_requests=False).replace(smart_fetch_ai=False)
    throttle = bench.AiThrottle(9.0)
    started = time.monotonic()

    import threading

    lock = threading.Lock()

    def save(row: dict) -> None:
        con.execute(
            "INSERT OR REPLACE INTO ai_deep VALUES (:url,:source,:gate,:via,:quality,"
            ":chars_before,:chars_after,:verify_ok,:answer,:notes,:ts)",
            row,
        )
        con.commit()

    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(deep_one, cfg, throttle, lock, seed): seed for seed in todo}
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
                    print("save-fail:", type(exc).__name__, exc, flush=True)
            if done_set:
                saved = con.execute("SELECT COUNT(*) FROM ai_deep").fetchone()[0]
                print(f"[deep {saved}/{len(todo)}]", flush=True)

    fold(con)
    remaining = len(todo) - con.execute("SELECT COUNT(*) FROM ai_deep").fetchone()[0]
    if remaining <= 0:
        print("AI-DEEP DONE", flush=True)
        return 0
    print(f"AI-DEEP partial: {remaining} left — re-invoke", flush=True)
    return 3


def deep_one(
    cfg: BridgeConfig,
    throttle: bench.AiThrottle,
    lock: threading.Lock,
    seed: dict,
) -> dict:
    url = seed["url"]

    def complete(prompt: str) -> str:
        return throttle.complete(cfg, prompt)

    row = {
        "url": url,
        "source": seed["source"],
        "gate": "failed",
        "via": "",
        "quality": "",
        "chars_before": seed["chars"],
        "chars_after": 0,
        "verify_ok": None,
        "answer": None,
        "notes": "",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        res = ai_scrape(cfg, url, ai_extract="always", smart=True, complete=complete)
    except Exception as exc:
        row["notes"] = f"{type(exc).__name__}: {exc}"[:300]
        return row
    row["via"] = res.via
    row["quality"] = res.quality
    row["chars_after"] = len(res.text)
    if not res.ok:
        row["gate"] = "fetch_failed"
        row["notes"] = (res.error or "")[:300]
        return row
    if res.via == "ai-extract":
        row["gate"] = "accepted"
    elif any("no recoverable content" in n for n in res.notes):
        row["gate"] = "none"
    elif any("rejected" in n for n in res.notes):
        row["gate"] = "rejected"
    elif any("failed" in n for n in res.notes):
        row["gate"] = "ai_failed"
    else:
        row["gate"] = "unchanged"
    row["notes"] = " | ".join(res.notes)[:400]
    if res.via == "ai-extract":
        try:
            answer = throttle.complete(
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
    """Fold the ai_deep table into the campaign report as its own section."""
    from collections import Counter

    report_path = bench.REPORT
    if not report_path.exists():
        bench.build_report()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    rows = [
        dict(
            zip(
                [c[0] for c in con.execute("SELECT * FROM ai_deep LIMIT 1").description],
                r,
                strict=False,
            )
        )
        for r in con.execute("SELECT * FROM ai_deep")
    ]
    gates = Counter(r["gate"] for r in rows)
    by_source: dict[str, Counter] = {}
    for r in rows:
        by_source.setdefault(r["source"] or "?", Counter())[r["gate"]] += 1
    verified = [r for r in rows if r["verify_ok"]]
    report["ai_deep"] = {
        "design": (
            "always-mode rescue on every nav_only page + a deterministic sample of "
            "40 ok pages (gate honesty control); accepted rescues get one grounded "
            "verify question"
        ),
        "attempted": len(rows),
        "gates": dict(gates),
        "by_source": {k: dict(v) for k, v in sorted(by_source.items())},
        "verify_ok": len(verified),
        "examples": [
            {
                "url": r["url"],
                "chars": f"{r['chars_before']}->{r['chars_after']}",
                "answer": (r["answer"] or "")[:160],
            }
            for r in verified[:6]
        ],
    }
    report_path.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["ai_deep"]["gates"], indent=1), flush=True)


if __name__ == "__main__":
    sys.exit(main())
