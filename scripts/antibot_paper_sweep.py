#!/usr/bin/env python3
"""Literature sweep: how websites stop automated agents, and how agents respond.

Queries the arXiv export API across multiple topic lenses (bot detection,
anti-scraping, CAPTCHAs, fingerprinting, rate limiting, crawler evasion,
AI-agent access control, paywalls/TDM), dedupes by arXiv ID, and emits:

  /home/z/my-project/scripts/antibot_paper_sweep.json  -- full records
  /home/z/my-project/freegem/docs/antibot_papers.md    -- citable appendix

Hermetic except for the arXiv API itself. 3 s sleep between queries per
arXiv etiquette; one retry per query.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

QUERIES: list[str] = [
    'all:"bot detection"',
    'all:"anti-bot"',
    'all:"web scraping"',
    'all:"anti-scraping"',
    'all:"crawler" AND all:"detection"',
    'all:"CAPTCHA"',
    'all:"browser fingerprinting"',
    'all:"rate limiting"',
    'all:"web crawler" AND all:"blocking"',
    'all:"automated" AND all:"account creation"',
    'all:"AI agents" AND all:"web"',
    'all:"LLM agents" AND all:"web browsing"',
    'all:"paywall"',
    'all:"TLS fingerprint"',
    'all:"headless browser"',
    'all:"data scraping"',
    'all:"text and data mining"',
    'all:"adversarial" AND all:"web crawling"',
    'all:"clickstream" AND all:"bot"',
    'all:"session" AND all:"credential stuffing"',
]

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"

OUT_JSON = Path("/home/z/my-project/scripts/antibot_paper_sweep.json")
OUT_MD = Path("/home/z/my-project/freegem/docs/antibot_papers.md")


def fetch_query(query: str, max_results: int = 60) -> list[dict]:
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {
            "search_query": query,
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
        }
    )
    req = urllib.request.Request(url, headers={"User-Agent": "freegem-research/1.0"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = resp.read()
            break
        except Exception as exc:
            print(f"  ! attempt {attempt} failed: {exc}", file=sys.stderr)
            if attempt == 2:
                return []
            time.sleep(6)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        print(f"  ! xml parse error: {exc}", file=sys.stderr)
        return []

    papers: list[dict] = []
    for entry in root.findall(f"{ATOM}entry"):
        raw_id = (entry.findtext(f"{ATOM}id") or "").strip()
        m = re.search(r"arxiv\.org/abs/([0-9v.]+)", raw_id)
        if not m:
            continue
        arxiv_id = m.group(1)
        title = re.sub(r"\s+", " ", (entry.findtext(f"{ATOM}title") or "").strip())
        summary = re.sub(r"\s+", " ", (entry.findtext(f"{ATOM}summary") or "").strip())
        published = (entry.findtext(f"{ATOM}published") or "")[:10]
        authors = [
            re.sub(r"\s+", " ", (a.findtext(f"{ATOM}name") or "").strip())
            for a in entry.findall(f"{ATOM}author")
        ]
        # first two sentences of the abstract as the takeaway line
        sentences = re.split(r"(?<=[.!?])\s+", summary)
        takeaway = " ".join(sentences[:2])[:300]
        papers.append(
            {
                "arxiv_id": arxiv_id,
                "title": title,
                "year": int(published[:4]) if published[:4].isdigit() else 0,
                "published": published,
                "first_author": authors[0] if authors else "",
                "n_authors": len(authors),
                "abstract": summary[:1200],
                "takeaway": takeaway,
                "queries": [query],
            }
        )
    return papers


def main() -> int:
    merged: dict[str, dict] = {}
    per_query_counts: dict[str, int] = {}
    for i, q in enumerate(QUERIES):
        papers = fetch_query(q)
        per_query_counts[q] = len(papers)
        print(f"[{i + 1}/{len(QUERIES)}] {q}: {len(papers)} results")
        for p in papers:
            aid = p["arxiv_id"]
            if aid in merged:
                merged[aid]["queries"].append(q)
            else:
                merged[aid] = p
        time.sleep(3.2)  # arXiv etiquette

    records = sorted(merged.values(), key=lambda p: (-p["year"], p["title"]))
    payload = {
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n_queries": len(QUERIES),
        "per_query_counts": per_query_counts,
        "n_unique_papers": len(records),
        "papers": records,
    }
    OUT_JSON.write_text(json.dumps(payload, indent=1, ensure_ascii=False))

    lines = [
        "# Appendix: arXiv literature sweep — how websites stop automated agents",
        "",
        f"Generated {payload['generated_utc']} by `scripts/antibot_paper_sweep.py` "
        f"({len(QUERIES)} topic lenses, {len(records)} unique papers after dedup). "
        "Takeaway lines are the opening sentences of each abstract, quoted to keep "
        "the sweep honest (no invented summaries). Sorted newest-first.",
        "",
    ]
    year = None
    for p in records:
        if p["year"] != year:
            year = p["year"]
            lines += [f"## {year}", ""]
        et_al = " et al." if p["n_authors"] > 1 else ""
        lines.append(
            f"- **{p['title']}** — {p['first_author']}{et_al}, {p['year']}. "
            f"[arXiv:{p['arxiv_id']}](https://arxiv.org/abs/{p['arxiv_id']}). "
            f"*{p['takeaway']}*"
        )
    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    OUT_MD.write_text("\n".join(lines) + "\n")
    print(f"\nunique papers: {len(records)}")
    print(f"wrote {OUT_JSON}")
    print(f"wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
