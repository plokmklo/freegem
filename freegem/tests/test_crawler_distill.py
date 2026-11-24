"""Distill contract: modes, degrade-on-failure, extractive cut properties."""

from __future__ import annotations

import unittest
from unittest import mock

from freegem.config import BridgeConfig
from freegem.crawler import distill
from freegem.crawler.distill import (
    DISTILL_INPUT_CHARS,
    DISTILL_OUTPUT_CHARS,
    distill_text,
    extractive_summary,
)

FAKE_CFG: BridgeConfig = mock.MagicMock()


def _rich_text() -> str:
    paras = []
    for i in range(30):
        paras.append(f"Section {i}")
        paras.append(("This paragraph carries real facts and numbers 42. " * 6).strip())
        paras.append("Manage cookies Accept all privacy policy sign in subscribe.")
    return "\n\n".join(paras)


class DistillTextTests(unittest.TestCase):
    def test_empty_body_short_circuits(self) -> None:
        for text in ("", "   \n\t  "):
            with self.subTest(len=len(text)):
                out, mode = distill_text(FAKE_CFG, "https://d.test", text)
                self.assertEqual(out, "")
                self.assertEqual(mode, "empty")

    def test_gemini_mode_on_success(self) -> None:
        with (
            mock.patch.object(distill, "resolve_model", return_value=("m", 1, 0, "", {})),
            mock.patch.object(distill, "generate", return_value="  distilled  ") as gen,
        ):
            out, mode = distill_text(FAKE_CFG, "https://d.test", _rich_text())
        self.assertEqual(mode, "gemini")
        self.assertEqual(out, "distilled")
        self.assertIn("Section 0", gen.call_args_list[0].args[1])

    def test_gemini_output_is_bounded(self) -> None:
        big = "x" * (DISTILL_OUTPUT_CHARS + 5000)
        with (
            mock.patch.object(distill, "resolve_model", return_value=("m", 1, 0, "", {})),
            mock.patch.object(distill, "generate", return_value=big),
        ):
            out, mode = distill_text(FAKE_CFG, "https://d.test", _rich_text())
        self.assertEqual(mode, "gemini")
        self.assertEqual(len(out), DISTILL_OUTPUT_CHARS)

    def test_prompt_input_is_capped(self) -> None:
        tail = "END-SIGNAL-MUST-NOT-APPEAR"
        body = ("y" * DISTILL_INPUT_CHARS) + " " + tail
        with (
            mock.patch.object(distill, "resolve_model", return_value=("m", 1, 0, "", {})),
            mock.patch.object(distill, "generate", side_effect=lambda c, p, *a: p) as gen,
        ):
            out, _ = distill_text(FAKE_CFG, "https://d.test", body)
        self.assertNotIn(tail, gen.call_args_list[0].args[1])
        self.assertLessEqual(len(out), DISTILL_OUTPUT_CHARS)

    def test_model_failure_degrades_to_extractive(self) -> None:
        for boom in (RuntimeError("no session"), ValueError("")):
            with (
                self.subTest(err=type(boom).__name__),
                mock.patch.object(distill, "generate", side_effect=boom),
            ):
                out, mode = distill_text(FAKE_CFG, "https://d.test", _rich_text())
            self.assertEqual(mode, "extractive")
            self.assertIn("Section", out)

    def test_blank_model_reply_degrades(self) -> None:
        with (
            mock.patch.object(distill, "resolve_model", return_value=("m", 1, 0, "", {})),
            mock.patch.object(distill, "generate", return_value="   "),
        ):
            out, mode = distill_text(FAKE_CFG, "https://d.test", _rich_text())
        self.assertEqual(mode, "extractive")
        self.assertTrue(out)


class ExtractiveTests(unittest.TestCase):
    def test_deterministic(self) -> None:
        text = _rich_text()
        self.assertEqual(extractive_summary(text), extractive_summary(text))

    def test_drops_boilerplate_keeps_facts(self) -> None:
        body = (
            "Important Heading\n"
            + ("Real content with facts, figures 3.14 and statements. " * 4)
            + "\n\nCookie notice: we use cookies. Accept all | Privacy policy details.\n\n"
            + ("More real content, unrelated numbers 2718. " * 4)
        )
        out = extractive_summary(body)
        self.assertIn("Real content", out)
        self.assertIn("More real content", out)
        self.assertNotIn("Cookie notice", out)

    def test_heading_dies_with_its_boilerplate_block(self) -> None:
        # Boilerplate detection applies to paragraphs >= 80 and < 220 chars
        # (long rants are treated as content, per the module's rule).
        body = "Legal\n\n" + "Please read our privacy policy and terms of service before use. " * 2
        self.assertTrue(80 <= len(body.split("\n\n")[1]) < 220)
        out = extractive_summary(body)
        self.assertNotIn("privacy policy", out)
        self.assertNotIn("Legal", out)

    def test_long_paragraph_truncated(self) -> None:
        body = "z" * 9000
        out = extractive_summary(body)
        self.assertTrue(out.endswith("…"))
        self.assertLess(len(out), len(body))

    def test_output_respects_max_chars(self) -> None:
        body = "\n\n".join(f"Para {i} " + ("dense filler words here. " * 8) for i in range(40))
        out = extractive_summary(body, max_chars=300)
        self.assertLessEqual(len(out), 300)

    def test_short_noise_only_input_yields_bounded_output(self) -> None:
        out = extractive_summary("no\nno2\nno3")  # lines <80 chars: no paragraphs
        self.assertIsInstance(out, str)
        self.assertLessEqual(len(out), DISTILL_OUTPUT_CHARS)


if __name__ == "__main__":
    unittest.main()
