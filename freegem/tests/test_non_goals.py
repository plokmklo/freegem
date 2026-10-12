"""Tests for the canonical non-goals data and its API surfacing."""

import unittest

from freegem.non_goals import NON_GOALS, non_guarantee_labels


class NonGoalsDataTests(unittest.TestCase):
    def test_expected_boundaries_present(self) -> None:
        labels = non_guarantee_labels()
        self.assertGreaterEqual(len(labels), 8)
        self.assertIn("No guaranteed scraping SLA", labels)
        self.assertIn(
            "No CAPTCHA solving, anti-bot bypass, stealth, or fingerprint evasion",
            labels,
        )
        self.assertIn("No free proxy pool or managed browser fleet", labels)
        self.assertIn("No proprietary web index or built-in SERP", labels)
        self.assertIn("No unlimited crawling or unlimited autoscaling", labels)
        self.assertIn(
            "No universal JavaScript, login, WebSocket, CAPTCHA, PDF, DOCX, or media support",
            labels,
        )
        self.assertIn("No deterministic or always-correct LLM extraction", labels)
        self.assertIn(
            "No hosted queues, databases, dashboards, proxies, or billing-free infrastructure",
            labels,
        )

    def test_labels_unique(self) -> None:
        labels = non_guarantee_labels()
        self.assertEqual(len(labels), len(set(labels)))

    def test_every_goal_fully_populated(self) -> None:
        for goal in NON_GOALS:
            self.assertTrue(goal.label.strip())
            self.assertTrue(goal.assumption.strip())
            self.assertTrue(goal.reality.strip())
            # Realities explain what IS done, not just what is not.
            self.assertGreater(len(goal.reality), 60)

    def test_helper_matches_data(self) -> None:
        self.assertEqual(non_guarantee_labels(), [goal.label for goal in NON_GOALS])


if __name__ == "__main__":
    unittest.main()
