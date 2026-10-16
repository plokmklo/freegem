"""Campaign + --distill on an oversized page must complete, not deadlock (issue #41).

The campaign lock is non-reentrant and ``work()`` held it while calling
``_distill_if_large``, which re-acquired the SAME lock for the distilled-row
write — a guaranteed self-deadlock on the first page at/above the distill
threshold. Small pages never reached that line, which is why every existing
campaign-shaped test stayed green.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from freegem.config import BridgeConfig
from freegem.crawler.campaign import DISTILL_THRESHOLD_BYTES, run_campaign

_REAL_GETADDRINFO = socket.getaddrinfo
_REAL_CREATE_CONNECTION = socket.create_connection
_FAKE_HOST = "campaign.test"
_CHECKED_IP = "93.184.216.34"

BIG = b"x" * DISTILL_THRESHOLD_BYTES  # first size that reaches the distill path


def _fake_resolver(host: str, *args: object, **kwargs: object) -> list[tuple]:
    if host == _FAKE_HOST:
        return [(2, 1, 6, "", (_CHECKED_IP, 0))]
    return _REAL_GETADDRINFO(host, *args, **kwargs)  # type: ignore[arg-type]


def _fake_create_connection(address: tuple, *args: object, **kwargs: object) -> socket.socket:
    assert address[0] == _CHECKED_IP
    return _REAL_CREATE_CONNECTION(("127.0.0.1", address[1]), *args)  # type: ignore[arg-type]


class _BigPageHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/robots.txt":
            body = b"User-agent: *\nAllow: /\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/big":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(BIG)))
            self.end_headers()
            self.wfile.write(BIG)
            return
        self.send_error(404)


class CampaignDistillDeadlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.resolver = mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_resolver)
        self.resolver.start()
        self.addCleanup(self.resolver.stop)
        self.dial = mock.patch(
            "freegem.ssrf.socket.create_connection", side_effect=_fake_create_connection
        )
        self.dial.start()
        self.addCleanup(self.dial.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _BigPageHandler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://{_FAKE_HOST}:{self.server.server_port}"

    def test_oversized_page_with_distill_completes_without_deadlock(self) -> None:
        state = os.path.join(self.tmp.name, "campaign.db")
        box: dict[str, object] = {}

        def run() -> None:
            try:
                box["result"] = run_campaign(
                    BridgeConfig(log_requests=False),
                    [{"url": f"{self.base}/big", "category": "t"}],
                    state_path=state,
                    concurrency=2,
                    host_delay_sec=0.0,
                    timeout_sec=30.0,
                    distill=True,
                )
            except BaseException as exc:  # noqa: BLE001 - surfaced via box
                box["error"] = exc

        # Distill is mocked: the test pins the LOCKING contract, not Gemini or
        # the extractive summarizer (which would also make the test slow).
        with mock.patch(
            "freegem.crawler.campaign.distill_text",
            return_value=("a distilled summary", "extractive"),
        ):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            thread.join(timeout=40)
        self.assertFalse(thread.is_alive(), "campaign deadlocked on the distill path (issue #41)")
        self.assertNotIn("error", box, f"campaign raised: {box.get('error')}")
        self.assertIn("result", box)
        db = sqlite3.connect(state)
        try:
            distilled = db.execute("SELECT COUNT(*) FROM distilled").fetchone()
        finally:
            db.close()
        self.assertIsNotNone(distilled, "campaign state db missing after run")
        self.assertEqual(distilled[0], 1)


if __name__ == "__main__":
    unittest.main()
