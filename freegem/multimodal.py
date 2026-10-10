"""Image upload for multimodal requests.

Gemini Web does not accept inline image bytes: the bytes go to a separate
resumable-upload endpoint and only a reference id comes back. This module does
that handshake and also fetches remote images the client passed as URLs.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import BridgeConfig
from .gemini import (
    BridgeError,
    _ssl_ctx,
    account_prefix,
    load_cookie,
    log,
    make_sapisidhash,
)

__all__ = ["detect_image_mime", "fetch_image_bytes", "reset_page_tokens", "upload_image"]

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)
_WEBP = (b"RIFF", b"WEBP")
_FTYP_BRANDS = {
    b"avif": "image/avif",
    b"avis": "image/avif",
    b"heic": "image/heic",
    b"heix": "image/heic",
    b"hevc": "image/hevc",
    b"hevx": "image/hevc",
}

_PAGE_TOKENS: dict[str, Any] = {"tokens": {}, "ts": 0.0, "fetched": False}
_TOKEN_TTL_SEC = 600.0
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def detect_image_mime(data: bytes, fallback: str = "image/png") -> str:
    """Infer a raster MIME type from magic bytes; ``fallback`` when unknown."""
    if not isinstance(data, (bytes, bytearray)):
        return fallback
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == _WEBP[0] and data[8:12] == _WEBP[1]:
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _FTYP_BRANDS:
            return _FTYP_BRANDS[brand]
    return fallback


def _page_tokens(cfg: BridgeConfig) -> dict[str, str]:
    """Fetch and cache the upload-session tokens embedded in Gemini's page.

    The cache records whether a fetch *happened*, separately from whether it
    *succeeded*. Guarding on "tokens is non-empty" instead means a failed fetch
    leaves the cache permanently unsatisfied, so every image after the first
    triggers another authenticated page load — an avoidable request per image
    exactly when the network is already struggling.
    """
    import time

    now = time.time()
    age = now - float(_PAGE_TOKENS["ts"])
    if _PAGE_TOKENS["fetched"] and age < _TOKEN_TTL_SEC:
        return _PAGE_TOKENS["tokens"]
    if not _PAGE_TOKENS["fetched"] and float(_PAGE_TOKENS["ts"]) > 0 and age < 15:
        # Recent FAILED fetch: back off briefly instead of re-scraping an
        # authenticated page per image, but do not pin the failure for the
        # full TTL -- one transient network blip used to lock in the
        # hardcoded fallback tokens for 10 minutes.
        return _PAGE_TOKENS["tokens"]

    headers = {"User-Agent": _UA}
    cookie_str, sapisid = load_cookie(cfg)
    if cookie_str:
        headers["Cookie"] = cookie_str
    if sapisid:
        headers["Authorization"] = make_sapisidhash(str(sapisid))

    tokens: dict[str, str] = {}
    try:
        url = f"https://gemini.google.com{account_prefix(cfg)}/app"
        request = urllib.request.Request(url, headers=headers)
        if cfg.proxy:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": cfg.proxy, "https": cfg.proxy}),
                urllib.request.HTTPSHandler(context=_ssl_ctx()),
            )
            response = opener.open(request, timeout=30)
        else:
            response = urllib.request.urlopen(request, context=_ssl_ctx(), timeout=30)
        html = response.read().decode("utf-8", errors="replace")

        for key, pattern in (
            ("push_id", r'"qKIAYe":"([^"]+)"'),
            ("pctx", r'"Ylro7b":"([^"]+)"'),
        ):
            match = re.search(pattern, html)
            if match:
                tokens[key] = match.group(1)
    except Exception as exc:
        log(cfg, f"upload-token fetch failed: {exc}")

    if tokens:
        _PAGE_TOKENS.update({"tokens": tokens, "ts": now, "fetched": True})
    else:
        # Cache failures as failures: ts records the attempt for the 15s
        # backoff, fetched stays False so a later call retries for real.
        _PAGE_TOKENS.update({"tokens": {}, "ts": now, "fetched": False})
    return tokens


def reset_page_tokens() -> None:
    """Force the next upload to re-read tokens from the page (used by tests)."""
    _PAGE_TOKENS.update({"tokens": {}, "ts": 0.0, "fetched": False})


def upload_image(cfg: BridgeConfig, data: bytes, mime: str, filename: str = "image.png") -> str:
    """Upload bytes and return the reference id to put in the request payload."""
    tokens = _page_tokens(cfg)
    push_id = str(tokens.get("push_id", "feeds/mcudyrk2a4khkz"))
    pctx = str(tokens.get("pctx", "CgcSBWjK7pYx"))
    cookie_str, sapisid = load_cookie(cfg)

    start_headers = {
        "Push-ID": push_id,
        "X-Tenant-Id": "bard-storage",
        "X-Client-Pctx": pctx,
        "X-Goog-Upload-Header-Content-Length": str(len(data)),
        "X-Goog-Upload-Header-Content-Type": mime,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        "User-Agent": _UA,
    }
    if cookie_str:
        start_headers["Cookie"] = cookie_str
    if sapisid:
        start_headers["Authorization"] = make_sapisidhash(str(sapisid))

    start_url = "https://content-push.googleapis.com/upload/"
    request = urllib.request.Request(start_url, data=b"", headers=start_headers, method="POST")
    # Both upload steps carry Cookie + SAPISIDHASH, so both use an opener that
    # refuses redirects outright (see _NoRedirect).
    if cfg.proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": cfg.proxy, "https": cfg.proxy}),
            urllib.request.HTTPSHandler(context=_ssl_ctx()),
            _NoRedirect(),
        )
    else:
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_ssl_ctx()),
            _NoRedirect(),
        )
    response = opener.open(request, timeout=30)

    upload_url = response.headers.get("X-Goog-Upload-URL") or response.headers.get(
        "x-goog-upload-url"
    )
    if not upload_url:
        raise BridgeError("upload endpoint returned no upload URL")
    upload_url = _validate_upload_url(cfg, upload_url)

    upload_headers = {
        "X-Goog-Upload-Command": "upload, finalize",
        "X-Goog-Upload-Offset": "0",
        "Content-Type": "application/octet-stream",
        "User-Agent": _UA,
    }
    if cookie_str:
        upload_headers["Cookie"] = cookie_str
    if sapisid:
        upload_headers["Authorization"] = make_sapisidhash(str(sapisid))

    request2 = urllib.request.Request(upload_url, data=data, headers=upload_headers, method="POST")
    response2 = opener.open(request2, timeout=60)
    file_ref = response2.read().decode("utf-8", errors="replace").strip()

    if not file_ref.startswith("/"):
        raise BridgeError(f"upload returned an unusable reference: {file_ref[:64]!r}")
    log(cfg, f"uploaded {filename} ({len(data)} bytes) -> {file_ref[:48]}...")
    return file_ref


def _validate_upload_url(cfg: BridgeConfig, upload_url: str) -> str:
    """Refuse an upload URL that is not on a Google upload host.

    The second step of the resumable upload posts the image bytes *together
    with the session cookie and the SAPISIDHASH header* to a URL taken from a
    response header. Normally that header is a `googleapis.com` address, but a
    malicious or compromised HTTP proxy — and `proxy` is a supported
    configuration for exactly this project — can answer that header with any
    host it likes and harvest the session. Constrain the host before sending
    anything credential-bearing to it.
    """
    try:
        parsed = urllib.parse.urlparse(upload_url)
    except ValueError as exc:
        raise BridgeError(f"upload endpoint returned an unparseable URL: {exc}") from exc

    if parsed.scheme != "https":
        raise BridgeError(f"refusing non-HTTPS upload URL (scheme {parsed.scheme!r})")

    host = (parsed.hostname or "").lower()
    allowed = {"content-push.googleapis.com", "upload.googleapis.com"}
    if host not in allowed:
        log(cfg, f"refusing upload URL on unexpected host {host!r}")
        raise BridgeError(
            "upload endpoint redirected to an unexpected host; refusing to send "
            "the session cookie there"
        )
    return upload_url


def _host_resolves_public(host: str) -> bool:
    """True when every address ``host`` resolves to is a public IP.

    Client-supplied image URLs are fetched by this process, so an attacker who
    can set the URL can aim it at the user's LAN, loopback services, or cloud
    metadata (169.254.169.254). Resolve first and refuse non-public targets.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False
    return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects on requests that carry session credentials.

    Host validation on the *first* URL is not enough: urllib transparently
    follows 30x and copies the Cookie and SAPISIDHASH headers onto the next
    hop, so a hostile proxy can pass validation and then redirect the
    credentialed POST anywhere. No redirects means one URL, one validated host.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise BridgeError(f"refusing redirect of credentialed request to {newurl!r}")


class _PublicOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Re-check every redirect hop of an uncredentialed image fetch."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in ("http", "https"):
            raise urllib.error.URLError(f"redirect to scheme {parsed.scheme!r} refused")
        if not _host_resolves_public(parsed.hostname or ""):
            raise urllib.error.URLError(f"redirect to non-public host refused: {newurl!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_image_bytes(cfg: BridgeConfig, url: str) -> bytes:
    """Download a remote image. ``file://`` and private hosts are refused."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        log(cfg, f"refusing image fetch for scheme {parsed.scheme or 'none'!r}")
        return b""
    if not _host_resolves_public(parsed.hostname or ""):
        log(cfg, f"refusing image fetch for non-public host {parsed.hostname!r}")
        return b""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": _UA})
        handlers: list = [urllib.request.HTTPSHandler(context=ssl.create_default_context())]
        if cfg.proxy:
            handlers.insert(0, urllib.request.ProxyHandler({"http": cfg.proxy, "https": cfg.proxy}))
        handlers.append(_PublicOnlyRedirect())
        opener = urllib.request.build_opener(*handlers)
        response = opener.open(request, timeout=30)
        return response.read()
    except Exception as exc:
        log(cfg, f"image fetch failed: {exc}")
        return b""


def decode_data_url(url: str) -> tuple[bytes, str] | None:
    """Decode ``data:[mime][;base64],<payload>`` into (bytes, mime)."""
    match = re.match(r"^data:([^;,]+)?(;base64)?,(.*)$", url, re.DOTALL)
    if not match:
        return None
    mime = match.group(1) or "image/png"
    payload = match.group(3)
    try:
        if match.group(2):
            return base64.b64decode(payload, validate=True), mime
        return urllib.parse.unquote_to_bytes(payload), mime
    except (ValueError, TypeError, binascii.Error):
        return None
