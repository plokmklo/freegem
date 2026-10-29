"""Regression test for version drift across project metadata (issue #65).

Ensures ``freegem --version`` matches the package version declared in
``pyproject.toml``, ``__init__.py``, and ``CHANGELOG.md``.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PKG_INIT = ROOT / "freegem" / "__init__.py"
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"


def _extract_init_version() -> str:
    text = PKG_INIT.read_text()
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"').strip("'")
    raise AssertionError("__version__ not found in __init__.py")


def _extract_pyproject_version() -> str:
    text = PYPROJECT.read_text()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise AssertionError("version not found in pyproject.toml")
    return match.group(1)


def _extract_changelog_version() -> str:
    text = CHANGELOG.read_text()
    # Format: ## [2.11.0] — 2026-10-20. A leading "## [Unreleased]"
    # section is normal between releases; the released version is the
    # first dated heading at or below it.
    for line in text.strip().splitlines():
        if line.startswith("## ["):
            version = line.split("[")[1].split("]")[0]
            if version.lower() != "unreleased":
                return version
    raise AssertionError("versioned CHANGELOG header not found")


class VersionConsistencyTests(unittest.TestCase):
    def test_init_version(self) -> None:
        version = _extract_init_version()
        self.assertTrue(version, "__init__.py version is empty")

    def test_pyproject_version(self) -> None:
        version = _extract_pyproject_version()
        self.assertTrue(version, "pyproject.toml version is empty")

    def test_changelog_version(self) -> None:
        version = _extract_changelog_version()
        self.assertTrue(version, "CHANGELOG.md version is empty")

    def test_all_versions_match(self) -> None:
        init_v = _extract_init_version()
        pyproject_v = _extract_pyproject_version()
        changelog_v = _extract_changelog_version()
        self.assertEqual(
            init_v,
            pyproject_v,
            f"__init__.py ({init_v}) != pyproject.toml ({pyproject_v})",
        )
        self.assertEqual(
            init_v,
            changelog_v,
            f"__init__.py ({init_v}) != CHANGELOG.md ({changelog_v})",
        )


if __name__ == "__main__":
    unittest.main()
