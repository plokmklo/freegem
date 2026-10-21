"""Focused test: 200 URLs through the new parallel_scraper with adaptive concurrency.

Tests the improved DNS timeout fix, 503 retry strategy, and adaptive
concurrency with AI summarization. Collects ALL errors for analysis.

Run:  .venv/bin/python scrape_200_test.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from freegem.config import BridgeConfig
from freegem.parallel_scraper import scrape_parallel, botwall_ratio

CWD = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(CWD, "scrape_200_results.json")

# 200 URLs: Wikipedia + docs + real-world sites + botwalled + edge cases.
WIKI_PAGES = [
    "Python_(programming_language)",
    "Machine_learning",
    "JavaScript",
    "C_(programming_language)",
    "Linux",
    "HTTP",
    "JSON",
    "REST",
    "Docker_(software)",
    "Kubernetes",
    "Git",
    "Cloud_computing",
    "Artificial_intelligence",
    "Data_scraping",
    "SQL",
    "Flask_(web_framework)",
    "Django",
    "React_(software)",
    "Node.js",
    "Rust_(programming_language)",
]

PYTHON_DOCS = [
    "https://docs.python.org/3/library/urllib.html",
    "https://docs.python.org/3/library/http.client.html",
    "https://docs.python.org/3/library/threading.html",
    "https://docs.python.org/3/library/concurrent.futures.html",
    "https://docs.python.org/3/library/ssl.html",
    "https://docs.python.org/3/library/json.html",
    "https://docs.python.org/3/library/logging.html",
    "https://docs.python.org/3/library/typing.html",
    "https://docs.python.org/3/tutorial/index.html",
    "https://docs.python.org/3/library/dataclasses.html",
]

# Real-world sites of various kinds (some botwalled, some not).
REAL_SITES = [
    "https://example.com",
    "https://example.org",
    "https://www.python.org",
    "https://en.wikipedia.org/wiki/Web_scraping",
    "https://en.wikipedia.org/wiki/Bot_(computing)",
    "https://www.iana.org/domains/example",
    "https://httpbin.org/html",
    "https://httpbin.org/xml",
    "https://httpbin.org/headers",
    "https://httpbin.org/ip",
    "https://httpbin.org/user-agent",
    "https://httpbin.org/cookies",
    "https://httpbin.org/get",
    "https://httpbin.org/uuid",
    "https://httpbin.org/anything",
    # Botwalled / rate-limited (these will likely fail and exercise the ladder)
    "https://www.reddit.com",
    "https://www.medium.com",
    "https://www.quora.com",
    "https://www.twitch.tv",
    "https://www.imgur.com",
]

# Programming language docs (diverse backends).
LANG_DOCS = [
    "https://docs.python.org/3/",
    "https://react.dev/",
    "https://vuejs.org/",
    "https://angular.io/docs",
    "https://nextjs.org/docs",
    "https://nuxt.com/docs",
    "https://svelte.dev/docs",
    "https://docs.rs/",
    "https://doc.rust-lang.org/book/",
    "https://doc.rust-lang.org/rust-by-example/",
    "https://go.dev/doc/",
    "https://go.dev/blog/",
    "https://docs.oracle.com/en/java/",
    "https://docs.oracle.com/javase/8/docs/",
    "https://learn.microsoft.com/en-us/dotnet/csharp/",
    "https://learn.microsoft.com/en-us/java/",
    "https://spring.io/guides",
    "https://django.org/documentation/",
    "https://flask.palletsprojects.com/",
    "https://fastapi.tiangolo.com/",
]

# HTTP status edge cases.
EDGE_CASES = [
    "https://httpbin.org/status/200",
    "https://httpbin.org/status/301",
    "https://httpbin.org/status/404",
    "https://httpbin.org/status/500",
    "https://httpbin.org/status/503",
    "https://httpbin.org/status/429",
    "https://httpbin.org/redirect/1",
    "https://httpbin.org/redirect/2",
    "https://httpbin.org/redirect/3",
    "https://httpbin.org/delay/1",
    "https://httpbin.org/delay/2",
    "https://httpbin.org/delay/3",
    "https://httpbin.org/image/png",
    "https://httpbin.org/image/jpeg",
    "https://httpbin.org/image/svg",
    "https://httpbin.org/pdf",
    "https://httpbin.org/robots.txt",
    "https://httpbin.org/cache",
    "https://httpbin.org/etag",
    "https://httpbin.org/vary",
]


def build_200_urls() -> list[str]:
    """Build a list of 200 URLs for testing."""
    urls: list[str] = []
    # Wikipedia pages (20) + variants
    for page in WIKI_PAGES:
        urls.append(f"https://en.wikipedia.org/wiki/{page}")
    # Python docs (10)
    urls.extend(PYTHON_DOCS)
    # Real sites (20)
    urls.extend(REAL_SITES)
    # Language docs (20)
    urls.extend(LANG_DOCS)
    # Edge cases (20)
    urls.extend(EDGE_CASES)

    # Fill to 200 with varied endpoints
    extras = [
        "https://httpbin.org/base64/SFRUUEJJT24gRWFzeQ==",
        "https://httpbin.org/gzip",
        "https://httpbin.org/deflate",
        "https://httpbin.org/bearer",
        "https://httpbin.org/drip?duration=1&numbytes=5",
        "https://httpbin.org/hidden-basicauth/user/passwd",
        "https://httpbin.org/response-headers?Hello=World",
        "https://httpbin.org/redirect-to?url=https://example.com",
        "https://httpbin.org/relative-redirect/1",
        "https://httpbin.org/absolute-redirect/1",
        "https://httpbin.org/cookies/set?test=value",
        "https://httpbin.org/basic-auth/user/passwd",
        "https://httpbin.org/anything/test",
        "https://httpbin.org/html",
        "https://httpbin.org/links/10/0",
        "https://httpbin.org/links/10/1",
        "https://httpbin.org/stream/1",
        "https://httpbin.org/stream/3",
        "https://httpbin.org/stream-bytes/100",
        "https://httpbin.org/stream-bytes/1000",
        "https://en.wikipedia.org/wiki/API",
        "https://en.wikipedia.org/wiki/REST_API",
        "https://en.wikipedia.org/wiki/Web_crawler",
        "https://en.wikipedia.org/wiki/CAPTCHA",
        "https://en.wikipedia.org/wiki/Rate_limiting",
        "https://en.wikipedia.org/wiki/HTTP_403",
        "https://en.wikipedia.org/wiki/HTTP_429",
        "https://en.wikipedia.org/wiki/robots.txt",
        "https://en.wikipedia.org/wiki/User_agent",
        "https://en.wikipedia.org/wiki/Cloudflare",
    ] * 5  # repeat to fill up to 200
    urls.extend(extras)
    urls = urls[:200]
    return urls


def main() -> None:
    urls = build_200_urls()
    assert len(urls) == 200, f"expected 200 URLs, got {len(urls)}"

    cfg = BridgeConfig()
    print(f"Testing {len(urls)} URLs with adaptive concurrency (10 workers, smart=True)...")
    print(f"Testing 503 retry strategy + DNS timeout fix + bot-wall detection...")

    start = time.monotonic()
    results, digest = scrape_parallel(
        cfg,
        urls,
        concurrency=10,
        max_chars=10_000,
        smart=True,
        summarize=False,  # skip AI summarization for raw performance test
    )
    elapsed = time.monotonic() - start

    # Collect all errors for analysis
    errors: list[dict] = []
    for r in results:
        if not r.ok:
            errors.append({
                "url": r.url,
                "error": r.error[:200],
                "via": r.via,
            })

    report = {
        "total_urls": len(urls),
        "ok": digest.ok,
        "failed": digest.failed,
        "botwall_count": digest.botwall,
        "botwall_ratio": botwall_ratio(results),
        "avg_elapsed_ms": round(digest.avg_elapsed_ms, 1),
        "peak_concurrency": digest.peak_concurrency,
        "elapsed_sec": round(elapsed, 2),
        "via_counts": digest.via_counts,
        "errors": errors,
        "sample_results": [
            {
                "url": r.url[:80],
                "ok": r.ok,
                "chars": r.chars,
                "via": r.via,
                "truncated": r.truncated,
                "elapsed_ms": r.elapsed_ms,
            }
            for r in results[:30]
        ],
    }

    Path(OUT).write_text(json.dumps(report, indent=2, ensure_ascii=False))

    print()
    print("=" * 70)
    print(f"RESULTS: {digest.ok}/{len(urls)} OK, {digest.failed} failed, {digest.botwall} botwall")
    print(f"Bot-wall ratio: {botwall_ratio(results):.0%}")
    print(f"Peak concurrency: {digest.peak_concurrency}")
    print(f"Avg elapsed: {digest.avg_elapsed_ms:.0f}ms per URL")
    print(f"Total elapsed: {elapsed:.1f}s")
    print(f"Via counts: {digest.via_counts}")
    print(f"Results saved to {OUT}")
    print("=" * 70)


if __name__ == "__main__":
    main()
