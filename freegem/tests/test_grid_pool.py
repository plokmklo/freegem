"""Tests for the Grid capacity manager (freegem.grid_pool)."""

from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from freegem.grid_pool import (
    GridLease,
    GridNode,
    GridPool,
    GridPoolTimeout,
    fetch_with_grid,
)
from freegem.selenium_fetcher import SeleniumOptions, SeleniumResult


class CapacityTests(unittest.TestCase):
    def test_least_loaded_scheduling(self) -> None:
        pool = GridPool(
            [GridNode("a", "http://a:4444", 4), GridNode("b", "http://b:4444", 2)],
            checkout_timeout_sec=0.5,
        )
        leases = [pool.checkout() for _ in range(3)]
        # ratios after each pick: a=0 -> b=0 (tie, first wins) -> then a=1/4 < b=1/2 -> a
        self.assertEqual([lease.node_name for lease in leases], ["a", "b", "a"])
        stats = pool.stats()
        self.assertEqual(stats["in_flight_total"], 3)
        self.assertEqual(stats["capacity"], 6)
        for lease in leases:
            pool.release(lease)
        self.assertEqual(pool.stats()["in_flight_total"], 0)

    def test_saturation_blocks_then_times_out(self) -> None:
        pool = GridPool([GridNode("solo", "http://s:4444", 1)], checkout_timeout_sec=0.2)
        lease = pool.checkout()
        start = time.monotonic()
        with self.assertRaises(GridPoolTimeout):
            pool.checkout()
        self.assertGreaterEqual(time.monotonic() - start, 0.18)
        pool.release(lease)
        second = pool.checkout()  # freed session is usable again
        pool.release(second)
        self.assertEqual(pool.stats()["timeouts_total"], 1)

    def test_release_wakes_waiter(self) -> None:
        pool = GridPool([GridNode("solo", "http://s:4444", 1)], checkout_timeout_sec=2.0)
        first = pool.checkout()
        got: list[GridLease] = []

        def waiter() -> None:
            got.append(pool.checkout())

        thread = threading.Thread(target=waiter, daemon=True)
        thread.start()
        time.sleep(0.1)
        pool.release(first)
        thread.join(timeout=2.0)
        self.assertTrue(got, "waiter must be woken by release")
        pool.release(got[0])

    def test_session_context_releases_on_error(self) -> None:
        pool = GridPool([GridNode("a", "http://a:4444", 1)], checkout_timeout_sec=0.3)
        with self.assertRaises(RuntimeError):
            with pool.session():
                raise RuntimeError("boom")
        lease = pool.checkout()  # capacity must be back
        pool.release(lease)

    def test_disabled_node_skipped_and_capacity_drops(self) -> None:
        pool = GridPool(
            [GridNode("a", "http://a:4444", 1), GridNode("b", "http://b:4444", 1)],
            checkout_timeout_sec=0.3,
        )
        pool.set_active("a", False)
        self.assertEqual(pool.stats()["capacity"], 1)
        lease = pool.checkout()
        self.assertEqual(lease.node_name, "b")
        pool.release(lease)
        with self.assertRaises(GridPoolTimeout):
            pool.set_active("b", False)
            pool.checkout()
        pool.set_active("b", True)

    def test_node_accounting_rules(self) -> None:
        pool = GridPool(checkout_timeout_sec=0.1)
        with self.assertRaises(ValueError):
            pool.add_node("", "http://x:4444")
        with self.assertRaises(ValueError):
            pool.add_node("z", "http://x:4444", 0)
        pool.add_node("z", "http://z:4444", 2)
        with self.assertRaises(ValueError):
            pool.add_node("z", "http://z:4444", 2)
        lease = pool.checkout()
        with self.assertRaises(ValueError):
            pool.remove_node("z")  # holding a lease
        pool.release(lease)
        self.assertTrue(pool.remove_node("z"))
        self.assertFalse(pool.remove_node("never-existed"))

    def test_concurrent_double_release_cannot_overbook(self) -> None:
        """Two threads releasing the SAME lease at once must decrement once.

        The old code tested ``lease.released`` outside the lock, so both
        racers could pass the check and double-decrement -- the node then
        hands out a third session on a max-2 budget.
        """
        for _ in range(100):
            pool = GridPool([GridNode("n", "http://n:4444", 2)], checkout_timeout_sec=0.1)
            lease_a = pool.checkout()
            lease_b = pool.checkout()
            barrier = threading.Barrier(2)

            def racer(lease: GridLease = lease_a) -> None:
                barrier.wait()
                pool.release(lease)

            threads = [threading.Thread(target=racer) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            # b is still held: a correct pool reports exactly 1 in flight.
            self.assertEqual(pool.stats()["in_flight_total"], 1)
            pool.release(lease_b)
            self.assertEqual(pool.stats()["in_flight_total"], 0)


class FetchWithGridTests(unittest.TestCase):
    def test_routes_to_leased_endpoint_and_releases(self) -> None:
        pool = GridPool([GridNode("n1", "http://grid:4444", 1)], checkout_timeout_sec=0.5)
        captured: dict = {}

        def fake_fetch(opts: SeleniumOptions) -> SeleniumResult:
            captured["grid_url"] = opts.grid_url
            captured["in_flight_during_fetch"] = pool.stats()["in_flight_total"]
            return SeleniumResult(url=opts.url, final_url=opts.url, ok=True, text="rendered")

        with mock.patch("freegem.grid_pool.fetch_with_selenium", side_effect=fake_fetch):
            result = fetch_with_grid(pool, SeleniumOptions(url="https://example.com"))

        self.assertTrue(result.ok)
        self.assertEqual(captured["grid_url"], "http://grid:4444")
        self.assertEqual(captured["in_flight_during_fetch"], 1)
        self.assertEqual(pool.stats()["in_flight_total"], 0)  # released after fetch

    def test_capacity_timeout_returns_failed_result(self) -> None:
        pool = GridPool([GridNode("n1", "http://grid:4444", 1)], checkout_timeout_sec=0.1)
        lease = pool.checkout()  # saturate
        result = fetch_with_grid(pool, SeleniumOptions(url="https://example.com"), timeout_sec=0.1)
        self.assertFalse(result.ok)
        self.assertIn("grid capacity", result.error or "")
        pool.release(lease)


if __name__ == "__main__":
    unittest.main()
