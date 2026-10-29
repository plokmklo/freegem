"""web_research parameter contract: the `query` alias and honest descriptions.

Issues #70 / #71: every other research-style tool (ai_research, ai_extract,
ai_crawl) names its prompt parameter `query`, and a caller following that
convention hit "ValueError: web_research requires a 'question' argument".
`run_research_tool` accepts both names now.

Issue #73: the advertised description must LEAD with the one requirement a
caller cannot guess -- the pages are supplied up front, this tool does not
search the web -- instead of burying `urls` in an Args block that
`tools/list` never shows. These tests pin the description at the catalog
level (TOOL_SPECS) and at the schema level (FastMCP's generated input
schema), hermetically: every seam is mocked, nothing hits the network or a
live Gemini session (#72's contract).
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest import mock

from freegem.config import BridgeConfig
from freegem.mcp_tools import TOOL_SPECS
from freegem.web_research import run_research_tool

_MINIMAL_ARGS: dict[str, Any] = {"urls": ["https://a.test/page", "https://b.test/page"]}


def _spec(name: str) -> dict[str, Any]:
    """The TOOL_SPECS entry for a tool, failing loudly when it is gone."""
    for spec in TOOL_SPECS:
        if spec["name"] == name:
            return spec
    raise AssertionError(f"{name!r} missing from TOOL_SPECS")


class ResearchToolArgumentTests(unittest.TestCase):
    """run_research_tool: `question` and its `query` alias dispatch the same."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(guard=True, log_requests=False)

    def _dispatch(self, arguments: dict[str, Any]) -> mock.MagicMock:
        with mock.patch("freegem.web_research.research_cli", return_value="RESEARCH OUT") as cli:
            out = run_research_tool(self.cfg, arguments)
        self.assertEqual(out, "RESEARCH OUT")
        return cli

    def test_question_argument_dispatches(self) -> None:
        cli = self._dispatch({**_MINIMAL_ARGS, "question": "the question"})
        self.assertEqual(cli.call_args.kwargs["query"], "the question")

    def test_query_alias_dispatches_identically(self) -> None:
        cli = self._dispatch({**_MINIMAL_ARGS, "query": "the question"})
        self.assertEqual(cli.call_args.kwargs["query"], "the question")

    def test_question_wins_when_both_are_given(self) -> None:
        cli = self._dispatch({**_MINIMAL_ARGS, "question": "primary", "query": "secondary"})
        self.assertEqual(cli.call_args.kwargs["query"], "primary")

    def test_missing_prompt_names_both_accepted_parameters(self) -> None:
        with self.assertRaises(ValueError) as caught:
            run_research_tool(self.cfg, _MINIMAL_ARGS)
        message = str(caught.exception)
        self.assertIn("'question'", message)
        self.assertIn("'query'", message)

    def test_empty_urls_list_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            run_research_tool(self.cfg, {"urls": [], "question": "q"})

    def test_non_list_urls_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            run_research_tool(self.cfg, {"urls": "https://a.test/", "question": "q"})


class ToolCatalogDescriptionTests(unittest.TestCase):
    """The advertised description leads with the real requirement (#73)."""

    def test_description_leads_with_the_urls_requirement(self) -> None:
        description = _spec("web_research")["description"]
        first_sentence = description.split(".")[0]
        self.assertTrue(description.startswith("Research pages YOU provide"))
        self.assertIn("does not search the web", first_sentence)
        self.assertIn("urls", first_sentence)
        self.assertIn("question", first_sentence)

    def test_description_names_the_query_alias(self) -> None:
        description = _spec("web_research")["description"]
        self.assertIn("query", description)
        self.assertIn("alias", description)

    def test_description_points_to_gemini_search_for_finding_pages(self) -> None:
        self.assertIn("gemini_search", _spec("web_research")["description"])

    def test_every_spec_still_carries_a_summary_and_description(self) -> None:
        for spec in TOOL_SPECS:
            with self.subTest(tool=spec["name"]):
                self.assertTrue(str(spec["summary"]).strip())
                self.assertTrue(str(spec["description"]).strip())


class FastMcpWebResearchSchemaTests(unittest.TestCase):
    """The generated input schema: urls required, question/query both offered."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig(guard=True, log_requests=False)

    def _server(self) -> Any:
        try:
            import mcp  # noqa: F401
        except ImportError:  # pragma: no cover - depends on install
            self.skipTest("mcp extra not installed")
        from freegem.mcp_tools import build_server

        return build_server(self.cfg)

    def test_schema_requires_urls_and_offers_the_alias(self) -> None:
        tools = asyncio.run(self._server().list_tools())
        tool = next(t for t in tools if t.name == "web_research")
        schema = tool.inputSchema
        self.assertEqual(schema["required"], ["urls"])
        self.assertIn("question", schema["properties"])
        self.assertIn("query", schema["properties"])

    def test_query_alias_flows_through_the_mcp_layer(self) -> None:
        server = self._server()
        with mock.patch("freegem.web_research.research_cli", return_value="ANSWERED"):
            result = asyncio.run(
                server.call_tool("web_research", {**_MINIMAL_ARGS, "query": "the question"})
            )
        self.assertIn("ANSWERED", str(result))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
