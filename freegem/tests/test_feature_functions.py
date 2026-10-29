"""Check ALL feature functions: a package-wide surface contract.

One hermetic module holding freegem's whole advertised feature surface
honest:

* every feature module imports (optional extras are guarded at import
  time by design, so a bare install must be able to import the package);
* every `__all__` member of every module really exists there and every
  advertised callable still exposes an inspectable signature -- a moved
  function, a renamed entry point, or a dangling re-export fails here even
  when no per-module test noticed;
* every tool advertised in TOOL_SPECS dispatches end to end with its
  external seams mocked, and so do the four management tools -- a tool
  added to the catalog without hermetic coverage fails the sweep.

No test touches the network, a browser, or a Gemini session (issue #72's
contract: hermetic seams, never a live-API loop).
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import pkgutil
import unittest
from typing import Any
from unittest import mock

import freegem
from freegem.config import BridgeConfig
from freegem.jobs import Job, JobQueueFull
from freegem.mcp_tools import TOOL_SPECS, run_tool

#: The advertised catalog drives the dispatch sweep: a new TOOL_SPECS entry
#: without a row here fails the sweep until it gets hermetic coverage.
_DISPATCH_ROWS: dict[str, dict[str, Any]] = {
    "gemini_search": {"query": "q"},
    "gemini_ask": {"question": "q"},
    "gemini_deep_think": {"problem": "p"},
    "gemini_advisor": {"topic": "t"},
    "gemini_chat": {"messages": [{"role": "user", "content": "hi"}]},
    "gemini_list_models": {},
    "web_scrape": {"url": "https://a.test/page"},
    "web_research": {"urls": ["https://a.test/page"], "question": "q"},
    "web_verify": {"claims": ["a claim"], "urls": ["https://a.test/page"]},
}


def _walk_error(name: str) -> None:
    """walk_packages must not silently skip an unimportable subpackage."""
    raise AssertionError(f"feature subpackage {name!r} failed to import")


def _feature_module_names() -> list[str]:
    """Every module of the package, minus the test tree and __main__."""
    names: list[str] = []
    for _finder, name, _ispkg in pkgutil.walk_packages(
        freegem.__path__, prefix="freegem.", onerror=_walk_error
    ):
        if name == "freegem.__main__" or name.startswith("freegem.tests"):
            continue
        names.append(name)
    return names


class _FakeProfileManager:
    """Stand-in for ProfileManager: same call surface, touches no disk."""

    def list_profiles(self) -> list[dict[str, Any]]:
        return [{"name": "alpha"}]


class FeatureModuleImportTests(unittest.TestCase):
    """A bare install must be able to import every feature module."""

    def test_the_walk_covers_the_feature_package(self) -> None:
        names = _feature_module_names()
        for required in ("freegem.web_scraper", "freegem.web_research", "freegem.mcp_tools"):
            self.assertIn(required, names)
        self.assertIn("freegem.crawler", names)
        self.assertGreaterEqual(len(names), 30, "the surface walk found suspiciously few modules")

    def test_every_feature_module_imports(self) -> None:
        for name in _feature_module_names():
            with self.subTest(module=name):
                importlib.import_module(name)


class AdvertisedSurfaceTests(unittest.TestCase):
    """Every __all__ member exists in its module and is inspectable."""

    def test_every_advertised_member_is_reachable_and_inspectable(self) -> None:
        for name in _feature_module_names():
            module = importlib.import_module(name)
            for member in getattr(module, "__all__", []):
                with self.subTest(module=name, member=member):
                    obj = getattr(module, member)  # fails loudly when dangling
                    if callable(obj):
                        inspect.signature(obj)  # fails loudly when opaque


class ToolCatalogSweepTests(unittest.TestCase):
    """Every advertised tool dispatches end to end, every seam mocked (#72)."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig()
        self._patchers: list[Any] = []

    def tearDown(self) -> None:
        for patcher in self._patchers:
            patcher.stop()

    def _mock(self, target: str) -> Any:
        patcher = mock.patch(target, return_value="SEAM OUT")
        self._patchers.append(patcher)
        return patcher.start()

    def test_every_advertised_tool_dispatches(self) -> None:
        self._mock("freegem.mcp_tools.generate")
        for target in (
            "freegem.web_scraper.run_scrape_tool",
            "freegem.web_research.run_research_tool",
            "freegem.claim_verify.run_verify_tool",
        ):
            self._mock(target)
        for spec in TOOL_SPECS:
            name = spec["name"]
            with self.subTest(tool=name):
                self.assertIn(name, _DISPATCH_ROWS, "new tool: add a hermetic dispatch row")
                out = run_tool(self.cfg, name, _DISPATCH_ROWS[name])
                self.assertIsInstance(out, str)
                self.assertTrue(out.strip(), "every advertised tool must return output")

    def test_the_management_tools_still_answer(self) -> None:
        """The four freegem_* management tools share the dispatch surface but
        sit outside TOOL_SPECS; pin them too, with no disk and no network."""

        class _FullRunner:
            def submit(self, job_type: str, params: dict[str, Any]) -> Job:
                raise JobQueueFull("bounded queue is full")

        with mock.patch.dict(os.environ, {"FREEGEM_API_KEY": ""}):
            with mock.patch("freegem.mcp_tools.ProfileManager", _FakeProfileManager):
                for name, arguments, expected in (
                    ("freegem_get_job", {"job_id": "job-nope"}, {"error": "Job not found"}),
                    (
                        "freegem_get_artifact",
                        {"artifact_id": "art-nope"},
                        {"error": "Artifact not found"},
                    ),
                    ("freegem_manage_profile", {"action": "list"}, [{"name": "alpha"}]),
                ):
                    with self.subTest(tool=name):
                        payload = json.loads(run_tool(self.cfg, name, arguments))
                        self.assertEqual(payload, expected)
            with mock.patch("freegem.jobs.job_runner", return_value=_FullRunner()):
                payload = json.loads(
                    run_tool(self.cfg, "freegem_submit_job", {"job_type": "scrape", "params": {}})
                )
        self.assertEqual(payload, {"error": "job queue is full; retry later"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
