"""AI-assisted scrape campaign: >=900 real websites through freegem.ai_scrape.

Resumable, checkpointed (sqlite), chunk-safe: run it repeatedly; each run
attempts seeds until either the corpus is exhausted (exit 0, report written)
or the wall-clock budget (AI_BENCH_TIME_BUDGET, default 520s) expires
(exit 3, checkpoint intact — re-invoke to continue).

Design decisions carried over from the prior 1000-run hybrid campaign:

* fetch workers run in parallel; AI calls are SERIALIZED through a global
  throttle (min interval + jitter + RateLimitError cooldown) because the
  anonymous Gemini bridge rate-limits hard under concurrency;
* the smart-fetch ladder's own ai-search rung is DISABLED for the run
  (cfg.smart_fetch_ai=False) so the only AI calls are this campaign's
  throttled rescue/verify calls — same policy as the hybrid run;
* every result (ok or failed) is checkpointed the moment it lands;
* phase B (verify) asks the model one grounded question per AI-RESCUED
  page, proving end-to-end that the rescued text answers questions.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import sys
import threading
import time
import urllib.parse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if not (REPO / "freegem" / "__init__.py").exists():  # running from outside a checkout
    REPO = Path("/home/z/my-project/freegem")
sys.path.insert(0, str(REPO))

from freegem.ai_scrape import AiMemory, ai_scrape  # noqa: E402
from freegem.config import BridgeConfig  # noqa: E402
from freegem.gemini import RateLimitError, generate  # noqa: E402
from freegem.web_scraper import extraction_quality  # noqa: E402

SEEDS = REPO / "scripts" / "p3_seeds_1000.jsonl"
DB = REPO / "scripts" / "ai_bench_state.db"
REPORT = Path(
    os.environ.get("AI_BENCH_REPORT", str(REPO / "scripts" / "ai_bench_report.json"))
)

FETCH_WORKERS = int(os.environ.get("AI_BENCH_WORKERS", "12"))
TIME_BUDGET = float(os.environ.get("AI_BENCH_TIME_BUDGET", "520"))
TARGET_MIN = int(os.environ.get("AI_BENCH_MIN", "900"))
AI_MIN_INTERVAL = float(os.environ.get("AI_BENCH_AI_MIN_INTERVAL", "9.0"))
PAGE_CHARS = 50_000

_schema = """
CREATE TABLE IF NOT EXISTS results (
    url TEXT PRIMARY KEY,
    category TEXT,
    kind TEXT,            -- ok | error
    via TEXT,
    quality TEXT,
    ai_used INTEGER,
    ai_notes TEXT,
    chars INTEGER,
    error TEXT,
    verify_answer TEXT,
    verify_ok INTEGER,
    ts TEXT
)
"""

con = sqlite3.connect(DB)
con.execute(_schema)
con.commit()


def load_seeds() -> list[dict]:
    seeds = []
    for line in SEEDS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            seeds.append(json.loads(line))
    return seeds


def done_urls() -> set[str]:
    return {r[0] for r in con.execute("SELECT url FROM results")}


class AiThrottle:
    """Serialize every model call: min interval + jitter + 429 cooldown."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._next_at = 0.0
        self.cooldown_until = 0.0
        self.calls = 0
        self.cooldowns = 0

    def complete(self, cfg: BridgeConfig, prompt: str) -> str:
        with self._lock:
            now = time.monotonic()
            floor = max(self._next_at, self.cooldown_until)
            if floor > now:
                time.sleep(floor - now)
            self._next_at = time.monotonic() + self.min_interval + random.uniform(1.0, 3.0)
            self.calls += 1
        try:
            return generate(cfg, prompt, 0, 0)
        except RateLimitError:
            with self._lock:
                self.cooldowns += 1
                self.cooldown_until = time.monotonic() + 75.0
            raise


def attempt(
    cfg: BridgeConfig,
    seed: dict,
    throttle: AiThrottle,
    memory: AiMemory,
    stats_lock: threading.Lock,
    stats: dict,
) -> dict:
    url = seed["url"]
    row = {
        "url": url,
        "category": seed.get("category", ""),
        "kind": "error",
        "via": "none",
        "quality": "",
        "ai_used": 0,
        "ai_notes": "",
        "chars": 0,
        "error": None,
        "verify_answer": None,
        "verify_ok": None,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    rescued = False

    def complete(prompt: str) -> str:
        return throttle.complete(cfg, prompt)

    try:
        res = ai_scrape(
            cfg,
            url,
            ai_extract="auto",
            max_chars=PAGE_CHARS,
            smart=True,
            complete=complete,
            memory=memory,
        )
    except Exception as exc:  # hostile page: record, never kill the run
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row

    if res.ok:
        row.update(kind="ok", via=res.via, quality=res.quality, chars=len(res.text))
        row["ai_used"] = int(res.ai_used)
        row["ai_notes"] = " | ".join(res.notes)[:400]
        rescued = res.via == "ai-extract"
    else:
        row["error"] = (res.error or "unknown")[:400]
        row["via"] = res.via
        row["ai_notes"] = " | ".join(res.notes)[:400]

    # Phase B: one grounded question per RESCUED page (proves the answer path).
    if rescued:
        try:
            answer = throttle.complete(
                cfg,
                "Answer in one sentence, using ONLY the page content below: "
                "what is this page about?\n\n" + res.text[:8000],
            )
            row["verify_answer"] = answer[:300]
            row["verify_ok"] = int(bool(answer.strip()))
        except Exception as exc:
            row["verify_answer"] = f"failed: {type(exc).__name__}"
            row["verify_ok"] = 0
    return row


def save(row: dict, stats_lock: threading.Lock, stats: dict) -> None:
    con.execute(
        "INSERT OR REPLACE INTO results VALUES (:url,:category,:kind,:via,:quality,"
        ":ai_used,:ai_notes,:chars,:error,:verify_answer,:verify_ok,:ts)",
        row,
    )
    con.commit()
    with stats_lock:
        stats["done"] += 1
        done = stats["done"]
    if done % 25 == 0:
        print(f"[{done}] latest: {row['kind']} via={row['via']} {row['url'][:70]}", flush=True)


def main() -> int:
    started = time.monotonic()
    seeds = load_seeds()
    done = done_urls()
    pending = [s for s in seeds if s["url"] not in done]
    print(f"corpus={len(seeds)} done={len(done)} pending={len(pending)}", flush=True)
    if not pending:
        build_report()
        print("ALL DONE — report at", REPORT, flush=True)
        return 0

    cfg = BridgeConfig(log_requests=False).replace(smart_fetch_ai=False)
    throttle = AiThrottle(AI_MIN_INTERVAL)
    memory = AiMemory(Path("/tmp/ai_bench_memory.json"))
    stats = {"done": 0}
    stats_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        futures = {
            pool.submit(attempt, cfg, seed, throttle, memory, stats_lock, stats): seed
            for seed in pending
        }
        pending_futs = set(futures)
        while pending_futs:
            over = (time.monotonic() - started) > TIME_BUDGET
            if over:
                for f in pending_futs:
                    f.cancel()  # queued work drops; in-flight attempts finish
            done_set, pending_futs = wait(pending_futs, timeout=30, return_when=FIRST_COMPLETED)
            for fut in done_set:
                if fut.cancelled():
                    continue
                try:
                    row = fut.result()
                except Exception as exc:  # belt and braces: attempt() already guards
                    seed_hit = futures[fut]
                    row = {
                        "url": seed_hit["url"],
                        "category": seed_hit.get("category", ""),
                        "kind": "error",
                        "via": "none",
                        "quality": "",
                        "ai_used": 0,
                        "ai_notes": "",
                        "chars": 0,
                        "error": f"driver: {type(exc).__name__}: {exc}"[:400],
                        "verify_answer": None,
                        "verify_ok": None,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    }
                save(row, stats_lock, stats)

    total_done = len(done_urls())
    attempted_now = stats["done"]
    print(
        f"chunk complete: +{attempted_now} this run, {total_done} total checkpointed, "
        f"ai_calls={throttle.calls} cooldowns={throttle.cooldowns}",
        flush=True,
    )
    if total_done >= TARGET_MIN and total_done == len(seeds):
        build_report()
        print("ALL DONE — report at", REPORT, flush=True)
        return 0
    if total_done >= TARGET_MIN and not pending:
        build_report()
        return 0
    return 3  # more work remains; re-invoke


def build_report() -> None:
    rows = [dict(zip([c[0] for c in con.execute("SELECT * FROM results LIMIT 1").description], r))
            for r in con.execute("SELECT * FROM results")]
    n = len(rows)
    ok_rows = [r for r in rows if r["kind"] == "ok"]
    errs = [r for r in rows if r["kind"] == "error"]
    quality: dict[str, int] = {}
    via: dict[str, int] = {}
    for r in ok_rows:
        quality[r["quality"]] = quality.get(r["quality"], 0) + 1
        via[r["via"]] = via.get(r["via"], 0) + 1
    rescued = [r for r in ok_rows if r["via"] == "ai-extract"]
    verify_ok = sum(1 for r in rescued if r["verify_ok"])
    rescue_attempts = sum(
        1 for r in ok_rows if "ai-extract attempted" in (r["ai_notes"] or "")
    )
    taxonomy: dict[str, int] = {}
    for r in errs:
        e = (r["error"] or "").lower()
        if "403" in e:
            taxonomy["hard_cdn_wall_403"] = taxonomy.get("hard_cdn_wall_403", 0) + 1
        elif "429" in e:
            taxonomy["rate_limited_429"] = taxonomy.get("rate_limited_429", 0) + 1
        elif "timed out" in e or "timeout" in e:
            taxonomy["timeout"] = taxonomy.get("timeout", 0) + 1
        elif "certificate" in e or "ssl" in e:
            taxonomy["tls"] = taxonomy.get("tls", 0) + 1
        elif "resolve" in e or "name or service" in e:
            taxonomy["dead_dns"] = taxonomy.get("dead_dns", 0) + 1
        elif "http" in e:
            taxonomy["other_http"] = taxonomy.get("other_http", 0) + 1
        else:
            taxonomy["other"] = taxonomy.get("other", 0) + 1
    per_category: dict[str, dict] = {}
    for r in rows:
        c = per_category.setdefault(r["category"], {"attempted": 0, "ok": 0, "ai_rescued": 0})
        c["attempted"] += 1
        if r["kind"] == "ok":
            c["ok"] += 1
            if r["via"] == "ai-extract":
                c["ai_rescued"] += 1
    report = {
        "campaign": "AI-assisted smart scraping — freegem 2.10.0 (8aadfca)",
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "totals": {
            "attempted": n,
            "readable": len(ok_rows),
            "readable_pct": round(100 * len(ok_rows) / n, 1) if n else 0,
            "honestly_unreadable": len(errs),
        },
        "quality_labels": quality,
        "via": via,
        "ai": {
            "rescue_attempts": rescue_attempts,
            "rescued": len(rescued),
            "rescued_pct_of_attempts": round(100 * len(rescued) / n, 1) if n else 0,
            "verify_asked": len(rescued),
            "verify_ok": verify_ok,
        },
        "failure_taxonomy": taxonomy,
        "per_category": dict(sorted(per_category.items())),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report["totals"], indent=1), flush=True)


if __name__ == "__main__":
    sys.exit(main())
