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
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import BridgeConfig

__all__ = [
    "BridgeError",
    "has_httpx",
    "log",
    "make_sapisidhash",
    "generate",
    "generate_stream",
    "load_cookie",
    "account_prefix",
    "clean_text",
]

try:  # optional: only needed for true token-by-token streaming
    import httpx  # type: ignore

    HAS_HTTPX = True
except Exception:  # pragma: no cover - depends on install
    HAS_HTTPX = False


class BridgeError(RuntimeError):
    """User-facing failure from the Gemini side.

    Carries an optional upstream error code so callers can distinguish
    "Google rejected the request" from "the network died".
    """

    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


_SSL_CTX: Optional[ssl.SSLContext] = None
_HTTPX_CLIENT = None
# mtime -> (cookie_str, sapisid) cache so a hot loop does not re-stat the file
_COOKIE_CACHE: Dict[str, Any] = {"mtime": None, "cookie": "", "sapisid": None}


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


def account_prefix(cfg: BridgeConfig) -> str:
    """``/u/<index>`` for non-default Google accounts, else empty."""
    if cfg.auth_user in (None, ""):
        return ""
    return f"/u/{cfg.auth_user}"


def load_cookie(cfg: BridgeConfig) -> Tuple[str, Optional[str]]:
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
        if _COOKIE_CACHE["mtime"] == mtime and _COOKIE_CACHE["cookie"]:
            return _COOKIE_CACHE["cookie"], _COOKIE_CACHE["sapisid"]

        with open(path, "r", encoding="utf-8") as fh:
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

        _COOKIE_CACHE.update(
            {"mtime": mtime, "cookie": cookie_str, "sapisid": sapisid}
        )
        return cookie_str, sapisid
    except Exception as exc:  # noqa: BLE001 - fall back to last known good
        log(cfg, f"cookie load failed, using cached value: {exc}")
        return _COOKIE_CACHE["cookie"], _COOKIE_CACHE["sapisid"]


def make_sapisidhash(sapisid: str) -> str:
    """Google's Authorization header format: ``SAPISIDHASH <ts>_<sha1>``."""
    ts = int(time.time())
    digest = hashlib.sha1(
        f"{ts} {sapisid} https://gemini.google.com".encode()
    ).hexdigest()
    return f"SAPISIDHASH {ts}_{digest}"


_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def build_headers(cfg: BridgeConfig) -> Dict[str, str]:
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


def _persistence_flags(inner: List[Any], temporary: bool) -> None:
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
    file_refs: Optional[List[str]] = None,
    extra_fields: Optional[Dict[int, Any]] = None,
) -> str:
    """Encode a request into Gemini's private wire format.

    The array positions below are not arbitrary: they were read out of Gemini's
    frontend bundle. ``inner[79]`` selects the model, ``inner[17]`` the thinking
    depth. Positions are written explicitly rather than by keyword so that a
    future field can be added without guessing what a sparse dict means
    upstream.
    """
    inner: List[Any] = [None] * 102

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
        r"```(?:python|javascript|text)\?code_(?:reference|stdout)"
        r"&code_event_index=\d+\n.*?```\n?",
        "",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(r"http://googleusercontent\.com/card_content/\d+\n?", "", text)
    return text.strip() if strip else text


def _extract_texts_from_line(line: str) -> List[str]:
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
        texts: List[str] = []
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
        for text in _extract_texts_from_line(line):
            if len(text) > len(best):
                best = text
    return clean_text(best)


def _httpx_client(cfg: BridgeConfig):
    global _HTTPX_CLIENT
    if _HTTPX_CLIENT is None and HAS_HTTPX:
        transport = httpx.HTTPTransport(proxy=cfg.proxy) if cfg.proxy else None
        _HTTPX_CLIENT = httpx.Client(
            transport=transport, timeout=cfg.request_timeout_sec, verify=True
        )
    return _HTTPX_CLIENT


def _post(cfg: BridgeConfig, body: bytes, url: str, headers: Dict[str, str]):
    """Send one request, honouring the configured proxy."""
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
    file_refs: Optional[List[str]] = None,
    extra_fields: Optional[Dict[int, Any]] = None,
) -> str:
    """Non-streaming generation with retry."""
    body = build_payload(prompt, model_id, think_mode, cfg, file_refs, extra_fields).encode()
    url = build_url(cfg)
    headers = build_headers(cfg)

    last_error: Optional[Exception] = None
    for attempt in range(1, cfg.retry_attempts + 1):
        try:
            with _post(cfg, body, url, headers) as response:
                raw = response.read().decode("utf-8", errors="replace")
            return extract_response_text(raw)
        except BridgeError:
            raise  # upstream said no; retrying the same request will not help
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < cfg.retry_attempts:
                log(cfg, f"attempt {attempt}/{cfg.retry_attempts} failed: {exc}; retrying")
                time.sleep(cfg.retry_delay_sec)
    raise BridgeError(f"Gemini request failed after {cfg.retry_attempts} attempts: {last_error}")


def generate_stream(
    cfg: BridgeConfig,
    prompt: str,
    model_id: int,
    think_mode: int,
    file_refs: Optional[List[str]] = None,
    extra_fields: Optional[Dict[int, Any]] = None,
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

    last_error: Optional[Exception] = None
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
                                code=int(match.group(1))
                                if match.group(1).isdigit()
                                else None,
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
                            delta = clean_text(text[len(emitted):], strip=False)
                            emitted = text
                            if delta:
                                yield delta
            return
        except BridgeError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < cfg.retry_attempts:
                log(cfg, f"stream attempt {attempt}/{cfg.retry_attempts} failed: {exc}; retrying")
                time.sleep(cfg.retry_delay_sec)
    raise BridgeError(f"Gemini stream failed after {cfg.retry_attempts} attempts: {last_error}")


def fetch_build_tag(cfg: BridgeConfig) -> Optional[str]:
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
    except Exception as exc:  # noqa: BLE001
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


def refresh_build_tag(cfg: BridgeConfig) -> Tuple[BridgeConfig, Optional[str]]:
    """Return (maybe-updated config, human-readable change).

    Returns a new config; never mutates the caller's. ``None`` change means
    nothing was found or the value was already current.
    """
    found = fetch_build_tag(cfg)
    if not found or found == cfg.gemini_bl:
        return cfg, None
    log(cfg, f"build tag updated: {cfg.gemini_bl} -> {found}")
    return cfg.replace(gemini_bl=found), f"{cfg.gemini_bl} -> {found}"
