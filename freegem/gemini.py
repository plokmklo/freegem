"""Gemini Web StreamGenerate client.

Talks to the same endpoint the Gemini web app uses, translating between a
plain prompt and Gemini's internal wire format. Cookie-authenticated when a
session file is configured, anonymous otherwise.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import ssl
import sys
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

from .config import BridgeConfig, redact_proxy
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

    ``kind`` records the best-effort classification of the empty answer:
    ``"rate_limit"`` (back off and retry) or ``"login_wall"`` (the session
    needs re-auth; retrying will not help). It defaults to
    ``"rate_limit"`` when the body gives no signal -- see
    :func:`_classify_empty_answer` for the exact signals used.
    """

    def __init__(self, message: str, code: int | None = None, kind: str = "rate_limit"):
        super().__init__(message, code)
        self.kind = kind


#: Body fragments that identify the sign-in page Gemini serves instead of an
#: answer when the session is dead. Matched case-insensitively against the
#: whole response body.
_LOGIN_WALL_MARKERS = (
    "accounts.google.com/servicelogin",
    "accounts.google.com/signin",
    "accounts.google.com/v3/signin",
    "sign in to continue",
    "sign in with your google account",
)

_KIND_TEXT = {"rate_limit": "rate limit", "login_wall": "login wall"}


def _classify_empty_answer(body: str) -> str:
    """Best-effort kind for an empty-200: ``"login_wall"`` or ``"rate_limit"``.

    A login wall is recognizable only by content markers: a sign-in page
    carries identifiable fragments while a rate-limit page is empty. Whether
    the request carried session cookies was considered as a second signal
    and rejected -- an authenticated request can still be served a bare
    login page (expired cookies) and an anonymous one a bare rate limit, so
    cookie presence cannot discriminate. Undeterminable falls back to the
    documented default ``"rate_limit"``: back off and retry, which is also
    the safe action when the guess is wrong.
    """
    lowered = (body or "").lower()
    for marker in _LOGIN_WALL_MARKERS:
        if marker in lowered:
            return "login_wall"
    return "rate_limit"


class _StreamRewriteError(BridgeError):
    """A Gemini streaming snapshot did not extend the previous one.

    Gemini serves progressively longer snapshots rather than true deltas. When a
    snapshot does not begin with what was already emitted (the model's output
    was rewritten mid-stream), retrying the whole request is safe ONLY if no
    meaningful output reached the client yet. If ``emitted`` is empty, this
    behaves as a transient retry; if non-empty, the stream aborts to avoid
    duplicating partial output.
    """


class _StaleBuildTagError(BridgeError):
    """The upstream answered with a ``BardErrorInfo`` marker.

    In practice that marker means the configured build tag no longer matches
    what Google serves, or the session died. It is an internal subtype so the
    retry loops can trigger the once-per-process build-tag recovery
    (:func:`_refresh_build_tag_once`) without string-matching the message;
    it changes nothing for callers catching :class:`BridgeError`.
    """


_SSL_CTX: ssl.SSLContext | None = None
# (abspath, mtime, size) -> (cookie_str, sapisid, xsrf) cache so a hot loop does
# not re-read the file. The path and size are part of the key: mtime alone let
# two cookie files with identical mtimes share one session (cross-account leak)
# and missed same-tick rewrites. The server handles requests on threads, so
# every read AND write of the dict goes through _COOKIE_CACHE_LOCK: unlocked,
# a miss->parse->update sequence can publish a torn entry (new key, old
# cookie) that briefly pairs one account's path with another's session.
_COOKIE_CACHE: dict[str, Any] = {"key": None, "cookie": "", "sapisid": None, "xsrf": None}
_COOKIE_CACHE_LOCK = threading.Lock()


def _safe_error_text(cfg: BridgeConfig, exc: Exception | None) -> str:
    """Render an exception for user-facing text without leaking secrets.

    Transport errors routinely embed the configured proxy URL verbatim --
    including any ``user:pass@`` credentials -- so every occurrence of the
    configured proxy string is replaced with its credential-free form
    (:func:`freegem.config.redact_proxy`) before it reaches a log line or a
    caller's terminal.
    """
    text = "" if exc is None else str(exc)
    proxy = cfg.proxy
    if proxy and proxy in text:
        redacted = redact_proxy(proxy)
        if redacted != proxy:
            text = text.replace(proxy, redacted)
    return text


def log(cfg: BridgeConfig, message: str) -> None:
    if not cfg.log_requests:
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

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


def _is_retryable_status(status: int) -> bool:
    """Only transient upstream statuses are worth a retry.

    429 (rate limit), 408 (request timeout) and the whole 5xx class are
    transient by nature. Every other 4xx -- 400 bad request, 401/403 auth
    failures -- reproduces identically on every attempt: the retry loop
    rebuilds neither the payload nor the headers, so hammering the endpoint
    only adds latency to an already-failed request.
    """
    if status == 429 or status == 408:
        return True
    return 500 <= status <= 599


def _backoff_delay(base: float, attempt: int, retry_after: float | None = None) -> float:
    """How long attempt N's failure should wait before retrying.

    Exponential in the attempt number (``base * 2^(N-1)``), capped so a long
    run of failures cannot wedge the caller. A server-provided Retry-After is
    honored when it asks for more than the exponential delay would -- the
    server knows its own capacity better than our curve does -- but is capped
    by the same ceiling.

    A half-random jitter (``±50%``) is folded into the exponential delay so
    that concurrent workers (e.g. the 3-worker benchmark) do not all retry on
    the same tick and re-trigger a fresh 429. The jitter never pushes the
    delay to zero or beyond the cap; Retry-After overrides it when present.
    """
    delay = min(base * (2 ** (attempt - 1)), _RETRY_BACKOFF_CAP_SEC)
    if retry_after is not None and retry_after > delay:
        # Honor the server's own hint when it asks for more than our curve.
        return min(retry_after, _RETRY_BACKOFF_CAP_SEC)
    # ±50% jitter prevents synchronized retries across concurrent workers
    # without ever zeroing the backoff or exceeding the cap. When the
    # exponential curve is already pinned at the ceiling, jitter is skipped:
    # there is nothing to spread and reducing a capped delay below the cap
    # would only invite fresh retries at exactly the wrong moment.
    if delay >= _RETRY_BACKOFF_CAP_SEC:
        return _RETRY_BACKOFF_CAP_SEC
    jitter = random.uniform(-0.5, 0.5) * delay
    return min(max(delay + jitter, 0.001), _RETRY_BACKOFF_CAP_SEC)


def account_prefix(cfg: BridgeConfig) -> str:
    """``/u/<index>`` for non-default Google accounts, else empty."""
    if cfg.auth_user in (None, ""):
        return ""
    return f"/u/{cfg.auth_user}"


def _read_session(cfg: BridgeConfig) -> tuple[str, str | None, str | None]:
    """Read the session file; return ``(cookie, sapisid, xsrf_token)``.

    Accepts both shapes the cookie-sync extension exports: a JSON object with
    ``cookie``/``sapisid`` (and optionally ``xsrf_token``), or a bare cookie
    string. Results are cached by ``(abspath, mtime, size)``.

    Two hard rules, both learned from production incidents:

    - A failure (unreadable, unparsable) may only fall back to the last good
      value cached for THE SAME PATH. A parse failure while reading cookie
      file B must never serve file A's session -- that is a cross-account
      leak. Any other case degrades to anonymous.
    - The content is read FIRST and the cache key is computed after, with a
      byte-count check against the post-read stat. The old stat-then-read
      order could cache a half-written snapshot under a key that later
      requests would consider current.
    """
    path = cfg.cookie_file
    if not path:
        return "", None, None
    import os

    if not os.path.exists(path):
        return "", None, None
    abspath = os.path.abspath(path)
    try:
        probe = (abspath, os.path.getmtime(path), os.path.getsize(path))
        with _COOKIE_CACHE_LOCK:
            if _COOKIE_CACHE["key"] == probe and _COOKIE_CACHE["cookie"]:
                return _COOKIE_CACHE["cookie"], _COOKIE_CACHE["sapisid"], _COOKIE_CACHE["xsrf"]

        with open(path, "rb") as fh:
            raw = fh.read()
        size_after = os.path.getsize(path)
        key = (abspath, os.path.getmtime(path), size_after)
        if len(raw) != size_after:
            raise OSError(
                f"session file changed while reading "
                f"({len(raw)} of {size_after} bytes); refusing a partial read"
            )
        content = raw.decode("utf-8").strip()
        if content.startswith("{"):
            data = json.loads(content)
            cookie_str = str(data.get("cookie", "") or "")
            sapisid = data.get("sapisid") or None
            xsrf = data.get("xsrf_token") or None
        else:
            cookie_str = content
            sapisid = None
            xsrf = None
            for pair in cookie_str.split(";"):
                if "=" not in pair:
                    continue
                k, v = pair.strip().split("=", 1)
                if k == "SAPISID":
                    sapisid = v or None
                    break

        with _COOKIE_CACHE_LOCK:
            _COOKIE_CACHE.update(
                {"key": key, "cookie": cookie_str, "sapisid": sapisid, "xsrf": xsrf}
            )
        return cookie_str, sapisid, xsrf
    except Exception as exc:
        log(cfg, f"cookie load failed, using cached value: {exc}")
        with _COOKIE_CACHE_LOCK:
            cached_key = _COOKIE_CACHE["key"]
            # Same-path guard: the cache may only serve the file that
            # produced it. Another file's key (or no cache at all) must
            # degrade to anonymous instead of leaking a session.
            if cached_key is not None and cached_key[0] == abspath and _COOKIE_CACHE["cookie"]:
                return _COOKIE_CACHE["cookie"], _COOKIE_CACHE["sapisid"], _COOKIE_CACHE["xsrf"]
        return "", None, None


def load_cookie(cfg: BridgeConfig) -> tuple[str, str | None]:
    """Read the session file, cached by (path, mtime, size).

    Accepts both shapes the cookie-sync extension exports: a JSON object with
    ``cookie``/``sapisid``, or a bare cookie string.
    On parse failure the last good value is kept rather than failing open to
    anonymous: a mid-write file should not silently downgrade the session.
    A failure on a *different* file never returns this file's session -- see
    :func:`_read_session` for the exact guard.
    """
    cookie, sapisid, _xsrf = _read_session(cfg)
    return cookie, sapisid


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

# Build-tag recovery state (issue #38): refreshed at most once per process,
# set when a BardErrorInfo rejection triggers _refresh_build_tag_once. The
# override is preferred by build_url for the rest of the process.
_BUILD_TAG_REFRESH_LOCK = threading.Lock()
_BUILD_TAG_REFRESHED = False
_BUILD_TAG_OVERRIDE: str | None = None


def _load_xsrf(cfg: BridgeConfig) -> str | None:
    """The xsrf token for the payload: config first, then the cookie file.

    The cookie-sync refresh script writes ``xsrf_token`` into the session JSON
    next to ``cookie``/``sapisid``, but the loader used to drop that field, so
    a config that never set the separate ``xsrf_token`` key silently sent its
    requests without the ``at`` parameter (and Google rejected or degraded
    them). Both sources are honored here; the explicit config wins.
    """
    if cfg.xsrf_token:
        return cfg.xsrf_token
    return _read_session(cfg)[2]


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
    xsrf = _load_xsrf(cfg)
    if xsrf:
        params["at"] = xsrf
    return urllib.parse.urlencode(params)


def build_url(cfg: BridgeConfig) -> str:
    reqid = int(time.time()) % 1000000
    prefix = account_prefix(cfg)
    # A mid-process refresh (see _refresh_build_tag_once) wins over the
    # configured tag: the process learned the configured one is stale the
    # hard way, and callers hold frozen configs that cannot be updated
    # underneath them.
    bl = _BUILD_TAG_OVERRIDE or cfg.gemini_bl
    return (
        f"https://gemini.google.com{prefix}/_/BardChatUi/data/"
        "assistant.lamda.BardFrontendService/StreamGenerate"
        f"?bl={bl}&hl=en&_reqid={reqid}&rt=c"
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

    Slot 0 of the line is the primary answer snapshot; the remaining strings
    are siblings (cards, related-question chips) that callers must not treat
    as answer text -- see ``extract_response_text`` and the streaming loop,
    both of which read slot 0 only.
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
        raise _StaleBuildTagError(
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
            # Separate connect (fast-fail for unreachable hosts) from read
            # (longer allowance for slow servers). A single blanket timeout
            # either fails too fast on slow reads or hangs too long on dead
            # hosts. Connect gets 10s; read gets the configured timeout.
            timeout = httpx.Timeout(
                connect=10.0,
                read=cfg.request_timeout_sec,
                write=10.0,
                pool=cfg.request_timeout_sec,
            )
            client = httpx.Client(transport=transport, timeout=timeout, verify=True)
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
                    # content: null while the retry loop never engaged. Other
                    # 4xx codes fail fast -- see _is_retryable_status.
                    if _is_retryable_status(status):
                        raise _UpstreamHTTPStatus(
                            int(status), _retry_after_seconds(response.headers)
                        )
                    raise BridgeError(
                        f"upstream returned HTTP {status} (client error; not retried)",
                        code=int(status),
                    )
                raw = response.read().decode("utf-8", errors="replace")
            text = extract_response_text(raw)
            if not text.strip():
                # A 200 login-wall or rate-limit page carries no BardErrorInfo
                # marker; returning "" made a failed request look like a model
                # that chose silence.
                kind = _classify_empty_answer(raw)
                raise RateLimitError(
                    f"upstream returned an empty answer (kind={kind}; {_KIND_TEXT[kind]})",
                    kind=kind,
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
        except _StaleBuildTagError:
            # One-time recovery: the marker usually means the build tag went
            # stale mid-process. Best effort -- the original error still
            # propagates (retrying the same body cannot help; the NEXT
            # request picks up the refreshed tag).
            _refresh_build_tag_once(cfg)
            raise
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
            # urllib's fallback path lands here (no status wrapper): extract
            # Retry-After exactly like the streaming branch does.
            last_error = exc
            if attempt < cfg.retry_attempts:
                delay = _backoff_delay(cfg.retry_delay_sec, attempt, _exc_retry_after(exc))
                note_gemini_retry()
                log(
                    cfg,
                    # Redact like the streaming branch: transport errors embed
                    # the proxy URL (with credentials) verbatim, and a
                    # retry-loop log line must not be the leak.
                    f"attempt {attempt}/{cfg.retry_attempts} failed: "
                    f"{_safe_error_text(cfg, exc)}; "
                    f"retrying in {delay:.1f}s",
                )
                time.sleep(delay)
    if isinstance(last_error, RateLimitError):
        # Surface the typed error, not a generic BridgeError: callers poll
        # for exactly this shape to decide "back off for minutes" vs "give up".
        kind = last_error.kind
        raise RateLimitError(
            f"Gemini returned no answer after {cfg.retry_attempts} attempts "
            f"(kind={kind}; {_KIND_TEXT[kind]})",
            kind=kind,
        ) from last_error
    raise BridgeError(
        f"Gemini request failed after {cfg.retry_attempts} attempts: "
        f"{_safe_error_text(cfg, last_error)}"
    )


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
        streamed = ""
        try:
            with client.stream("POST", url, content=body, headers=headers) as response:
                # Same policy as the non-streaming path: raise_for_status()
                # alone would retry every 4xx (httpx raises through the
                # generic except branch), so auth rejections fail fast here.
                stream_status = getattr(response, "status_code", None)
                if (
                    isinstance(stream_status, int)
                    and stream_status >= 400
                    and not _is_retryable_status(stream_status)
                ):
                    raise BridgeError(
                        f"upstream returned HTTP {stream_status} (client error; not retried)",
                        code=stream_status,
                    )
                response.raise_for_status()
                buffer = ""
                for chunk in response.iter_text():
                    buffer += chunk
                    streamed += chunk
                    if "BardErrorInfo" in buffer:
                        match = re.search(r"BardErrorInfo\s*\[(\d+)\]", buffer)
                        if match:
                            raise _StaleBuildTagError(
                                "Gemini rejected the request during streaming "
                                f"(BardErrorInfo {match.group(1)}).",
                                code=int(match.group(1)) if match.group(1).isdigit() else None,
                            )
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        texts = _extract_texts_from_line(line)
                        if not texts:
                            continue
                        # Slot 0 is the answer snapshot (the same choice
                        # extract_response_text makes); siblings -- cards,
                        # related-question chips -- are not deltas of the
                        # answer and must never trip the rewrite guard.
                        text = texts[0]
                        if text == emitted or emitted.startswith(text):
                            continue
                        if not text.startswith(emitted):
                            raise _StreamRewriteError(
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
                    texts = _extract_texts_from_line(buffer)
                    if texts:
                        text = texts[0]
                        if not (text == emitted or emitted.startswith(text)):
                            if not text.startswith(emitted):
                                raise _StreamRewriteError(
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
                kind = _classify_empty_answer(streamed)
                raise RateLimitError(
                    f"upstream returned an empty answer (kind={kind}; {_KIND_TEXT[kind]})",
                    kind=kind,
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
        except _StaleBuildTagError:
            # Same one-time recovery as the non-streaming path.
            _refresh_build_tag_once(cfg)
            raise
        except _StreamRewriteError as exc:
            # Gemini rewrote a snapshot that was never emitted. If nothing
            # reached the client (emitted is empty), retry is safe -- but
            # in practice the rewrite check only fires after at least one
            # chunk was extracted, so this usually falls through to the
            # BridgeError path and the CLI's non-streaming fallback handles
            # recovery. Keep the guard so a parser refactor cannot silently
            # duplicate partial output.
            if emitted:
                raise
            last_error = exc
            if attempt < cfg.retry_attempts:
                delay = _backoff_delay(cfg.retry_delay_sec, attempt, None)
                note_gemini_retry()
                log(
                    cfg,
                    f"stream attempt {attempt}/{cfg.retry_attempts} had a "
                    f"mid-stream rewrite before any output; retrying in {delay:.1f}s",
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
                    f"stream attempt {attempt}/{cfg.retry_attempts} failed: "
                    f"{_safe_error_text(cfg, exc)}; "
                    f"retrying in {delay:.1f}s",
                )
                time.sleep(delay)
    if isinstance(last_error, RateLimitError):
        kind = last_error.kind
        raise RateLimitError(
            f"Gemini stream returned no answer after {cfg.retry_attempts} attempts "
            f"(kind={kind}; {_KIND_TEXT[kind]})",
            kind=kind,
        ) from last_error
    raise BridgeError(
        f"Gemini stream failed after {cfg.retry_attempts} attempts: "
        f"{_safe_error_text(cfg, last_error)}"
    )


def _page_headers(cfg: BridgeConfig) -> dict[str, str]:
    """GET headers for a gemini.google.com page fetch, session-aware.

    Mirrors :func:`build_headers`' authentication (Cookie + SAPISIDHASH,
    account selector) without the POST-only content headers.
    """
    headers = {"User-Agent": _UA}
    cookie_str, sapisid, _xsrf = _read_session(cfg)
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(str(sapisid))
    if account_prefix(cfg):
        headers["X-Goog-AuthUser"] = str(cfg.auth_user)
    return headers


def _refresh_build_tag_once(cfg: BridgeConfig) -> None:
    """One per-process attempt to recover from a stale build tag.

    A ``BardErrorInfo`` marker usually means the configured ``bl`` tag no
    longer matches what Google serves. The build tag is otherwise refreshed
    only at serve startup, so a long-running process would keep failing until
    restarted. On the FIRST rejection in this process, re-scrape the tag
    through the session: signed-out, ``/app`` answers with a redirect to
    accounts.google.com whose HTML never contains a build tag.

    The attempt flag is consumed BEFORE the network call: with a broken
    network, one failure must not turn every subsequent request into an
    extra scrape. The fresh value is stored process-wide and preferred by
    :func:`build_url` -- caller configs are frozen dataclasses that cannot
    be updated underneath them.
    """
    global _BUILD_TAG_REFRESHED, _BUILD_TAG_OVERRIDE
    if not cfg.has_session_cookie():
        # The recovery fetch is session-authenticated; without a session
        # there is nothing to authenticate with (and the anonymous
        # fetch_build_tag already ran at startup).
        return
    with _BUILD_TAG_REFRESH_LOCK:
        if _BUILD_TAG_REFRESHED:
            return
        _BUILD_TAG_REFRESHED = True
        try:
            found = fetch_build_tag(cfg, session=True)
        except Exception as exc:
            log(cfg, f"build-tag refresh after rejection failed: {_safe_error_text(cfg, exc)}")
            return
        if found and found != cfg.gemini_bl:
            log(cfg, f"build tag auto-refreshed after rejection: {cfg.gemini_bl} -> {found}")
            _BUILD_TAG_OVERRIDE = found


def fetch_build_tag(cfg: BridgeConfig, session: bool = False) -> str | None:
    """Scrape the current build tag from Gemini's page.

    Two formats are seen in the wild. The original code matched only
    ``boq_assistant-bard-web-server_*`` and silently gave up when Google
    switched to ``boq_gemini-web-uiserver_*`` -- the auto-update looked like it
    worked because it never reported failure. Both patterns are matched now,
    and an actual result is required for the caller to consider it a success.

    ``session=True`` sends the same session headers a StreamGenerate call
    would (see :func:`_page_headers`) so the fetch works for logged-in
    sessions; the default keeps the historical anonymous scrape.
    """
    request = urllib.request.Request(
        f"https://gemini.google.com{account_prefix(cfg)}/app",
        headers=_page_headers(cfg) if session else {"User-Agent": _UA},
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
        log(cfg, f"build-tag fetch failed: {_safe_error_text(cfg, exc)}")
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
