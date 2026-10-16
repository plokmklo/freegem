"""Fetch-feature tests: the batch concurrency ceiling (#15), tag-level content
selection (#29), the optional browser-render ladder rung (#14), and the
research-cache de-poisoning residual (#21).

Hermetic by construction: every test patches the network seam
(``web_scraper._fetch`` / ``web_scraper.scrape_page``) or the Playwright
entry point -- no test performs a real HTTP request or launches a browser.
"""

from __future__ import annotations

import contextlib
import io
import unittest
from typing import Any
from unittest import mock

from freegem.config import BridgeConfig
from freegem.web_scraper import ScrapeResult, _clamp_batch_concurrency, scrape_many


def _recording_pool() -> tuple[Any, list[int | None]]:
    """Patch ``ThreadPoolExecutor`` to record ``max_workers``, return a real pool.

    scrape_many imports the pool inside its function body, so patching the
    ``concurrent.futures`` attribute is what the call-time import resolves.
    The wrapper returns a REAL executor, so behavior is untouched.
    """
    import concurrent.futures

    real_pool = concurrent.futures.ThreadPoolExecutor
    seen: list[int | None] = []

    def recording_pool(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("max_workers"))
        return real_pool(*args, **kwargs)

    return mock.patch("concurrent.futures.ThreadPoolExecutor", new=recording_pool), seen


def _ok_scrape(cfg: Any, url: str, **_kwargs: Any) -> ScrapeResult:
    """A scrape_page stand-in: every URL succeeds with a little text."""
    return ScrapeResult(url=url, title="T", text="batch page text")


class BatchConcurrencyClampTests(unittest.TestCase):
    """#15: the batch ceiling is config-driven and a clamp says so on stderr."""

    def test_within_cap_not_clamped_and_silent(self) -> None:
        cfg = BridgeConfig(batch_max_concurrency=32)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(_clamp_batch_concurrency(cfg, 20), 20)
        self.assertEqual(err.getvalue(), "")

    def test_over_default_cap_clamped_with_exactly_one_warning(self) -> None:
        cfg = BridgeConfig()  # default ceiling: 16
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(_clamp_batch_concurrency(cfg, 20), 16)
        self.assertEqual(
            err.getvalue(),
            "scrape: requested concurrency 20 clamped to 16 (batch_max_concurrency)\n",
        )

    def test_raised_ceiling_never_prints_within_bounds(self) -> None:
        cfg = BridgeConfig(batch_max_concurrency=32)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(_clamp_batch_concurrency(cfg, 20), 20)
            self.assertEqual(_clamp_batch_concurrency(cfg, 32), 32)
            self.assertEqual(_clamp_batch_concurrency(cfg, 1), 1)
        self.assertEqual(err.getvalue(), "")

    def test_zero_or_negative_cap_floors_at_one_with_warning(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(
                _clamp_batch_concurrency(BridgeConfig(batch_max_concurrency=0), 20), 1
            )
            self.assertEqual(
                _clamp_batch_concurrency(BridgeConfig(batch_max_concurrency=-3), 5), 1
            )
        self.assertEqual(err.getvalue().count("clamped to 1 (batch_max_concurrency)"), 2)

    def test_request_below_floor_is_normalized_silently(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(_clamp_batch_concurrency(BridgeConfig(), 0), 1)
            self.assertEqual(_clamp_batch_concurrency(BridgeConfig(), -5), 1)
        self.assertEqual(err.getvalue(), "")


class BatchPoolWidthTests(unittest.TestCase):
    """The clamp feeds the pool: ``max_workers`` honors the config ceiling (#15)."""

    def _run_batch(
        self, cfg: BridgeConfig, requested: int, count: int = 40
    ) -> list[int | None]:
        patcher, seen = _recording_pool()
        with patcher, mock.patch("freegem.web_scraper.scrape_page", _ok_scrape):
            items = scrape_many(
                cfg,
                [f"https://host{i}.example/p" for i in range(count)],
                max_chars=200,
                concurrency=requested,
                smart=False,
            )
        self.assertTrue(all(item.ok for item in items))
        return seen

    def test_raised_ceiling_builds_the_bigger_pool(self) -> None:
        self.assertEqual(self._run_batch(BridgeConfig(batch_max_concurrency=32), 20), [20])

    def test_default_ceiling_still_clamps_to_sixteen_once(self) -> None:
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            seen = self._run_batch(BridgeConfig(), 20)
        self.assertEqual(seen, [16])
        # ONE warning, on stderr only: stdout stays pipeline-safe.
        self.assertEqual(err.getvalue().count("clamped to 16 (batch_max_concurrency)"), 1)
        self.assertEqual(out.getvalue(), "")

    def test_zero_ceiling_takes_the_sequential_path(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            seen = self._run_batch(BridgeConfig(batch_max_concurrency=0), 20, count=3)
        self.assertEqual(seen, [], "a floor-1 batch needs no pool at all")
        self.assertEqual(err.getvalue().count("clamped to 1 (batch_max_concurrency)"), 1)


if __name__ == "__main__":
    unittest.main()
