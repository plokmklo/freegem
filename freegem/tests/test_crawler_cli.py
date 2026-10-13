"""The `freegem crawl` CLI: plan, run, status, resume semantics."""

from __future__ import annotations

import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from unittest import mock

from freegem.cli import main
from freegem.web_scraper import clear_robots_cache

_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CREATE_CONNECTION = socket.create_connection
_FAKE_HOST = "site.test"
_CHECKED_IP = "93.184.216.34"

PAGE = "<html><head><title>P</title></head><body><p>cli page</p></body></html>"


def _fake_resolver(host: str, *args: object, **kwargs: object) -> list[tuple]:
    if host == _FAKE_HOST:
        return [(2, 1, 6, "", (_CHECKED_IP, 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)  # type: ignore[arg-type]


def _fake_create_connection(address: tuple, *args: object, **kwargs: object) -> socket.socket:
    assert address[0] == _CHECKED_IP
    return _REAL_CREATE_CONNECTION(("127.0.0.1", address[1]), *args, **kwargs)  # type: ignore[arg-type]


class _CliSite(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits: ClassVar[dict[str, int]] = {}

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        type(self).hits[path] = type(self).hits.get(path, 0) + 1
        if path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/one", "/two"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


class CrawlCliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        clear_robots_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        _CliSite.hits = {}
        self.resolver = mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_resolver)
        self.resolver.start()
        self.addCleanup(self.resolver.stop)
        self.dial = mock.patch(
            "freegem.ssrf.socket.create_connection", side_effect=_fake_create_connection
        )
        self.dial.start()
        self.addCleanup(self.dial.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CliSite)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://{_FAKE_HOST}:{self.server.server_port}"

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        code: int = -1
        with mock.patch.dict(os.environ, {"FREEGEM_CONFIG": ""}):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                code = main(argv)
        return code, out.getvalue()

    def test_plan_prints_report_without_network(self) -> None:
        code, out = self.run_cli(
            ["crawl", "plan", "--seeds", f"{self.base}/one", "--max-pages", "3", "--max-depth", "0"]
        )
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(len(report["seeds"]), 1)
        self.assertEqual(report["budget_pages"], 3)
        self.assertEqual(_CliSite.hits, {})  # a plan never touches the network

    def test_plan_rejects_deny_domain_seed(self) -> None:
        code, out = self.run_cli(
            [
                "crawl",
                "plan",
                "--seeds",
                "https://other.example/",
                "--deny-domain",
                "other.example",
                "--max-depth",
                "0",
            ]
        )
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["seeds"], [])
        self.assertEqual(report["rejected"][0]["reason"], "deny_domain")

    def test_plan_allow_path_prefix_admits_matching_seed(self) -> None:
        """Scope prefixes through the FULL cli.main() path.

        This is the surface that looked broken during the 2026-10-13 external
        review — it was Git-Bash/MSYS mangling the flag VALUE, not the scope
        logic. Regression-lock the real behaviour: a seed whose path starts
        with the --allow-path prefix is admitted."""
        code, out = self.run_cli(
            [
                "crawl",
                "plan",
                "--seeds",
                "https://example.com/docs/page",
                "--allow-path",
                "/docs",
                "--max-depth",
                "0",
                "--max-pages",
                "5",
            ]
        )
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(len(report["seeds"]), 1)
        self.assertEqual(report["rejected"], [])

    def test_plan_allow_path_prefix_rejects_nonmatching_seed(self) -> None:
        code, out = self.run_cli(
            [
                "crawl",
                "plan",
                "--seeds",
                "https://example.com/blog/",
                "--allow-path",
                "/docs",
                "--max-depth",
                "0",
            ]
        )
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["seeds"], [])
        self.assertEqual(report["rejected"][0]["reason"], "out_of_scope_path")

    def test_run_fetches_and_status_reports(self) -> None:
        state = os.path.join(self.dir, "crawl.db")
        out_path = os.path.join(self.dir, "results.jsonl")
        code, out = self.run_cli(
            [
                "crawl",
                "run",
                "--seeds",
                f"{self.base}/",
                f"{self.base}/one",
                "--state",
                state,
                "--max-depth",
                "0",
                "--max-pages",
                "5",
                "--delay-ms",
                "0",
                "--out",
                out_path,
                "--format",
                "jsonl",
            ]
        )
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["pages_ok"], 2)
        with open(out_path, encoding="utf-8") as fh:
            records = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(records), 2)
        self.assertIn("url", records[0])
        # status over the same state file
        code, out = self.run_cli(["crawl", "status", "--state", state])
        self.assertEqual(code, 0)
        stats = json.loads(out)
        self.assertEqual(stats["by_status"].get("done"), 2)
        self.assertEqual(stats["queued"], 0)

    def test_rerun_over_same_state_is_a_noop(self) -> None:
        state = os.path.join(self.dir, "crawl.db")
        argv = [
            "crawl",
            "run",
            "--seeds",
            f"{self.base}/one",
            "--state",
            state,
            "--max-depth",
            "0",
            "--max-pages",
            "5",
            "--delay-ms",
            "0",
        ]
        code1, out1 = self.run_cli(argv)
        code2, out2 = self.run_cli(argv)
        self.assertEqual(code1, 0)
        self.assertEqual(code2, 0)
        self.assertEqual(json.loads(out1)["pages_ok"], 1)
        self.assertEqual(json.loads(out2)["pages_ok"], 0)  # everything already done
        self.assertEqual(_CliSite.hits.get("/one"), 1)

    def test_run_requires_state(self) -> None:
        code, _out = self.run_cli(
            ["crawl", "run", "--seeds", f"{self.base}/one", "--max-depth", "0"]
        )
        self.assertEqual(code, 2)

    def test_run_requires_seeds(self) -> None:
        code, _out = self.run_cli(["crawl", "run", "--state", os.path.join(self.dir, "x.db")])
        self.assertEqual(code, 2)

    def test_seed_file_txt_and_jsonl(self) -> None:
        txt = os.path.join(self.dir, "seeds.txt")
        with open(txt, "w", encoding="utf-8") as fh:
            fh.write(f"{self.base}/one\n# comment\n{self.base}/two\n")
        code, out = self.run_cli(
            ["crawl", "plan", "--seeds", txt, "--max-depth", "0", "--max-pages", "5"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["seeds"]), 2)
        jsonl = os.path.join(self.dir, "seeds.jsonl")
        with open(jsonl, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"url": f"{self.base}/one"}) + "\n")
            fh.write("not json\n")
        code, out = self.run_cli(
            ["crawl", "plan", "--seeds", jsonl, "--max-depth", "0", "--max-pages", "5"]
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out)["seeds"]), 1)

    def test_schema_bad_file_is_a_clean_error(self) -> None:
        schema = os.path.join(self.dir, "bad.json")
        with open(schema, "w", encoding="utf-8") as fh:
            fh.write('{"fields": [{"name": "x", "kind": "nonsense"}]}')
        state = os.path.join(self.dir, "crawl.db")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"FREEGEM_CONFIG": ""}):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = main(
                    [
                        "crawl",
                        "run",
                        "--seeds",
                        f"{self.base}/one",
                        "--state",
                        state,
                        "--max-depth",
                        "0",
                        "--schema",
                        schema,
                    ]
                )
        self.assertNotEqual(code, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
