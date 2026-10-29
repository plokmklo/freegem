"""Version-drift regression check (#65).

The 2.11.0 wave shipped three disagreeing versions (2.11.0 in
freegem/__init__.py and CHANGELOG.md, 2.9.4 in pyproject.toml). This module
fails CI the moment that happens again: the runtime version, the packaged
version, the installed distribution metadata, and the changelog must all
name the same release.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import freegem

_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    """The [project] version line, straight from pyproject.toml text."""
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise AssertionError('pyproject.toml has no top-level version = "..." line')
    return match.group(1)


class VersionConsistencyTests(unittest.TestCase):
    """Every place a version lives agrees with the runtime version."""

    def test_runtime_version_is_a_clean_release_string(self) -> None:
        self.assertIsNotNone(re.fullmatch(r"\d+\.\d+\.\d+", freegem.__version__))

    def test_pyproject_matches_the_runtime_version(self) -> None:
        self.assertEqual(_pyproject_version(), freegem.__version__)

    def test_changelog_documents_the_current_version(self) -> None:
        text = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{freegem.__version__}]", text)

    def test_installed_metadata_matches_the_runtime_version(self) -> None:
        from importlib import metadata

        try:
            installed = metadata.version("freegem")
        except metadata.PackageNotFoundError:  # pragma: no cover - dev checkout
            self.skipTest("freegem is not installed; run pip install -e '.[dev]'")
        self.assertEqual(installed, freegem.__version__)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
