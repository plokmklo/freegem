"""TLS phase: re-attempt the campaign's wall-class failures with the 2.12.0
opt-in `tls-browser` rung enabled.

Population: every `kind='error'` row in the campaign DB whose error carries
an HTTP status (the 403/401/429/5xx bot-wall class), MINUS the URLs the
2.11.0 AI wall phase already rescued (`ai_wall.gate='accepted'`), MINUS
anything this phase already attempted (resumable).

Each URL runs through the ordinary smart-fetch ladder
(`scrape_page`, smart_fetch on) with `smart_fetch_tls_browser=True` — so a
win is attributed to whatever rung actually opened the door: `tls-browser`,
or a rung that only ran because tls-browser failed. Every result keeps the
ladder's full honest taxonomy in `notes`. No AI calls in this phase; the
2.11.0 wall phase covered the AI-mediated population separately.

Chunk-safe like the other phases (AI_TLS_TIME_BUDGET seconds per chunk,
resumable sqlite table `tls_phase`); re-invoke until it reports DONE.
Folds a `tls_phase` section into the campaign report and writes a
standalone copy to $AI_TLS_REPORT (default: the download report directory).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
REPO = SCRIPTS_DIR.parent
if not (REPO / "freegem" / "__init__.py").exists():  # running from outside a checkout
    REPO = Path("/home/z/my-project/freegem")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(SCRIPTS_DIR))

import ai_scrape_bench as bench  # noqa: E402

from freegem.config import BridgeConfig  # noqa: E402
from freegem.web_scraper import ScrapeError, scrape_page  # noqa: E402

TIME_BUDGET = float(os.environ.get("AI_TLS_TIME_BUDGET", "420"))
WORKERS = int(os.environ.get("AI_TLS_WORKERS", "6"))
HTTP_ERR_RE = re.compile(r"HTTP (\d{3})")
DOWN_REPORT = Path(
    os.environ.get("AI_TLS_REPORT", "/home/z/my-project/download/tls_phase_report.json")
)

TLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS tls_phase (
    url TEXT PRIMARY KEY,
    category TEXT,
    gate TEXT,
    via TEXT,
    chars INTEGER,
    notes TEXT,
    ts TEXT
)
"""


def population(con: sqlite3.Connection) -> list[dict]:
    walls = [
        {"url": r[0], "category": r[1], "error": r[2]}
        for r in con.execute("SELECT url, category, error FROM results WHERE kind='error'")
        if HTTP_ERR_RE.search(r[2] or "")
    ]
    rescued = {r[0] for r in con.execute("SELECT url FROM ai_wall WHERE gate='accepted'")}
    done = {r[0] for r in con.execute("SELECT url FROM tls_phase")}
    return [r for r in walls if r["url"] not in rescued and r["url"] not in done]


def tls_one(cfg: BridgeConfig, seed: dict) -> dict:
    url = seed["url"]
    row = {
        "url": url,
        "category": seed["category"],
        "gate": "refused",
        "via": "",
        "chars": 0,
        "notes": "",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        res = scrape_page(cfg, url, max_chars=50_000, smart=True)
    except ScrapeError as exc:
        # The full honest taxonomy (per-rung refusals) is the note.
        row["notes"] = f"{exc}"[:400]
        if "bot-challenge page" in str(exc):
            row["gate"] = "challenge"
        return row
    except Exception as exc:  # hostile page: record, never kill the run
        row["notes"] = f"{type(exc).__name__}: {exc}"[:400]
        return row
    row["via"] = res.via
    row["chars"] = len(res.text)
    if res.via == "tls-browser":
        row["gate"] = "tls_win"
    elif res.via == "direct":
        row["gate"] = "direct_ok"
    else:
        row["gate"] = "ladder_win"
    row["notes"] = f"truncated={res.truncated}"
    return row


def fold(con: sqlite3.Connection) -> dict:
    rows = [
        dict(
            zip(
                [c[0] for c in con.execute("SELECT * FROM tls_phase LIMIT 1").description],
                r,
                strict=False,
            )
        )
        for r in con.execute("SELECT * FROM tls_phase")
    ]
    gates = Counter(r["gate"] for r in rows)
    wins = [r for r in rows if r["gate"] in ("tls_win", "direct_ok", "ladder_win")]
    via_counts = Counter(r["via"] for r in wins)
    per_category: dict[str, dict] = {}
    for r in rows:
        c = per_category.setdefault(r["category"] or "?", {"attempted": 0, "won": 0})
        c["attempted"] += 1
        if r["gate"] in ("tls_win", "direct_ok", "ladder_win"):
            c["won"] += 1
    examples = [{"url": r["url"], "via": r["via"], "chars": r["chars"]} for r in wins[:20]]
    return {
        "design": (
            "wall-class failures (HTTP-status errors, minus AI-wall-phase rescues) "
            "re-attempted through the standard smart-fetch ladder with the "
            "opt-in 2.12.0 tls-browser rung enabled (smart_fetch_tls_browser=True); "
            "no AI calls; every result carries the full per-rung taxonomy"
        ),
        "attempted": len(rows),
        "gate_counts": dict(gates),
        "wins": len(wins),
        "win_via": dict(via_counts),
        "per_category": per_category,
        "examples": examples,
    }


def main() -> int:
    started = time.monotonic()
    con = sqlite3.connect(bench.DB)
    con.execute(TLS_SCHEMA)
    con.commit()
    seeds = population(con)
    print(f"tls-phase population={len(seeds)}", flush=True)
    if not seeds:
        report = fold(con)
        DOWN_REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False))
        print("DONE —", json.dumps(report["gate_counts"]), flush=True)
        return 0

    cfg = BridgeConfig(log_requests=False).replace(
        smart_fetch_ai=False, smart_fetch_tls_browser=True
    )
    stats = {"done": 0}
    stats_lock = threading.Lock()
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(tls_one, cfg, seed): seed for seed in seeds}
        for future in list(futures):
            if (time.monotonic() - started) > TIME_BUDGET:
                future.cancel()
                continue
            try:
                row = future.result()
            except Exception as exc:  # driver-level safety net
                seed = futures[future]
                row = {
                    "url": seed["url"],
                    "category": seed["category"],
                    "gate": "driver_error",
                    "via": "",
                    "chars": 0,
                    "notes": f"{type(exc).__name__}: {exc}"[:400],
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            con.execute(
                "INSERT OR REPLACE INTO tls_phase VALUES (:url,:category,:gate,:via,"
                ":chars,:notes,:ts)",
                row,
            )
            con.commit()
            with stats_lock:
                stats["done"] += 1
                if stats["done"] % 10 == 0:
                    print(f"done={stats['done']}", flush=True)

    remaining = len(population(con))
    report = fold(con)
    DOWN_REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    # Fold into the campaign report too (same convention as the wall phase).
    if not bench.REPORT.exists():
        bench.build_report()
    campaign = json.loads(bench.REPORT.read_text(encoding="utf-8"))
    campaign["tls_phase"] = report
    bench.REPORT.write_text(json.dumps(campaign, indent=1, ensure_ascii=False))
    print(
        f"remaining={remaining} wins={report['wins']} gates={report['gate_counts']}",
        flush=True,
    )
    if remaining:
        print("NOT DONE — re-invoke for the next chunk", flush=True)
        return 2
    print("DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
