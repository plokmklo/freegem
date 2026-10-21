"""Edge-case tests for session profiles (freegem.session_profiles).

Covers name sanitization, corrupt on-disk state, expiry boundaries, and
malformed cookie entries -- the failure paths a real
``~/.config/freegem/profiles`` directory accumulates over time.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from freegem.session_profiles import ProfileManager, SessionProfile


class ProfilePathSanitizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.mgr = ProfileManager(storage_dir=self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_special_chars_stripped_and_lowercased(self) -> None:
        path = self.mgr._profile_path("My Profile (Prod)!")
        self.assertEqual(path.name, "myprofileprod.json")

    def test_punctuation_only_falls_back_to_default(self) -> None:
        self.assertEqual(self.mgr._profile_path("!!!").name, "default.json")

    def test_case_variants_collide_on_one_file(self) -> None:
        """Names differing only by case share a file -- last save wins."""
        self.mgr.save_profile(
            SessionProfile(name="Work", cookies=[{"name": "a", "value": "1"}])
        )
        path_b = self.mgr.save_profile(
            SessionProfile(name="work", cookies=[{"name": "b", "value": "2"}])
        )
        self.assertEqual(path_b.name, "work.json")
        loaded = self.mgr.get_profile("WORK")
        assert loaded is not None
        self.assertEqual(loaded.cookies[0]["value"], "2")


class CorruptStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.mgr = ProfileManager(storage_dir=self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_raw(self, name: str, text: str) -> Path:
        path = Path(self.temp_dir.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_corrupt_json_returns_none(self) -> None:
        self._write_raw("broken.json", "{not json")
        self.assertIsNone(self.mgr.get_profile("broken"))

    def test_json_missing_name_key_returns_none(self) -> None:
        # from_dict raises KeyError on data["name"]; get_profile swallows it.
        self._write_raw("noname.json", json.dumps({"cookies": []}))
        self.assertIsNone(self.mgr.get_profile("noname"))

    def test_list_skips_corrupt_files(self) -> None:
        self.mgr.save_profile(SessionProfile(name="good"))
        self._write_raw("broken.json", "{not json")
        entries = self.mgr.list_profiles()
        self.assertEqual([e["name"] for e in entries], ["good"])
        # non-JSON files in the directory are ignored entirely
        self._write_raw("notes.txt", "hello")
        self.assertEqual(len(self.mgr.list_profiles()), 1)


class ExpiryBoundaryTests(unittest.TestCase):
    def test_exactly_at_expiry_is_expired(self) -> None:
        # Comparison is >=: a profile expires AT its expires_at, not after.
        prof = SessionProfile(name="x", expires_at=1000.0)
        self.assertTrue(prof.is_expired(now=1000.0))
        self.assertFalse(prof.is_expired(now=999.999))

    def test_no_expiry_never_expires(self) -> None:
        prof = SessionProfile(name="x")
        self.assertFalse(prof.is_expired(now=float("inf")))


class RoundTripTests(unittest.TestCase):
    def test_from_dict_minimal(self) -> None:
        prof = SessionProfile.from_dict({"name": "bare"})
        self.assertEqual(prof.name, "bare")
        self.assertEqual(prof.cookies, [])
        self.assertEqual(prof.origins, [])
        self.assertIsNone(prof.expires_at)
        self.assertEqual(prof.description, "")
        self.assertEqual(prof.metadata, {})

    def test_to_from_dict_roundtrip(self) -> None:
        prof = SessionProfile(
            name="rt",
            cookies=[{"name": "k", "value": "v"}],
            origins=[{"origin": "https://x.example", "localStorage": []}],
            expires_at=123.0,
            description="d",
            metadata={"env": "test"},
        )
        again = SessionProfile.from_dict(prof.to_dict())
        self.assertEqual(again, prof)


class MissingProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.mgr = ProfileManager(storage_dir=self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_exports_of_missing_profile_are_none(self) -> None:
        self.assertIsNone(self.mgr.export_playwright_storage_state("ghost"))
        self.assertIsNone(self.mgr.export_cookie_jar("ghost"))

    def test_delete_missing_returns_false(self) -> None:
        self.assertFalse(self.mgr.delete_profile("ghost"))


class CookieJarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.mgr = ProfileManager(storage_dir=self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_malformed_cookie_is_skipped(self) -> None:
        prof = SessionProfile(
            name="mixed",
            cookies=[
                {"name": "ok", "value": "1", "domain": "example.com", "path": "/"},
                {"name": "bad", "value": "2", "domain": None, "path": "/"},
            ],
        )
        self.mgr.save_profile(prof)
        jar = self.mgr.export_cookie_jar("mixed")
        assert jar is not None
        self.assertEqual([c.name for c in jar], ["ok"])

    def test_secure_and_leading_dot_flags(self) -> None:
        prof = SessionProfile(
            name="flags",
            cookies=[
                {
                    "name": "s",
                    "value": "1",
                    "domain": ".example.com",
                    "path": "/",
                    "secure": True,
                }
            ],
        )
        self.mgr.save_profile(prof)
        jar = self.mgr.export_cookie_jar("flags")
        assert jar is not None
        cookie = next(iter(jar))
        self.assertTrue(cookie.secure)
        self.assertTrue(cookie.domain_initial_dot)


class DefaultStorageDirTests(unittest.TestCase):
    def test_xdg_env_is_honoured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                mgr = ProfileManager()
            self.assertEqual(mgr.storage_dir, Path(tmp) / "freegem" / "profiles")
            self.assertTrue(mgr.storage_dir.exists())


if __name__ == "__main__":
    unittest.main()
