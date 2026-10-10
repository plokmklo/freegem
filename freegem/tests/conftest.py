"""Shared test fixtures.

The robots.txt cache is process-wide by design (that is its whole point),
which makes it cross-test state: one test's _FakeSite robots answer must not
leak into the next test's expectations. Clear it before every test.
"""

from __future__ import annotations

import pytest

from freegem.web_scraper import clear_robots_cache


@pytest.fixture(autouse=True)
def _fresh_robots_cache() -> None:
    clear_robots_cache()
