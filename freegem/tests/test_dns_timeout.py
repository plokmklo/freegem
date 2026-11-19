"""Bounded DNS resolution (issue #88): a stalled resolver must not hang a
fetch thread.

``socket.getaddrinfo`` has no timeout of its own; before this fix a stalled
lookup held the campaign's worker threads FOREVER -- the fetch timeout never
engaged because the dial had not started. The resolve step now runs on a
throwaway daemon thread with a hard wall-clock cap and raises an
``SSRFRefused`` whose "cannot resolve ... stalled" marker the fetch taxonomy
already classifies as ``network`` (a dead domain, not an SSRF refusal).
"""

from __future__ import annotations

import socket
import time
import unittest
from unittest import mock

from freegem.config import BridgeConfig
from freegem.crawler.fetch import FetchOutcome, _is_dns_failure, crawl_fetch
from freegem.ssrf import DEFAULT_RESOLVE_TIMEOUT_SEC, SSRFRefused, pick_public_ips


class BoundedResolveTests(unittest.TestCase):
    def test_stalled_resolver_times_out_with_dns_marker(self) -> None:
        def stalled(host: str, *args: object, **kwargs: object) -> list[tuple]:
            time.sleep(30)  # simulate a resolver that never answers
            return []

        with mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=stalled):
            started = time.monotonic()
            with self.assertRaisesRegex(SSRFRefused, "cannot resolve.*stalled"):
                pick_public_ips("stalled.test", timeout_sec=0.2)
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0, "the stalled resolver was not time-bounded")

    def test_stall_marker_classifies_as_network_not_ssrf(self) -> None:
        message = "cannot resolve 'stalled.test': resolver stalled (>5.0s)"
        self.assertTrue(_is_dns_failure(message), "stalled marker must count as network")

    def test_real_resolution_still_works(self) -> None:
        infos = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with mock.patch("freegem.ssrf.socket.getaddrinfo", return_value=infos):
            ips = pick_public_ips("example.test")
        self.assertEqual(ips, ["93.184.216.34"])

    def test_resolution_errors_keep_their_taxonomy(self) -> None:
        with mock.patch(
            "freegem.ssrf.socket.getaddrinfo",
            side_effect=socket.gaierror(-2, "Name or service not known"),
        ):
            with self.assertRaisesRegex(SSRFRefused, "cannot resolve"):
                pick_public_ips("dead.test", timeout_sec=1.0)

    def test_default_timeout_is_bounded(self) -> None:
        self.assertLessEqual(DEFAULT_RESOLVE_TIMEOUT_SEC, 10.0)
        self.assertGreater(DEFAULT_RESOLVE_TIMEOUT_SEC, 0.0)

    def test_crawl_fetch_of_stalled_host_returns_network_outcome(self) -> None:
        """End to end: the campaign path (crawl_fetch) converts a stalled
        resolution into an honest ``network`` outcome instead of hanging."""

        def stalled(host: str, *args: object, **kwargs: object) -> list[tuple]:
            time.sleep(30)
            return []

        with mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=stalled):
            started = time.monotonic()
            outcome = crawl_fetch(
                BridgeConfig(guard=True, log_requests=False),
                "https://stalled.test/page",
            )
            elapsed = time.monotonic() - started
        self.assertEqual(outcome.kind, "network")
        self.assertIn("stalled", (outcome.error or ""))
        self.assertLess(elapsed, 30.0, "crawl_fetch hung on a stalled resolver")
        self.assertIsInstance(outcome, FetchOutcome)


if __name__ == "__main__":
    unittest.main()
