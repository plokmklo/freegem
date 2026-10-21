"""Edge-case tests for the Gemini model registry (freegem.models).

The happy paths live in test_config_models.py; these cover the parsing
corners, fallback rules, and registry-isolation guarantees that only show
up when a client sends a hostile or stale model identifier.
"""

from __future__ import annotations

import unittest

from freegem.models import (
    DEFAULT_MODEL,
    MODELS,
    describe_model,
    parse_think_override,
    resolve_model,
)


class ParseThinkOverrideEdgeTests(unittest.TestCase):
    def test_bare_marker_keeps_full_name(self) -> None:
        # "@think=2" with no base: rpartition yields an empty base, which the
        # function restores to the full input rather than erroring.
        base, level, err = parse_think_override("@think=2")
        self.assertEqual(base, "@think=2")
        self.assertEqual(level, 2)
        self.assertIsNone(err)

    def test_negative_level_out_of_range(self) -> None:
        base, level, err = parse_think_override("gemini-auto@think=-1")
        self.assertEqual(base, "gemini-auto")
        self.assertIsNone(level)
        self.assertIn("out of range", err or "")

    def test_explicit_plus_sign_accepted(self) -> None:
        self.assertEqual(
            parse_think_override("gemini-auto@think=+3"),
            ("gemini-auto", 3, None),
        )

    def test_multiple_markers_use_last(self) -> None:
        base, level, err = parse_think_override("gemini-auto@think=1@think=4")
        self.assertEqual((base, level, err), ("gemini-auto@think=1", 4, None))

    def test_empty_level_is_invalid(self) -> None:
        _, level, err = parse_think_override("gemini-auto@think=")
        self.assertIsNone(level)
        self.assertIn("Invalid think level", err or "")

    def test_float_level_is_invalid(self) -> None:
        _, _, err = parse_think_override("gemini-auto@think=2.5")
        self.assertIn("Invalid think level", err or "")


class ResolveModelEdgeTests(unittest.TestCase):
    def test_invalid_override_short_circuits(self) -> None:
        result = resolve_model("gemini-3.6-flash@think=99")
        self.assertEqual(
            result,
            (None, None, None, "Think level out of range: 99 (expected 0-4)", None),
        )

    def test_unknown_default_uses_registry_default(self) -> None:
        # If the configured default is itself unknown, the echoed name must
        # still be a model that actually routes upstream.
        name, mode, _think, err, _extra = resolve_model("nope", default="also-missing")
        self.assertIsNone(err)
        self.assertEqual(name, DEFAULT_MODEL)
        self.assertEqual(mode, MODELS[DEFAULT_MODEL].mode)

    def test_known_custom_default(self) -> None:
        name, mode, _think, err, _ = resolve_model("nope", default="gemini-flash-lite")
        self.assertIsNone(err)
        self.assertEqual(name, "gemini-flash-lite")
        self.assertEqual(mode, MODELS["gemini-flash-lite"].mode)

    def test_extra_fields_are_copied_not_shared(self) -> None:
        _name, _mode, _think, _err, extra = resolve_model("gemini-3.1-pro-enhanced")
        assert extra is not None
        extra[999] = "injected"  # must not leak into the registry
        self.assertNotIn(999, MODELS["gemini-3.1-pro-enhanced"].extra or {})
        _n2, _m2, _t2, _e2, extra2 = resolve_model("gemini-3.1-pro-enhanced")
        assert extra2 is not None
        self.assertNotIn(999, extra2)

    def test_non_enhanced_model_has_no_extra(self) -> None:
        *_, extra = resolve_model("gemini-3.6-flash")
        self.assertIsNone(extra)

    def test_overridden_model_keeps_mode(self) -> None:
        result = resolve_model("gemini-3.1-pro-enhanced@think=1")
        self.assertEqual(result[:4], ("gemini-3.1-pro-enhanced", 3, 1, None))


class DescribeModelTests(unittest.TestCase):
    def test_describe_strips_override(self) -> None:
        self.assertEqual(
            describe_model("gemini-3.5-flash-thinking@think=2"),
            MODELS["gemini-3.5-flash-thinking"].desc,
        )

    def test_every_registered_model_has_description(self) -> None:
        for name in MODELS:
            self.assertEqual(describe_model(name), MODELS[name].desc)


class ModelSpecTests(unittest.TestCase):
    def test_specs_are_frozen(self) -> None:
        spec = MODELS["gemini-auto"]
        with self.assertRaises(Exception):
            spec.mode = 1  # type: ignore[misc]

    def test_only_pro_models_require_cookie(self) -> None:
        flagged = {n for n, s in MODELS.items() if s.requires_cookie}
        self.assertEqual(flagged, {"gemini-3.1-pro", "gemini-3.1-pro-enhanced"})


if __name__ == "__main__":
    unittest.main()
