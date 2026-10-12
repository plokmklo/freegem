"""Tests for session/storage-state profiles (session_profiles.py)."""

from __future__ import annotations

import tempfile
import time
import unittest
from http.cookiejar import Cookie
from pathlib import Path
from typing import Any

from freegem.session_profiles import ProfileManager, SessionProfile


class TestSessionProfiles(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.mgr = ProfileManager(storage_dir=self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_save_and_get_profile(self) -> None:
        prof = SessionProfile(
            name="test-user",
            cookies=[{"name": "sid", "value": "12345", "domain": "example.com", "path": "/"}],
            description="Test user profile",
            expires_at=time.time() + 3600,
        )
        saved_path = self.mgr.save_profile(prof)
        self.assertTrue(Path(saved_path).exists())

        loaded = self.mgr.get_profile("test-user")
        assert loaded is not None
        self.assertEqual(loaded.name, "test-user")
        self.assertEqual(len(loaded.cookies), 1)
        self.assertEqual(loaded.cookies[0]["value"], "12345")
        self.assertFalse(loaded.is_expired())

    def test_expired_profile(self) -> None:
        prof = SessionProfile(
            name="expired-user",
            cookies=[],
            expires_at=time.time() - 100,
        )
        self.mgr.save_profile(prof)

        # Should be None by default when getting expired profile
        self.assertIsNone(self.mgr.get_profile("expired-user"))

        # Should be accessible if include_expired=True
        loaded = self.mgr.get_profile("expired-user", include_expired=True)
        assert loaded is not None
        self.assertTrue(loaded.is_expired())

    def test_list_and_delete_profile(self) -> None:
        prof1 = SessionProfile(name="p1")
        prof2 = SessionProfile(name="p2")
        self.mgr.save_profile(prof1)
        self.mgr.save_profile(prof2)

        profiles = self.mgr.list_profiles()
        self.assertEqual(len(profiles), 2)

        deleted = self.mgr.delete_profile("p1")
        self.assertTrue(deleted)

        profiles_after = self.mgr.list_profiles()
        self.assertEqual(len(profiles_after), 1)
        self.assertEqual(profiles_after[0]["name"], "p2")

    def test_export_adapters(self) -> None:
        prof = SessionProfile(
            name="export-test",
            cookies=[{"name": "session", "value": "xyz", "domain": ".example.com", "path": "/"}],
            origins=[{"origin": "https://example.com", "localStorage": [{"name": "theme", "value": "dark"}]}],
        )
        self.mgr.save_profile(prof)

        pw_state: dict[str, Any] | None = self.mgr.export_playwright_storage_state("export-test")
        assert pw_state is not None
        self.assertEqual(len(pw_state["cookies"]), 1)
        self.assertEqual(len(pw_state["origins"]), 1)

        jar = self.mgr.export_cookie_jar("export-test")
        assert jar is not None
        cookies_list = list(jar)
        self.assertEqual(len(cookies_list), 1)
        cookie: Cookie = cookies_list[0]
        self.assertEqual(cookie.name, "session")
        self.assertEqual(cookie.value, "xyz")


if __name__ == "__main__":
    unittest.main()
