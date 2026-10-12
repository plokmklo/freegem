"""CLI tests.

``main(argv)`` is called directly with an argv list and returns an exit code,
so no subprocess is needed. Commands that would block or hit the network are
either short-circuited with mocks or exercised through the non-blocking
subcommands (``models``, ``doctor``).
"""

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest import mock

from freegem.cli import main
from freegem.config import BridgeConfig
from freegem.mcp_tools import run_tool


class VersionAndHelpTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_version(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_help_exits_zero(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_no_command_defaults_to_serve(self) -> None:
        """Bare invocation starts the server; assert it does not crash on parse."""
        with mock.patch("freegem.cli.cmd_serve", return_value=0) as serve:
            code, _, _ = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertTrue(serve.called)

    def test_models_command(self) -> None:
        code, out, _ = self.run_cli(["models"])
        self.assertEqual(code, 0)
        self.assertIn("gemini-3.6-flash", out)
        self.assertIn("gemini-3.5-flash-thinking", out)

    def test_unknown_command_rejected(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["frobnicate"])
        self.assertEqual(ctx.exception.code, 2)


class ConfigOverrideTests(unittest.TestCase):
    def test_cli_overrides_take_precedence(self) -> None:
        import argparse

        from freegem.cli import _apply_overrides

        args = argparse.Namespace(model="gemini-3.1-pro", cookie_file=None, proxy=None, quiet=True)
        cfg = BridgeConfig(default_model="gemini-3.6-flash")
        result = _apply_overrides(cfg, args)
        self.assertEqual(result.default_model, "gemini-3.1-pro")
        self.assertFalse(result.log_requests)  # --quiet

    def test_original_config_unmodified(self) -> None:
        import argparse

        from freegem.cli import _apply_overrides

        args = argparse.Namespace(model="gemini-auto", cookie_file=None, proxy=None, quiet=False)
        cfg = BridgeConfig(default_model="gemini-3.6-flash")
        _apply_overrides(cfg, args)
        self.assertEqual(cfg.default_model, "gemini-3.6-flash")


class AskCommandTests(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_ask_prints_answer(self) -> None:
        with mock.patch("freegem.gemini.generate_stream", return_value=iter(["the answer"])):
            code, out, _ = self.run_cli(["ask", "what", "is", "this"])
        self.assertEqual(code, 0)
        self.assertIn("the answer", out)

    def test_ask_search_routes_to_search_tool(self) -> None:
        with mock.patch("freegem.cli.run_tool", return_value="sourced") as run:
            code, _, _ = self.run_cli(["ask", "--search", "latest", "news"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args[0][1], "gemini_search")

    def test_ask_plain_streams_answer(self) -> None:
        """Plain ask streams: generate_stream is consumed and printed live."""
        with mock.patch(
            "freegem.gemini.generate_stream", return_value=iter(["hel", "lo wor", "ld"])
        ) as stream:
            code, out, _ = self.run_cli(["ask", "hello"])
        self.assertEqual(code, 0)
        self.assertIn("hello world", out)
        self.assertTrue(stream.called)

    def test_ask_plain_falls_back_without_httpx(self) -> None:
        with (
            mock.patch("freegem.gemini.HAS_HTTPX", False),
            mock.patch("freegem.gemini.generate", return_value="whole answer") as gen,
        ):
            code, out, _ = self.run_cli(["ask", "hello"])
        self.assertEqual(code, 0)
        self.assertIn("whole answer", out)
        self.assertTrue(gen.called)

    def test_ask_failure_returns_nonzero(self) -> None:
        from freegem.gemini import BridgeError

        with mock.patch("freegem.gemini.generate_stream", side_effect=BridgeError("no session")):
            code, _, err = self.run_cli(["ask", "hello"])
        self.assertEqual(code, 1)
        self.assertIn("no session", err)

    def test_empty_prompt_rejected(self) -> None:
        code, _, _ = self.run_cli(["ask", "   "])
        self.assertEqual(code, 2)


class DoctorCommandTests(unittest.TestCase):
    """Doctor must be read-only: it never sends a generation request."""

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_doctor_reports_config_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth = os.path.join(tmp, "auth.json")
            with open(auth, "w", encoding="utf-8") as fh:
                json.dump(
                    {"cookie": "SID=x; SAPISID=y", "sapisid": "y", "xsrf_token": "TOK"},
                    fh,
                )
            code, out, _ = self.run_cli(["--config", os.path.join(tmp, "missing.json"), "doctor"])
        self.assertEqual(code, 0)
        self.assertIn("Configuration", out)
        self.assertIn("gemini.google.com", out)
        self.assertNotIn("TOK", out)
        self.assertNotIn("SAPISID=y", out)

    def test_doctor_never_generates(self) -> None:
        with mock.patch("freegem.gemini.generate") as generate:
            self.run_cli(["doctor"])
        generate.assert_not_called()

    def test_doctor_with_cookie_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth = os.path.join(tmp, "gemini-auth.json")
            with open(auth, "w", encoding="utf-8") as fh:
                json.dump({"cookie": "SID=a; SAPISID=b", "sapisid": "b"}, fh)
            code, out, _ = self.run_cli(
                ["--cookie-file", auth, "--config", os.path.join(tmp, "none.json"), "doctor"]
            )
        self.assertEqual(code, 0)
        self.assertIn("cookie file", out)

    def test_doctor_flags_missing_cookie(self) -> None:
        code, out, _ = self.run_cli(
            ["--cookie-file", "/no/such/auth.json", "--config", "/no/such/cfg.json", "doctor"]
        )
        self.assertEqual(code, 1)
        self.assertIn("MISSING", out)


class ServeCommandTests(unittest.TestCase):
    def test_non_loopback_bind_warns(self) -> None:
        """The warning is the only guard against exposing a session."""
        import argparse

        from freegem.cli import cmd_serve

        args = argparse.Namespace(port=None, host="0.0.0.0", no_refresh=True)
        cfg = BridgeConfig(host="0.0.0.0")

        server = mock.MagicMock()
        with (
            mock.patch("freegem.server.ThreadedServer", return_value=server),
            mock.patch("freegem.gemini.refresh_build_tag", return_value=(cfg, None)),
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                cmd_serve(cfg, args)
        self.assertIn("WARNING", err.getvalue())
        self.assertIn("Google session", err.getvalue())

    def test_loopback_bind_is_silent(self) -> None:
        import argparse

        from freegem.cli import cmd_serve

        args = argparse.Namespace(port=None, host="127.0.0.1", no_refresh=True)
        cfg = BridgeConfig()
        server = mock.MagicMock()
        with (
            mock.patch("freegem.server.ThreadedServer", return_value=server),
            mock.patch("freegem.gemini.refresh_build_tag", return_value=(cfg, None)),
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                cmd_serve(cfg, args)
        self.assertNotIn("WARNING", err.getvalue())


class McpDispatchTests(unittest.TestCase):
    """``run_tool`` is the pure core of the MCP face; test it without ``mcp``."""

    def setUp(self) -> None:
        self.cfg = BridgeConfig()

    def test_list_models(self) -> None:
        out = run_tool(self.cfg, "gemini_list_models", {})
        self.assertIn("gemini-3.6-flash", out)
        self.assertIn("anonymous", out)

    def test_search_prompt_includes_source_rules(self) -> None:
        with mock.patch("freegem.mcp_tools.generate", return_value="answer") as generate:
            run_tool(self.cfg, "gemini_search", {"query": "python version"})
        prompt = generate.call_args[0][1]
        self.assertIn("Cite sources inline", prompt)
        self.assertIn("python version", prompt)

    def test_search_recency_added(self) -> None:
        with mock.patch("freegem.mcp_tools.generate", return_value="a"):
            with mock.patch("freegem.mcp_tools.generate") as generate:
                run_tool(self.cfg, "gemini_search", {"query": "q", "recency": "today"})
        self.assertIn("today", generate.call_args[0][1])

    def test_ask_system_prepended(self) -> None:
        with mock.patch("freegem.mcp_tools.generate") as generate:
            generate.return_value = "a"
            run_tool(self.cfg, "gemini_ask", {"question": "q", "system": "be terse"})
        self.assertTrue(generate.call_args[0][1].startswith("be terse"))

    def test_advisor_structure(self) -> None:
        with mock.patch("freegem.mcp_tools.generate") as generate:
            generate.return_value = "a"
            run_tool(
                self.cfg,
                "gemini_advisor",
                {"topic": "buy or build", "context": "ctx", "constraints": "budget"},
            )
        prompt = generate.call_args[0][1]
        for section in (
            "Recommendation",
            "Options",
            "Tradeoffs",
            "Risks",
            "What would change my mind",
        ):
            self.assertIn(section, prompt)
        self.assertIn("ctx", prompt)
        self.assertIn("budget", prompt)

    def test_chat_roles_rendered(self) -> None:
        with mock.patch("freegem.mcp_tools.generate") as generate:
            generate.return_value = "a"
            run_tool(
                self.cfg,
                "gemini_chat",
                {
                    "messages": [
                        {"role": "system", "content": "sys"},
                        {"role": "user", "content": "usr"},
                    ]
                },
            )
        prompt = generate.call_args[0][1]
        self.assertIn("[System instruction]: sys", prompt)
        self.assertIn("usr", prompt)

    def test_deep_think_uses_thinking_model(self) -> None:
        """generate(cfg, prompt, mode, think, ...): mode 2 is THINKING."""
        with mock.patch("freegem.mcp_tools.generate") as generate:
            generate.return_value = "a"
            run_tool(self.cfg, "gemini_deep_think", {"problem": "hard"})
        self.assertEqual(generate.call_args[0][2], 2)  # mode_id
        self.assertEqual(generate.call_args[0][3], 0)  # think depth: deepest

    def test_unknown_tool_raises(self) -> None:
        with self.assertRaises(ValueError):
            run_tool(self.cfg, "nope", {})

    def test_empty_answer_becomes_error(self) -> None:
        from freegem.gemini import BridgeError

        with mock.patch("freegem.mcp_tools.generate", return_value=""):
            with self.assertRaises(BridgeError):
                run_tool(self.cfg, "gemini_ask", {"question": "q"})

    def test_model_override_respected(self) -> None:
        with mock.patch("freegem.mcp_tools.generate") as generate:
            generate.return_value = "a"
            run_tool(self.cfg, "gemini_ask", {"question": "q", "model": "gemini-3.1-pro"})
        self.assertEqual(generate.call_args[0][2], 3)  # PRO mode


class McpBuildTests(unittest.TestCase):
    def test_build_server_requires_mcp_package(self) -> None:
        """Without the optional dep the error must say how to install it."""
        import freegem.mcp_tools as mt

        with mock.patch.object(
            mt,
            "build_server",
            side_effect=ImportError(
                "the 'mcp' package is required for the MCP face. "
                "Install it with:  pip install 'freegem[mcp]'"
            ),
        ):
            with self.assertRaises(ImportError) as ctx:
                mt.build_server(BridgeConfig())
        self.assertIn("freegem[mcp]", str(ctx.exception))

    def test_server_info_reports_our_version_not_sdk(self) -> None:
        """serverInfo.version must be freegem's, not the mcp package's.

        FastMCP has no version= kwarg, so an unset ``_mcp_server.version``
        makes the SDK fall back to its own package version -- clients would
        have logged e.g. ``1.28.1`` as our release.
        """
        import freegem as gb
        from freegem.mcp_tools import build_server

        try:
            server = build_server(BridgeConfig())
            self.assertEqual(server._mcp_server.version, gb.__version__)
        except ImportError:
            self.skipTest("mcp package not installed")


class FlagPlacementTests(unittest.TestCase):
    """Global flags must work before AND after the subcommand.

    argparse gives subparser defaults precedence over values the main parser
    already parsed, so the SUPPRESS defaults are load-bearing: without them
    `freegem --quiet serve` silently lost --quiet.
    """

    def parse(self, argv: list[str]) -> Any:
        from freegem.cli import _build_parser

        return _build_parser().parse_args(argv)

    def test_quiet_after_subcommand(self) -> None:
        args = self.parse(["serve", "--quiet"])
        self.assertTrue(getattr(args, "quiet", False))

    def test_quiet_before_subcommand(self) -> None:
        args = self.parse(["--quiet", "serve"])
        self.assertTrue(getattr(args, "quiet", False))

    def test_quiet_default_is_false(self) -> None:
        args = self.parse(["serve"])
        self.assertFalse(getattr(args, "quiet", False))

    def test_config_after_subcommand(self) -> None:
        args = self.parse(["ask", "--config", "x.json", "hi"])
        self.assertEqual(getattr(args, "config", None), "x.json")

    def test_crawl_run_model_after_nested_subcommand(self) -> None:
        # Residual gap: the crawl SUB-subparsers (plan/run) parsed from a
        # parent that lacked the common flags, so the model flag failed only
        # when placed after "run" -- the exact place users put it.
        args = self.parse(
            ["crawl", "run", "--model", "gemini-2.5-flash", "--seeds", "https://example.com"]
        )
        self.assertEqual(getattr(args, "model", None), "gemini-2.5-flash")

    def test_crawl_plan_config_after_nested_subcommand(self) -> None:
        args = self.parse(["crawl", "plan", "--config", "x.json", "--seeds", "https://e.com"])
        self.assertEqual(getattr(args, "config", None), "x.json")

    def test_config_before_subcommand(self) -> None:
        args = self.parse(["--config", "x.json", "doctor"])
        self.assertEqual(getattr(args, "config", None), "x.json")

    def test_later_occurrence_wins(self) -> None:
        args = self.parse(["--config", "a.json", "serve", "--config", "b.json"])
        self.assertEqual(getattr(args, "config", None), "b.json")

    def test_main_end_to_end_after_subcommand(self) -> None:
        """A flag after the subcommand must reach the config, not argparse's error."""
        import io as _io
        from contextlib import redirect_stderr, redirect_stdout

        from freegem.cli import main

        out, err = _io.StringIO(), _io.StringIO()
        with (
            mock.patch("gemini_bridge.cli.cmd_serve", return_value=0)
            if False
            else mock.patch("freegem.cli.cmd_serve", return_value=0) as serve,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = main(["serve", "--quiet"])
        self.assertEqual(code, 0)
        cfg = serve.call_args[0][0]
        self.assertFalse(cfg.log_requests)


if __name__ == "__main__":
    unittest.main()


class ScrapeBatchCliTests(unittest.TestCase):
    """Batch scrape at the CLI: JSONL output, cache flags, honest exit codes."""

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def _items(self) -> list[Any]:
        from freegem.web_scraper import BatchItem

        return [
            BatchItem(
                url="https://a.example/",
                ok=True,
                title="A",
                text="alpha page",
                chars=10,
                via="html",
            ),
            BatchItem(url="https://b.example/", ok=False, error="HTTP 403 fetching 'u'"),
        ]

    def test_batch_jsonl_output(self) -> None:
        with mock.patch("freegem.web_scraper.scrape_many", return_value=self._items()) as many:
            code, out, _ = self.run_cli(
                ["scrape", "https://a.example/", "https://b.example/", "--format", "jsonl"]
            )
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in out.strip().splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0]["ok"])
        self.assertEqual(lines[0]["title"], "A")
        self.assertIn("alpha page", lines[0]["text"])
        self.assertFalse(lines[1]["ok"])
        self.assertIn("HTTP 403", lines[1]["error"])
        self.assertEqual(many.call_args.kwargs["concurrency"], 4)

    def test_batch_text_output_marks_failures(self) -> None:
        with mock.patch("freegem.web_scraper.scrape_many", return_value=self._items()):
            code, out, _ = self.run_cli(["scrape", "https://a.example/", "https://b.example/"])
        self.assertEqual(code, 0)
        self.assertIn("alpha page", out)
        self.assertIn("error: HTTP 403", out)

    def test_batch_all_failed_exits_one(self) -> None:
        from freegem.web_scraper import BatchItem

        dead = [BatchItem(url="https://x.example/", ok=False, error="HTTP 403")]
        with mock.patch("freegem.web_scraper.scrape_many", return_value=dead):
            code, _, _ = self.run_cli(["scrape", "https://x.example/"])
        self.assertEqual(code, 1)

    def test_cache_flags_reach_the_scraper(self) -> None:
        with mock.patch("freegem.web_scraper.scrape_cli", return_value="ok") as scrape:
            code, _, _ = self.run_cli(
                ["scrape", "https://a.example/", "--refresh", "--cache-ttl", "60"]
            )
        self.assertEqual(code, 0)
        self.assertIsNotNone(scrape.call_args.kwargs["cache"])
        self.assertTrue(scrape.call_args.kwargs["refresh"])
        self.assertEqual(scrape.call_args.kwargs["cache"].ttl, 60)

    def test_no_cache_skips_cache_entirely(self) -> None:
        with mock.patch("freegem.web_scraper.scrape_cli", return_value="ok") as scrape:
            self.run_cli(["scrape", "https://a.example/", "--no-cache"])
        self.assertIsNone(scrape.call_args.kwargs["cache"])

    def test_ask_auto_uses_200k_budget(self) -> None:
        with (
            mock.patch("freegem.web_scraper.scrape_cli", return_value="ok") as scrape,
            mock.patch("freegem.web_scraper._ask_gemini", return_value="answer"),
        ):
            self.run_cli(["scrape", "https://a.example/", "--ask", "what?"])
        self.assertEqual(scrape.call_args.kwargs["max_chars"], 200_000)

    def test_no_max_chars_default_is_50k(self) -> None:
        with mock.patch("freegem.web_scraper.scrape_cli", return_value="ok") as scrape:
            self.run_cli(["scrape", "https://a.example/"])
        self.assertEqual(scrape.call_args.kwargs["max_chars"], 50_000)

    def test_no_smart_disables_ladder(self) -> None:
        with mock.patch("freegem.web_scraper.scrape_cli", return_value="ok") as scrape:
            self.run_cli(["scrape", "https://a.example/", "--no-smart"])
        self.assertFalse(scrape.call_args.kwargs["smart"])

    def test_ask_rejected_for_batch(self) -> None:
        code, _, err = self.run_cli(
            ["scrape", "https://a.example/", "https://b.example/", "--ask", "what?"]
        )
        self.assertEqual(code, 2)
        self.assertIn("--ask works with a single URL", err)
