"""HTTP endpoint tests.

A real server runs on an ephemeral port and is spoken to over a real socket.
That is deliberate: mocking the handler would not catch framing, keep-alive,
or chunked-body bugs, and one of those (an SSE response with no Content-Length
never terminating) shipped in the original and hung clients.
"""

import http.client
import inspect
import io
import json
import threading
import time
import unittest
from typing import Any, ClassVar
from unittest import mock

from freegem.config import BridgeConfig
from freegem.gemini import BridgeError
from freegem.server import GeminiHandler, ThreadedServer, estimate_tokens

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


class MalformedRequestTests(EndpointTestCase):
    """Malformed client input must return a protocol error, not a traceback.

    Each case below was a client-triggerable HTTP 500 found by fuzzing the
    published package. A 500 implies server fault; a malformed request is the
    client's problem and should read as such.
    """

    def test_content_as_int_is_accepted_not_crashed(self) -> None:
        with mock.patch("freegem.server.generate", return_value="ok") as generate:
            status, _, _ = self.post(
                "/v1/chat/completions",
                {"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": 123}]},
            )
        self.assertEqual(status, 200)
        self.assertEqual(generate.call_count, 1)

    def test_content_as_dict_is_accepted_not_crashed(self) -> None:
        with mock.patch("freegem.server.generate", return_value="ok"):
            status, _, _ = self.post(
                "/v1/chat/completions",
                {"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": {"a": 1}}]},
            )
        self.assertEqual(status, 200)

    def test_tool_call_function_as_string(self) -> None:
        with mock.patch("freegem.server.generate", return_value="ok"):
            status, _, _ = self.post(
                "/v1/chat/completions",
                {
                    "model": "gemini-3.6-flash",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "x",
                            "tool_calls": [{"function": "notadict"}],
                        }
                    ],
                },
            )
        self.assertEqual(status, 200)

    def test_google_system_instruction_as_string(self) -> None:
        with mock.patch("freegem.server.generate", return_value="ok"):
            status, _, _ = self.post(
                "/v1beta/models/gemini-3.6-flash:generateContent",
                {
                    "systemInstruction": "be brief",
                    "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                },
            )
        self.assertEqual(status, 200)

    def test_google_tool_config_as_string(self) -> None:
        with mock.patch("freegem.server.generate", return_value="ok"):
            status, _, _ = self.post(
                "/v1beta/models/gemini-3.6-flash:generateContent",
                {
                    "toolConfig": "nope",
                    "tools": [{"functionDeclarations": [{"name": "a"}]}],
                    "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
                },
            )
        self.assertEqual(status, 200)

    def test_responses_tools_with_non_dict_entry(self) -> None:
        with mock.patch("freegem.server.generate", return_value="ok"):
            status, _, _ = self.post(
                "/v1/responses", {"input": "hi", "tools": ["notadict", {"name": "f"}]}
            )
        self.assertEqual(status, 200)

    def test_messages_not_a_list(self) -> None:
        with mock.patch("freegem.server.generate") as generate:
            generate.return_value = "ok"
            status, _, _raw = self.post(
                "/v1/chat/completions",
                {"model": "gemini-3.6-flash", "messages": "notalist"},
            )
        # Either a clean 400 or a 200 -- what must not happen is a 500.
        self.assertIn(status, (200, 400))

    def test_messages_with_string_entries(self) -> None:
        with mock.patch("freegem.server.generate", return_value="ok"):
            status, _, _ = self.post(
                "/v1/chat/completions",
                {
                    "model": "gemini-3.6-flash",
                    "messages": ["a", "b", {"role": "user", "content": "c"}],
                },
            )
        self.assertEqual(status, 200)

    def test_large_body_is_read_not_truncated(self) -> None:
        """A 200KB prompt must arrive intact, not be cut short by a slow read.

        The interesting property is that the handler's socket timeout does not
        fire while a large-but-legitimate body is still streaming in, so the
        full prompt reaches the model rather than a truncated fragment.
        """
        payload_content = "y" * 200_000
        with mock.patch("freegem.server.generate", return_value="ok") as generate:
            status, _, _ = self.post(
                "/v1/chat/completions",
                {
                    "model": "gemini-3.6-flash",
                    "messages": [{"role": "user", "content": payload_content}],
                },
            )
        self.assertEqual(status, 200)
        sent_prompt = generate.call_args[0][1]
        self.assertIn(payload_content, sent_prompt)

    def test_server_has_a_request_timeout(self) -> None:
        """The read timeout is what stops a stalled client eating a thread."""
        from freegem.server import GeminiHandler

        self.assertGreater(GeminiHandler.timeout, 0)

    def test_timeout_is_reported_as_408_not_500(self) -> None:
        """A client that promises a body then stops sends gets a 408.

        BaseHTTPRequestHandler converts a socket TimeoutError into a bare 500
        before do_POST runs, which misattributes a client fault to the server.
        """
        handler_source = inspect.getsource(GeminiHandler.do_POST)
        self.assertIn("408", handler_source)
        self.assertIn("TimeoutError", handler_source)


class CrossOriginPOSTTests(EndpointTestCase):
    """POSTs from non-loopback Origins must die before the Gemini call.

    A cross-site "simple" POST (Content-Type: text/plain) never triggers a
    CORS preflight, so the only defense is checking Origin and Content-Type
    inside do_POST itself. These tests pin both layers: the request must be
    refused AND generate() must never be reached (a 403 that still called
    Gemini would bill the user's session for an attacker's prompt).
    """

    BODY: ClassVar[dict[str, Any]] = {
        "model": "gemini-3.6-flash",
        "messages": [{"role": "user", "content": "hi"}],
    }

    @mock.patch("freegem.server.generate")
    def test_evil_origin_text_plain_403(self, generate: Any) -> None:
        """The reported attack: evil Origin + text/plain simple POST."""
        status, _, raw = self.post(
            "/v1/chat/completions",
            self.BODY,
            headers={"Origin": "https://evil.example", "Content-Type": "text/plain"},
        )
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", json.loads(raw)["error"]["message"])
        generate.assert_not_called()

    @mock.patch("freegem.server.generate")
    def test_evil_origin_with_json_403(self, generate: Any) -> None:
        """Even a perfectly formed JSON body is refused from a foreign Origin."""
        status, _, _ = self.post(
            "/v1/chat/completions", self.BODY, headers={"Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)
        generate.assert_not_called()

    @mock.patch("freegem.server.generate")
    def test_origin_null_403(self, generate: Any) -> None:
        """``Origin: null`` (sandboxed iframe, redirected post) is not loopback."""
        status, _, _ = self.post("/v1/chat/completions", self.BODY, headers={"Origin": "null"})
        self.assertEqual(status, 403)
        generate.assert_not_called()

    @mock.patch("freegem.server.generate")
    def test_text_plain_without_origin_415(self, generate: Any) -> None:
        """No Origin, but a non-JSON content type: the second layer refuses.

        Browsers cannot send application/json without a preflight, so this
        blocks the simple-request forgery even if the Origin check is ever
        bypassed.
        """
        status, _, raw = self.post(
            "/v1/chat/completions", self.BODY, headers={"Content-Type": "text/plain"}
        )
        self.assertEqual(status, 415)
        self.assertIn("application/json", json.loads(raw)["error"]["message"])
        generate.assert_not_called()

    @mock.patch("freegem.server.generate")
    def test_form_urlencoded_415(self, generate: Any) -> None:
        status, _, _ = self.post(
            "/v1/chat/completions",
            self.BODY,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self.assertEqual(status, 415)
        generate.assert_not_called()

    @mock.patch("freegem.server.generate")
    def test_charset_suffix_accepted(self, generate: Any) -> None:
        """``application/json; charset=utf-8`` is the same content type."""
        generate.return_value = "ok"
        status, _, _ = self.post(
            "/v1/chat/completions",
            self.BODY,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        self.assertEqual(status, 200)

    @mock.patch("freegem.server.generate")
    def test_loopback_origin_allowed(self, generate: Any) -> None:
        """A page served from this machine's own loopback may talk to the API."""
        generate.return_value = "ok"
        status, _, raw = self.post(
            "/v1/chat/completions",
            self.BODY,
            headers={
                "Origin": "http://localhost:3000",
                "Content-Type": "application/json",
            },
        )
        self.assertEqual(status, 200)
        # The CORS echo only ever goes to loopback origins (existing behavior).
        self.assertEqual(json.loads(raw)["choices"][0]["message"]["content"], "ok")

    @mock.patch("freegem.server.generate")
    def test_missing_origin_curl_style_allowed(self, generate: Any) -> None:
        """curl and SDKs send no Origin at all; they keep working."""
        generate.return_value = "ok"
        status, _, _ = self.post("/v1/chat/completions", self.BODY)
        self.assertEqual(status, 200)


class ThreadCapTests(unittest.TestCase):
    """ThreadedServer must cap its worker threads and shed load with a 503.

    Each in-flight request holds a thread for up to the 65s handler timeout;
    without a cap, stalled connections grow the thread count without bound.
    """

    def _boot(self, cap: int) -> ThreadedServer:
        server = ThreadedServer(
            ("127.0.0.1", 0),
            BridgeConfig(log_requests=False),
            max_concurrent_requests=cap,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, timeout=5)
        return server

    def _get(self, port: int, path: str) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            conn.request("GET", path)
            response = conn.getresponse()
            raw = response.read()
            return response.status, raw
        finally:
            conn.close()

    def _post_chat(self, port: int) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        try:
            conn.request(
                "POST",
                "/v1/chat/completions",
                json.dumps({"messages": [{"role": "user", "content": "hi"}]}),
                {"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            raw = response.read()
            return response.status, raw
        finally:
            conn.close()

    def test_zero_cap_refuses_everything_without_a_thread(self) -> None:
        """cap=0: every request gets 503 and no worker slot is ever held."""
        server = self._boot(0)
        port = server.server_address[1]
        status, raw = self._get(port, "/health")
        self.assertEqual(status, 503)
        self.assertIn("server busy", raw.decode("utf-8"))
        self.assertIn("bridge_error", raw.decode("utf-8"))
        self.assertEqual(server._active_requests, 0)

    def test_admit_release_round_trip(self) -> None:
        server = self._boot(2)
        self.assertTrue(server._admit())
        self.assertTrue(server._admit())
        self.assertFalse(server._admit())  # full
        server._release()
        self.assertTrue(server._admit())  # slot freed

    def test_request_beyond_cap_gets_503_while_first_is_in_flight(self) -> None:
        """A held Gemini call occupies the only slot; the next request sheds."""
        server = self._boot(1)
        port = server.server_address[1]
        started = threading.Event()
        release = threading.Event()

        def slow_generate(*args: Any, **kwargs: Any) -> str:
            started.set()
            release.wait(timeout=10)
            return "done"

        first_result: dict[str, int] = {}

        def first_request() -> None:
            first_result["status"], _ = self._post_chat(port)

        with mock.patch("freegem.server.generate", side_effect=slow_generate):
            worker = threading.Thread(target=first_request, daemon=True)
            worker.start()
            self.assertTrue(started.wait(timeout=10), "generate was never reached")
            self.assertEqual(server._active_requests, 1)

            status, raw = self._get(port, "/health")
            self.assertEqual(status, 503)
            self.assertIn("server busy", raw.decode("utf-8"))

            release.set()
            worker.join(timeout=10)
            self.assertEqual(first_result["status"], 200)

        # The slot came back after the request finished.
        for _ in range(200):
            if server._active_requests == 0:
                break
            time.sleep(0.01)
        self.assertEqual(server._active_requests, 0)


class QueueTests(ThreadCapTests):
    """request_queue_wait_sec > 0: brief admission queueing before the 503."""

    def _boot(self, cap: int, queue_wait: float = 0.0) -> ThreadedServer:
        cfg = BridgeConfig(log_requests=False, request_queue_wait_sec=queue_wait)
        server = ThreadedServer(("127.0.0.1", 0), cfg, max_concurrent_requests=cap)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, timeout=5)
        return server

    def test_503_carries_retry_after(self) -> None:
        conn = http.client.HTTPConnection("127.0.0.1", self._boot(0).server_address[1], timeout=10)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            response.read()
            self.assertEqual(response.status, 503)
            self.assertEqual(response.headers.get("Retry-After"), "2")
        finally:
            conn.close()

    @mock.patch("freegem.server.generate")
    def test_waiter_is_admitted_when_a_slot_frees(self, generate: Any) -> None:
        """cap=1 + queue: a second request waits for the slot instead of 503."""
        server = self._boot(1, queue_wait=3.0)
        port = server.server_address[1]
        started = threading.Event()
        release = threading.Event()

        def slow_generate(*args: Any, **kwargs: Any) -> str:
            started.set()
            release.wait(timeout=10)
            return "done"

        first_status: dict[str, int] = {}

        def first_request() -> None:
            first_status["code"], _ = self._post_chat(port)

        with mock.patch("freegem.server.generate", side_effect=slow_generate):
            worker = threading.Thread(target=first_request, daemon=True)
            worker.start()
            self.assertTrue(started.wait(timeout=10), "generate was never reached")
            self.assertEqual(server._active_requests, 1)

            def release_soon() -> None:
                time.sleep(0.4)
                release.set()

            threading.Thread(target=release_soon, daemon=True).start()
            began = time.monotonic()
            status, _raw = self._get(port, "/health")
            waited = time.monotonic() - began
            self.assertEqual(status, 200)  # queued, not shed
            self.assertGreaterEqual(waited, 0.35)
            worker.join(timeout=10)
            self.assertEqual(first_status["code"], 200)

    @mock.patch("freegem.server.generate")
    def test_queue_overflow_still_sheds_immediately(self, generate: Any) -> None:
        """Active cap + queue both full: the extra connection gets 503 at once."""
        server = self._boot(1, queue_wait=5.0)
        port = server.server_address[1]
        started = threading.Event()
        release = threading.Event()

        def slow_generate(*args: Any, **kwargs: Any) -> str:
            started.set()
            release.wait(timeout=10)
            return "done"

        with mock.patch("freegem.server.generate", side_effect=slow_generate):
            first = threading.Thread(target=lambda: self._post_chat(port), daemon=True)
            first.start()
            self.assertTrue(started.wait(timeout=10))
            self.assertEqual(server._active_requests, 1)

            # Second connection takes the one queue slot (it will hang there).
            queued = threading.Thread(target=lambda: self._post_chat(port), daemon=True)
            queued.start()
            time.sleep(0.3)  # let it settle into the queue
            self.assertEqual(server._active_requests, 2)

            began = time.monotonic()
            status, raw = self._get(port, "/health")
            self.assertEqual(status, 503)
            self.assertLess(time.monotonic() - began, 2.0)  # refused, not queued
            self.assertIn(b"server busy", raw)
            release.set()
            first.join(timeout=10)
            queued.join(timeout=10)
