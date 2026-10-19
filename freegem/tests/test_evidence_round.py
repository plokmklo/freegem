"""The evidence round (2.12.0): WARC capture, GFM tables, politeness
receipts, the map command, the chunk surface, and fetch hardening.

Contract style like test_hardening_round: every feature is pinned by the
observable bytes/records it produces, so a refactor that changes the
contract fails here first.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

from freegem.cli import main as cli_main
from freegem.config import BridgeConfig
from freegem.crawler.extract import HtmlDom, html_to_markdown
from freegem.crawler.fetch import FetchOutcome, _retry_after, read_body_stream, supported_encodings
from freegem.crawler.frontier import Frontier
from freegem.crawler.receipts import build_receipts, render_markdown
from freegem.crawler.urls import Scope
from freegem.crawler.warc import WARCError, WarcWriter, iter_records, summary, verify
from freegem.tables import extract_tables
from freegem.web_scraper import ScrapeError, clear_robots_cache

# ── WARC writer / reader ─────────────────────────────────────────────────────


class _CounterUuids:
    """Deterministic urn:uuid generator: 00000000-...-0001, 0002, ..."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> Any:
        self.n += 1
        value = f"00000000-0000-0000-0000-{self.n:012d}"
        return type("_FakeUuid", (), {"__str__": lambda _self, _v=value: _v})()


class WarcWriterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "evidence.warc")
        self.uuids = _CounterUuids()

        def _fixed_clock() -> float:
            return 1_700_000_000.0

        self.clock = _fixed_clock  # fixed WARC-Date
        self.writer = WarcWriter(
            self.path,
            software="freegem/test",
            robots_stance="never consulted (test)",
            uuid_gen=self.uuids,
            clock=self.clock,
        )

    def test_roundtrip_records_types_and_targets(self) -> None:
        self.writer.warcinfo({"freegem-delay-ms": "0"})
        request_id = self.writer.request(
            "http://site.test/", headers={"User-Agent": "freegem/test"}
        )
        self.writer.response(
            "http://site.test/",
            status=200,
            headers={"Content-Type": "text/html"},
            body=b"<p>hi</p>",
            request_id=request_id,
        )
        self.writer.metadata("http://site.test/", {"freegem-wall-class": "bot_check"})
        self.writer.close()

        records = list(iter_records(self.path))
        self.assertEqual([r.type for r in records], ["warcinfo", "request", "response", "metadata"])
        self.assertEqual(records[2].target, "http://site.test/")
        # The warcinfo record id is referenced by later records.
        info_id = records[0].field("WARC-Record-ID")
        self.assertEqual(records[2].field("WARC-Warcinfo-ID"), info_id)
        # Deterministic ids made it through.
        self.assertIn("00000000-0000-0000-0000-000000000001", info_id)

    def test_verify_passes_on_intact_file(self) -> None:
        self.writer.warcinfo()
        self.writer.request("http://site.test/a")
        self.writer.response(
            "http://site.test/a", status=200, headers={"X-Test": "1"}, body=b"body-bytes"
        )
        self.writer.close()
        report = verify(self.path)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["types"], {"warcinfo": 1, "request": 1, "response": 1})

    def test_tampered_body_is_detected(self) -> None:
        self.writer.response("http://site.test/", status=200, body=b"original-bytes")
        self.writer.close()
        raw = bytearray(Path(self.path).read_bytes())
        index = raw.find(b"original-bytes")
        raw[index] = ord("X")
        Path(self.path).write_bytes(bytes(raw))
        report = verify(self.path)
        self.assertFalse(report["ok"])
        self.assertTrue(any("digest" in p for p in report["problems"]))

    def test_truncated_tail_raises(self) -> None:
        self.writer.response("http://site.test/", status=200, body=b"x" * 64)
        self.writer.close()
        raw = Path(self.path).read_bytes()
        Path(self.path).write_bytes(raw[:-10])
        with self.assertRaises(WARCError):
            list(iter_records(self.path))

    def test_response_payload_is_http_exchange(self) -> None:
        self.writer.response(
            "http://site.test/x", status=403, headers={"Server": "t"}, body=b"denied"
        )
        self.writer.close()
        record = next(iter(iter_records(self.path)))
        self.assertTrue(record.payload.startswith(b"HTTP/1.1 403 Forbidden\r\n"))
        self.assertTrue(record.payload.endswith(b"\r\n\r\ndenied"))
        self.assertEqual(record.field("WARC-Truncated"), "")

    def test_truncation_flag_for_capped_bodies(self) -> None:
        self.writer.response(
            "http://site.test/big", status=200, body=b"partial", truncated="length"
        )
        self.writer.close()
        record = next(iter(iter_records(self.path)))
        self.assertEqual(record.field("WARC-Truncated"), "length")

    def test_body_path_streams_spilled_bodies(self) -> None:
        spill = os.path.join(self.tmp.name, "body.bin")
        Path(spill).write_bytes(b"spilled-payload")
        self.writer.response("http://site.test/", status=200, body_path=spill)
        self.writer.close()
        record = next(iter(iter_records(self.path)))
        self.assertTrue(record.payload.endswith(b"spilled-payload"))

    def test_summary_counts_walls(self) -> None:
        self.writer.warcinfo()
        self.writer.response("http://site.test/wall", status=403, body=b"no")
        self.writer.metadata("http://site.test/wall", {"freegem-wall-class": "bot_check"})
        self.writer.close()
        data = summary(self.path)
        self.assertEqual(data["types"], {"warcinfo": 1, "response": 1, "metadata": 1})
        self.assertEqual(data["wall_classes"], {"bot_check": 1})
        self.assertEqual(data["distinct_targets"], 1)  # warcinfo has no target

    def test_field_injection_is_refused(self) -> None:
        with self.assertRaises(WARCError):
            self.writer.metadata("http://site.test/", {"evil": "value\r\nWARC-Type: response"})
        self.writer.close()


# ── GFM tables ───────────────────────────────────────────────────────────────


TABLE_HTML = """
<html><body>
<table>
  <caption>Plans</caption>
  <thead><tr><th>Plan</th><th>Price</th></tr></thead>
  <tbody>
    <tr><td>Free</td><td>0</td></tr>
    <tr><td>Pro | max</td><td>99</td></tr>
    <tr><td>Ragged</td></tr>
  </tbody>
</table>
</body></html>
"""


class TableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dom = HtmlDom()
        self.dom.feed(TABLE_HTML)
        self.tables = extract_tables(self.dom.root)

    def test_header_rows_and_caption(self) -> None:
        self.assertEqual(len(self.tables), 1)
        table = self.tables[0]
        self.assertEqual(table.header, ["Plan", "Price"])
        self.assertEqual(table.rows[0], ["Free", "0"])
        self.assertEqual(table.caption, "Plans")

    def test_gfm_render_escapes_pipes_and_pads(self) -> None:
        md = self.tables[0].to_markdown()
        lines = md.splitlines()
        self.assertEqual(lines[0], "*Plans*")
        self.assertTrue(lines[2].startswith("| Plan | Price |"))
        self.assertIn("Pro \\| max", md)
        ragged = next(ln for ln in lines if ln.startswith("| Ragged"))
        self.assertEqual(ragged.count("|"), lines[2].count("|"))

    def test_csv_render(self) -> None:
        csv_text = self.tables[0].to_csv()
        self.assertEqual(csv_text.splitlines()[0], "Plan,Price")
        self.assertIn("Free,0", csv_text)

    def test_spanned_cells_are_counted_not_expanded(self) -> None:
        dom = HtmlDom()
        dom.feed("<table><tr><th>a</th><th>b</th></tr><tr><td colspan='2'>wide</td></tr></table>")
        table = extract_tables(dom.root)[0]
        self.assertEqual(table.spanned_cells, 1)
        self.assertEqual(table.rows, [["wide"]])  # raw rows stay ragged...
        self.assertIn("| wide |", table.to_markdown())  # ...rendering pads

    def test_markdown_pipeline_renders_tables(self) -> None:
        md = html_to_markdown(TABLE_HTML)
        self.assertIn("| Plan | Price |", md)
        self.assertIn("| Free | 0 |", md)

    def test_script_inside_cell_is_ignored(self) -> None:
        dom = HtmlDom()
        dom.feed("<table><tr><td>a<script>bad()</script>b</td></tr></table>")
        table = extract_tables(dom.root)[0]
        self.assertEqual(table.rows, [["ab"]])


# ── politeness receipts ──────────────────────────────────────────────────────


class ReceiptsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state.db")
        self.frontier = Frontier(self.state)
        self.addCleanup(self.frontier.close)
        self.frontier.push("http://a.test/1", host="a.test", depth=0)
        self.frontier.push("http://a.test/2", host="a.test", depth=0)
        self.frontier.push("http://b.test/1", host="b.test", depth=0)
        self.frontier.mark_done("http://a.test/1", http_status=200)
        self.frontier.mark_done("http://a.test/2", http_status=304)
        self.frontier.mark_error("http://b.test/1", "HTTP 403", retry=False)

    def test_totals_and_wall_counts(self) -> None:
        report = build_receipts(self.state, user_agent="freegem/test", now=1_700_000_000.0)
        self.assertEqual(report["totals"]["urls"], 3)
        self.assertEqual(report["totals"]["done"], 2)
        self.assertEqual(report["totals"]["not_modified_304"], 1)
        self.assertEqual(report["totals"]["walls"], 1)
        self.assertEqual(report["hosts"]["a.test"]["http_2xx"], 1)

    def test_digest_is_deterministic(self) -> None:
        first = build_receipts(self.state, user_agent="u", now=1_700_000_000.0)
        second = build_receipts(self.state, user_agent="u", now=1_700_000_000.0)
        self.assertEqual(first["receipt_digest"], second["receipt_digest"])

    def test_delay_consistency_verdicts(self) -> None:
        lax = build_receipts(self.state, delay_ms=0, now=1_700_000_000.0)
        strict = build_receipts(self.state, delay_ms=60_000, now=1_700_000_000.0)
        self.assertTrue(lax["hosts"]["a.test"]["pacing"]["consistent"])
        self.assertFalse(strict["hosts"]["a.test"]["pacing"]["consistent"])

    def test_markdown_render_carries_host_and_digest(self) -> None:
        report = build_receipts(self.state, user_agent="freegem/test", now=1_700_000_000.0)
        text = render_markdown(report)
        self.assertIn("# Politeness receipt", text)
        self.assertIn("a.test", text)
        self.assertIn(report["receipt_digest"], text)


# ── fetch hardening ──────────────────────────────────────────────────────────


class RetryAfterTests(unittest.TestCase):
    NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)

    def test_seconds_form(self) -> None:
        self.assertEqual(_retry_after({"Retry-After": "30"}), 30.0)

    def test_http_date_future(self) -> None:
        value = _retry_after(
            {"Retry-After": "Tue, 20 Oct 2026 12:01:00 GMT"},
            now=self.NOW,
        )
        self.assertEqual(value, 60.0)

    def test_http_date_past_is_zero(self) -> None:
        value = _retry_after(
            {"Retry-After": "Tue, 20 Oct 2026 11:59:00 GMT"},
            now=self.NOW,
        )
        self.assertEqual(value, 0.0)

    def test_garbage_is_none(self) -> None:
        self.assertIsNone(_retry_after({"Retry-After": "soon-ish"}))
        self.assertIsNone(_retry_after({}))
        self.assertIsNone(_retry_after(None))


class _FakeBrotli:
    """Duck-typed brotlicffi: Decompressor().decompress must exist."""

    class Decompressor:
        def decompress(self, data: bytes) -> bytes:
            return data.replace(b"BR~", b"")


class EncodingTests(unittest.TestCase):
    def test_gzip_always_advertised(self) -> None:
        self.assertIn("gzip", supported_encodings())
        self.assertIn("deflate", supported_encodings())

    def test_optional_codecs_appear_only_when_importable(self) -> None:
        import freegem.crawler.fetch as fetch_module

        with (
            mock.patch.object(fetch_module, "_brotli", None),
            mock.patch.object(fetch_module, "_zstandard", None),
        ):
            self.assertNotIn("br", supported_encodings())
        with mock.patch.object(fetch_module, "_brotli", _FakeBrotli):
            self.assertIn("br", supported_encodings())

    def test_brotli_body_decodes_when_extra_present(self) -> None:
        import freegem.crawler.fetch as fetch_module

        class _Reader:
            headers: ClassVar[dict[str, str]] = {"Content-Encoding": "br"}

            def __init__(self) -> None:
                self._chunks = iter([b"BR~hello", b"BR~world", b""])

            def read(self, _n: int) -> bytes:
                return next(self._chunks)

        with mock.patch.object(fetch_module, "_brotli", _FakeBrotli):
            body, path, total = read_body_stream(_Reader(), max_bytes=1000, spill_bytes=1000)
        self.assertIsNone(path)
        self.assertEqual(body, b"helloworld")
        self.assertEqual(total, 10)

    def test_unknown_encoding_is_refused(self) -> None:
        class _Reader:
            headers: ClassVar[dict[str, str]] = {"Content-Encoding": "braille"}

            def read(self, _n: int) -> bytes:
                return b""

        with self.assertRaises(ScrapeError):
            read_body_stream(_Reader(), max_bytes=1000, spill_bytes=1000)

    def test_corrupt_brotli_is_an_honest_failure(self) -> None:
        import freegem.crawler.fetch as fetch_module

        class _BadBrotli:
            class Decompressor:
                def decompress(self, _data: bytes) -> bytes:
                    raise ValueError("bad magic")

        class _Reader:
            headers: ClassVar[dict[str, str]] = {"Content-Encoding": "br"}

            def read(self, _n: int) -> bytes:
                return b"\xff\xff"

        with mock.patch.object(fetch_module, "_brotli", _BadBrotli):
            with self.assertRaises(ScrapeError):
                read_body_stream(_Reader(), max_bytes=1000, spill_bytes=1000)


# ── CLI surfaces ─────────────────────────────────────────────────────────────


def _run_cli(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = cli_main(argv)
    return code, buffer.getvalue()


class ChunkCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.page = Path(self.tmp.name) / "page.html"
        self.page.write_text(
            "<html><head><title>T</title></head><body><p>" + ("word " * 400) + "</p></body></html>"
        )

    def test_jsonl_chunks_carry_offsets(self) -> None:
        code, out = _run_cli(["scrape", str(self.page), "--chunks", "400", "--format", "jsonl"])
        self.assertEqual(code, 0, out)
        records = [json.loads(line) for line in out.strip().splitlines()]
        self.assertGreater(len(records), 1)
        self.assertEqual(records[0]["type"], "chunk")
        self.assertEqual(records[0]["index"], 0)
        self.assertEqual(records[0]["total"], len(records))
        self.assertTrue(records[0]["text"])
        self.assertLessEqual(records[0]["end_char"] - records[0]["start_char"], 400 + 200)

    def test_text_chunks_have_section_headers(self) -> None:
        code, out = _run_cli(["scrape", str(self.page), "--chunks", "300"])
        self.assertEqual(code, 0, out)
        self.assertIn("--- chunk 1/", out)

    def test_chunks_reject_multiple_urls(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli_main(["scrape", str(self.page), str(self.page), "--chunks", "300"])
        self.assertEqual(code, 2)
        self.assertIn("single URL", err.getvalue())


MAP_HTML = """
<html><head><title>Hub</title></head><body>
<a href="/docs/guide.html">Guide</a>
<a href="/blog/post-1.html">Post</a>
<a href="https://cdn.example/asset.js">Asset</a>
<a href="/docs/guide.html#section">Dup</a>
<script>fetch("/api/v1/items")</script>
</body></html>
"""


class MapCliTests(unittest.TestCase):
    def _patched_fetch(self) -> FetchOutcome:
        return FetchOutcome(
            url="https://hub.test/",
            kind="ok",
            status=200,
            body=MAP_HTML.encode(),
            content_type="text/html; charset=utf-8",
        )

    def test_map_scores_and_dedupes(self) -> None:
        import freegem.crawler.fetch as fetch_module

        with mock.patch.object(fetch_module, "crawl_fetch", return_value=self._patched_fetch()):
            code, out = _run_cli(["map", "https://hub.test/", "--format", "jsonl"])
        self.assertEqual(code, 0, out)
        entries = [json.loads(line) for line in out.strip().splitlines()]
        urls = [e["url"] for e in entries]
        self.assertIn("https://hub.test/docs/guide.html", urls)
        self.assertIn("https://hub.test/api/v1/items", urls)
        self.assertNotIn("https://hub.test/", urls)  # the seed itself is not a target
        self.assertEqual(len(urls), len(set(urls)))  # fragment dup collapsed
        guide = next(e for e in entries if e["url"].endswith("guide.html"))
        # Href attributes are lifted by BOTH the link walk and the endpoint
        # scanner (it reads href/src/action too) -- two sources, one target.
        self.assertEqual(guide["sources"], ["js_endpoint", "link"])
        api = next(e for e in entries if e["url"].endswith("/api/v1/items"))
        self.assertEqual(api["sources"], ["js_endpoint"])
        self.assertGreater(guide["score"], api["score"])  # docs beat bare api paths

    def test_map_text_format_scores_first(self) -> None:
        import freegem.crawler.fetch as fetch_module

        with mock.patch.object(fetch_module, "crawl_fetch", return_value=self._patched_fetch()):
            code, out = _run_cli(["map", "https://hub.test/"])
        self.assertEqual(code, 0, out)
        lines = out.strip().splitlines()
        self.assertRegex(lines[0], r"^\s*\d+\.\d+  https://")

    def test_map_fetch_failure_is_an_error(self) -> None:
        import freegem.crawler.fetch as fetch_module

        failed = FetchOutcome(url="https://hub.test/", kind="timeout", error="timed out")
        with mock.patch.object(fetch_module, "crawl_fetch", return_value=failed):
            code, _out = _run_cli(["map", "https://hub.test/"])
        self.assertEqual(code, 1)


# ── WARC + receipts end to end through the crawler ───────────────────────────

_REAL_GETADDRINFO = __import__("socket").getaddrinfo
_REAL_CREATE_CONNECTION = __import__("socket").create_connection
_FAKE_HOST = "evidence.test"
_CHECKED_IP = "93.184.216.34"

_EVIDENCE_INDEX = (
    "<html><head><title>Evidence</title></head><body>"
    '<a href="/docs/a.html">A</a>'
    '<a href="/wall">Wall</a>'
    "</body></html>"
)
_EVIDENCE_A = "<html><head><title>A</title></head><body><p>aaa</p></body></html>"


def _fake_resolver(host: str, *args: object, **kwargs: object) -> list[tuple]:
    if host == _FAKE_HOST:
        return [(2, 1, 6, "", (_CHECKED_IP, 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)


def _fake_create_connection(address: tuple, *args: object, **kwargs: object) -> Any:
    assert address[0] == _CHECKED_IP, f"dial target {address[0]!r} is not the checked IP"
    return _REAL_CREATE_CONNECTION(("127.0.0.1", address[1]), *args, **kwargs)


class _EvidenceSite(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            body = _EVIDENCE_INDEX.encode()
        elif path == "/docs/a.html":
            body = _EVIDENCE_A.encode()
        elif path == "/wall":
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"denied!!!")
            return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class EvidenceEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_robots_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        resolver = mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_resolver)
        resolver.start()
        self.addCleanup(resolver.stop)
        dial = mock.patch(
            "freegem.ssrf.socket.create_connection", side_effect=_fake_create_connection
        )
        dial.start()
        self.addCleanup(dial.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _EvidenceSite)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading = __import__("threading")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://{_FAKE_HOST}:{self.server.server_port}"
        self.cfg = BridgeConfig(guard=True, log_requests=False)

    def _crawl(self, **kwargs: Any) -> Any:
        from freegem.crawler import CrawlSpec, ResourceLimits, run

        self.state = os.path.join(self.dir, "state.db")
        self.warc_path = os.path.join(self.dir, "evidence.warc")
        spec = CrawlSpec(
            seeds=(f"{self.base}/",),
            scope=Scope(max_depth=1, max_pages=10, deny_paths=("/404",)),
            limits=ResourceLimits(timeout_sec=5, max_retries=0),
            delay_ms=0,
            concurrency=1,
            # Plain fetch: the escalation ladder's wayback rung would dial the
            # real web.archive.org for the 403 wall -- this test pins the
            # capture contract, not the ladder.
            resilient=False,
            state_path=self.state,
            warc_path=self.warc_path,
            **kwargs,
        )
        frontier = Frontier(self.state)
        try:
            return run(self.cfg, spec, frontier)
        finally:
            frontier.close()

    def test_crawl_writes_verifiable_warc_with_walls(self) -> None:
        counters = self._crawl()
        self.assertGreater(counters.pages_ok, 0)
        report = verify(self.warc_path)
        self.assertTrue(report["ok"], report["problems"])
        data = summary(self.warc_path)
        self.assertIn("request", data["types"])
        self.assertIn("response", data["types"])
        self.assertIn("warcinfo", data["types"])
        self.assertEqual(data["wall_classes"], {"bot_check": 1})
        # The warcinfo record carries the honest robots stance.
        info = next(r for r in iter_records(self.warc_path) if r.type == "warcinfo")
        self.assertIn("never fetched or consulted", info.payload.decode())

    def test_crawl_report_cli_from_state(self) -> None:
        self._crawl()
        code, out = _run_cli(
            ["crawl", "report", "--state", self.state, "--format", "json", "--delay-ms", "0"]
        )
        self.assertEqual(code, 0, out)
        report = json.loads(out)
        self.assertEqual(report["kind"], "freegem-politeness-receipt")
        self.assertIn(_FAKE_HOST, report["hosts"])
        self.assertGreaterEqual(report["totals"]["walls"], 1)
        code, out = _run_cli(["crawl", "report", "--state", self.state])
        self.assertEqual(code, 0)
        self.assertIn("# Politeness receipt", out)

    def test_crawl_run_warc_flag_plumbs_through_cli(self) -> None:
        out_path = os.path.join(self.dir, "cli.warc")
        state_path = os.path.join(self.dir, "cli-state.db")
        code, out = _run_cli(
            [
                "crawl",
                "run",
                "--seeds",
                f"{self.base}/",
                "--state",
                state_path,
                "--warc",
                out_path,
                "--max-pages",
                "3",
                "--max-depth",
                "0",
                "--delay-ms",
                "0",
                "--timeout",
                "5",
                "--max-retries",
                "0",
            ]
        )
        self.assertEqual(code, 0, out)
        report = verify(out_path)
        self.assertTrue(report["ok"], report["problems"])
