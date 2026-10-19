"""Listen-backlog burst tests: 64 clients, no connection resets.

The kernel accept queue sits IN FRONT of the application's admission queue:
``socketserver`` defaults ``request_queue_size`` to 5, so a burst of real
clients overflows it and the kernel resets connections before the server can
shed them politely with 503s. The admission queue (``request_queue_wait_sec``)
and the concurrency cap bound what executes, but nothing could absorb the
connect storm itself. ``ThreadedServer.request_queue_size = 128`` does.

These tests pin both layers:

* the backlog attribute itself (the static guarantee);
* a real 64-client burst against the concurrency cap of 8 -- every client
  must get a well-formed answer (200 from the mock, or the server's own 503
  shed) and never a connection-level error.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from typing import Any, ClassVar
from unittest import mock

from freegem.server import ThreadedServer
from freegem.tests.test_server import HermeticServerTestCase

BURST_CLIENTS = 64


class BurstBacklogTests(HermeticServerTestCase):
    """One server per test (cap 8 from the hermetic base); real sockets."""

    BODY: ClassVar[dict[str, Any]] = {
        "model": "gemini-3.6-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }

    def test_listen_backlog_is_128(self) -> None:
        """socketserver's silent default of 5 reset burst clients."""
        self.assertEqual(ThreadedServer.request_queue_size, 128)

    @mock.patch("freegem.server.generate")
    def test_burst_of_64_clients_gets_no_connection_resets(self, generate: Any) -> None:
        # A short held slot makes the oversubscription deterministic: with
        # the cap at 8 and each response held 200 ms, the other 56 burst
        # clients cannot ride a freed slot and MUST be shed with 503s.
        def slow_generate(*_args: Any, **_kwargs: Any) -> str:
            time.sleep(0.2)
            return "ok"

        generate.side_effect = slow_generate
        barrier = threading.Barrier(BURST_CLIENTS, timeout=30)
        statuses: list[int | None] = [None] * BURST_CLIENTS
        errors: list[str] = [""] * BURST_CLIENTS

        def one_client(i: int) -> None:
            try:
                barrier.wait()
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
                try:
                    conn.request(
                        "POST",
                        "/v1/chat/completions",
                        body=json.dumps(self.BODY),
                        headers={"Content-Type": "application/json"},
                    )
                    response = conn.getresponse()
                    response.read()
                    statuses[i] = response.status
                finally:
                    conn.close()
            except Exception as exc:  # connection-level failure is the bug
                errors[i] = f"{type(exc).__name__}: {exc}"

        threads = [threading.Thread(target=one_client, args=(i,)) for i in range(BURST_CLIENTS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
            self.assertFalse(t.is_alive(), "burst client hung")

        self.assertEqual(errors, [""] * BURST_CLIENTS, "connection-level failures")
        answered = [s for s in statuses if s is not None]
        self.assertEqual(len(answered), BURST_CLIENTS, "every client must be answered")
        for status in answered:
            self.assertIn(status, (200, 503), f"unexpected status {status}")
        # The cap is 8 and the burst is instantaneous, so a healthy server
        # sheds most of the burst with its own 503 -- that is the contract.
        self.assertGreaterEqual(statuses.count(200), 1)
        self.assertGreaterEqual(statuses.count(503), 1)
