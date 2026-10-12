"""Gemini Web StreamGenerate client.

Talks to the same endpoint the Gemini web app uses, translating between a
plain prompt and Gemini's internal wire format. Cookie-authenticated when a
session file is configured, anonymous otherwise.
"""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .config import BridgeConfig
from .metrics import note_gemini_retry

__all__ = [
    "HAS_HTTPX",
    "BridgeError",
    "RateLimitError",
    "account_prefix",
    "clean_text",
    "generate",
    "generate_stream",
    "load_cookie",
    "log",
    "make_sapisidhash",
]

try:  # optional: only needed for true token-by-token streaming
    # No ignore needed: [tool.mypy.overrides] sets ignore_missing_imports
    # for httpx.*, so this resolves whether httpx is installed or not.
    import httpx

    HAS_HTTPX = True
except Exception:  # pragma: no cover - depends on install
    HAS_HTTPX = False


class BridgeError(RuntimeError):
    """User-facing failure from the Gemini side.

    Carries an optional upstream error code so callers can distinguish
    "Google rejected the request" from "the network died".
    """

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class RateLimitError(BridgeError):
    """Gemini answered HTTP 200 but the body carried no answer text.

    In practice an empty 200 is the anonymous-tier rate limit or a login
    wall -- the two upstream states that serve a page without a
    BardErrorInfo marker, indistinguishable from the body alone. Typed so
    callers can treat "back off and try again later" differently from a
    genuine upstream rejection. It stays retryable inside this module (a
    short backoff usually clears the anonymous limit), and after the
    retries run out it is re-raised as itself -- not wrapped in a generic
    :class:`BridgeError` -- so ``except RateLimitError`` keeps working.
    """


_SSL_CTX: ssl.SSLContext | None = None
# (abspath, mtime, size) -> (cookie_str, sapisid) cache so a hot loop does not
# re-read the file. The path and size are part of the key: mtime alone let two
# cookie files with identical mtimes share one session (cross-account leak) and
# missed same-tick rewrites.
_COOKIE_CACHE: dict[str, Any] = {"key": None, "cookie": "", "sapisid": None}


def log(cfg: BridgeConfig, message: str) -> None:
    if not cfg.log_requests:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    import sys

    print(f"[{stamp}] {message}", file=sys.stderr, flush=True)


def _ssl_ctx() -> ssl.SSLContext:
    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = ssl.create_default_context()
    return _SSL_CTX


# Backoff never waits longer than this, even when Retry-After asks for more.
# The cap must sit ABOVE the Retry-After windows upstreams actually send:
# truncating a server's explicit "come back in 90s" to the old 30s cap just
# guaranteed another 429 on the next attempt. Two minutes still bounds a
# foreground caller while letting the server's own hint be honored.
_RETRY_BACKOFF_CAP_SEC = 120.0


class _UpstreamHTTPStatus(RuntimeError):
    """The upstream answered with an HTTP error status.

    Carries the parsed Retry-After header (if any) so the retry loop can honor
    it instead of guessing.
    """

    def __init__(self, status: int, retry_after: float | None = None) -> None:
        super().__init__(f"upstream HTTP {status}")
        self.status = status
        self.retry_after = retry_after


def _retry_after_seconds(headers: Any) -> float | None:
    """Parse a Retry-After header into seconds.

    Handles both forms the RFC allows: delay-seconds (``120``) and an HTTP
    date. Anything unparseable, absent, or already in the past becomes None
    ("no hint"), and the exponential backoff decides on its own.
    """
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except Exception:
        return None
    if not raw:
        return None
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))  # delay-seconds form
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)  # HTTP-date form
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _exc_retry_after(exc: Exception) -> float | None:
    """Best-effort Retry-After extraction from an exception.

    urllib's HTTPError carries ``.headers`` directly; httpx's HTTPStatusError
    carries them on ``.response.headers``. Both shapes are covered so the
    streaming and non-streaming retry loops share one helper.
    """
    headers = getattr(exc, "headers", None)
    if headers is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
    return _retry_after_seconds(headers)


def _backoff_delay(base: float, attempt: int, retry_after: float | None = None) -> float:
    """How long attempt N's failure should wait before retrying.

    Exponential in the attempt number (``base * 2^(N-1)``), capped so a long
    run of failures cannot wedge the caller. A server-provided Retry-After is
    honored when it asks for more than the exponential delay would -- the
    server knows its own capacity better than our curve does -- but is capped
    by the same ceiling.
    """
    delay = min(base * (2 ** (attempt - 1)), _RETRY_BACKOFF_CAP_SEC)
    if retry_after is not None and retry_after > delay:
        delay = min(retry_after, _RETRY_BACKOFF_CAP_SEC)
    return delay


def account_prefix(cfg: BridgeConfig) -> str:
    """``/u/<index>`` for non-default Google accounts, else empty."""
    if cfg.auth_user in (None, ""):
        return ""
    return f"/u/{cfg.auth_user}"


def load_cookie(cfg: BridgeConfig) -> tuple[str, str | None]:
    """Read the session file, cached by mtime.

    Accepts both shapes the cookie-sync extension exports:
    a JSON object with ``cookie``/``sapisid``, or a bare cookie string.
    On parse failure the last good value is kept rather than failing open to
    anonymous: a mid-write file should not silently downgrade the session.
    """
    path = cfg.cookie_file
    if not path:
        return "", None
    import os

    if not os.path.exists(path):
        return "", None
    try:
        mtime = os.path.getmtime(path)
        key = (os.path.abspath(path), mtime, os.path.getsize(path))
        if _COOKIE_CACHE["key"] == key and _COOKIE_CACHE["cookie"]:
            return _COOKIE_CACHE["cookie"], _COOKIE_CACHE["sapisid"]

        with open(path, encoding="utf-8") as fh:
            content = fh.read().strip()

        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = str(data.get("cookie", "") or "")
            sapisid = data.get("sapisid") or None
        else:
            cookie_str = content
            sapisid = None
            for pair in cookie_str.split(";"):
                if "=" not in pair:
                    continue
                k, v = pair.strip().split("=", 1)
                if k == "SAPISID":
                    sapisid = v or None
                    break

        _COOKIE_CACHE.update({"key": key, "cookie": cookie_str, "sapisid": sapisid})
        return cookie_str, sapisid
    except Exception as exc:
        log(cfg, f"cookie load failed, using cached value: {exc}")
        return _COOKIE_CACHE["cookie"], _COOKIE_CACHE["sapisid"]


def make_sapisidhash(sapisid: str) -> str:
    """Google's Authorization header format: ``SAPISIDHASH <ts>_<sha1>``.

    SHA1 is mandated by Google's scheme -- substituting a stronger hash makes
    every authenticated request fail. ``usedforsecurity=False`` records that
    intent so a future reader does not "fix" it into a broken login.
    """
    ts = int(time.time())
    digest = hashlib.sha1(
        f"{ts} {sapisid} https://gemini.google.com".encode(),
        usedforsecurity=False,
    ).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def build_headers(cfg: BridgeConfig) -> dict[str, str]:
    prefix = account_prefix(cfg)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://gemini.google.com",
        "Referer": f"https://gemini.google.com{prefix}/app",
        "X-Same-Domain": "1",
        "User-Agent": _UA,
    }
    if prefix:
        headers["X-Goog-AuthUser"] = str(cfg.auth_user)
    cookie_str, sapisid = load_cookie(cfg)
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(str(sapisid))
    return headers


def _persistence_flags(inner: list[Any], temporary: bool) -> None:
    """Set the chat-persistence fields Gemini Web uses.

    ``temporary=True`` marks the request as an ephemeral chat so it does not
    land in the account's history.
    """
    if temporary:
        inner[41] = [1]
        inner[45] = 1
    else:
        inner[41] = [2]


def build_payload(
    prompt: str,
    model_id: int,
    think_mode: int,
    cfg: BridgeConfig,
    file_refs: list[str] | None = None,
    extra_fields: dict[int, Any] | None = None,
) -> str:
    """Encode a request into Gemini's private wire format.

    The array positions below are not arbitrary: they were read out of Gemini's
    frontend bundle. ``inner[79]`` selects the model, ``inner[17]`` the thinking
    depth. Positions are written explicitly rather than by keyword so that a
    future field can be added without guessing what a sparse dict means
    upstream.
    """
    inner: list[Any] = [None] * 102

    if file_refs:
        refs = [[None, None, ref] for ref in file_refs]
        inner[0] = [prompt, 0, None, refs, None, None, 0]
    else:
        inner[0] = [prompt, 0, None, None, None, None, 0]

    inner[1] = ["en"]
    inner[2] = ["", "", "", None, None, None, None, None, None, ""]
    inner[6] = [0]
    inner[7] = 1
    inner[10] = 1
    inner[11] = 0
    inner[17] = [[think_mode]]
    inner[18] = 0
    inner[27] = 1
    inner[30] = [4]
    _persistence_flags(inner, cfg.temporary_chats)
    inner[53] = 0
    inner[59] = str(uuid.uuid4())
    inner[61] = []
    inner[68] = 1
    inner[79] = model_id

    if extra_fields:
        for key, value in extra_fields.items():
            inner[key] = value

    outer = [None, json.dumps(inner)]
    params = {"f.req": json.dumps(outer)}
    if cfg.xsrf_token:
        params["at"] = cfg.xsrf_token
    return urllib.parse.urlencode(params)


def build_url(cfg: BridgeConfig) -> str:
    reqid = int(time.time()) % 1000000
    prefix = account_prefix(cfg)
    return (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={cfg.gemini_bl}&hl=en&_reqid={reqid}&rt=c"
    )


def clean_text(text: str, strip: bool = True) -> str:
    """Strip Gemini's code-reference and card-content markers from output."""
    text = re.sub(
        # Any fence language, not just python/javascript/text: bash/json/go/rust
        # markers leaked through the old alternation in both code paths.
        r"```[\w+#.-]+\?code_(?:reference|stdout)"
        r"&code_event_index=\d+\n.*?```\n?",
        "",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(r"http://googleusercontent\.com/card_content/\d+\n?", "", text)
    return text.strip() if strip else text


def _extract_texts_from_line(line: str) -> list[str]:
    """Yield candidate answer strings from one ``wrb.fr`` response line.

    Gemini streams progressively longer snapshots of the same answer, so the
    longest string in a line is the current best version.
    """
    if '"wrb.fr"' not in line or len(line) < 200:
        return []
    try:
        arr = json.loads(line)
        inner_str = arr[0][2]
        if not inner_str or len(inner_str) < 50:
            return []
        inner = json.loads(inner_str)
        if not (isinstance(inner, list) and len(inner) > 4 and inner[4]):
            return []
        texts: list[str] = []
        for part in inner[4]:
            if isinstance(part, list) and len(part) > 1 and isinstance(part[1], list):
                for item in part[1]:
                    if isinstance(item, str) and item:
                        texts.append(item)
        return texts
    except (json.JSONDecodeError, IndexError, TypeError):
        return []


def extract_response_text(raw: str) -> str:
    """Final answer from a full (non-streamed) Gemini response."""
    match = re.search(r"BardErrorInfo\s*\[(\d+)\]", raw)
    if match:
        raise BridgeError(
            f"Gemini rejected the request (BardErrorInfo {match.group(1)}). "
            "The build tag may be stale, or the session needs a refresh.",
            code=int(match.group(1)) if match.group(1).isdigit() else None,
        )
    best = ""
    for line in raw.split("\n"):
        texts = _extract_texts_from_line(line)
        if not texts:
            continue
        # Slot 0 of a line is the primary answer; sibling strings are cards,
        # related-question chips, etc. Longest-across-siblings discarded the
        # answer whenever a chip list was longer than it.
        if len(texts[0]) > len(best):
            best = texts[0]
    return clean_text(best)


_HTTPX_CLIENTS: dict[tuple[str | None, float], Any] = {}
_CLIENTS_LOCK = threading.Lock()


def _httpx_client(cfg: BridgeConfig) -> Any:
    """A shared, pooled httpx.Client, keyed by its connection-relevant settings.

    The key is the whole point: a client built for proxy A must never serve
    traffic meant for proxy B, and a timeout change must not be silently
    ignored because an old client was cached. One pool per distinct
    (proxy, timeout) pair; every Gemini call -- streaming and, now,
    non-streaming -- reuses it, saving a fresh TLS handshake per request.
    """
    key = (cfg.proxy, float(cfg.request_timeout_sec))
    with _CLIENTS_LOCK:
        client = _HTTPX_CLIENTS.get(key)
        if client is None and HAS_HTTPX:
            transport = httpx.HTTPTransport(proxy=cfg.proxy) if cfg.proxy else None
            client = httpx.Client(transport=transport, timeout=cfg.request_timeout_sec, verify=True)
            _HTTPX_CLIENTS[key] = client
        return client


class _UpstreamResponse:
    """One non-streaming upstream response, normalized across urllib/httpx.

    ``generate()`` only needs three things from whichever client answered:
    the numeric status, the headers (for Retry-After), and the body bytes.
    urllib and httpx name all three differently; this wrapper names them
    once so the retry loop stays client-agnostic.
    """

    def __init__(self, raw: Any) -> None:
        self._raw = raw
        status = getattr(raw, "status_code", None)
        if status is None:
            status = getattr(raw, "status", None) or getattr(raw, "code", None)
        self.status = status
        self.headers = getattr(raw, "headers", None)

    def read(self) -> bytes:
        return self._raw.read()

    def close(self) -> None:
        closer = getattr(self._raw, "close", None)
        if callable(closer):
            closer()

    def __enter__(self) -> _UpstreamResponse:
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def _post(cfg: BridgeConfig, body: bytes, url: str, headers: dict[str, str]) -> Any:
    """Send one request, honouring the configured proxy.

    Prefers the pooled httpx client (connection reuse across calls); falls
    back to urllib -- one fresh connection per request -- when httpx is not
    installed. Both paths return a response the retry loop can read the
    same way.
    """
    client = _httpx_client(cfg)
    if client is not None:
        return _UpstreamResponse(client.post(url, content=body, headers=headers))
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    if cfg.proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": cfg.proxy, "https": cfg.proxy}),
            urllib.request.HTTPSHandler(context=_ssl_ctx()),
        )
        return opener.open(request, timeout=cfg.request_timeout_sec)
    return urllib.request.urlopen(request, context=_ssl_ctx(), timeout=cfg.request_timeout_sec)


def generate(
    cfg: BridgeConfig,
    prompt: str,
    model_id: int,
    think_mode: int,
    file_refs: list[str] | None = None,
    extra_fields: dict[int, Any] | None = None,
) -> str:
    """Non-streaming generation with retry."""
    body = build_payload(prompt, model_id, think_mode, cfg, file_refs, extra_fields).encode()
    url = build_url(cfg)
    headers = build_headers(cfg)

    last_error: Exception | None = None
    for attempt in range(1, cfg.retry_attempts + 1):
        try:
            with _post(cfg, body, url, headers) as response:
                status = getattr(response, "status", None) or getattr(response, "code", None)
                if isinstance(status, int) and status >= 400:
                    # Retryable on purpose: 429/5xx are transient, and without
                    # this a rate-limit page streamed back as HTTP 200 with
                    # content: null while the retry loop never engaged.
                    raise _UpstreamHTTPStatus(int(status), _retry_after_seconds(response.headers))
                raw = response.read().decode("utf-8", errors="replace")
            text = extract_response_text(raw)
            if not text.strip():
                # A 200 login-wall or rate-limit page carries no BardErrorInfo
                # marker; returning "" made a failed request look like a model
                # that chose silence.
                raise RateLimitError(
                    "upstream returned an empty answer (rate limit or login wall?)"
                )
            return text
        except RateLimitError as exc:
            # The empty-200 IS the transient failure mode: retry it like a
            # 429 instead of failing fast like a hard upstream rejection.
            last_error = exc
            if attempt < cfg.retry_attempts:
                delay = _backoff_delay(cfg.retry_delay_sec, attempt, _exc_retry_after(exc))
                note_gemini_retry()
                log(
                    cfg,
                    f"attempt {attempt}/{cfg.retry_attempts} returned an empty "
                    f"answer; retrying in {delay:.1f}s",
                )
                time.sleep(delay)
        except BridgeError:
            raise  # upstream said no; retrying the same request will not help
        except _UpstreamHTTPStatus as exc:
            last_error = exc
            if attempt < cfg.retry_attempts:
                delay = _backoff_delay(cfg.retry_delay_sec, attempt, exc.retry_after)
                note_gemini_retry()
                log(
                    cfg,
                    f"attempt {attempt}/{cfg.retry_attempts} failed: {exc}; "
                    f"retrying in {delay:.1f}s",
                )
                time.sleep(delay)
        except Exception as exc:
            last_error = exc
            if attempt < cfg.retry_attempts:
                delay = _backoff_delay(cfg.retry_delay_sec, attempt)
                note_gemini_retry()
                log(
                    cfg,
                    f"attempt {attempt}/{cfg.retry_attempts} failed: {exc}; "
                    f"retrying in {delay:.1f}s",
                )
                time.sleep(delay)
    if isinstance(last_error, RateLimitError):
        # Surface the typed error, not a generic BridgeError: callers poll
        # for exactly this shape to decide "back off for minutes" vs "give up".
        raise RateLimitError(
            f"Gemini returned no answer after {cfg.retry_attempts} attempts "
            "(rate limit or login wall?)"
        ) from last_error
    raise BridgeError(f"Gemini request failed after {cfg.retry_attempts} attempts: {last_error}")


def generate_stream(
    cfg: BridgeConfig,
    prompt: str,
    model_id: int,
    think_mode: int,
    file_refs: list[str] | None = None,
    extra_fields: dict[int, Any] | None = None,
) -> Iterator[str]:
    """Stream the answer as it is produced.

    Gemini sends progressively longer snapshots rather than true deltas, so
    each yielded chunk is the difference from the previous snapshot. If a
    snapshot stops extending the previous one, that is treated as a protocol
    error rather than a truncated answer, because silently emitting a rewrite
    would corrupt the client's accumulated text.
    """
    if not HAS_HTTPX:
        text = generate(cfg, prompt, model_id, think_mode, file_refs, extra_fields)
        if text:
            yield text
        return

    body = build_payload(prompt, model_id, think_mode, cfg, file_refs, extra_fields)
    url = build_url(cfg)
    headers = build_headers(cfg)
    client = _httpx_client(cfg)

    last_error: Exception | None = None
    for attempt in range(1, cfg.retry_attempts + 1):
        emitted = ""
        try:
            with client.stream("POST", url, content=body, headers=headers) as response:
                response.raise_for_status()
                buffer = ""
                for chunk in response.iter_text():
                    buffer += chunk
                    if "BardErrorInfo" in buffer:
                        match = re.search(r"BardErrorInfo\s*\[(\d+)\]", buffer)
                        if match:
                            raise BridgeError(
                                "Gemini rejected the request during streaming "
                                f"(BardErrorInfo {match.group(1)}).",
                                code=int(match.group(1)) if match.group(1).isdigit() else None,
                            )
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        for text in _extract_texts_from_line(line):
                            if text == emitted or emitted.startswith(text):
                                continue
                            if not text.startswith(emitted):
                                raise BridgeError(
                                    "Gemini rewrote an already-streamed block; "
                                    "aborting to avoid corrupting the answer."
                                )
                            delta = clean_text(text[len(emitted) :], strip=False)
                            emitted = text
                            if delta:
                                yield delta
                # Flush the remainder: a final frame without a trailing newline
                # would otherwise be dropped (the non-streaming path does not
                # have this asymmetry).
                if buffer:
                    for text in _extract_texts_from_line(buffer):
                        if text == emitted or emitted.startswith(text):
                            continue
                        if not text.startswith(emitted):
                            raise BridgeError(
                                "Gemini rewrote an already-streamed block; "
                                "aborting to avoid corrupting the answer."
                            )
                        delta = clean_text(text[len(emitted) :], strip=False)
                        emitted = text
                        if delta:
                            yield delta
            if not emitted:
                # Same failure shape as the non-streaming path: a 200 whose
                # body never yields an answer is the rate limit or a login
                # wall. Nothing was emitted, so a retry cannot duplicate.
                raise RateLimitError(
                    "upstream returned an empty answer (rate limit or login wall?)"
                )
            return
        except RateLimitError as exc:
            # Empty-200 is transient and nothing reached the client, so
            # replaying the stream from scratch is safe.
            last_error = exc
            if attempt < cfg.retry_attempts:
                delay = _backoff_delay(cfg.retry_delay_sec, attempt, _exc_retry_after(exc))
                note_gemini_retry()
                log(
                    cfg,
                    f"stream attempt {attempt}/{cfg.retry_attempts} returned an "
                    f"empty answer; retrying in {delay:.1f}s",
                )
                time.sleep(delay)
        except BridgeError:
            raise
        except Exception as exc:
            if emitted:
                # Deltas from this attempt already reached the client; replaying
                # the stream from scratch would duplicate everything emitted so
                # far (verified: "Hello" + retry "Hello world" -> "HelloHelloHello").
                raise BridgeError(
                    "stream failed after emitting output; refusing to retry "
                    "because the client would receive duplicated text"
                ) from exc
            last_error = exc
            if attempt < cfg.retry_attempts:
                delay = _backoff_delay(cfg.retry_delay_sec, attempt, _exc_retry_after(exc))
                note_gemini_retry()
                log(
                    cfg,
                    f"stream attempt {attempt}/{cfg.retry_attempts} failed: {exc}; "
                    f"retrying in {delay:.1f}s",
                )
                time.sleep(delay)
    if isinstance(last_error, RateLimitError):
        raise RateLimitError(
            f"Gemini stream returned no answer after {cfg.retry_attempts} attempts "
            "(rate limit or login wall?)"
        ) from last_error
    raise BridgeError(f"Gemini stream failed after {cfg.retry_attempts} attempts: {last_error}")


def fetch_build_tag(cfg: BridgeConfig) -> str | None:
    """Scrape the current build tag from Gemini's page.

    Two formats are seen in the wild. The original code matched only
    ``boq_assistant-bard-web-server_*`` and silently gave up when Google
    switched to ``boq_gemini-web-uiserver_*`` -- the auto-update looked like it
    worked because it never reported failure. Both patterns are matched now,
    and an actual result is required for the caller to consider it a success.
    """
    request = urllib.request.Request(
        f"https://gemini.google.com{account_prefix(cfg)}/app", headers={"User-Agent": _UA}
    )
    try:
        if cfg.proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": cfg.proxy, "https": cfg.proxy}),
                urllib.request.HTTPSHandler(context=_ssl_ctx()),
            )
            response = opener.open(request, timeout=15)
        else:
            response = urllib.request.urlopen(request, context=_ssl_ctx(), timeout=15)
        html = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        log(cfg, f"build-tag fetch failed: {exc}")
        return None

    for pattern in (
        r"(boq_gemini-web-uiserver_\d+\.\d+_p\d+)",
        r"(boq_assistant-bard-web-server_\d+\.\d+_p\d+)",
    ):
        match = re.search(pattern, html)
        if match:
            return match.group(1)
    log(cfg, "build-tag not found in page; keeping configured value")
    return None


def refresh_build_tag(cfg: BridgeConfig) -> tuple[BridgeConfig, str | None]:
    """Return (maybe-updated config, human-readable change).

    Returns a new config; never mutates the caller's. ``None`` change means
    nothing was found or the value was already current.
    """
    found = fetch_build_tag(cfg)
    if not found or found == cfg.gemini_bl:
        return cfg, None
    log(cfg, f"build tag updated: {cfg.gemini_bl} -> {found}")
    return cfg.replace(gemini_bl=found), f"{cfg.gemini_bl} -> {found}"
