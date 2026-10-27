"""Hermetic tests for the MCP tool-dispatch layer (mcp_tools.py).

``run_tool`` is pure dispatch: no network, no browser, no model. Every test
patches the one seam its tool touches — ``freegem.mcp_tools.generate`` for
the Gemini tools, the ``run_*_tool`` entry points for the web tools, and the
job-runner / profile-manager singletons for the job tools — so the full
dispatch surface is exercised without the ``mcp`` package and without a real
Gemini session (issue #72: a live-API 10x loop is rate-limited and slow; the
suite's contract is hermetic seams). Only ``build_server`` needs ``mcp``, and
its missing-dependency path is pinned by blocking the import.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from freegem.config import BridgeConfig
from freegem.gemini import BridgeError
from freegem.jobs import GLOBAL_JOB_MANAGER, Job, JobQueueFull
from freegem.mcp_tools import build_server, model_help, run_tool


def _blank_env_api_key(case: unittest.TestCase) -> None:
    """Keep a developer's FREEGEM_API_KEY out of the no-auth test paths."""
    patcher = mock.patch.dict(os.environ, {"FREEGEM_API_KEY": ""})
    patcher.start()
    case.addCleanup(patcher.stop)


class _FakeProfile:
    def to_dict(self) -> dict[str, Any]:
        return {"name": "alpha"}


class _FakeProfileManager:
    """Stand-in for ProfileManager: same call surface, touches no disk."""

    def list_profiles(self) -> list[dict[str, Any]]:
        return [{"name": "alpha", "cookies": 1}]

    def get_profile(self, name: str) -> Any:
        return _FakeProfile() if name == "alpha" else None

    def delete_profile(self, name: str) -> bool:
        return name == "alpha"


class GeminiToolTests(unittest.TestCase):
    """The five generation tools: prompt shape, model routing, rendering."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig()

    def test_search_prompt_pins_query_rules_and_recency(self) -> None:
        with mock.patch("freegem.mcp_tools.generate", return_value="ok") as gen:
            out = run_tool(
                self.cfg,
                "gemini_search",
                {"query": "who won the 2026 final", "recency": "past week"},
            )
        self.assertEqual(out, "ok")
        prompt = gen.call_args.args[1]
        self.assertIn("who won the 2026 final", prompt)
        self.assertIn("Sources:", prompt)
        self.assertIn("Limit the information to this timeframe: past week.", prompt)

    def test_search_routes_to_the_all_around_flash_default(self) -> None:
        with mock.patch("freegem.mcp_tools.generate", return_value="ok") as gen:
            run_tool(self.cfg, "gemini_search", {"query": "what is new"})
        # gemini-3.6-flash: mode 1 (FAST), think 4 (auto)
        self.assertEqual(gen.call_args.args[2], 1)
        self.assertEqual(gen.call_args.args[3], 4)

    def test_search_without_recency_has_no_timeframe_line(self) -> None:
        with mock.patch("freegem.mcp_tools.generate", return_value="ok") as gen:
            run_tool(self.cfg, "gemini_search", {"query": "what is X"})
        self.assertNotIn("timeframe", gen.call_args.args[1])

    def test_ask_plain_question(self) -> None:
        with mock.patch("freegem.mcp_tools.generate", return_value="ok") as gen:
            out = run_tool(self.cfg, "gemini_ask", {"question": "what is a semaphore"})
        self.assertEqual(out, "ok")
        self.assertEqual(gen.call_args.args[1], "what is a semaphore")

    def test_ask_prepends_system_instruction(self) -> None:
        with mock.patch("freegem.mcp_tools.generate", return_value="ok") as gen:
            run_tool(
                self.cfg,
                "gemini_ask",
                {"question": "summarize", "system": "answer like a pirate"},
            )
        self.assertEqual(gen.call_args.args[1], "answer like a pirate\n\nsummarize")

    def test_deep_think_passes_problem_and_the_thinking_model(self) -> None:
        with mock.patch("freegem.mcp_tools.generate", return_value="ok") as gen:
            run_tool(self.cfg, "gemini_deep_think", {"problem": "design a rate limiter"})
        self.assertEqual(gen.call_args.args[1], "design a rate limiter")
        # gemini-3.5-flash-thinking: mode 2 (THINKING), think 0 (deepest)
        self.assertEqual(gen.call_args.args[2], 2)
        self.assertEqual(gen.call_args.args[3], 0)

    def test_advisor_prompt_is_structured_with_context_and_constraints(self) -> None:
        with mock.patch("freegem.mcp_tools.generate", return_value="ok") as gen:
            run_tool(
                self.cfg,
                "gemini_advisor",
                {
                    "topic": "monorepo or polyrepo",
                    "context": "team of 6",
                    "constraints": "no new tooling",
                },
            )
        prompt = gen.call_args.args[1]
        self.assertIn("## Recommendation", prompt)
        self.assertIn("## What would change my mind", prompt)
        self.assertIn("Question: monorepo or polyrepo", prompt)
        self.assertIn("Context: team of 6", prompt)
        self.assertIn("Constraints: no new tooling", prompt)

    def test_chat_renders_roles_and_skips_junk_entries(self) -> None:
        with mock.patch("freegem.mcp_tools.generate", return_value="ok") as gen:
            run_tool(
                self.cfg,
                "gemini_chat",
                {
                    "messages": [
                        {"role": "system", "content": "be terse"},
                        {"role": "assistant", "content": "earlier answer"},
                        {"role": "user", "content": "continue"},
                        "not a dict",
                        {"content": "role defaults to user"},
                    ]
                },
            )
        self.assertEqual(
            gen.call_args.args[1],
            "\n\n".join(
                [
                    "[System instruction]: be terse",
                    "[Assistant]: earlier answer",
                    "continue",
                    "role defaults to user",
                ]
            ),
        )

    def test_chat_rejects_non_list_messages(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            run_tool(self.cfg, "gemini_chat", {"messages": "nope"})
        self.assertIn("messages must be a list", str(ctx.exception))

    def test_explicit_model_override_routes_the_request(self) -> None:
        with mock.patch("freegem.mcp_tools.generate", return_value="ok") as gen:
            run_tool(self.cfg, "gemini_ask", {"question": "hi", "model": "gemini-flash-lite"})
        # gemini-flash-lite: mode 6 (FLASH_LITE)
        self.assertEqual(gen.call_args.args[2], 6)

    def test_unknown_model_name_falls_back_to_the_default(self) -> None:
        # resolve_model's documented contract: unknown client names route to
        # the default instead of erroring (client ids predate the registry).
        with mock.patch("freegem.mcp_tools.generate", return_value="ok") as gen:
            out = run_tool(self.cfg, "gemini_ask", {"question": "hi", "model": "gpt-4o"})
        self.assertEqual(out, "ok")
        self.assertEqual(gen.call_args.args[2], 1)

    def test_list_models_describes_registry_and_session(self) -> None:
        out = run_tool(self.cfg, "gemini_list_models", {})
        self.assertIn("Available models:", out)
        self.assertIn("gemini-3.6-flash", out)
        self.assertIn("@think=", out)
        self.assertIn("Default model: gemini-3.6-flash", out)
        self.assertIn("Session: none (anonymous)", out)
        self.assertIn("system env", out)


class RunNormalizationTests(unittest.TestCase):
    """_run: resolution failures and empty generations fail loudly."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig()

    def test_empty_generation_raises_bridge_error(self) -> None:
        with mock.patch("freegem.mcp_tools.generate", return_value=""):
            with self.assertRaises(BridgeError) as ctx:
                run_tool(self.cfg, "gemini_ask", {"question": "hi"})
        self.assertIn("Gemini returned nothing", str(ctx.exception))

    def test_out_of_range_think_override_is_a_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            run_tool(
                self.cfg,
                "gemini_ask",
                {"question": "hi", "model": "gemini-3.6-flash@think=9"},
            )
        self.assertIn("Think level out of range", str(ctx.exception))

    def test_malformed_think_override_is_a_value_error(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            run_tool(
                self.cfg,
                "gemini_ask",
                {"question": "hi", "model": "gemini-3.6-flash@think=deep"},
            )
        self.assertIn("Invalid think level", str(ctx.exception))


class DispatchTests(unittest.TestCase):
    """Pure dispatch hygiene: unknown names, None arguments, delegation."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig()

    def test_unknown_tool_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            run_tool(self.cfg, "gemini_nope", {})
        self.assertIn("unknown tool: gemini_nope", str(ctx.exception))

    def test_none_arguments_are_tolerated(self) -> None:
        out = run_tool(self.cfg, "gemini_list_models", None)
        self.assertIn("Available models:", out)

    def test_web_tools_delegate_to_their_entry_points(self) -> None:
        arguments = {"urls": ["https://a.example/"], "question": "q"}
        for tool, target in (
            ("web_scrape", "freegem.web_scraper.run_scrape_tool"),
            ("web_research", "freegem.web_research.run_research_tool"),
            ("web_verify", "freegem.claim_verify.run_verify_tool"),
        ):
            with self.subTest(tool=tool):
                with mock.patch(target, return_value="DELEGATED") as entry:
                    out = run_tool(self.cfg, tool, arguments)
                self.assertEqual(out, "DELEGATED")
                entry.assert_called_once_with(self.cfg, arguments)


class McpAuthTests(unittest.TestCase):
    """The MCP api-key gate: the config value or FREEGEM_API_KEY, nothing else."""

    def setUp(self) -> None:
        _blank_env_api_key(self)
        self.cfg = BridgeConfig(api_key="sekrit")

    def test_missing_or_wrong_key_is_rejected_on_every_gated_tool(self) -> None:
        for name, arguments in (
            ("freegem_submit_job", {"job_type": "scrape", "params": {}}),
            ("freegem_get_job", {"job_id": "job-x"}),
            ("freegem_get_artifact", {"artifact_id": "art-x"}),
            ("freegem_manage_profile", {"action": "list"}),
        ):
            with self.subTest(tool=name):
                with self.assertRaises(PermissionError):
                    run_tool(self.cfg, name, arguments)
                with self.assertRaises(PermissionError):
                    run_tool(self.cfg, name, {**arguments, "api_key": "wrong"})

    def test_matching_key_reaches_the_tool(self) -> None:
        out = json.loads(
            run_tool(self.cfg, "freegem_get_job", {"job_id": "job-nope", "api_key": "sekrit"})
        )
        self.assertEqual(out, {"error": "Job not found"})

    def test_env_var_key_is_honored_without_a_config_value(self) -> None:
        cfg = BridgeConfig()
        with mock.patch.dict(os.environ, {"FREEGEM_API_KEY": "envkey"}):
            with self.assertRaises(PermissionError):
                run_tool(cfg, "freegem_get_job", {"job_id": "job-x"})
            out = json.loads(
                run_tool(cfg, "freegem_get_job", {"job_id": "job-x", "api_key": "envkey"})
            )
        self.assertEqual(out, {"error": "Job not found"})


class JobToolTests(unittest.TestCase):
    """The job/artifact tools against the global manager, without execution."""

    def setUp(self) -> None:
        _blank_env_api_key(self)
        self.cfg = BridgeConfig()

    def test_get_job_not_found_is_json_not_a_crash(self) -> None:
        out = json.loads(run_tool(self.cfg, "freegem_get_job", {"job_id": "job-nope"}))
        self.assertEqual(out, {"error": "Job not found"})

    def test_get_artifact_not_found_is_json_not_a_crash(self) -> None:
        out = json.loads(run_tool(self.cfg, "freegem_get_artifact", {"artifact_id": "art-nope"}))
        self.assertEqual(out, {"error": "Artifact not found"})

    def test_get_artifact_returns_the_artifact_metadata(self) -> None:
        job = GLOBAL_JOB_MANAGER.create_job("playwright", {"url": "https://a.example/"})
        art = GLOBAL_JOB_MANAGER.save_artifact(job.id, "shot.png", "image/png", b"png bytes")
        self.addCleanup(lambda: GLOBAL_JOB_MANAGER.jobs.pop(job.id, None))
        self.addCleanup(lambda: GLOBAL_JOB_MANAGER.artifacts.pop(art.id, None))
        self.addCleanup(lambda: Path(art.file_path).unlink(missing_ok=True))
        out = json.loads(run_tool(self.cfg, "freegem_get_artifact", {"artifact_id": art.id}))
        self.assertEqual(out["id"], art.id)
        self.assertEqual(out["job_id"], job.id)
        self.assertEqual(out["filename"], "shot.png")
        self.assertEqual(out["file_path"], art.file_path)

    def test_submit_job_uses_the_configured_runner(self) -> None:
        submitted: list[tuple[str, dict[str, Any]]] = []

        class _RecordingRunner:
            def submit(self, job_type: str, params: dict[str, Any]) -> Job:
                submitted.append((job_type, params))
                return Job(id="job-fake-1", type=job_type, status="queued", params=params)

        with mock.patch("freegem.jobs.job_runner", return_value=_RecordingRunner()):
            out = json.loads(
                run_tool(
                    self.cfg,
                    "freegem_submit_job",
                    {"job_type": "scrape", "params": {"url": "https://a.example/"}},
                )
            )
        self.assertEqual(out["id"], "job-fake-1")
        self.assertEqual(out["status"], "queued")
        self.assertEqual(submitted, [("scrape", {"url": "https://a.example/"})])

    def test_submit_job_queue_full_is_a_json_backpressure_signal(self) -> None:
        class _FullRunner:
            def submit(self, job_type: str, params: dict[str, Any]) -> Job:
                raise JobQueueFull("bounded queue is full")

        with mock.patch("freegem.jobs.job_runner", return_value=_FullRunner()):
            out = json.loads(
                run_tool(self.cfg, "freegem_submit_job", {"job_type": "scrape", "params": {}})
            )
        self.assertEqual(out, {"error": "job queue is full; retry later"})


class ProfileToolTests(unittest.TestCase):
    """Profile management through a fake ProfileManager (no disk)."""

    def setUp(self) -> None:
        _blank_env_api_key(self)
        self.cfg = BridgeConfig()

    def _run(self, arguments: dict[str, Any]) -> Any:
        with mock.patch("freegem.mcp_tools.ProfileManager", _FakeProfileManager):
            return json.loads(run_tool(self.cfg, "freegem_manage_profile", arguments))

    def test_list_action(self) -> None:
        self.assertEqual(self._run({"action": "list"}), [{"name": "alpha", "cookies": 1}])

    def test_get_action_found_and_missing(self) -> None:
        self.assertEqual(self._run({"action": "get", "name": "alpha"}), {"name": "alpha"})
        self.assertEqual(self._run({"action": "get", "name": "nope"}), {"error": "Not found"})

    def test_delete_action_reports_the_outcome(self) -> None:
        self.assertEqual(self._run({"action": "delete", "name": "alpha"}), {"deleted": True})
        self.assertEqual(self._run({"action": "delete", "name": "nope"}), {"deleted": False})

    def test_unknown_action_is_rejected(self) -> None:
        self.assertIn("Unknown action", self._run({"action": "purge"})["error"])


class ServerBuildTests(unittest.TestCase):
    """build_server's optional-dependency contract, without the mcp package."""

    def test_missing_mcp_package_raises_an_actionable_import_error(self) -> None:
        # Blocking the import machinery reproduces the "mcp not installed"
        # failure shape: build_server's only import inside the try is the mcp
        # one, so it raises and names the actionable extra.
        with mock.patch("builtins.__import__", side_effect=ImportError("blocked for test")):
            with self.assertRaises(ImportError) as ctx:
                build_server(BridgeConfig())
        self.assertIn("freegem[mcp]", str(ctx.exception))

    def test_model_help_lists_the_registry_and_the_default(self) -> None:
        out = model_help(BridgeConfig())
        self.assertIn("freegem v", out)
        self.assertIn("gemini-3.6-flash", out)
        self.assertIn("default: gemini-3.6-flash", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
