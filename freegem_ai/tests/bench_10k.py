"""Benchmark harness: 10,000-URL scrape through freegem_ai.scrape.

Generates 10,000 unique loopback URLs, fetches them with
``freegem_ai.scrape(..., no_limit=True, concurrency=100)`` against a
throwaway ThreadingHTTPServer behind the same DNS/dial fakes test_scraper
uses, and reports timing/throughput/error rate.

Run with:

    .venv/Scripts/python -m freegem_ai.tests.bench_10k
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from freegem.config import BridgeConfig
from freegem.tests.test_scraper import (
    _FAKE_HOST,
    _fake_create_connection,
    _fake_resolver,
)
from freegem.web_scraper import clear_robots_cache

HOST = _FAKE_HOST
BODY = "x" * 2000  # ~2 KB per page body
NUM_URLS = 10_000
CONCURRENCY = 100


class _BenchSite(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        body = BODY.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_bench() -> None:
    clear_robots_cache()
    resolver = mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_resolver)
    resolver.start()
    dial = mock.patch(
        "freegem.ssrf.socket.create_connection",
        side_effect=_fake_create_connection,
    )
    dial.start()

    server = ThreadingHTTPServer(("127.0.0.1", 0), _BenchSite)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()

    base = f"http://{HOST}:{port}"
    urls = [f"{base}/page{i}" for i in range(NUM_URLS)]

    cfg = BridgeConfig(guard=True, log_requests=False)

    print(f"Benchmarking {NUM_URLS} URLs at concurrency={CONCURRENCY} (no_limit=True)...")

    # Patch generate so the AI pass is a no-op (we are measuring fetch throughput).
    fake_generate = mock.Mock(return_value="bench-clean")
    with mock.patch("freegem_ai.core.generate", fake_generate):
        t0 = time.monotonic()
        from freegem_ai import scrape

        results = scrape(
            cfg,
            urls,
            ai_clean=True,
            ai_summarize=True,
            smart=False,
            no_limit=True,
            concurrency=CONCURRENCY,
        )
        elapsed = time.monotonic() - t0

    server.shutdown()
    server.server_close()
    dial.stop()
    resolver.stop()

    ok = sum(1 for r in results if r.ok)
    failed = NUM_URLS - ok
    rps = NUM_URLS / elapsed if elapsed > 0 else float("inf")

    print("\nResults:")
    print(f"  Total URLs:      {NUM_URLS}")
    print(f"  Successful:      {ok}")
    print(f"  Failed:          {failed}")
    print(f"  Elapsed:         {elapsed:.2f}s")
    print(f"  Throughput:      {rps:.1f} URLs/s")
    print(f"  Avg latency:     {elapsed / NUM_URLS * 1000:.2f} ms/URL")
    print(f"  AI passes:       {fake_generate.call_count} generate() calls")
    if elapsed > 0:
        avg_per = elapsed / (NUM_URLS / CONCURRENCY) * 1000
        print(f"  Per-batch latency (avg): {avg_per:.2f} ms")


if __name__ == "__main__":
    run_bench()
