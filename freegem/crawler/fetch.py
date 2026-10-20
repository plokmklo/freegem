"""One guarded HTTP GET for the crawler, with an honest outcome taxonomy.

Wraps the same SSRF-pinned opener the scraper uses (private hosts, loopback,
link-local metadata, and non-HTTP schemes are refused at dial time), caps the
body on the wire AND after decompression, and reports WHAT happened instead of
raising a soup of exceptions:

``ok`` fetched -- ``http_error`` got a 4xx/5xx -- ``ssrf`` the guard refused --
``network`` DNS/socket failure -- ``timeout`` -- ``too_large`` body over the
cap -- ``type_refused`` content type the crawler does not parse.

NO robots.txt, BY DESIGN: the crawler never fetches and never consults a
site's robots.txt. A robots.txt is attacker-controlled text served from the
same origin as the pages being read; parsing it (or, worse, feeding it to a
model) is a documented prompt-injection and policy-poisoning vector -- other
scrapers and AI agents have been steered by lines planted in robots.txt.
freegem's stance is that a published page is published to be read: the only
courtesies are per-host pacing and never attacking (no auth bypass, no wall
cracking, no hammering an origin).

Bodies are read STREAMING with a hard cap (default 100 MB). Anything past the
in-memory threshold spills to a temp file (``outcome.body_path``) so a huge
page costs disk, not RAM; ``outcome.text()`` decodes spill files
incrementally with the same charset precedence as in-memory bodies.

Conditional requests: pass ``etag``/``last_modified`` and a 304 comes back as
``ok`` with ``not_modified=True`` and no body, so resume runs re-check pages
without re-downloading them.
"""

from __future__ import annotations

import codecs
import contextlib
import http.cookiejar
import json
import os
import re
import ssl
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

try:  # optional extras: freegem[brotli] -- brotlicffi is the pure-ish build
    import brotlicffi as _brotli
except ImportError:  # pragma: no cover - exercised via the fallback below
    try:
        import brotli as _brotli
    except ImportError:
        _brotli = None
try:  # optional extra: freegem[zstd]
    import zstandard as _zstandard
except ImportError:  # pragma: no cover
    _zstandard = None

from ..config import BridgeConfig
from ..gemini import _ssl_ctx
from ..ssrf import Resolver, SSRFRefused, build_guarded_opener, url_allowed
from ..web_scraper import ScrapeError
from .hardening import ResourceLimits, decode_body

__all__ = ["FetchOutcome", "crawl_fetch", "decode_file", "read_body_stream", "text_of_outcome"]

#: Content types the crawler knows how to cut records out of.
_ALLOWED_MIME_PREFIXES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/json",
    "text/json",
    "application/xml",
    "text/xml",
    "application/rss+xml",
    "application/atom+xml",
    "application/xhtml",
)


def supported_encodings() -> tuple[str, ...]:
    """Encodings we can HONESTLY advertise: every one is decode-able.

    gzip/deflate are stdlib. ``br``/``zstd`` appear only when the optional
    extra is importable (``freegem[brotli]`` / ``freegem[zstd]``) -- never
    advertise an encoding the streaming reader cannot cut: the reader
    enforces byte caps DURING decompression, so a new codec must arrive
    with the same guard, not as an afterthought.
    """
    encodings = ["gzip", "deflate"]
    if _brotli is not None:
        encodings.append("br")
    if _zstandard is not None:
        encodings.append("zstd")
    return tuple(encodings)


#: Default header set: honest freegem identity, decodable encodings only.
_BASE_HEADERS: dict[str, str] = {
    "User-Agent": "freegem-web-scraper/2.6 (+https://github.com/plokmklo/freegem)",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,"
    "application/xml;q=0.8,text/plain;q=0.7,*/*;q=0.5",
    "Accept-Language": "en,*;q=0.5",
    "Accept-Encoding": ", ".join(supported_encodings()),
}

_CHUNK = 65536

_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.IGNORECASE)


@dataclass
class FetchOutcome:
    """What one fetch attempt produced, whatever 'produced' means here."""

    url: str
    kind: str  # ok | http_error | ssrf | network | timeout | too_large | type_refused
    status: int | None = None
    body: bytes | None = None
    #: When the body spilled to disk: path of the temp file (``body`` is None).
    body_path: str | None = None
    #: Total decompressed size in bytes, whether held in memory or spilled.
    body_size: int = 0
    content_type: str = ""
    etag: str | None = None
    last_modified: str | None = None
    final_url: str | None = None
    retry_after: float | None = None
    not_modified: bool = False
    elapsed_ms: int = 0
    error: str | None = None
    #: How the content was obtained: "origin" here; the resilient layer keeps
    #: this field and adds ``fallback`` notes instead.
    via: str = "origin"
    #: Which escalation produced this outcome: "" (plain), "origin_root",
    #: "scheme_swap", "host_swap", "tls_unverified", ...
    fallback: str = ""
    tls_unverified: bool = False
    attempts: int = 1
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.kind == "ok"

    @property
    def blocked(self) -> bool:
        """Auth/bot walls: 401/403/429 and explicit anti-bot signals."""
        return self.status in (401, 403, 429)

    def text(self) -> str:
        """Decoded body for HTML/JSON/JSON-less pages ('' when absent)."""
        if self.body is not None:
            return decode_body(self.body, self.content_type)
        if self.body_path:
            return decode_file(self.body_path, self.content_type)
        return ""

    def json(self) -> Any:
        if self.body is None and not self.body_path:
            return None
        try:
            return json.loads(self.text())
        except ValueError:
            return None

    def summary(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "status": self.status,
            "blocked": self.blocked,
            "elapsed_ms": self.elapsed_ms,
            "bytes": self.body_size or len(self.body or b""),
            "not_modified": self.not_modified,
            "fallback": self.fallback,
            "error": self.error,
        }


def text_of_outcome(outcome: FetchOutcome) -> str:
    """Body as text for pages the crawler accepts; '' otherwise."""
    return outcome.text() if outcome.ok and not outcome.not_modified else ""


def crawl_fetch(
    cfg: BridgeConfig,
    url: str,
    *,
    limits: ResourceLimits | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    extra_headers: dict[str, str] | None = None,
    spill_dir: str = "",
    tls_context: ssl.SSLContext | None = None,
    resolver: Resolver | None = None,
    cookie_jar: http.cookiejar.CookieJar | None = None,
) -> FetchOutcome:
    """Fetch one URL under the full guard stack. Never raises for fetch-level
    problems -- the outcome IS the result. Raises only on programming errors
    (bad URL spelling is normalized away upstream).

    ``tls_context`` swaps the SSL context (the resilient layer passes an
    unverified one as a LAST-resort step for broken certificate chains; the
    resulting outcome is flagged ``tls_unverified`` so callers can disclose
    it). ``extra_headers`` merges over the base set, replacing same-named
    keys -- that is how browser-grade header profiles are applied upstream.
    ``resolver`` swaps the resolve-and-check step (the ladder's DoH fallback);
    ``cookie_jar`` lets origin-granted cookies ride along across attempts.
    """
    cap = limits or ResourceLimits()
    started = time.monotonic()
    outcome = FetchOutcome(url=url, kind="network")

    try:
        url = url_allowed(url)
    except SSRFRefused as exc:
        # A refused ADDRESS (private/loopback/link-local) is the guard working;
        # an unresolvable NAME is a network condition, not an SSRF refusal.
        # The taxonomy matters: the campaign report must not count a dead
        # domain as "the guard caught an attack".
        outcome.kind = "network" if _is_dns_failure(str(exc)) else "ssrf"
        outcome.error = str(exc)
        outcome.elapsed_ms = _ms(started)
        return outcome

    headers = dict(_BASE_HEADERS)
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    if extra_headers:
        headers.update(extra_headers)

    request = urllib.request.Request(url, headers=headers)
    try:
        response = build_guarded_opener(
            cfg.proxy,
            tls_context if tls_context is not None else _ssl_ctx(),
            resolver,
            cookie_jar,
        ).open(request, timeout=cap.timeout_sec)
    except urllib.error.HTTPError as exc:
        outcome.kind = "http_error"
        outcome.status = exc.code
        outcome.headers = _headers_of(exc.headers)
        outcome.retry_after = _retry_after(exc.headers)
        outcome.final_url = getattr(exc, "url", url)
        outcome.error = f"HTTP {exc.code}"
        outcome.elapsed_ms = _ms(started)
        return outcome
    except TimeoutError as exc:
        outcome.kind = "timeout"
        outcome.error = str(exc) or "timed out"
        outcome.elapsed_ms = _ms(started)
        return outcome
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        outcome.kind = "network"
        outcome.error = str(reason)
        outcome.elapsed_ms = _ms(started)
        return outcome
    except SSRFRefused as exc:
        # Same taxonomy rule as the url_allowed gate: a dial that failed to
        # RESOLVE is a network condition; only a refused ADDRESS is the guard.
        outcome.kind = "network" if _is_dns_failure(str(exc)) else "ssrf"
        outcome.error = str(exc)
        outcome.elapsed_ms = _ms(started)
        return outcome

    try:
        outcome.status = response.status
        outcome.headers = _headers_of(response.headers)
        outcome.content_type = (response.headers.get("Content-Type") or "").strip()
        outcome.etag = response.headers.get("ETag")
        outcome.last_modified = response.headers.get("Last-Modified")
        outcome.final_url = response.geturl()
        mime = outcome.content_type.split(";")[0].strip().lower()
        if outcome.status == 304:
            outcome.not_modified = True
            outcome.kind = "ok"
        elif mime and not mime.startswith(_ALLOWED_MIME_PREFIXES):
            outcome.kind = "type_refused"
            outcome.error = f"content type {mime!r} is not parsed"
            response.close()
        else:
            try:
                outcome.body, outcome.body_path, outcome.body_size = read_body_stream(
                    response,
                    max_bytes=cap.max_bytes,
                    spill_bytes=cap.spill_bytes,
                    spill_dir=spill_dir,
                )
                outcome.kind = "ok"
            except ScrapeError as exc:
                _cleanup_spill(outcome.body_path)
                outcome.body_path = None
                outcome.kind = "too_large" if "cap" in str(exc) else "network"
                outcome.error = str(exc)
    except Exception as exc:  # mid-read socket failures land here
        _cleanup_spill(outcome.body_path)
        outcome.body_path = None
        outcome.kind = "network"
        outcome.error = str(exc)
    outcome.elapsed_ms = _ms(started)
    return outcome


# ─── streaming body reader ────────────────────────────────────────────────────


def read_body_stream(
    response: Any,
    *,
    max_bytes: int,
    spill_bytes: int,
    spill_dir: str = "",
) -> tuple[bytes | None, str | None, int]:
    """Read the body up to ``max_bytes`` (wire AND decompressed), spilling to
    a temp file once past ``spill_bytes`` so huge pages never sit in RAM.

    Returns ``(memory_bytes, spill_path, total_size)`` -- exactly one of the
    first two is populated. Raises :class:`ScrapeError` past ``max_bytes``
    (decompression bombs included: the cap is enforced DURING decompression).
    """
    encoding = (
        (getattr(response.headers, "get", lambda *_: "")("Content-Encoding") or "").strip().lower()
    )
    dobj: Any = None
    if encoding in ("gzip", "x-gzip", "deflate"):
        dobj = zlib.decompressobj(wbits=47)
    elif encoding in ("br", "zstd"):
        dobj = _optional_decoder(encoding)
    elif encoding not in ("", "identity"):
        # A server sent an encoding we never advertised (or a brand new
        # one): storing raw compressed bytes as "text" would be worse than
        # an honest failure. Raise; the caller sorts it under ``network``.
        raise ScrapeError(f"undecodable Content-Encoding: {encoding!r}")

    chunks: list[bytes] = []
    spill_fh: Any = None
    spill_path: str | None = None
    total = 0
    try:
        while True:
            wire = response.read(_CHUNK)
            if not wire:
                break
            piece = wire
            if dobj is not None:
                piece = _inflate(dobj, wire, encoding)
            if not piece:
                continue
            total += len(piece)
            if total > max_bytes:
                raise ScrapeError(f"response exceeds the {max_bytes}-byte cap")
            if spill_fh is None:
                if total <= spill_bytes:
                    chunks.append(piece)
                    continue
                if not spill_dir:
                    # No spill home given: keep the hard cap honest by
                    # refusing past the memory budget instead of ballooning.
                    raise ScrapeError(f"response exceeds the {spill_bytes}-byte memory cap")
                os.makedirs(spill_dir, exist_ok=True)
                fd, spill_path = tempfile.mkstemp(
                    prefix="freegem-body-", suffix=".bin", dir=spill_dir
                )
                spill_fh = os.fdopen(fd, "wb")
                for held in chunks:
                    spill_fh.write(held)
                chunks.clear()
            spill_fh.write(piece)
    except BaseException:
        if spill_fh is not None:
            spill_fh.close()
            spill_fh = None
        if spill_path is not None:
            _cleanup_spill(spill_path)
        raise
    if spill_fh is not None:
        spill_fh.close()
    if spill_path is not None:
        return None, spill_path, total
    return b"".join(chunks), None, total


def _optional_decoder(encoding: str) -> Any:
    """Decoder object for an optional codec, or an honest ScrapeError."""
    if encoding == "br":
        if _brotli is None:
            raise ScrapeError(
                "body is brotli-encoded but no decoder is installed (pip install 'freegem[brotli]')"
            )
        return _brotli.Decompressor()
    if _zstandard is None:
        raise ScrapeError(
            "body is zstd-encoded but no decoder is installed (pip install 'freegem[zstd]')"
        )
    return _zstandard.ZstdDecompressor().decompressobj()


def _inflate(dobj: Any, wire: bytes, encoding: str = "gzip") -> bytes:
    """One decompress step; on a bad zlib header retry as raw deflate once."""
    if encoding in ("br", "zstd"):
        try:
            return dobj.decompress(wire)
        except Exception as exc:  # brotli/zstd error types vary by backend
            raise ScrapeError(f"corrupt compressed body: {exc}") from exc
    try:
        return dobj.decompress(wire)
    except zlib.error as exc:
        if dobj.unused_data or getattr(dobj, "unconsumed_tail", b""):
            raise ScrapeError(f"corrupt compressed body: {exc}") from exc
        try:
            dobj2 = zlib.decompressobj(wbits=-15)
            return dobj2.decompress(wire)
        except zlib.error:
            raise ScrapeError(f"corrupt compressed body: {exc}") from exc


def _cleanup_spill(path: str | None) -> None:
    if path:
        with contextlib.suppress(OSError):
            os.unlink(path)


# ─── spill-file decoding ──────────────────────────────────────────────────────


def decode_file(path: str, content_type: str, *, chunk_size: int = 1 << 20) -> str:
    """Decode a spilled body file incrementally, honoring the same charset
    precedence as :func:`decode_body` (header charset, BOM, meta charset,
    UTF-8 replacement). One pass, bounded memory."""
    with open(path, "rb") as fh:
        head = fh.read(2048)
        fh.seek(0)
        encoding = _detect_encoding(head, content_type)
        try:
            decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        except LookupError:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        parts: list[str] = []
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            parts.append(decoder.decode(chunk))
        parts.append(decoder.decode(b"", final=True))
        return "".join(parts)


def _detect_encoding(raw: bytes, content_type: str) -> str:
    """Codec NAME for a body, using decode_body's precedence."""
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    if match:
        name = match.group(1)
        try:
            codecs.lookup(name)
            return name
        except LookupError:
            pass
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            return encoding
    meta = _META_CHARSET.search(raw[:2048])
    if meta:
        name = meta.group(1).decode("ascii", "ignore")
        try:
            codecs.lookup(name)
            return name
        except LookupError:
            pass
    return "utf-8"


def _is_dns_failure(message: str) -> bool:
    """True when an SSRFRefused actually describes a DNS resolution failure."""
    lowered = message.lower()
    markers = (
        "cannot resolve",
        "name resolution",
        "name or service not known",
        "nodename nor servname",
        "no address associated",
        "no addresses resolved",
        "no usable addresses resolved",
        "temporary failure",
        "not known",
    )
    return any(marker in lowered for marker in markers)


def _retry_after(headers: Any, now: datetime | None = None) -> float | None:
    """Retry-After as seconds: the delay-seconds form, or the HTTP-date form
    (RFC 7231) measured against ``now`` (default: real UTC time). A past
    date means "retry now" -> 0.0, matching the gemini.py bridge policy."""
    raw = headers.get("Retry-After") if headers else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        dt = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (dt - reference).total_seconds())


def _headers_of(headers: Any) -> dict[str, str]:
    if not headers:
        return {}
    return {str(k).lower(): str(v) for k, v in headers.items()}


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
