"""HTTP server: OpenAI-compatible and Google-native faces.

There is no authentication in this project. The access control is that the
server binds to loopback by default. If you need to expose it beyond your own
machine, put it behind a reverse proxy that does the auth -- do not add keys
here, because the thing being protected is your Google session, not this code.
"""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any

from . import __version__
from .config import BridgeConfig
from .gemini import BridgeError, generate, generate_stream, log
from .models import MODELS, resolve_model
from .multimodal import detect_image_mime, fetch_image_bytes, upload_image
from .tools import (
    ImageRef,
    google_contents_to_prompt,
    messages_to_prompt,
    parse_function_calls,
    parse_tool_calls,
)

__all__ = ["GeminiHandler", "ThreadedServer", "estimate_tokens"]


def estimate_tokens(prompt: str, completion: str) -> dict[str, int]:
    """Approximate token counts.

    Gemini's web endpoint returns no usage data, so this is a character-count
    estimate (4 chars/token). It exists so clients that read ``usage`` do not
    crash; it is not accurate and is documented as such in the README.
    """
    p, c = len(prompt) // 4, len(completion or "") // 4
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def upload_images(cfg: BridgeConfig, images: list[ImageRef]) -> list[str] | None:
    """Turn parsed image refs into Gemini file references."""
    if not images:
        return None
    refs: list[str] = []
    for data, mime in images:
        if isinstance(data, str):
            data = fetch_image_bytes(cfg, data)
            if not data:
                raise BridgeError(f"could not download image: {data!r}")
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise BridgeError("image part decoded to no data")
        resolved_mime = detect_image_mime(bytes(data), mime or "image/png")
        try:
            refs.append(upload_image(cfg, bytes(data), resolved_mime))
        except BridgeError:
            raise
        except Exception as exc:
            raise BridgeError(f"image upload failed: {exc}") from exc
    return refs or None


class GeminiHandler(BaseHTTPRequestHandler):
    """Serves all protocol faces from one handler.

    ``server.config`` is set by :class:`ThreadedServer`; every request reads it
    read-only, so concurrent requests cannot corrupt each other's settings.
    """

    server_version = f"freegem/{__version__}"
    protocol_version = "HTTP/1.1"

    # ── plumbing ─────────────────────────────────────────────────────────────

    @property
    def cfg(self) -> BridgeConfig:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        client = self.client_address[0] if self.client_address else "-"
        log(self.cfg, f"{client} {fmt % args}")

    def _send(self, body: bytes, status: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, data: Any, status: int = 200) -> None:
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"), status, "application/json")

    def send_error_json(self, message: str, status: int) -> None:
        self.send_json({"error": {"message": message, "type": "bridge_error"}}, status)

    def _start_sse(self) -> None:
        """Begin an SSE response.

        SSE has no Content-Length, so the only end-of-body signal is the
        connection closing. Forcing ``close_connection`` prevents a client
        from hanging after the final ``[DONE]`` waiting for more data.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True

    def _sse(self, event: str | None, payload: Any) -> None:
        """Write one SSE frame.

        ``payload`` is JSON-encoded, so the OpenAI ``[DONE]`` sentinel cannot go
        through here: it must be a bare ``data: [DONE]`` line, not the JSON
        string ``"[DONE]"``. Clients match the literal, so wrapping it breaks
        end-of-stream detection in every OpenAI SDK.
        """
        frame = ""
        if event:
            frame += f"event: {event}\n"
        frame += f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()

    def _sse_done(self) -> None:
        """The OpenAI end-of-stream sentinel, as a literal line."""
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # ── request body ─────────────────────────────────────────────────────────

    def _read_body(self) -> bytes:
        """Read the body, handling both Content-Length and chunked transfer."""
        encoding = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in encoding:
            chunks: list[bytes] = []
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    break
                try:
                    size = int(size_line.split(b";", 1)[0].strip(), 16)
                except ValueError as err:
                    raise ValueError("malformed chunked body") from err
                if size == 0:
                    while True:
                        trailer = self.rfile.readline()
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)  # trailing CRLF
            return b"".join(chunks)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict | None:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            self.send_error_json("request body is not valid JSON", 400)
            return None
        if not isinstance(parsed, dict):
            self.send_error_json("request body must be a JSON object", 400)
            return None
        return parsed

    # ── routing ──────────────────────────────────────────────────────────────

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        try:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/health"):
                self.send_json(
                    {
                        "status": "ok",
                        "version": __version__,
                        "models": list(MODELS),
                        "session": "cookie" if self.cfg.has_session_cookie() else "anonymous",
                    }
                )
            elif path == "/v1/models":
                self.send_json(
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": name,
                                "object": "model",
                                "created": 1700000000,
                                "owned_by": "google",
                                "description": spec.desc,
                            }
                            for name, spec in MODELS.items()
                        ],
                    }
                )
            elif path == "/v1beta/models":
                self.send_json(
                    {
                        "models": [
                            {
                                "name": f"models/{name}",
                                "displayName": name,
                                "description": spec.desc,
                                "supportedGenerationMethods": [
                                    "generateContent",
                                    "streamGenerateContent",
                                ],
                            }
                            for name, spec in MODELS.items()
                        ]
                    }
                )
            else:
                self.send_error_json("not found", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log(self.cfg, f"GET error: {exc}")
            with contextlib.suppress(Exception):
                self.send_error_json(str(exc), 500)

    def do_POST(self) -> None:
        try:
            path = self.path.split("?", 1)[0]
            if path == "/v1/chat/completions":
                self._handle_chat()
            elif path == "/v1/responses":
                self._handle_responses()
            elif ":streamGenerateContent" in path:
                self._handle_google(stream=True)
            elif ":generateContent" in path:
                self._handle_google(stream=False)
            elif path.startswith("/v1/"):
                # catch-all: clients differ on exact path, treat as chat
                self._handle_chat()
            else:
                self.send_error_json("not found", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log(self.cfg, f"POST error: {exc}")
            with contextlib.suppress(Exception):
                self.send_error_json(str(exc), 500)

    # ── shared core ──────────────────────────────────────────────────────────

    def _resolve(self, requested: str | None) -> tuple[str, int, int, dict] | None:
        name, mode_id, think_mode, err, extra = resolve_model(
            requested or self.cfg.default_model, self.cfg.default_model
        )
        if err:
            self.send_error_json(err, 400)
            return None
        if not name:
            log(self.cfg, f"unknown model {requested!r}; using {self.cfg.default_model}")
        # resolve_model's contract: no error means every field is populated.
        # Its Optional return type cannot express that; a violation must fail
        # loudly here rather than as int(None) downstream.
        assert name is not None and mode_id is not None and think_mode is not None, (
            "resolve_model broke its contract"
        )
        return name, int(mode_id), int(think_mode), extra or {}

    def _generation_error(self, exc: Exception) -> None:
        if isinstance(exc, BridgeError):
            log(self.cfg, f"upstream: {exc}")
            self.send_error_json(str(exc), 502)
        else:
            log(self.cfg, f"generation error: {exc}")
            self.send_error_json(str(exc), 502)

    # ── /v1/chat/completions ─────────────────────────────────────────────────

    def _handle_chat(self) -> None:
        req = self._json_body()
        if req is None:
            return
        resolved = self._resolve(req.get("model"))
        if resolved is None:
            return
        model_name, model_id, think_mode, extra_fields = resolved

        tools = req.get("tools")
        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(req.get("messages") or [], tools, tool_choice)
        if not prompt.strip():
            self.send_error_json("no prompt content", 400)
            return

        try:
            file_refs = upload_images(self.cfg, images)
        except BridgeError as exc:
            self.send_error_json(str(exc), 502)
            return

        stream = bool(req.get("stream"))
        use_tools = bool(tools) and tool_choice != "none"
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created = int(time.time())

        # Streaming with tools is emulated: the full answer is generated first,
        # parsed, then replayed as a single chunk. OpenAI clients expect tool
        # calls to arrive complete; a partial fence cannot be parsed anyway.
        if stream and not use_tools:
            self._chat_stream(
                completion_id,
                created,
                model_name,
                prompt,
                model_id,
                think_mode,
                file_refs,
                extra_fields,
            )
            return

        try:
            text = generate(self.cfg, prompt, model_id, think_mode, file_refs, extra_fields)
        except Exception as exc:
            self._generation_error(exc)
            return

        tool_calls = None
        if use_tools and text:
            text, tool_calls = parse_tool_calls(text)
        message: dict[str, Any] = {"role": "assistant", "content": text or None}
        if tool_calls:
            message["tool_calls"] = tool_calls
        finish = "tool_calls" if tool_calls else "stop"

        if stream:
            self._start_sse()
            self._sse(
                None,
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": message, "finish_reason": finish}],
                },
            )
            self._sse_done()
            return

        self.send_json(
            {
                "id": completion_id,
                "object": "chat.completion",
                "created": created,
                "model": model_name,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": estimate_tokens(prompt, text or ""),
            }
        )

    def _chat_stream(
        self,
        completion_id: str,
        created: int,
        model_name: str,
        prompt: str,
        model_id: int,
        think_mode: int,
        file_refs: list[str] | None,
        extra_fields: dict,
    ) -> None:
        try:
            self._start_sse()
            self._sse(
                None,
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                    ],
                },
            )
            for delta in generate_stream(
                self.cfg, prompt, model_id, think_mode, file_refs, extra_fields
            ):
                self._sse(
                    None,
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "choices": [
                            {"index": 0, "delta": {"content": delta}, "finish_reason": None}
                        ],
                    },
                )
            self._sse(
                None,
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model_name,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                },
            )
            self._sse_done()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log(self.cfg, f"stream error: {exc}")

    # ── /v1/responses (Codex CLI) ────────────────────────────────────────────

    def _handle_responses(self) -> None:
        req = self._json_body()
        if req is None:
            return
        resolved = self._resolve(req.get("model"))
        if resolved is None:
            return
        model_name, model_id, think_mode, extra_fields = resolved

        messages: list[dict] = []
        if req.get("instructions"):
            messages.append({"role": "system", "content": req["instructions"]})

        input_items = req.get("input", [])
        if isinstance(input_items, str):
            messages.append({"role": "user", "content": input_items})
        elif isinstance(input_items, list):
            for item in input_items:
                if isinstance(item, str):
                    messages.append({"role": "user", "content": item})
                elif isinstance(item, dict):
                    messages.append(self._responses_item_to_message(item))

        tools = req.get("tools")
        if tools:
            normalised = []
            for tool in tools:
                if tool.get("type") == "function" and "function" not in tool:
                    normalised.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool.get("name", ""),
                                "description": tool.get("description", ""),
                                "parameters": tool.get("parameters", {}),
                            },
                        }
                    )
                else:
                    normalised.append(tool)
            tools = normalised

        tool_choice = req.get("tool_choice", "auto")
        prompt, images = messages_to_prompt(messages, tools, tool_choice)
        if not prompt.strip():
            self.send_error_json("no input content", 400)
            return

        try:
            file_refs = upload_images(self.cfg, images)
            text = generate(self.cfg, prompt, model_id, think_mode, file_refs, extra_fields)
        except Exception as exc:
            self._generation_error(exc)
            return

        use_tools = bool(tools) and tool_choice != "none"
        tool_calls = None
        if use_tools and text:
            text, tool_calls = parse_tool_calls(text)

        output: list[dict] = []
        if tool_calls:
            for call in tool_calls:
                output.append(
                    {
                        "type": "function_call",
                        "id": call["id"],
                        "call_id": call["id"],
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                        "status": "completed",
                    }
                )
        if text or not tool_calls:
            output.append(
                {
                    "type": "message",
                    "id": f"msg_{uuid.uuid4().hex[:12]}",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text or "", "annotations": []}],
                }
            )

        usage = {
            "input_tokens": len(prompt) // 4,
            "output_tokens": len(text or "") // 4,
            "total_tokens": (len(prompt) + len(text or "")) // 4,
        }

        if req.get("stream"):
            self._responses_stream(model_name, output, usage)
        else:
            self.send_json(
                {
                    "id": f"resp_{uuid.uuid4().hex[:16]}",
                    "object": "response",
                    "created_at": int(time.time()),
                    "status": "completed",
                    "model": model_name,
                    "output": output,
                    "usage": usage,
                }
            )

    @staticmethod
    def _responses_item_to_message(item: dict) -> dict:
        kind = item.get("type")
        if kind == "function_call_output":
            return {
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "name": item.get("name", ""),
                "content": item.get("output", ""),
            }
        if kind in ("input_text", "input_image", "image"):
            return {"role": "user", "content": [item]}
        if item.get("role") == "assistant" or (
            kind == "message" and item.get("role") == "assistant"
        ):
            content = item.get("content", "")
            texts, calls = "", []
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "output_text":
                        texts += part.get("text", "")
                    elif part.get("type") == "function_call":
                        calls.append(part)
            elif isinstance(content, str):
                texts = content
            message: dict[str, Any] = {"role": "assistant", "content": texts or None}
            if calls:
                message["tool_calls"] = [
                    {
                        "id": call.get("call_id", f"call_{idx}"),
                        "type": "function",
                        "function": {
                            "name": call.get("name", ""),
                            "arguments": call.get("arguments", "{}"),
                        },
                    }
                    for idx, call in enumerate(calls)
                ]
            return message
        return {"role": item.get("role", "user"), "content": item.get("content", "")}

    def _responses_stream(self, model_name: str, output: list[dict], usage: dict) -> None:
        response_id = f"resp_{uuid.uuid4().hex[:16]}"
        created = int(time.time())
        sequence = 0

        def emit(event_type: str, **fields: Any) -> None:
            nonlocal sequence
            sequence += 1
            self._sse(event_type, {"type": event_type, "sequence_number": sequence, **fields})

        base = {"id": response_id, "object": "response", "created_at": created, "model": model_name}
        self._start_sse()
        emit(
            "response.created",
            response={**base, "status": "in_progress", "output": [], "usage": None},
        )
        emit(
            "response.in_progress",
            response={**base, "status": "in_progress", "output": [], "usage": None},
        )

        for output_index, item in enumerate(output):
            if item["type"] == "function_call":
                pending = {**item, "arguments": "", "status": "in_progress"}
                emit("response.output_item.added", output_index=output_index, item=pending)
                emit(
                    "response.function_call_arguments.delta",
                    item_id=item["id"],
                    output_index=output_index,
                    delta=item["arguments"],
                )
                emit(
                    "response.function_call_arguments.done",
                    item_id=item["id"],
                    output_index=output_index,
                    arguments=item["arguments"],
                )
                emit("response.output_item.done", output_index=output_index, item=item)
            else:
                pending = {**item, "content": [], "status": "in_progress"}
                emit("response.output_item.added", output_index=output_index, item=pending)
                for content_index, part in enumerate(item["content"]):
                    anchors = {
                        "item_id": item["id"],
                        "output_index": output_index,
                        "content_index": content_index,
                    }
                    emit(
                        "response.content_part.added",
                        **anchors,
                        part={"type": "output_text", "text": "", "annotations": []},
                    )
                    emit("response.output_text.delta", **anchors, delta=part["text"])
                    emit("response.output_text.done", **anchors, text=part["text"])
                    emit("response.content_part.done", **anchors, part=part)
                emit("response.output_item.done", output_index=output_index, item=item)

        emit(
            "response.completed",
            response={**base, "status": "completed", "output": output, "usage": usage},
        )

    # ── /v1beta (Gemini CLI) ─────────────────────────────────────────────────

    def _handle_google(self, stream: bool) -> None:
        req = self._json_body()
        if req is None:
            return

        # /v1beta/models/<model>:generateContent
        tail = self.path.split("/v1beta/models/", 1)[-1]
        requested = tail.split(":", 1)[0] if ":" in tail else tail
        resolved = self._resolve(requested or None)
        if resolved is None:
            return
        model_name, model_id, think_mode, extra_fields = resolved

        tool_config = req.get("toolConfig") or {}
        fc_mode = (tool_config.get("functionCallingConfig") or {}).get("mode", "AUTO")
        has_tools = bool(req.get("tools")) and fc_mode != "NONE"

        prompt, images = google_contents_to_prompt(req)
        if not prompt.strip():
            self.send_error_json("no content", 400)
            return

        try:
            file_refs = upload_images(self.cfg, images)
        except BridgeError as exc:
            self.send_error_json(str(exc), 502)
            return

        log(
            self.cfg,
            f"google api: model={model_name} stream={stream} tools={has_tools} prompt={len(prompt)}",
        )

        if stream and not has_tools:
            self._google_stream(prompt, model_name, model_id, think_mode, file_refs, extra_fields)
            return

        try:
            text = generate(self.cfg, prompt, model_id, think_mode, file_refs, extra_fields)
        except Exception as exc:
            self._generation_error(exc)
            return

        parts: list[dict] = []
        if has_tools and text:
            cleaned, function_calls = parse_function_calls(text)
            if function_calls:
                if cleaned:
                    parts.append({"text": cleaned})
                for call in function_calls:
                    parts.append({"functionCall": {"name": call["name"], "args": call["args"]}})
            else:
                parts.append({"text": text})
        else:
            parts.append(
                {
                    "text": text
                    or "Gemini returned an empty response. Retry, or check the session cookie."
                }
            )

        payload = {
            "candidates": [
                {"content": {"parts": parts, "role": "model"}, "finishReason": "STOP", "index": 0}
            ],
            "usageMetadata": estimate_tokens(prompt, text or ""),
            "modelVersion": model_name,
        }
        if stream:
            self._start_sse()
            self._sse(None, payload)
        else:
            self.send_json(payload)

    def _google_stream(
        self,
        prompt: str,
        model_name: str,
        model_id: int,
        think_mode: int,
        file_refs: list[str] | None,
        extra_fields: dict,
    ) -> None:
        try:
            self._start_sse()
            collected = ""
            for delta in generate_stream(
                self.cfg, prompt, model_id, think_mode, file_refs, extra_fields
            ):
                if not delta:
                    continue
                collected += delta
                self._sse(
                    None,
                    {
                        "candidates": [
                            {
                                "content": {"parts": [{"text": delta}], "role": "model"},
                                "index": 0,
                            }
                        ],
                        "modelVersion": model_name,
                    },
                )
            self._sse(
                None,
                {
                    "candidates": [{"finishReason": "STOP", "index": 0}],
                    "usageMetadata": estimate_tokens(prompt, collected),
                    "modelVersion": model_name,
                },
            )
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log(self.cfg, f"google stream error: {exc}")


class ThreadedServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], config: BridgeConfig):
        super().__init__(address, GeminiHandler)
        self.config = config  # read-only for handlers
