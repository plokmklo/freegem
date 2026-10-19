"""OpenAI-client compatibility surface.

Grounded in what clients actually send this bridge (SillyTavern, JanitorAI,
RAG stacks, legacy tooling): hardcoded model names, legacy /v1/completions,
embeddings probes, and the OpenAI `n` parameter. Each behavior here is
honest: aliases route where the config says, unsupported things say so in
JSON instead of dying in a catch-all.
"""

from __future__ import annotations

import http.client
import json
import unittest
from typing import Any, ClassVar
from unittest import mock

from freegem.config import BridgeConfig
from freegem.tests.test_server import HermeticServerTestCase


def loads(raw: str | bytes) -> dict[str, Any]:
    return dict(json.loads(raw))


def new_conn(port: int) -> http.client.HTTPConnection:
    return http.client.HTTPConnection("127.0.0.1", port, timeout=10)


class CompatServerTestCase(HermeticServerTestCase):
    _aliases: ClassVar[dict[str, str]] = {}

    def _make_config(self) -> BridgeConfig:
        return BridgeConfig(log_requests=False, model_aliases=dict(self._aliases))

    BODY: ClassVar[dict[str, Any]] = {
        "model": "gemini-3.6-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }


class EmbeddingsTests(CompatServerTestCase):
    def test_embeddings_is_an_honest_501(self) -> None:
        """RAG stacks get the real reason, not the chat catch-all's 400."""
        status, _, raw = self.post("/v1/embeddings", {"model": "x", "input": "hi"})
        self.assertEqual(status, 501)
        message = str(loads(raw)["error"]["message"])
        self.assertIn("embeddings", message)
        self.assertIn("web session", message)

    def test_embeddings_drains_body_before_answering(self) -> None:
        """A second request on the same connection must not see leftovers."""
        conn = new_conn(self.port)
        try:
            conn.request(
                "POST",
                "/v1/embeddings",
                body='{"input": "hi"}',
                headers={"Content-Type": "application/json"},
            )
            first = conn.getresponse()
            first.read()
            self.assertEqual(first.status, 501)
            conn.request("GET", "/health")
            second = conn.getresponse()
            second.read()
            self.assertEqual(second.status, 200)
        finally:
            conn.close()


class LegacyCompletionsTests(CompatServerTestCase):
    @mock.patch("freegem.server.generate")
    def test_single_prompt_returns_text_choice(self, generate: Any) -> None:
        generate.return_value = "the completion"
        status, _, raw = self.post(
            "/v1/completions", {"model": "gemini-3.6-flash", "prompt": "say hi"}
        )
        self.assertEqual(status, 200)
        payload = loads(raw)
        self.assertEqual(payload["object"], "text_completion")
        self.assertTrue(payload["id"].startswith("cmpl-"))
        self.assertEqual(payload["choices"][0]["text"], "the completion")
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")

    @mock.patch("freegem.server.generate")
    def test_one_element_prompt_list_is_accepted(self, generate: Any) -> None:
        generate.return_value = "ok"
        status, _, raw = self.post(
            "/v1/completions", {"model": "gemini-3.6-flash", "prompt": ["hello"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(loads(raw)["choices"][0]["text"], "ok")

    def test_streaming_legacy_is_an_honest_400(self) -> None:
        status, _, raw = self.post(
            "/v1/completions",
            {"model": "gemini-3.6-flash", "prompt": "hi", "stream": True},
        )
        self.assertEqual(status, 400)
        self.assertIn("non-streaming", loads(raw)["error"]["message"])

    def test_multi_prompt_list_is_an_honest_400(self) -> None:
        status, _, _ = self.post(
            "/v1/completions",
            {"model": "gemini-3.6-flash", "prompt": ["a", "b"]},
        )
        self.assertEqual(status, 400)

    def test_empty_prompt_is_an_honest_400(self) -> None:
        status, _, _ = self.post("/v1/completions", {"model": "gemini-3.6-flash", "prompt": "   "})
        self.assertEqual(status, 400)


class ChoiceCountTests(CompatServerTestCase):
    def test_chat_n_greater_than_one_is_an_honest_400(self) -> None:
        status, _, raw = self.post("/v1/chat/completions", {**self.BODY, "n": 3})
        self.assertEqual(status, 400)
        self.assertIn("n=1", loads(raw)["error"]["message"])

    def test_chat_n_one_is_accepted(self) -> None:
        with mock.patch("freegem.server.generate", return_value="ok"):
            status, _, _ = self.post("/v1/chat/completions", {**self.BODY, "n": 1})
        self.assertEqual(status, 200)


class ModelAliasTests(CompatServerTestCase):
    _aliases: ClassVar[dict[str, str]] = {
        "gpt-4o": "gemini-3.6-flash",
        "my-writer": "gemini-3.5-flash-thinking",
    }

    @mock.patch("freegem.server.generate")
    def test_alias_routes_and_echoes_resolved_name(self, generate: Any) -> None:
        generate.return_value = "ok"
        status, _, raw = self.post("/v1/chat/completions", {**self.BODY, "model": "gpt-4o"})
        self.assertEqual(status, 200)
        self.assertEqual(loads(raw)["model"], "gemini-3.6-flash")

    @mock.patch("freegem.server.generate")
    def test_unknown_name_still_falls_back_to_default(self, generate: Any) -> None:
        """The Codex-CLI fallback survives: unknown names hit the default."""
        generate.return_value = "ok"
        status, _, raw = self.post("/v1/chat/completions", {**self.BODY, "model": "gpt-99-turbo"})
        self.assertEqual(status, 200)
        self.assertEqual(loads(raw)["model"], "gemini-3.6-flash")

    @mock.patch("freegem.server.generate")
    def test_alias_can_carry_think_mode(self, generate: Any) -> None:
        generate.return_value = "ok"
        status, _, raw = self.post("/v1/chat/completions", {**self.BODY, "model": "my-writer"})
        self.assertEqual(status, 200)
        self.assertEqual(loads(raw)["model"], "gemini-3.5-flash-thinking")


class AliasConfigTests(unittest.TestCase):
    def test_config_accepts_dict_and_compact_string(self) -> None:
        cfg = BridgeConfig.from_dict({"model_aliases": {"a": "b"}})
        self.assertEqual(cfg.model_aliases, {"a": "b"})
        cfg = BridgeConfig.from_dict({"model_aliases": "a=b,c=d"})
        self.assertEqual(cfg.model_aliases, {"a": "b", "c": "d"})

    def test_alias_maps_are_never_shared(self) -> None:
        a = BridgeConfig()
        b = BridgeConfig()
        self.assertIsNot(a.model_aliases, b.model_aliases)

    def test_malformed_compact_string_is_dropped(self) -> None:
        cfg = BridgeConfig.from_dict({"model_aliases": "a=b,oops"})
        self.assertEqual(cfg.model_aliases, {})


if __name__ == "__main__":
    unittest.main()
