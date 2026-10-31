"""Contract tests for the bounded single-agent job orchestrator (issue #66).

Deterministic and hermetic: every tool seam (gemini search, research,
claim verification) is mocked -- no network, no Gemini, no real fetch.
What is pinned is the ORCHESTRATION contract:

- the plan is allowlisted and its tools are exactly what executes;
- tool arguments are schema-validated before anything runs;
- structural budgets (steps, LLM calls, execution time) stop work between
  steps instead of apologizing afterwards;
- a tool failure isolates to its step and never invents findings;
- citations cannot be fabricated: ``sources`` only contains URLs that
  tool results actually carried, never URLs mined out of an answer;
- missing evidence keeps its own verdict, never reported as contradiction;
- the ``agent`` job type wires into ``execute_job``, and the runner's
  cooperative cancellation overrides a late completion.
"""

from __future__ import annotations

import itertools
import threading
import time
import unittest
from typing import Any
from unittest import mock

from freegem.agent import (
    AGENT_TOOLS,
    AgentLimits,
    plan_task,
    run_agent,
    validate_agent_params,
)
from freegem.claim_verify import ClaimResult, Evidence, VerifyResult
from freegem.config import BridgeConfig
from freegem.jobs import JobManager, JobRunner, execute_job
from freegem.web_research import ResearchResult, ResearchSource

CFG = BridgeConfig(guard=True)

TASK = "compare scraping frameworks"

SEARCH_TEXT = (
    "Comparison candidates found. Sources:\n"
    "- https://a.example/framework-x\n"
    "- https://b.example/framework-y"
)


def _research_result(answer: str) -> ResearchResult:
    return ResearchResult(
        question=TASK,
        answer=answer,
        sources=[
            ResearchSource(url="https://a.example/framework-x", ok=True, chars=120),
            ResearchSource(url="https://b.example/framework-y", ok=True, chars=120),
        ],
        gemini_used=True,
    )


def _verify_result() -> VerifyResult:
    return VerifyResult(
        claims=[
            ClaimResult(
                number=1,
                claim="framework-x is faster",
                status="supported",
                evidence=[
                    Evidence(
                        quote="framework-x benchmarks fastest",
                        source_marker="S1",
                        verified=True,
                    ),
                ],
            ),
            ClaimResult(
                number=2,
                claim="framework-y is unmaintained",
                status="not_found",
            ),
        ],
        gemini_used=True,
    )


class PlanTests(unittest.TestCase):
    """The plan is allowlisted, bounded, and deterministic."""

    def test_plan_uses_only_allowlisted_tools_in_order(self) -> None:
        plan = plan_task(TASK, AgentLimits())
        tools = [entry["tool"] for entry in plan]
        self.assertEqual(tools, ["ask", "research", "verify_claims"])
        for entry in plan:
            self.assertIn(entry["tool"], AGENT_TOOLS)

    def test_registry_has_no_phantom_tools(self) -> None:
        # The registry is the allowlist the plan is validated against:
        # exactly the three phases the agent implements, nothing more.
        self.assertEqual(set(AGENT_TOOLS), {"ask", "research", "verify_claims"})

    def test_plan_rejects_non_allowlisted_tool(self) -> None:
        with (
            mock.patch("freegem.agent.AGENT_TOOLS", frozenset({"ask"})),
            self.assertRaises(ValueError),
        ):
            plan_task(TASK, AgentLimits())


class ValidateParamsTests(unittest.TestCase):
    """Tool arguments are schema-validated before anything runs."""

    def test_valid_params_return_task_and_default_limits(self) -> None:
        task, limits = validate_agent_params({"task": TASK})
        self.assertEqual(task, TASK)
        self.assertEqual(limits.max_steps, 20)
        self.assertEqual(limits.max_llm_calls, 10)

    def test_limit_overrides_are_coerced_to_ints(self) -> None:
        _, limits = validate_agent_params(
            {"task": TASK, "limits": {"max_steps": "5", "max_llm_calls": 2}}
        )
        self.assertEqual(limits.max_steps, 5)
        self.assertEqual(limits.max_llm_calls, 2)

    def test_non_dict_params_rejected(self) -> None:
        with self.assertRaises(ValueError):
            validate_agent_params(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_empty_or_non_string_task_rejected(self) -> None:
        for bad in ({}, {"task": ""}, {"task": "   "}, {"task": 42}):
            with self.assertRaises(ValueError):
                validate_agent_params(bad)

    def test_out_of_range_limits_rejected(self) -> None:
        for key, value in (
            ("max_steps", 0),
            ("max_pages", 0),
            ("max_chars", 0),
            ("max_llm_calls", 0),
            ("max_exec_seconds", 0),
        ):
            with self.assertRaises(ValueError):
                validate_agent_params({"task": TASK, "limits": {key: value}})


class RunAgentContractTests(unittest.TestCase):
    """One bounded request runs the plan and returns the required shape."""

    def test_happy_path_runs_the_whole_plan(self) -> None:
        with (
            mock.patch("freegem.agent.run_tool", return_value=SEARCH_TEXT) as search,
            mock.patch("freegem.agent.research", return_value=_research_result("x wins")),
            mock.patch("freegem.agent.verify", return_value=_verify_result()) as checked,
        ):
            result = run_agent(CFG, TASK)
        # Tool selection: the search ran through the grounded MCP tool.
        args = search.call_args[0]
        self.assertEqual(args[1], "gemini_search")
        self.assertEqual(args[2], {"query": TASK})
        checked.assert_called_once()
        # The result carries the full documented shape.
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, "x wins")
        self.assertEqual(
            result.sources,
            ["https://a.example/framework-x", "https://b.example/framework-y"],
        )
        statuses = [claim["status"] for claim in result.verified_claims]
        self.assertEqual(statuses, ["supported", "not_found"])
        self.assertNotIn("contradicted", statuses)
        tools_used = [s["tool"] for s in result.steps]
        self.assertEqual(tools_used, ["ask", "research", "verify_claims"])
        self.assertTrue(all(s["ok"] for s in result.steps))
        self.assertEqual(result.usage["llm_calls"], 3)
        self.assertEqual(result.usage["pages_processed"], 2)
        self.assertEqual(result.usage["steps_executed"], 3)

    def test_sources_cannot_be_fabricated(self) -> None:
        # The research answer mentions a URL that no tool ever fetched or
        # returned: it must NOT leak into sources.
        poisoned_url = "https://fabricated.example/never-fetched"
        poisoned = _research_result(f"see {poisoned_url} for details")
        with (
            mock.patch("freegem.agent.run_tool", return_value=SEARCH_TEXT),
            mock.patch("freegem.agent.research", return_value=poisoned),
            mock.patch("freegem.agent.verify", return_value=_verify_result()) as checked,
        ):
            result = run_agent(CFG, TASK)
        self.assertIn(poisoned_url, result.answer)
        self.assertNotIn(poisoned_url, result.sources)
        self.assertEqual(
            result.sources,
            ["https://a.example/framework-x", "https://b.example/framework-y"],
        )
        # Verification only ever sees sources that were actually carried.
        verify_args = checked.call_args[1]
        self.assertEqual(verify_args["urls"], result.sources)

    def test_llm_budget_stops_every_step(self) -> None:
        with (
            mock.patch("freegem.agent.run_tool", return_value=SEARCH_TEXT) as search,
            mock.patch("freegem.agent.research", return_value=_research_result("x")) as studied,
            mock.patch("freegem.agent.verify", return_value=_verify_result()) as checked,
        ):
            result = run_agent(CFG, TASK, AgentLimits(max_llm_calls=0))
        search.assert_not_called()
        studied.assert_not_called()
        checked.assert_not_called()
        self.assertEqual(result.status, "failed")
        self.assertIn("LLM-call budget 0 exhausted", result.limitations)
        self.assertTrue(result.steps)
        self.assertTrue(all(not s["ok"] for s in result.steps))

    def test_step_budget_stops_the_plan(self) -> None:
        with mock.patch("freegem.agent.run_tool", return_value=SEARCH_TEXT) as search:
            result = run_agent(CFG, TASK, AgentLimits(max_steps=0))
        search.assert_not_called()
        self.assertEqual(result.status, "failed")
        self.assertIn("step budget 0 exhausted", result.limitations)

    def test_time_budget_stops_work_between_steps(self) -> None:
        # Fake clock: starts at 0.0, the search's budget check sees 0.5,
        # every later check sees 10_000 -- past the 300s budget. Work must
        # stop between steps, not after the whole plan ran.
        clock = itertools.chain([0.0, 0.5], itertools.repeat(10_000.0))
        with (
            mock.patch("freegem.agent.run_tool", return_value=SEARCH_TEXT),
            mock.patch("freegem.agent.research", return_value=_research_result("x")) as studied,
            mock.patch("freegem.agent.verify", return_value=_verify_result()) as checked,
            mock.patch("freegem.agent.time.perf_counter", side_effect=lambda: next(clock)),
        ):
            result = run_agent(CFG, TASK)
        studied.assert_not_called()
        checked.assert_not_called()
        self.assertIn("execution time budget 300s exhausted", result.limitations)
        # The search step ran inside the budget; the later phases were
        # skipped, each as its own recorded step.
        self.assertEqual(len(result.steps), 3)
        self.assertTrue(result.steps[0]["ok"])
        self.assertFalse(result.steps[1]["ok"])
        self.assertFalse(result.steps[2]["ok"])
        self.assertEqual(result.usage["llm_calls"], 1)
        self.assertEqual(result.usage["elapsed_sec"], 10000.0)

    def test_tool_failure_isolates_to_its_step(self) -> None:
        with (
            mock.patch("freegem.agent.run_tool", side_effect=RuntimeError("bridge down")),
            mock.patch("freegem.agent.research", return_value=_research_result("x")) as studied,
            mock.patch("freegem.agent.verify", return_value=_verify_result()) as checked,
        ):
            result = run_agent(CFG, TASK)
        studied.assert_not_called()
        checked.assert_not_called()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.answer, "")
        self.assertEqual(result.sources, [])
        # Missing evidence is never reported as contradiction: no verdicts.
        self.assertEqual(result.verified_claims, [])
        self.assertIn("search step failed: bridge down", result.limitations)
        self.assertFalse(result.steps[0]["ok"])
        self.assertIn("bridge down", result.steps[0]["error"])

    def test_empty_task_fails_without_calling_any_tool(self) -> None:
        with mock.patch("freegem.agent.run_tool") as search:
            result = run_agent(CFG, "   ")
        search.assert_not_called()
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.limitations, ["task is empty"])


class AgentJobWiringTests(unittest.TestCase):
    """The ``agent`` job type goes through the existing job machinery."""

    def test_execute_job_completes_and_stores_the_structured_result(self) -> None:
        mgr = JobManager()
        with (
            mock.patch("freegem.jobs.GLOBAL_JOB_MANAGER", mgr),
            mock.patch("freegem.agent.run_tool", return_value=SEARCH_TEXT),
            mock.patch("freegem.agent.research", return_value=_research_result("x wins")),
            mock.patch("freegem.agent.verify", return_value=_verify_result()),
        ):
            job = mgr.create_job("agent", {"task": TASK})
            execute_job(job, CFG)
        record = mgr.get_job(job.id)
        assert record is not None
        self.assertEqual(record.status, "completed")
        assert record.result is not None
        self.assertEqual(record.result["answer"], "x wins")
        self.assertEqual(
            record.result["sources"],
            ["https://a.example/framework-x", "https://b.example/framework-y"],
        )
        self.assertIsNone(record.error)

    def test_execute_job_fails_cleanly_on_invalid_params(self) -> None:
        mgr = JobManager()
        with mock.patch("freegem.jobs.GLOBAL_JOB_MANAGER", mgr):
            job = mgr.create_job("agent", {"task": "   "})
            execute_job(job, CFG)
        record = mgr.get_job(job.id)
        assert record is not None
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.error, "params.task must be a non-empty string")

    def test_cancel_during_run_overrides_the_late_completion(self) -> None:
        # Cooperative cancellation: the runner never delivers a result for
        # a cancelled job, even when the agent body finishes afterwards.
        mgr = JobManager()
        started = threading.Event()
        release = threading.Event()

        def slow_search(cfg: Any, name: str, arguments: dict[str, Any]) -> str:
            started.set()
            release.wait(timeout=5.0)
            return SEARCH_TEXT

        runner = JobRunner(mgr, lambda job: execute_job(job, CFG), max_workers=1)
        try:
            with (
                mock.patch("freegem.jobs.GLOBAL_JOB_MANAGER", mgr),
                mock.patch("freegem.agent.run_tool", side_effect=slow_search),
                mock.patch("freegem.agent.research", return_value=_research_result("x")),
                mock.patch("freegem.agent.verify", return_value=_verify_result()),
            ):
                job = runner.submit("agent", {"task": TASK})
                self.assertTrue(started.wait(timeout=5.0))
                self.assertTrue(runner.cancel(job.id))
                release.set()
                deadline = time.monotonic() + 5.0
                record = mgr.get_job(job.id)
                while time.monotonic() < deadline:
                    assert record is not None, "job vanished"
                    if record.status in ("cancelled", "completed", "failed"):
                        break
                    time.sleep(0.01)
                    record = mgr.get_job(job.id)
                assert record is not None, "job vanished"
                self.assertEqual(record.status, "cancelled")
                self.assertEqual(record.error, "cancelled during run")
        finally:
            runner.close()


if __name__ == "__main__":
    unittest.main()
