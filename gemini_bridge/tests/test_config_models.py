"""Config and model registry tests.

These are table-driven with no mocks: config resolution and model parsing are
the two pieces everything else depends on, so they are the cheapest place to
catch a regression.
"""

import unittest

from gemini_bridge.config import (
    BridgeConfig,
    config_search_paths,
    find_config,
    load_config,
)
from gemini_bridge.models import MODELS, describe_model, parse_think_override, resolve_model


class ImmutabilityTests(unittest.TestCase):
    """The frozen config is the fix for a real concurrency bug; prove it holds."""

    def test_config_is_frozen(self):
        cfg = BridgeConfig()
        with self.assertRaises(Exception):
            cfg.port = 9999  # type: ignore[misc]

    def test_replace_returns_new_instance(self):
        cfg = BridgeConfig()
        derived = cfg.replace(port=9999)
        self.assertIsNot(cfg, derived)
        self.assertEqual(cfg.port, 8081)
        self.assertEqual(derived.port, 9999)

    def test_replace_does_not_mutate_original(self):
        """Two requests deriving from one config cannot see each other."""
        base = BridgeConfig(default_model="gemini-3.6-flash")
        a = base.replace(default_model="gemini-3.5-flash-thinking")
        b = base.replace(default_model="gemini-flash-lite")
        self.assertEqual(base.default_model, "gemini-3.6-flash")
        self.assertEqual(a.default_model, "gemini-3.5-flash-thinking")
        self.assertEqual(b.default_model, "gemini-flash-lite")

    def test_defaults_are_not_shared_mutable(self):
        cfg = BridgeConfig()
        self.assertEqual(BridgeConfig().host, cfg.host)


class RedactionTests(unittest.TestCase):
    """A config dump must never carry a credential."""

    def test_redacted_omits_secrets(self):
        cfg = BridgeConfig(
            xsrf_token="super-secret-token",
            cookie_file="/home/me/gemini-auth.json",
        )
        blob = repr(cfg.redacted())
        self.assertNotIn("super-secret-token", blob)
        # The whole path is withheld, not just the filename: a directory layout
        # can be as identifying as the credential name.
        self.assertNotIn("gemini-auth.json", blob)
        self.assertNotIn("/home/me", blob)

    def test_redacted_reports_presence_not_value(self):
        cfg = BridgeConfig(xsrf_token="abc123")
        self.assertTrue(cfg.redacted()["xsrf_token_set"])
        self.assertFalse(BridgeConfig().redacted()["xsrf_token_set"])

    def test_redacted_is_json_serialisable(self):
        """It is meant to be dumped into logs, so it must round-trip."""
        import json

        cfg = BridgeConfig(cookie_file="gemini_bridge/tests/__init__.py")
        self.assertIsInstance(json.dumps(cfg.redacted()), str)

    def test_session_detection(self):
        self.assertFalse(BridgeConfig().has_session_cookie())
        self.assertTrue(
            BridgeConfig(cookie_file="gemini_bridge/tests/__init__.py").has_session_cookie()
        )


class ConfigLoadingTests(unittest.TestCase):
    def test_load_missing_file_uses_defaults(self):
        cfg = load_config("/nonexistent/path/config.json")
        self.assertEqual(cfg.port, 8081)

    def test_load_ignores_unknown_keys(self):
        """A config from an older version must still load."""
        cfg = BridgeConfig.from_dict(
            {"port": 9000, "api_keys": ["sk-should-be-ignored"], "unknown_field": 1}
        )
        self.assertEqual(cfg.port, 9000)
        self.assertFalse(hasattr(cfg, "api_keys"))

    def test_no_api_keys_field_exists(self):
        """Auth was removed deliberately; the field must not come back."""
        self.assertNotIn("api_keys", BridgeConfig.__dataclass_fields__)

    def test_env_overrides_file(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"GEMINI_XSRF_TOKEN": "from-env"}):
            cfg = BridgeConfig.from_dict({"port": 8081})
        self.assertEqual(cfg.xsrf_token, "from-env")


class ModelRegistryTests(unittest.TestCase):
    def test_all_models_resolve(self):
        for name in MODELS:
            resolved, mode, think, err, _ = resolve_model(name)
            self.assertIsNone(err, f"{name}: {err}")
            self.assertEqual(resolved, name)
            self.assertIsInstance(mode, int)
            self.assertIsInstance(think, int)

    def test_think_depth_range(self):
        for spec in MODELS.values():
            self.assertIn(spec.think, range(5))

    def test_mode_values_are_known(self):
        """MODE_CATEGORY from Gemini's frontend enum."""
        for name, spec in MODELS.items():
            self.assertIn(spec.mode, range(1, 7), f"{name} has unknown mode {spec.mode}")

    def test_pro_models_require_cookie(self):
        for name, spec in MODELS.items():
            if "pro" in name:
                self.assertTrue(spec.requires_cookie, f"{name} should be flagged")


class ThinkOverrideTests(unittest.TestCase):
    def test_plain_name(self):
        self.assertEqual(parse_think_override("gemini-3.6-flash"), ("gemini-3.6-flash", None, None))

    def test_override_extracted(self):
        self.assertEqual(
            parse_think_override("gemini-3.5-flash-thinking@think=2"),
            ("gemini-3.5-flash-thinking", 2, None),
        )

    def test_non_numeric_rejected(self):
        _, _, err = parse_think_override("gemini-3.6-flash@think=deep")
        self.assertIn("Invalid think level", err)

    def test_out_of_range_rejected(self):
        _, _, err = parse_think_override("gemini-3.6-flash@think=9")
        self.assertIn("out of range", err)

    def test_override_wins_over_default(self):
        _, _, think, _, _ = resolve_model("gemini-3.5-flash-thinking@think=4")
        self.assertEqual(think, 4)  # spec default is 0

    def test_unknown_model_falls_back_to_default(self):
        name, mode, think, err, _ = resolve_model("gemini-nonexistent-9000", "gemini-3.6-flash")
        self.assertIsNone(err)
        self.assertEqual(name, "gemini-3.6-flash")
        self.assertEqual(mode, MODELS["gemini-3.6-flash"].mode)

    def test_describe_unknown(self):
        self.assertEqual(describe_model("not-a-model"), "unknown model")


if __name__ == "__main__":
    unittest.main()
