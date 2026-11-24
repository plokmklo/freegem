"""BloomSeenSet contract: no false negatives, honest stats, opt-in semantics."""

from __future__ import annotations

import unittest

from freegem.crawler.bloom import BloomSeenSet


class ConstructionTests(unittest.TestCase):
    def test_rejects_bad_capacity(self) -> None:
        for cap in (0, -1):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                BloomSeenSet(capacity=cap)

    def test_rejects_bad_fp_rate(self) -> None:
        for rate in (0.0, 1.0, -0.1, 1.5):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                BloomSeenSet(capacity=128, fp_rate=rate)

    def test_sized_to_expected_count_not_offer_count(self) -> None:
        f = BloomSeenSet(capacity=64, fp_rate=0.01)
        bits = f.stats()["bits"]
        for i in range(500):  # far past the design point: memory stays fixed
            f.add_if_absent(f"https://x.test/p{i}")
        self.assertEqual(f.stats()["bits"], bits)


class MembershipTests(unittest.TestCase):
    def test_new_then_seen(self) -> None:
        f = BloomSeenSet(capacity=1000)
        self.assertTrue(f.add_if_absent("https://a.test/1"))
        self.assertTrue("https://a.test/1" in f)
        self.assertFalse(f.add_if_absent("https://a.test/1"))
        self.assertEqual(len(f), 1)  # re-offer never re-counts

    def test_zero_impossible_for_added_items(self) -> None:
        # Deterministic 5k-key corpus: the filter must answer present for
        # EVERY key it was fed to -- false negatives would drop crawl URLs.
        f = BloomSeenSet(capacity=5000, fp_rate=0.01)
        urls = [f"https://n.test/{i}" for i in range(5000)]
        for u in urls:
            f.add_if_absent(u)
        self.assertTrue(all(u in f for u in urls))
        readds = [f.add_if_absent(u) for u in urls]
        self.assertFalse(any(readds))  # every re-offer reports probably-seen

    def test_never_seen_answers_are_usually_true_negatives(self) -> None:
        f = BloomSeenSet(capacity=2000, fp_rate=0.01)
        for i in range(2000):
            f.add_if_absent(f"https://fill.test/{i}")
        probes = [f"https://fresh.test/{i}" for i in range(3000)]
        fps = sum(1 for p in probes if p in f)
        self.assertLessEqual(fps / len(probes), 0.05)  # far above nominal 0.01

    def test_positions_are_deterministic_for_item_and_geometry(self) -> None:
        # Bit math depends only on the item and the design point, never on
        # insertion order or instance identity: identical replay sequences
        # produce identical answers and identical load stats.
        a = BloomSeenSet(capacity=512)
        b = BloomSeenSet(capacity=512)
        self.assertEqual(
            [a._positions(f"https://same.test/{n}") for n in range(50)],
            [b._positions(f"https://same.test/{n}") for n in range(50)],
        )
        first = [a.add_if_absent(f"https://same.test/{i}") for i in range(200)]
        a2, b2 = BloomSeenSet(capacity=512), BloomSeenSet(capacity=512)
        replay = [a2.add_if_absent(f"https://same.test/{i}") for i in range(200)]
        again = [b2.add_if_absent(f"https://same.test/{i}") for i in range(200)]
        self.assertEqual(first, replay)
        self.assertEqual(again, [True] * 200)
        self.assertEqual(b2.stats(), a2.stats())


class StatsTests(unittest.TestCase):
    def test_stats_report_design_and_load(self) -> None:
        f = BloomSeenSet(capacity=128, fp_rate=0.02)
        f.add_if_absent("https://s.test/x")
        s = f.stats()
        self.assertEqual(s["capacity"], 128)
        self.assertEqual(s["fp_rate"], 0.02)
        self.assertEqual(s["items"], len(f))
        self.assertGreaterEqual(s["hashes"], 2)
        self.assertEqual(s["memory_bytes"], (s["bits"] + 7) // 8)

    def test_empty_filter_holds_nothing(self) -> None:
        f = BloomSeenSet(capacity=64)
        self.assertFalse("any" in f)
        self.assertFalse("anything-else" in f)
        self.assertEqual(len(f), 0)


if __name__ == "__main__":
    unittest.main()
