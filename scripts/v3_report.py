"""Final v3 campaign report from the state DB (comparable with v2's)."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter

DB = "/home/z/my-project/download/campaign/campaign_state.db"
OUT = "/home/z/my-project/download/campaign/report_v3_final.json"

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
rows = [dict(r) for r in con.execute("SELECT * FROM results")]

readable = [r for r in rows if r["kind"] == "ok" and not r["error"]]
failures = [r for r in rows if r["error"]]

taxonomy: Counter[str] = Counter()
for r in failures:
    status, err = r["status"], (r["error"] or "").lower()
    if status == 403:
        taxonomy["hard_cdn_wall_403"] += 1
    elif status == 429:
        taxonomy["rate_limited_429"] += 1
    elif status in (401, 402, 451):
        taxonomy["auth_paywall_legal"] += 1
    elif status in (404, 410):
        taxonomy["dead_path_404"] += 1
    elif status in (301, 302, 303, 307, 308):
        taxonomy[f"redirect_loop_{status}"] += 1
    elif status is not None:
        taxonomy[f"other_http_error_{status}"] += 1
    elif "timed out" in err:
        taxonomy["timeout_slow_origin"] += 1
    elif "certificate" in err or "ssl" in err:
        taxonomy["tls_failure"] += 1
    elif "cannot resolve" in err or "name or service" in err:
        taxonomy["dead_dns"] += 1
    else:
        taxonomy["network_egress_or_dead"] += 1

fallback_ok: Counter[str] = Counter()
via_ok: Counter[str] = Counter()
for r in readable:
    if r["fallback"]:
        fallback_ok[r["fallback"]] += 1
    via_ok[r["via"] or "origin"] += 1

host403: Counter[str] = Counter()
for r in failures:
    if r["status"] == 403:
        host403[(r["url"] or "").split("/")[2]] += 1

payload = {
    "campaign": "freegem bulk fetch v3 (2.7.0 ladder: shared cookie jar, DoH fallback, "
    "wayback last resort, 5xx origin-root, 3 profiles, no robots.txt by design)",
    "attempted": len(rows),
    "readable": len(readable),
    "readable_pct": round(100.0 * len(readable) / len(rows), 2),
    "total_bytes_readable": sum(r["bytes"] or 0 for r in readable),
    "failure_taxonomy": dict(taxonomy.most_common()),
    "readable_via_fallback": dict(fallback_ok.most_common()),
    "readable_via": dict(via_ok.most_common()),
    "hard_wall_403_hosts_count": len(host403),
    "v3_recoveries_vs_v2": [
        "wordpress.org (429 window reset)",
        "www.standaard.be + /about (Chrome profile)",
        "www.tagesanzeiger.ch + /about (shared cookie jar resolves the 302 "
        "entitlement-token loop; origin_root)",
        "www.sohu.com/about (5xx -> origin-root fallback)",
    ],
    "honest_limits_of_this_run": [
        "The sandbox cannot reach web.archive.org (TCP connect timeout), so the "
        "wayback last resort — which would cover most of the remaining walls and "
        "dead domains from an unrestricted network — fails here and is reported "
        "honestly as network/timeout.",
        "The egress IP is datacenter-flagged: the 403 bucket is Akamai/Cloudflare "
        "IP-reputation, not a header problem (three browser profiles do not move it).",
        "Dead domains confirmed NXDOMAIN at DNS-over-HTTPS (www.tcl.tk, "
        "www.hankyoreh.co.kr): nothing to read at the origin.",
    ],
}

with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
print(json.dumps({k: payload[k] for k in ("attempted", "readable", "readable_pct")}, indent=2))
print("taxonomy:", json.dumps(payload["failure_taxonomy"], indent=2))
print("fallback attribution:", json.dumps(payload["readable_via_fallback"], indent=2))
