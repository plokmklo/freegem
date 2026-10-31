#!/usr/bin/env python3
"""Build the target list for the live 1000-site scrape experiment.

Downloads a public top-sites ranking, takes the top 1000 domains, and
writes urls.json ({"source", "count", "urls"}). The Majestic Million CSV
is the primary source; the Tranco daily list is the fallback. The run
records which source it used so results stay attributable.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from typing import Callable

TARGET = 1000
UA = "Mozilla/5.0 (compatible; freegem-live-bench/1.0)"


def _fetch(url: str, timeout: int = 90) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def try_majestic() -> tuple[list[str], str]:
    """Top-1000 domains from the Majestic Million CSV."""
    text = _fetch("https://downloads.majestic.com/majestic_million.csv").decode(
        "utf-8", "replace"
    )
    domains = []
    for row in csv.DictReader(io.StringIO(text)):
        domain = (row.get("Domain") or "").strip().lower()
        if domain:
            domains.append(domain)
    if len(domains) < TARGET:
        raise ValueError(f"majestic csv had only {len(domains)} domains")
    return [f"https://{d}" for d in domains[:TARGET]], "majestic_million.csv top 1000"


def try_tranco() -> tuple[list[str], str]:
    """Top-1000 domains from today's Tranco list."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    meta = json.loads(_fetch(f"https://tranco-list.eu/api/lists/date?date={today}"))
    ids = meta.get("list_id") or []
    if isinstance(ids, str):
        ids = [ids]
    if not ids:
        raise ValueError("tranco api returned no list id")
    list_id = str(ids[0])
    endpoints = (
        f"https://tranco-list.eu/download/{list_id}",
        f"https://tranco-list.eu/api/lists/{list_id}/download",
    )
    raw = None
    for endpoint in endpoints:
        try:
            raw = _fetch(endpoint, timeout=120)
            break
        except Exception:
            continue
    if raw is None:
        raise ValueError("tranco download failed on both endpoints")
    archive = zipfile.ZipFile(io.BytesIO(raw))
    text = archive.read(archive.namelist()[0]).decode("utf-8", "replace")
    domains = []
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and parts[1].strip():
            domains.append(parts[1].strip().lower())
    if len(domains) < TARGET:
        raise ValueError(f"tranco list had only {len(domains)} domains")
    return [f"https://{d}" for d in domains[:TARGET]], f"tranco list {list_id} top 1000"


def main() -> int:
    parser = argparse.ArgumentParser(description="build the live-1000 target list")
    parser.add_argument("--out", default="urls.json")
    args = parser.parse_args()

    sources: list[tuple[str, Callable[[], tuple[list[str], str]]]] = [
        ("majestic", try_majestic),
        ("tranco", try_tranco),
    ]
    errors = []
    for name, builder in sources:
        try:
            urls, source = builder()
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            print(f"source {name} failed: {exc}", file=sys.stderr)
            continue
        seen = set()
        unique = []
        for url in urls:
            if url not in seen:
                seen.add(url)
                unique.append(url)
        payload = {"source": source, "count": min(TARGET, len(unique)), "urls": unique[:TARGET]}
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        print(f"built {payload['count']} urls from {source}")
        return 0
    print("all list sources failed:\n" + "\n".join(errors), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
