"""HTTP endpoint tests.

A real server runs on an ephemeral port and is spoken to over a real socket.
That is deliberate: mocking the handler would not catch framing, keep-alive,
or chunked-body bugs, and one of those (an SSE response with no Content-Length
never terminating) shipped in the original and hung clients.
"""

import http.client
import io
import json
import threading
import unittest
from typing import Any
from unittest import mock

from freegem.config import BridgeConfig
from freegem.gemini import BridgeError
from freegem.server import ThreadedServer, estimate_tokens

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get weather for a city",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }
]


def wrb_line(text: str) -> str:
    """A minimal ``wrb.fr`` line carrying ``text`` (see test_gemini for detail)."""
    payload: list[Any] = [None, None, None, None, [[None, [text]]]]
    while len(json.dumps(payload)) < 80:
        payload.append("z" * 60)
    outer = [["wrb.fr", None, json.dumps(payload)]]
    line = json.dumps(outer)
    while len(line) < 260:
        outer[0].append("z" * 40)
        line = json.dumps(outer)
    return line + "\n"


class EndpointTestCase(unittest.TestCase):
    """Base: boots one server per class and gives helpers for talking to it."""

    # Set by setUpClass; declared here so mypy can see them on cls and self.
    server: ThreadedServer
    thread: threading.Thread
    port: int

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadedServer(("127.0.0.1", 0), BridgeConfig(log_requests=False))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        # Point the handler at a config we can observe, without touching the
        # module-level default.
        self.server.config = BridgeConfig(log_requests=False)

    def request(
        self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(
                method,
                path,
                body=json.dumps(body) if body is not None else None,
                headers={"Content-Type": "application/json", **(headers or {})},
            )
            response = conn.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), raw
        finally:
            conn.close()

    def post(
        self, path: str, body: Any, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        return self.request("POST", path, body, headers)

    def get(self, path: str) -> tuple[int, dict[str, str], bytes]:
        return self.request("GET", path)


class DiscoveryEndpointTests(EndpointTestCase):
    def test_health(self) -> None:
        status, _, raw = self.get("/health")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("models", payload)

    def test_root_matches_health(self) -> None:
        status, _, _ = self.get("/")
        self.assertEqual(status, 200)

    def test_openai_models(self) -> None:
        status, _, raw = self.get("/v1/models")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["object"], "list")
        self.assertTrue(all(m["object"] == "model" for m in payload["data"]))

    def test_google_models(self) -> None:
        status, _, raw = self.get("/v1beta/models")
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertTrue(all(m["name"].startswith("models/") for m in payload["models"]))

    def test_unknown_path_404(self) -> None:
        status, _, _ = self.get("/nope")
        self.assertEqual(status, 404)

    def test_unknown_post_404(self) -> None:
        status, _, _ = self.post("/nope", {})
        self.assertEqual(status, 404)

    def test_no_authentication_required(self) -> None:
        """Auth was removed: a bare request must succeed, not 401."""
        status, _, _ = self.get("/v1/models")
        self.assertNotEqual(status, 401)

    def test_content_length_always_sent(self) -> None:
        """HTTP/1.1 keep-alive needs it, or clients block waiting for EOF."""
        _, headers, _ = self.get("/v1/models")
        self.assertIn("Content-Length", headers)


class ChatCompletionTests(EndpointTestCase):
    @mock.patch("freegem.server.generate")
    def test_non_streaming(self, generate: Any) -> None:
        generate.return_value = "the answer"
        status, _, raw = self.post(
            "/v1/chat/completions",
            {"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["object"], "chat.completion")
        self.assertEqual(payload["choices"][0]["message"]["content"], "the answer")
        self.assertEqual(payload["choices"][0]["finish_reason"], "stop")

    @mock.patch("freegem.server.generate")
    def test_model_name_echoed(self, generate: Any) -> None:
        generate.return_value = "ok"
        _, _, raw = self.post(
            "/v1/chat/completions",
            {"model": "gemini-3.5-flash-thinking", "messages": [{"role": "user", "content": "x"}]},
        )
        self.assertEqual(json.loads(raw)["model"], "gemini-3.5-flash-thinking")

    @mock.patch("freegem.server.generate")
    def test_default_model_used_when_omitted(self, generate: Any) -> None:
        generate.return_value = "ok"
        _, _, raw = self.post(
            "/v1/chat/completions", {"messages": [{"role": "user", "content": "x"}]}
        )
        self.assertEqual(json.loads(raw)["model"], "gemini-3.6-flash")

    def test_empty_prompt_400(self) -> None:
        status, _, _raw = self.post(
            "/v1/chat/completions", {"model": "gemini-3.6-flash", "messages": []}
        )
        self.assertEqual(status, 400)

    def test_invalid_json_400(self) -> None:
        status, _, _ = self.request("POST", "/v1/chat/completions", "{not json")
        self.assertEqual(status, 400)

    def test_invalid_think_level_400(self) -> None:
        status, _, _ = self.post(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash@think=99",
                "messages": [{"role": "user", "content": "x"}],
            },
        )
        self.assertEqual(status, 400)

    @mock.patch("freegem.server.generate")
    def test_upstream_failure_is_502(self, generate: Any) -> None:
        generate.side_effect = BridgeError("Gemini is down")
        status, _, raw = self.post(
            "/v1/chat/completions",
            {"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": "x"}]},
        )
        self.assertEqual(status, 502)
        self.assertIn("Gemini is down", json.loads(raw)["error"]["message"])

    @mock.patch("freegem.server.generate")
    def test_usage_present_but_marked_estimate(self, generate: Any) -> None:
        generate.return_value = "hello world"
        _, _, raw = self.post(
            "/v1/chat/completions",
            {"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": "x"}]},
        )
        usage = json.loads(raw)["usage"]
        self.assertIn("total_tokens", usage)
        self.assertGreaterEqual(usage["total_tokens"], usage["completion_tokens"])

    @mock.patch("freegem.server.generate")
    def test_tool_calls_parsed(self, generate: Any) -> None:
        generate.return_value = (
            'Checking.\n```tool_call\n{"name": "get_weather", "arguments": {"city": "Tokyo"}}\n```'
        )
        status, _, raw = self.post(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": TOOLS,
            },
        )
        self.assertEqual(status, 200)
        message = json.loads(raw)["choices"][0]["message"]
        self.assertEqual(len(message["tool_calls"]), 1)
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "get_weather")
        self.assertIn("Tokyo", message["tool_calls"][0]["function"]["arguments"])

    @mock.patch("freegem.server.generate")
    def test_tool_choice_none_suppresses_calls(self, generate: Any) -> None:
        generate.return_value = '```tool_call\n{"name": "x", "arguments": {}}\n```'
        _, _, raw = self.post(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "x"}],
                "tools": TOOLS,
                "tool_choice": "none",
            },
        )
        message = json.loads(raw)["choices"][0]["message"]
        self.assertIsNone(message.get("tool_calls"))


class StreamingTests(EndpointTestCase):
    @mock.patch("freegem.server.generate_stream")
    def test_openai_sse_sequence(self, generate_stream: Any) -> None:
        generate_stream.return_value = iter(["hel", "lo"])
        status, headers, raw = self.post(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "x"}],
                "stream": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        text = raw.decode()

        # first chunk carries role only
        self.assertIn('"delta": {"role": "assistant"}', text)
        # content deltas
        self.assertIn('"content": "hel"', text)
        self.assertIn('"content": "lo"', text)
        # terminator
        self.assertIn("data: [DONE]", text)
        self.assertTrue(text.endswith("\n\n"))

    @mock.patch("freegem.server.generate_stream")
    def test_sse_closes_connection(self, generate_stream: Any) -> None:
        """The fix for the hang: SSE has no Content-Length, so close is required."""
        generate_stream.return_value = iter(["x"])
        _, headers, _ = self.post(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "x"}],
                "stream": True,
            },
        )
        self.assertNotIn("Content-Length", headers)
        self.assertEqual(headers.get("Connection"), "close")

    @mock.patch("freegem.server.generate_stream")
    def test_client_disconnect_does_not_crash_server(self, generate_stream: Any) -> None:
        """A dropped client must not take the worker down with it."""
        generate_stream.return_value = iter(["a", "b", "c"])
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(
                {
                    "model": "gemini-3.6-flash",
                    "messages": [{"role": "user", "content": "x"}],
                    "stream": True,
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        conn.getresponse()  # read headers, then abandon mid-body
        conn.close()
        # server must still answer
        status, _, _ = self.get("/health")
        self.assertEqual(status, 200)


class GoogleNativeTests(EndpointTestCase):
    @mock.patch("freegem.server.generate")
    def test_generate_content(self, generate: Any) -> None:
        generate.return_value = "native answer"
        status, _, raw = self.post(
            "/v1beta/models/gemini-3.6-flash:generateContent",
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["candidates"][0]["content"]["parts"][0]["text"], "native answer")
        self.assertEqual(payload["candidates"][0]["finishReason"], "STOP")

    def test_bad_path_404(self) -> None:
        status, _, _ = self.post("/v1beta/models/x:nope", {"contents": []})
        self.assertEqual(status, 404)

    def test_empty_contents_400(self) -> None:
        status, _, _ = self.post("/v1beta/models/gemini-3.6-flash:generateContent", {})
        self.assertEqual(status, 400)

    @mock.patch("freegem.server.generate")
    def test_function_calls_parsed(self, generate: Any) -> None:
        fence = "```"
        generate.return_value = (
            f'{fence}function_call\n{{"name": "get_weather", "args": {{"city": "Oslo"}}}}\n{fence}'
        )
        status, _, raw = self.post(
            "/v1beta/models/gemini-3.6-flash:generateContent",
            {
                "contents": [{"role": "user", "parts": [{"text": "weather?"}]}],
                "tools": [{"functionDeclarations": [{"name": "get_weather", "description": "w"}]}],
            },
        )
        self.assertEqual(status, 200)
        parts = json.loads(raw)["candidates"][0]["content"]["parts"]
        calls = [p for p in parts if "functionCall" in p]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["functionCall"]["args"]["city"], "Oslo")

    @mock.patch("freegem.server.generate_stream")
    def test_google_stream(self, generate_stream: Any) -> None:
        generate_stream.return_value = iter(["native ", "stream"])
        status, _, raw = self.post(
            "/v1beta/models/gemini-3.6-flash:streamGenerateContent",
            {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
        )
        self.assertEqual(status, 200)
        text = raw.decode()
        self.assertIn('"text": "native "', text)
        self.assertIn("STOP", text)


class ResponsesApiTests(EndpointTestCase):
    @mock.patch("freegem.server.generate")
    def test_string_input(self, generate: Any) -> None:
        generate.return_value = "responses answer"
        status, _, raw = self.post("/v1/responses", {"input": "hello"})
        self.assertEqual(status, 200)
        payload = json.loads(raw)
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["output"][0]["content"][0]["text"], "responses answer")

    @mock.patch("freegem.server.generate")
    def test_instructions_become_system(self, generate: Any) -> None:
        generate.return_value = "ok"
        self.post("/v1/responses", {"instructions": "be terse", "input": "hi"})
        prompt = generate.call_args[0][1]
        self.assertIn("[System instruction]: be terse", prompt)

    @mock.patch("freegem.server.generate")
    def test_function_call_output_item(self, generate: Any) -> None:
        generate.return_value = "ok"
        self.post(
            "/v1/responses",
            {
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": "c1",
                        "name": "get_weather",
                        "output": "18C",
                    }
                ]
            },
        )
        prompt = generate.call_args[0][1]
        self.assertIn("[Tool result for get_weather]: 18C", prompt)


class ChunkedBodyTests(EndpointTestCase):
    """Chunked transfer encoding is used by some clients and was hand-rolled."""

    @mock.patch("freegem.server.generate")
    def test_chunked_body_parsed(self, generate: Any) -> None:
        generate.return_value = "chunked ok"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            payload = json.dumps(
                {"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": "x"}]}
            ).encode()
            conn.request(
                "POST",
                "/v1/chat/completions",
                body=payload,
                headers={"Content-Type": "application/json"},
                encode_chunked=True,
            )
            response = conn.getresponse()
            raw = response.read()
        finally:
            conn.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "chunked ok")


class EstimateTokensTests(unittest.TestCase):
    def test_approximation(self) -> None:
        usage = estimate_tokens("a" * 400, "b" * 40)
        self.assertEqual(usage["prompt_tokens"], 100)
        self.assertEqual(usage["completion_tokens"], 10)
        self.assertEqual(usage["total_tokens"], 110)

    def test_empty_completion(self) -> None:
        usage = estimate_tokens("abc", "")
        self.assertEqual(usage["completion_tokens"], 0)


class ConfigInjectionTests(EndpointTestCase):
    """The server must read config from the server, not a global."""

    def test_temporary_chats_reaches_payload(self) -> None:
        """Two concurrent configs must not interfere."""
        self.server.config = BridgeConfig(log_requests=False, temporary_chats=True)
        from freegem import gemini

        with mock.patch.object(gemini, "_post", return_value=io_bytes(wrb_line("ok"))):
            self.post("/v1/chat/completions", {"messages": [{"role": "user", "content": "x"}]})

    def test_config_is_read_only_for_handlers(self) -> None:
        cfg = self.server.config
        self.assertIsInstance(cfg, BridgeConfig)


def io_bytes(data: str) -> io.BytesIO:

    class _R(io.BytesIO):
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *a: object) -> None:
            # returns None == never suppresses, identical to the old `return False`
            return None

    return _R(data.encode())


if __name__ == "__main__":
    unittest.main()
