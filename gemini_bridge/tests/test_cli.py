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
from unittest import mock

from gemini_bridge.cli import main
from gemini_bridge.config import BridgeConfig
from gemini_bridge.mcp_tools import run_tool


class VersionAndHelpTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_version(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["--version"])
        self.assertEqual(ctx.exception.code, 0)

    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_no_command_defaults_to_serve(self):
        """Bare invocation starts the server; assert it does not crash on parse."""
        with mock.patch("gemini_bridge.cli.cmd_serve", return_value=0) as serve:
            code, _, _ = self.run_cli([])
        self.assertEqual(code, 0)
        self.assertTrue(serve.called)

    def test_models_command(self):
        code, out, _ = self.run_cli(["models"])
        self.assertEqual(code, 0)
        self.assertIn("gemini-3.6-flash", out)
        self.assertIn("gemini-3.5-flash-thinking", out)

    def test_unknown_command_rejected(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(["frobnicate"])
        self.assertEqual(ctx.exception.code, 2)


class ConfigOverrideTests(unittest.TestCase):
    def test_cli_overrides_take_precedence(self):
        import argparse

        from gemini_bridge.cli import _apply_overrides

        args = argparse.Namespace(
            model="gemini-3.1-pro", cookie_file=None, proxy=None, quiet=True
        )
        cfg = BridgeConfig(default_model="gemini-3.6-flash")
        result = _apply_overrides(cfg, args)
        self.assertEqual(result.default_model, "gemini-3.1-pro")
        self.assertFalse(result.log_requests)  # --quiet

    def test_original_config_unmodified(self):
        import argparse

        from gemini_bridge.cli import _apply_overrides

        args = argparse.Namespace(model="gemini-auto", cookie_file=None, proxy=None, quiet=False)
        cfg = BridgeConfig(default_model="gemini-3.6-flash")
        _apply_overrides(cfg, args)
        self.assertEqual(cfg.default_model, "gemini-3.6-flash")


class AskCommandTests(unittest.TestCase):
    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_ask_prints_answer(self):
        with mock.patch("gemini_bridge.mcp_tools._run", return_value="the answer"):
            code, out, _ = self.run_cli(["ask", "what", "is", "this"])
        self.assertEqual(code, 0)
        self.assertIn("the answer", out)

    def test_ask_search_routes_to_search_tool(self):
        with mock.patch("gemini_bridge.cli.run_tool", return_value="sourced") as run:
            code, _, _ = self.run_cli(["ask", "--search", "latest", "news"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args[0][1], "gemini_search")

    def test_ask_plain_routes_to_ask_tool(self):
        with mock.patch("gemini_bridge.cli.run_tool", return_value="plain") as run:
            self.run_cli(["ask", "hello"])
        self.assertEqual(run.call_args[0][1], "gemini_ask")

    def test_ask_failure_returns_nonzero(self):
        from gemini_bridge.gemini import BridgeError

        with mock.patch(
            "gemini_bridge.cli.run_tool", side_effect=BridgeError("no session")
        ):
            code, _, err = self.run_cli(["ask", "hello"])
        self.assertEqual(code, 1)
        self.assertIn("no session", err)

    def test_empty_prompt_rejected(self):
        code, _, _ = self.run_cli(["ask", "   "])
        self.assertEqual(code, 2)


class DoctorCommandTests(unittest.TestCase):
    """Doctor must be read-only: it never sends a generation request."""

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_doctor_reports_config_without_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = os.path.join(tmp, "auth.json")
            with open(auth, "w", encoding="utf-8") as fh:
                json.dump(
                    {"cookie": "SID=x; SAPISID=y", "sapisid": "y", "xsrf_token": "TOK"},
                    fh,
                )
            code, out, _ = self.run_cli(
                ["--config", os.path.join(tmp, "missing.json"), "doctor"]
            )
        self.assertEqual(code, 0)
        self.assertIn("Configuration", out)
        self.assertIn("gemini.google.com", out)
        self.assertNotIn("TOK", out)
        self.assertNotIn("SAPISID=y", out)

    def test_doctor_never_generates(self):
        with mock.patch("gemini_bridge.gemini.generate") as generate:
            self.run_cli(["doctor"])
        generate.assert_not_called()

    def test_doctor_with_cookie_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = os.path.join(tmp, "gemini-auth.json")
            with open(auth, "w", encoding="utf-8") as fh:
                json.dump({"cookie": "SID=a; SAPISID=b", "sapisid": "b"}, fh)
            code, out, _ = self.run_cli(
                ["--cookie-file", auth, "--config", os.path.join(tmp, "none.json"), "doctor"]
            )
        self.assertEqual(code, 0)
        self.assertIn("cookie file", out)

    def test_doctor_flags_missing_cookie(self):
        code, out, _ = self.run_cli(
            ["--cookie-file", "/no/such/auth.json", "--config", "/no/such/cfg.json", "doctor"]
        )
        self.assertEqual(code, 1)
        self.assertIn("MISSING", out)


class ServeCommandTests(unittest.TestCase):
    def test_non_loopback_bind_warns(self):
        """The warning is the only guard against exposing a session."""
        from gemini_bridge.cli import cmd_serve
        import argparse

        args = argparse.Namespace(port=None, host="0.0.0.0", no_refresh=True)
        cfg = BridgeConfig(host="0.0.0.0")

        server = mock.MagicMock()
        with mock.patch("gemini_bridge.server.ThreadedServer", return_value=server), \
             mock.patch("gemini_bridge.gemini.refresh_build_tag", return_value=(cfg, None)):
            err = io.StringIO()
            with redirect_stderr(err):
                cmd_serve(cfg, args)
        self.assertIn("WARNING", err.getvalue())
        self.assertIn("Google session", err.getvalue())

    def test_loopback_bind_is_silent(self):
        from gemini_bridge.cli import cmd_serve
        import argparse

        args = argparse.Namespace(port=None, host="127.0.0.1", no_refresh=True)
        cfg = BridgeConfig()
        server = mock.MagicMock()
        with mock.patch("gemini_bridge.server.ThreadedServer", return_value=server), \
             mock.patch("gemini_bridge.gemini.refresh_build_tag", return_value=(cfg, None)):
            err = io.StringIO()
            with redirect_stderr(err):
                cmd_serve(cfg, args)
        self.assertNotIn("WARNING", err.getvalue())


class McpDispatchTests(unittest.TestCase):
    """``run_tool`` is the pure core of the MCP face; test it without ``mcp``."""

    def setUp(self):
        self.cfg = BridgeConfig()

    def test_list_models(self):
        out = run_tool(self.cfg, "gemini_list_models", {})
        self.assertIn("gemini-3.6-flash", out)
        self.assertIn("anonymous", out)

    def test_search_prompt_includes_source_rules(self):
        with mock.patch("gemini_bridge.mcp_tools.generate", return_value="answer") as generate:
            run_tool(self.cfg, "gemini_search", {"query": "python version"})
        prompt = generate.call_args[0][1]
        self.assertIn("Cite sources inline", prompt)
        self.assertIn("python version", prompt)

    def test_search_recency_added(self):
        with mock.patch("gemini_bridge.mcp_tools.generate", return_value="a"):
            with mock.patch("gemini_bridge.mcp_tools.generate") as generate:
                run_tool(self.cfg, "gemini_search", {"query": "q", "recency": "today"})
        self.assertIn("today", generate.call_args[0][1])

    def test_ask_system_prepended(self):
        with mock.patch("gemini_bridge.mcp_tools.generate") as generate:
            generate.return_value = "a"
            run_tool(self.cfg, "gemini_ask", {"question": "q", "system": "be terse"})
        self.assertTrue(generate.call_args[0][1].startswith("be terse"))

    def test_advisor_structure(self):
        with mock.patch("gemini_bridge.mcp_tools.generate") as generate:
            generate.return_value = "a"
            run_tool(
                self.cfg,
                "gemini_advisor",
                {"topic": "buy or build", "context": "ctx", "constraints": "budget"},
            )
        prompt = generate.call_args[0][1]
        for section in ("Recommendation", "Options", "Tradeoffs", "Risks", "What would change my mind"):
            self.assertIn(section, prompt)
        self.assertIn("ctx", prompt)
        self.assertIn("budget", prompt)

    def test_chat_roles_rendered(self):
        with mock.patch("gemini_bridge.mcp_tools.generate") as generate:
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

    def test_deep_think_uses_thinking_model(self):
        """generate(cfg, prompt, mode, think, ...): mode 2 is THINKING."""
        with mock.patch("gemini_bridge.mcp_tools.generate") as generate:
            generate.return_value = "a"
            run_tool(self.cfg, "gemini_deep_think", {"problem": "hard"})
        self.assertEqual(generate.call_args[0][2], 2)  # mode_id
        self.assertEqual(generate.call_args[0][3], 0)  # think depth: deepest

    def test_unknown_tool_raises(self):
        with self.assertRaises(ValueError):
            run_tool(self.cfg, "nope", {})

    def test_empty_answer_becomes_error(self):
        from gemini_bridge.gemini import BridgeError

        with mock.patch("gemini_bridge.mcp_tools.generate", return_value=""):
            with self.assertRaises(BridgeError):
                run_tool(self.cfg, "gemini_ask", {"question": "q"})

    def test_model_override_respected(self):
        with mock.patch("gemini_bridge.mcp_tools.generate") as generate:
            generate.return_value = "a"
            run_tool(self.cfg, "gemini_ask", {"question": "q", "model": "gemini-3.1-pro"})
        self.assertEqual(generate.call_args[0][2], 3)  # PRO mode
class McpBuildTests(unittest.TestCase):
    def test_build_server_requires_mcp_package(self):
        """Without the optional dep the error must say how to install it."""
        import gemini_bridge.mcp_tools as mt

        with mock.patch.object(mt, "build_server", side_effect=ImportError(
            "the 'mcp' package is required for the MCP face. "
            "Install it with:  pip install 'gemini-bridge[mcp]'"
        )):
            with self.assertRaises(ImportError) as ctx:
                mt.build_server(BridgeConfig())
        self.assertIn("gemini-bridge[mcp]", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
