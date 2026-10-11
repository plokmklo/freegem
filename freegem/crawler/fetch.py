"""One guarded HTTP GET for the crawler, with an honest outcome taxonomy.

Wraps the same SSRF-pinned opener the scraper uses (private hosts, loopback,
link-local metadata, and non-HTTP schemes are refused at dial time), honors
robots.txt through the shared cache, caps the body on the wire AND after
decompression, and reports WHAT happened instead of raising a soup of
exceptions:

``ok`` fetched -- ``http_error`` got a 4xx/5xx -- ``robots`` robots.txt
disallows -- ``ssrf`` the guard refused -- ``network`` DNS/socket failure --
``timeout`` -- ``too_large`` body over the cap -- ``type_refused`` content
type the crawler does not parse -- ``auth`` 401/403 (the "blocked" bucket
the campaign counts).

Conditional requests: pass ``item.etag``/``item.last_modified`` and a 304
comes back as ``ok`` with ``not_modified=True`` and no body, so resume runs
re-check pages without re-downloading them.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..config import BridgeConfig
from ..gemini import _ssl_ctx
from ..ssrf import SSRFRefused, build_guarded_opener, url_allowed
from ..web_scraper import (
    _UA,
    RobotsCache,
    ScrapeError,
    _read_capped,
)
from .hardening import ResourceLimits, decode_body

__all__ = ["FetchOutcome", "crawl_fetch", "text_of_outcome"]

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

#: Reason buckets, grouped for counters and the campaign report.
_RETRYABLE_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class FetchOutcome:
    """What one fetch attempt produced, whatever 'produced' means here."""

    url: str
    kind: str  # ok | http_error | robots | ssrf | network | timeout | too_large | type_refused
    status: int | None = None
    body: bytes | None = None
    content_type: str = ""
    etag: str | None = None
    last_modified: str | None = None
    final_url: str | None = None
    retry_after: float | None = None
    not_modified: bool = False
    elapsed_ms: int = 0
    error: str | None = None
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
        if self.body is None:
            return ""
        return decode_body(self.body, self.content_type)

    def json(self) -> Any:
        if self.body is None:
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
            "bytes": len(self.body) if self.body else 0,
            "not_modified": self.not_modified,
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
    robots: RobotsCache | None = None,
    etag: str | None = None,
    last_modified: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> FetchOutcome:
    """Fetch one URL under the full guard stack. Never raises for fetch-level
    problems -- the outcome IS the result. Raises only on programming errors
    (bad URL spelling is normalized away upstream)."""
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

    if robots is not None:
        try:
            robots.can_fetch(cfg, url, _UA)
        except ScrapeError as exc:
            outcome.kind = "robots"
            outcome.error = str(exc)
            outcome.elapsed_ms = _ms(started)
            return outcome

    headers: dict[str, str] = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,"
        "application/xml;q=0.8,text/plain;q=0.7,*/*;q=0.5",
        "Accept-Language": "en,*;q=0.5",
        "Accept-Encoding": "gzip",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    if extra_headers:
        headers.update(extra_headers)

    request = urllib.request.Request(url, headers=headers)
    try:
        response = build_guarded_opener(cfg.proxy, _ssl_ctx()).open(
            request, timeout=cap.timeout_sec
        )
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
                outcome.body = _read_capped(response, limit=cap.max_bytes)
                outcome.kind = "ok"
            except ScrapeError as exc:
                outcome.kind = "too_large" if "cap" in str(exc) else "network"
                outcome.error = str(exc)
    except Exception as exc:  # mid-read socket failures land here
        outcome.kind = "network"
        outcome.error = str(exc)
    outcome.elapsed_ms = _ms(started)
    return outcome


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


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("Retry-After") if headers else None
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _headers_of(headers: Any) -> dict[str, str]:
    if not headers:
        return {}
    return {str(k).lower(): str(v) for k, v in headers.items()}


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
