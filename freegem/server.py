"""HTTP server: OpenAI-compatible and Google-native faces.

There is no authentication in this project. The access control is that the
server binds to loopback by default. If you need to expose it beyond your own
machine, put it behind a reverse proxy that does the auth -- do not add keys
here, because the thing being protected is your Google session, not this code.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import struct
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any

from . import __version__
from .config import BridgeConfig
from .gemini import BridgeError, generate, generate_stream, log
from .jobs import (
    GLOBAL_JOB_MANAGER,
    JobQueueFull,
    configure_job_runner,
    job_runner,
)
from .metrics import ServerMetrics, new_request_id, record_audit_event
from .models import MODELS, resolve_model
from .multimodal import detect_image_mime, fetch_image_bytes, upload_image
from .non_goals import NON_GOALS
from .tools import (
    ImageRef,
    google_contents_to_prompt,
    messages_to_prompt,
    parse_function_calls,
    parse_tool_calls,
    validate_tool_calls,
)

__all__ = ["GeminiHandler", "ThreadedServer", "estimate_tokens"]

# Hard ceiling on request bodies. Without it a declared Content-Length of 2 GB
# is read straight into RAM by an unauthenticated caller.
MAX_BODY_BYTES = 32 * 1024 * 1024

# Hostnames that legitimately address a loopback server. A Host header outside
# this set means either a misdirected request or a DNS-rebinding attempt.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class _BodyTooLarge(Exception):
    """Request body exceeded MAX_BODY_BYTES."""


def _hostname_of(host_header: str) -> str:
    """Hostname part of a Host header: strips port and IPv6 brackets."""
    value = host_header.strip().lower()
    if value.startswith("["):  # [::1]:8081
        return value[1 : value.find("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if ":" in value else value


def _origin_is_loopback(origin: str) -> bool:
    """True when an Origin header addresses this machine's own server."""
    try:
        parsed = urllib.parse.urlsplit(origin)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and _hostname_of(parsed.netloc) in _LOOPBACK_HOSTS


def estimate_tokens(prompt: str, completion: str) -> dict[str, int]:
    """Approximate token counts.

    Gemini's web endpoint returns no usage data, so this is a character-count
    estimate (4 chars/token). It exists so clients that read ``usage`` do not
    crash; it is not accurate and is documented as such in the README.
    """
    p, c = len(prompt) // 4, len(completion or "") // 4
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def _google_usage_metadata(prompt: str, completion: str) -> dict[str, int]:
    """usageMetadata with the Generative Language API's key names.

    estimate_tokens() is shared with the OpenAI face (prompt_tokens/...), but
    the Google-native contract uses promptTokenCount/candidatesTokenCount/
    totalTokenCount -- Gemini CLI reads those keys. The counts are the same
    documented len//4 estimate either way.
    """
    est = estimate_tokens(prompt, completion)
    return {
        "promptTokenCount": est["prompt_tokens"],
        "candidatesTokenCount": est["completion_tokens"],
        "totalTokenCount": est["total_tokens"],
    }


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
    def _authenticate_request(self) -> bool:
        api_key = getattr(self.cfg, "api_key", None) or os.environ.get("FREEGEM_API_KEY")
        if not api_key:
            return True

        auth_header = self.headers.get("Authorization", "")
        key_header = self.headers.get("X-API-Key", "")

        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif key_header:
            token = key_header.strip()

        client_ip = self.client_address[0] if self.client_address else "127.0.0.1"
        req_id = getattr(self, "_request_id", "")

        if token == api_key:
            return True

        record_audit_event("auth_failure", client_ip, req_id, {"path": self.path})
        self.send_error_json("Unauthorized: invalid or missing API key", 401)
        return False

    def _handle_jobs_get(self, path: str) -> None:
        if not self._authenticate_request():
            return

        parts = [p for p in path.split("/") if p]
        # GET /v1/jobs
        if len(parts) == 2:
            jobs = GLOBAL_JOB_MANAGER.list_jobs()
            self.send_json({"object": "list", "data": jobs})
            return

        job_id = parts[2]
        job = GLOBAL_JOB_MANAGER.get_job(job_id)
        if not job:
            self.send_error_json("job not found", 404)
            return

        # GET /v1/jobs/{id}/results
        if len(parts) >= 4 and parts[3] == "results":
            self.send_json(
                {"id": job.id, "status": job.status, "result": job.result, "error": job.error}
            )
            return

        # GET /v1/jobs/{id}
        self.send_json(job.to_dict())

    def _handle_artifacts_get(self, path: str) -> None:
        if not self._authenticate_request():
            return

        parts = [p for p in path.split("/") if p]
        if len(parts) < 3:
            self.send_error_json("artifact id required", 400)
            return

        art_id = parts[2]
        art = GLOBAL_JOB_MANAGER.get_artifact(art_id)
        if not art or not os.path.exists(art.file_path):
            self.send_error_json("artifact not found", 404)
            return

        try:
            with open(art.file_path, "rb") as f:
                content = f.read()
            self._send(content, 200, art.content_type)
        except Exception as exc:
            self.send_error_json(f"failed to read artifact: {exc}", 500)

    def _handle_jobs_post(self) -> None:
        if not self._authenticate_request():
            return

        raw = self._read_body()
        if raw is None:
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_error_json("invalid JSON payload", 400)
            return

        job_type = data.get("type", "")
        params = data.get("params", {})
        if not job_type:
            self.send_error_json("job type is required", 400)
            return

        runner = job_runner() or configure_job_runner(self.cfg)
        try:
            job = runner.submit(job_type, params)
        except JobQueueFull:
            # Bounded backlog: saturation is a visible 429, not silent growth.
            self.send_json(
                {
                    "error": "job queue is full",
                    "code": "queue_full",
                    "hint": "retry later; the backlog is bounded by design",
                },
                429,
            )
            return
        self.send_json(job.to_dict(), 202)

    """Serves all protocol faces from one handler.

    ``server.config`` is set by :class:`ThreadedServer`; every request reads it
    read-only, so concurrent requests cannot corrupt each other's settings.
    """

    server_version = f"freegem/{__version__}"
    protocol_version = "HTTP/1.1"

    # Bound how long a connection may idle while we read its request. Without
    # this, a client that declares Content-Length: 99999 and sends 36 bytes
    # leaves this handler blocked in rfile.read() forever. Under ThreadingMixIn
    # each such connection permanently consumes a worker thread, so a handful of
    # half-open sockets exhausts the server. Also stops a client that opens a
    # stream and never reads from stalling the thread that is writing to it.
    timeout = 65

    # ── plumbing ─────────────────────────────────────────────────────────────

    def handle(self) -> None:
        """Serve one connection, with a timeout applied to the socket."""
        if self.timeout:
            with contextlib.suppress(OSError):
                self.connection.settimeout(self.timeout)
        try:
            super().handle()
        except TimeoutError:
            # The socket read timed out: the client went quiet mid-request.
            # BaseHTTPRequestHandler would turn this into a bare 500 with an
            # empty body, which looks like a server fault rather than a client
            # that stopped sending. Report it as what it is.
            with contextlib.suppress(Exception):
                self.send_error_json("request timed out waiting for the client", 408)

    @property
    def cfg(self) -> BridgeConfig:
        return self.server.config  # type: ignore[attr-defined]

    @property
    def _metrics(self) -> ServerMetrics:
        return self.server.server_metrics  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        client = self.client_address[0] if self.client_address else "-"
        log(self.cfg, f"{client} {fmt % args}")

    def _send(self, body: bytes, status: int, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        request_id = getattr(self, "_request_id", None)
        if request_id:
            self.send_header("X-Request-Id", request_id)
        # CORS only for loopback origins. "*" would let ANY webpage the user
        # visits drive this server and read its responses (browsers reach
        # loopback from any origin); non-browser clients need no CORS at all.
        origin = self.headers.get("Origin")
        if origin and _origin_is_loopback(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self._metrics.note_status(status)
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
        request_id = getattr(self, "_request_id", None)
        if request_id:
            self.send_header("X-Request-Id", request_id)
        origin = self.headers.get("Origin")
        if origin and _origin_is_loopback(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self._metrics.note_status(200)
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
            accumulated = 0
            while True:
                size_line = self.rfile.readline()
                if not size_line:
                    break
                try:
                    size = int(size_line.split(b";", 1)[0].strip(), 16)
                except ValueError as err:
                    raise ValueError("malformed chunked body") from err
                if size < 0:
                    # rfile.read(-1) would read until EOF and wedge the thread.
                    raise ValueError("negative chunk size")
                if size > MAX_BODY_BYTES:
                    raise _BodyTooLarge(f"chunk of {size} bytes exceeds the limit")
                if accumulated + size > MAX_BODY_BYTES:
                    # Per-chunk caps are not enough: a stream of under-cap
                    # chunks accumulates unbounded RAM before the post-join
                    # check ever runs. Refuse the moment the total would.
                    raise _BodyTooLarge(f"chunked body exceeds the {MAX_BODY_BYTES}-byte limit")
                if size == 0:
                    while True:
                        trailer = self.rfile.readline()
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                chunks.append(self.rfile.read(size))
                accumulated += size
                self.rfile.read(2)  # trailing CRLF
            body = b"".join(chunks)
            if len(body) > MAX_BODY_BYTES:
                raise _BodyTooLarge(f"body of {len(body)} bytes exceeds the limit")
            return body
        raw_length = (self.headers.get("Content-Length") or "0").strip()
        try:
            length = int(raw_length)
        except ValueError as err:
            raise ValueError("invalid Content-Length") from err
        if length < 0:
            raise ValueError("negative Content-Length")
        if length > MAX_BODY_BYTES:
            raise _BodyTooLarge(f"declared Content-Length {length} exceeds the limit")
        return self.rfile.read(length) if length else b""

    def _json_body(self) -> dict | None:
        try:
            raw = self._read_body()
        except _BodyTooLarge as exc:
            # The body was not drained; answering on a keep-alive connection
            # would parse the leftover bytes as the next request line.
            self.close_connection = True
            self.send_error_json(str(exc), 413)
            return None
        except ValueError as exc:
            self.close_connection = True
            self.send_error_json(str(exc), 400)
            return None
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

    def _client_may_speak(self) -> bool:
        """Reject requests whose Host header is not this machine.

        Loopback binding stops the LAN, but a browser reaches loopback from any
        origin, including via DNS rebinding (the browser believes the host is
        attacker-controlled while packets land here). Validating Host closes
        that path: rebinding requires a Host we do not answer for.
        """
        hostname = _hostname_of(self.headers.get("Host") or "")
        allowed = _LOOPBACK_HOSTS | {self.cfg.host.split("%")[0].lower()}
        if hostname not in allowed:
            log(self.cfg, f"rejected request with unexpected Host {hostname!r}")
            self.send_error_json("forbidden host", 403)
            return False
        return True

    def _post_may_run(self) -> bool:
        """Cross-site gate for POSTs: loopback Origin, JSON content type.

        Two independent layers, because either one alone can be bypassed:

        - Origin: a browser sends ``Origin`` on every cross-site POST, and a
          \"simple\" POST (text/plain or form-urlencoded) skips the CORS
          preflight entirely, so the preflight-based defenses elsewhere in
          this file never fire. Any Origin that is not this machine's own
          loopback server is therefore refused outright. A missing Origin
          header means a non-browser client (curl, an SDK, tests) and is
          allowed; ``Origin: null`` (sandboxed iframes, redirected posts) is
          NOT loopback and is refused.
        - Sec-Fetch-Site: browsers that implement Fetch Metadata send this on
          every request, including simple ones that skip preflight, and they
          do not let page scripts forge it (it is a forbidden header name).
          A value of ``cross-site`` is the browser itself swearing the request
          was initiated from another site -- the exact CSRF shape the Origin
          check catches -- so refusing it adds a third, independent gate that
          also covers cases where Origin is present but oddly formed. Missing
          header means a non-browser client (curl, an SDK, tests) and is
          allowed; ``same-site``/``same-origin``/``none`` are allowed.
        - Content-Type: browsers cannot attach ``application/json`` to a
          simple request -- the spec forces a preflight for it -- so requiring
          JSON blocks the text/plain forgery even if a future caller forgets
          the Origin check.
        """
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site == "cross-site":
            log(self.cfg, "rejected cross-site POST (Sec-Fetch-Site: cross-site)")
            self.send_error_json("cross-site POSTs are not allowed", 403)
            return False
        origin = self.headers.get("Origin")
        if origin is not None and not _origin_is_loopback(origin):
            log(self.cfg, f"rejected cross-origin POST from Origin {origin!r}")
            self.send_error_json("cross-origin POSTs are not allowed", 403)
            return False
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        if content_type.strip().lower() != "application/json":
            log(self.cfg, f"rejected POST with Content-Type {content_type.strip()!r}")
            self.send_error_json("Content-Type must be application/json", 415)
            return False
        return True

    def do_OPTIONS(self) -> None:
        if not self._client_may_speak():
            return
        self.send_response(204)
        origin = self.headers.get("Origin")
        if origin and _origin_is_loopback(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._client_may_speak():
            return
        try:
            path = self.path.split("?", 1)[0]
            if path in ("/", "/health"):
                self.send_json(
                    {
                        "status": "ok",
                        "version": __version__,
                        "models": list(MODELS),
                        "session": "cookie" if self.cfg.has_session_cookie() else "anonymous",
                        "non_guarantees": [goal.label for goal in NON_GOALS],
                    }
                )
            elif path == "/v1/about":
                # Scope boundaries, stated by the service itself: every
                # client that probes the server also learns what it does
                # NOT promise. Static data; no session or config leakage.
                self.send_json(
                    {
                        "version": __version__,
                        "non_goals": [
                            {
                                "label": goal.label,
                                "assumption": goal.assumption,
                                "reality": goal.reality,
                            }
                            for goal in NON_GOALS
                        ],
                        "stance": (
                            "FreeGem is a best-effort local reader of the "
                            "live web. It fetches politely and reports "
                            "failures honestly; the items above are "
                            "deliberate scope boundaries, not bugs."
                        ),
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
                                "created": int(time.time()),
                                "owned_by": "google",
                                "description": spec.desc,
                            }
                            for name, spec in MODELS.items()
                        ],
                    }
                )
            elif path == "/metrics":
                # Operational read: counts, latency percentiles, retries,
                # memory. Aggregates only -- no request bodies, no session
                # data, nothing secret; the server is loopback-bound anyway.
                snapshot = self._metrics.snapshot()
                runner = job_runner()
                if runner is not None:
                    snapshot["jobs"] = runner.stats()
                self.send_json(snapshot)
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
        started = time.monotonic()
        self._request_id = new_request_id()
        try:
            self._do_post()
        finally:
            self._metrics.latency.record(time.monotonic() - started)

    def do_DELETE(self) -> None:
        """Cancel a job: DELETE /v1/jobs/{id}.

        Queued jobs are cancelled immediately; running ones cooperatively --
        the cancel flag is set and the runner drops the result when the
        worker surfaces (threads cannot be killed in Python; the honest
        contract is "result not delivered", not "thread assassinated").
        """
        self._request_id = new_request_id()
        if not self._client_may_speak():
            return
        if not self._authenticate_request():
            return
        parts = [p for p in self.path.split("?", 1)[0].split("/") if p]
        if len(parts) < 3 or parts[1] != "jobs":
            self.send_error_json("expected DELETE /v1/jobs/{id}", 400)
            return
        runner = job_runner()
        if runner is None:
            self.send_error_json("job runner not configured", 503)
            return
        job_id = parts[2]
        if not runner.cancel(job_id):
            self.send_error_json("job not found or already terminal", 404)
            return
        job = GLOBAL_JOB_MANAGER.get_job(job_id)
        self.send_json(
            {
                "id": job_id,
                "status": job.status if job else "cancelled",
                "cancelled": True,
                "cooperative": job is not None and job.status == "processing",
            }
        )

    def _do_post(self) -> None:
        if not self._client_may_speak():
            return
        if not self._post_may_run():
            return
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
            # BaseHTTPRequestHandler.handle_one_request already converts a
            # socket TimeoutError into a 500 with the bare message "timed out"
            # before this frame runs, so a client that promises a body and goes
            # quiet surfaces as a server fault. Re-report it as 408.
            if isinstance(exc, TimeoutError):
                log(self.cfg, "client stopped sending mid-request")
                with contextlib.suppress(Exception):
                    self.send_error_json("client stopped sending the request", 408)
                return
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
        # Its Optional return type cannot express that. This is a hard failure
        # rather than an assert because asserts are stripped under `python -O`,
        # which would turn a contract break into a baffling int(None) TypeError.
        if name is None or mode_id is None or think_mode is None:
            raise RuntimeError("resolve_model returned no error but left fields unset")
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
            # A fenced call for a tool the request never declared would hand
            # the client something it cannot route; drop it here.
            tool_calls = validate_tool_calls(tool_calls, tools, tool_choice)
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
        started = time.monotonic()
        first_frame = True
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
                if first_frame:
                    # Time to first token: the number a 10-second answer
                    # actually feels like. Logged per stream so regressions
                    # (new buffering, upstream slowness) show up in the log.
                    first_frame = False
                    log(
                        self.cfg,
                        f"ttft {time.monotonic() - started:.2f}s "
                        f"({model_name}, first frame written)",
                    )
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
            # The stream is already open, so returning silently would hand the
            # client a 200 that ends without finish_reason or [DONE] -- a
            # failure indistinguishable from a completed empty answer. Emit an
            # error frame and still terminate the stream properly.
            with contextlib.suppress(Exception):
                self._sse(
                    None,
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model_name,
                        "error": {"message": str(exc), "type": "stream_error"},
                    },
                )
                self._sse_done()

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
        if tools and isinstance(tools, list):
            normalised = []
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
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
        else:
            tools = None

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
            tool_calls = validate_tool_calls(tool_calls, tools, tool_choice)

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
        if kind == "function_call":
            # Codex echoes its own prior tool calls back as input items; mapping
            # them to a user turn deleted the assistant's call from history.
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": item.get("call_id", ""),
                        "type": "function",
                        "function": {
                            "name": item.get("name", ""),
                            "arguments": item.get("arguments", "{}"),
                        },
                    }
                ],
            }
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

        tool_config = req.get("toolConfig")
        if not isinstance(tool_config, dict):
            tool_config = {}
        fc_config = tool_config.get("functionCallingConfig")
        if not isinstance(fc_config, dict):
            fc_config = {}
        fc_mode = fc_config.get("mode", "AUTO")
        tools_present = req.get("tools")
        has_tools = bool(tools_present) and fc_mode != "NONE"

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
            function_calls = validate_tool_calls(function_calls, tools_present)
            allowed = fc_config.get("allowedFunctionNames")
            if isinstance(allowed, list) and allowed and all(isinstance(n, str) for n in allowed):
                function_calls = [c for c in function_calls if c.get("name") in allowed]
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
            "usageMetadata": _google_usage_metadata(prompt, text or ""),
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
        started = time.monotonic()
        first_frame = True
        try:
            self._start_sse()
            collected = ""
            for delta in generate_stream(
                self.cfg, prompt, model_id, think_mode, file_refs, extra_fields
            ):
                if not delta:
                    continue
                if first_frame:
                    # Same TTFT metric the OpenAI face logs -- one number per
                    # stream, comparable across both API faces.
                    first_frame = False
                    log(
                        self.cfg,
                        f"ttft {time.monotonic() - started:.2f}s "
                        f"({model_name}, first frame written)",
                    )
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
                    "usageMetadata": _google_usage_metadata(prompt, collected),
                    "modelVersion": model_name,
                },
            )
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log(self.cfg, f"google stream error: {exc}")


# Refusal-path bounds. _refuse_busy runs on the accept thread, so every wait
# it performs must be bounded: 0.1 s to drain the request bytes, 1 s at most
# for the kernel to hand the 503 to the peer at close time.
_REFUSE_DRAIN_SEC = 0.1
_REFUSE_DRAIN_READS = 64  # against a client that never stops sending
_REFUSE_LINGER_SEC = 1


class ThreadedServer(ThreadingMixIn, HTTPServer):
    """A thread-per-request server with a hard cap on concurrent requests.

    Every in-flight request holds a worker thread for up to the handler's
    65-second timeout. Without a cap, a client that opens connections and
    stalls them (accidentally or on purpose) grows the thread count without
    bound. Requests beyond the cap get an immediate 503 -- written on the
    socket before any thread is spawned -- instead of a thread.
    """

    daemon_threads = True
    allow_reuse_address = True

    # The default cap. Plenty for one user's local bridge; configure by
    # constructing ThreadedServer(..., max_concurrent_requests=N).
    max_concurrent_requests = 32

    def __init__(
        self,
        address: tuple[str, int],
        config: BridgeConfig,
        *,
        max_concurrent_requests: int | None = None,
    ):
        super().__init__(address, GeminiHandler)
        self.config = config  # read-only for handlers
        # Bounded background-job execution (Phase 2): fixed pool, bounded
        # queue, durable SQLite status, timeouts, retries, retention.
        configure_job_runner(config)
        self._max_concurrent = (
            self.max_concurrent_requests
            if max_concurrent_requests is None
            else max_concurrent_requests
        )
        # Optional short admission queue. request_queue_wait_sec > 0 lets a
        # request WAIT (bounded) for a free slot instead of shedding it, which
        # smooths bursts of real clients without ever letting in-flight work
        # exceed the cap. The in-flight bound grows to active + queued, where
        # queued == active (at most a burst's worth); the active semaphore is
        # what actually enforces the concurrency cap.
        self._queue_wait = max(0.0, float(getattr(config, "request_queue_wait_sec", 0.0) or 0.0))
        queue_slots = self._max_concurrent if self._queue_wait > 0 else 0
        self._in_flight_limit = self._max_concurrent + queue_slots
        self._active_requests = 0
        self._admit_lock = threading.Lock()
        self._active_slots = threading.BoundedSemaphore(self._max_concurrent)
        #: Process-wide operational metrics, rendered by GET /metrics.
        self.server_metrics = ServerMetrics()

    def _admit(self) -> bool:
        """Atomically claim one in-flight slot; False when the server is full."""
        with self._admit_lock:
            if self._active_requests >= self._in_flight_limit:
                return False
            self._active_requests += 1
            return True

    def _release(self) -> None:
        with self._admit_lock:
            self._active_requests -= 1

    def _promote(self) -> bool:
        """Turn an in-flight slot into an ACTIVE slot (a concurrency permit).

        Without the queue this is a non-blocking try -- identical to the old
        refuse-fast behavior. With the queue it waits up to
        request_queue_wait_sec, then gives up (the caller sheds the request
        with a 503 + Retry-After).
        """
        if self._queue_wait <= 0:
            return self._active_slots.acquire(blocking=False)
        return self._active_slots.acquire(timeout=self._queue_wait)

    def process_request(self, request: Any, client_address: Any) -> None:
        # ``request`` is always a connected socket for an HTTPServer, but the
        # socketserver supertype types it as the wider stream/datagram union,
        # so the override takes Any and _refuse_busy narrows it.
        if not self._admit():
            self.server_metrics.note_refusal()
            self._refuse_busy(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            # The worker thread was never spawned (or failed to start), so the
            # slot it would have released on completion leaks. Give it back.
            self._release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            # Queue-or-run: wait (bounded) for a concurrency permit, or shed.
            if not self._promote():
                self.server_metrics.note_refusal()
                self._refuse_busy(request)
                return
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._active_slots.release()
        finally:
            self._release()

    @staticmethod
    def _refuse_busy(request: socket.socket) -> None:
        """Answer 503 on the raw socket and close, before any thread spawns.

        Two ordering rules make the 503 reach the client on every platform
        (a bare send-then-close drops it on Windows with WinError
        10053/10054 on the client):

        - Drain the request bytes FIRST. ``close()`` with unread data still
          in the receive buffer aborts the connection with a RST, and that
          RST races the 503 out the door. The client's bytes are normally
          already buffered locally, so the drain returns immediately; the
          timeout only covers a client still mid-body.
        - Close with a short finite ``SO_LINGER``. An immediate close while
          the send buffer holds unACKed data resets the connection too;
          lingering lets the kernel wait (bounded) for the response to be
          acknowledged instead of discarding it.
        """
        try:
            request.settimeout(_REFUSE_DRAIN_SEC)
            for _ in range(_REFUSE_DRAIN_READS):
                if not request.recv(65536):
                    break  # client closed its end: nothing more can arrive
        except OSError:
            pass  # timeout, reset, or hung up -- the receive side is drained
        body = b'{"error":{"message":"server busy: too many concurrent requests","type":"bridge_error"}}'
        head = (
            "HTTP/1.1 503 Service Unavailable\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Retry-After: 2\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        try:
            with contextlib.suppress(OSError):
                request.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("ii", 1, _REFUSE_LINGER_SEC),
                )
            request.sendall(head + body)
        except OSError:
            pass  # the client hung up first; nothing to tell
        finally:
            with contextlib.suppress(OSError):
                request.shutdown(socket.SHUT_WR)
            with contextlib.suppress(OSError):
                request.close()
