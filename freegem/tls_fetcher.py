"""The tls-browser rung: a current Chrome TLS fingerprint through curl_cffi.

Research context (docs/ANTIBOT_RESEARCH.md, v2.12.0 sweep): the single most
effective non-browser technique against TLS-fingerprinting bot walls is to
speak the SAME TLS as the browser whose headers you are already sending.
Python's stdlib TLS stack (and requests', and httpx's) produces a JA3 /
HTTP/2 fingerprint that no real Chrome has ever produced; sites that score
that fingerprint answer even a perfectly headered request with 403. The
community answer is `curl-impersonate` / `lexiforest/curl_cffi`: libcurl
with Chrome's exact ClientHello, extension order, ALPN, and HTTP/2
settings. Academically this closes the gap the fingerprinting literature
describes (the site can no longer separate "library client" from
"browser" on TLS alone).

What this module does, precisely:

* ONE fixed, current Chrome impersonation target per call --
  ``impersonate="chrome"`` -- and NO header overrides: curl_cffi sets the
  matching browser headers itself, because a mismatched header/TLS pair is
  itself a detection signal.
* Cookie warm-up, the community's other standard trick: some walls 403
  the FIRST request and clear on the second once ``Set-Cookie`` has been
  seen (Cloudflare's __cf_bm, Akamai's _abck round-trip). A curl_cffi
  ``Session`` carries cookies across two GETs inside ONE rung call; the
  session (and its cookies) is discarded at the end -- nothing persists.
* Honors ``cfg.proxy`` like every other fetch path: users who need
  egress infrastructure supply their own (NON_GOALS unchanged).

What it does NOT do, ever: CAPTCHA solving, challenge answering, header
rotation, identity rotation, referer/origin forgery. It is one honest
reader that happens to present the same TLS hello as the browser it is
modeled on -- the fetch-level equivalent of the existing
``browser-headers`` rung. OFF by default (config
``smart_fetch_tls_browser``): an explicit user choice, like
browser-render, never a silent fallback. Failures raise :class:`TlsFetchError`
with the exact status; the ladder records them honestly.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

__all__ = ["TlsFetchError", "is_curl_cffi_available", "tls_fetch_html"]

#: Warm-up retry only for statuses that mean "wall", not "wrong URL".
_WALL_STATUSES = frozenset({403, 405, 407, 429, 503})

#: One warm-up retry, not a hammer: the second GET is the cookie-clearing
#: shot community practice settles on.
_MAX_ATTEMPTS = 2

_availability_cache: bool | None = None


class TlsFetchError(Exception):
    """The tls-browser rung could not read the page; message says exactly why."""


def is_curl_cffi_available() -> bool:
    """True when the optional ``curl_cffi`` dependency imports."""
    global _availability_cache
    if _availability_cache is None:
        try:
            import curl_cffi  # noqa: F401

            _availability_cache = True
        except ImportError:
            _availability_cache = False
    return _availability_cache


def _proxies(cfg: Any) -> dict[str, str] | None:
    """curl_cffi proxy mapping from the same ``cfg.proxy`` every path uses."""
    proxy = getattr(cfg, "proxy", None)
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def tls_fetch_html(cfg: Any, url: str, *, timeout: float) -> str:
    """Fetch ``url`` through a Chrome-impersonated curl_cffi session.

    Two attempts at most inside one fresh ``Session``: the first GET, and
    -- only when the first hit a wall-class status -- one warm retry with
    whatever cookies the wall set. Returns the response body as text.
    Raises :class:`TlsFetchError` with the exact status on refusal, or
    when curl_cffi is not installed.
    """
    if not is_curl_cffi_available():
        raise TlsFetchError(
            f"tls-browser unavailable for {url!r}: curl_cffi is not installed "
            "(pip install curl_cffi)"
        )
    from curl_cffi import requests as curl_requests

    proxies: Any = _proxies(cfg)
    wall_sec = max(1.0, float(timeout))
    last_status = 0
    try:
        session: Any = curl_requests.Session(impersonate="chrome", timeout=wall_sec)
    except Exception as exc:  # pragma: no cover - depends on binary wheel
        raise TlsFetchError(f"tls-browser session setup failed for {url!r}: {exc}") from exc
    try:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = session.get(url, proxies=proxies)
            except Exception as exc:
                raise TlsFetchError(f"tls-browser fetch failed for {url!r}: {exc}") from exc
            last_status = int(getattr(response, "status_code", 0) or 0)
            body = response.text or ""
            if 200 <= last_status < 300 and body.strip():
                return body
            if last_status not in _WALL_STATUSES or attempt == _MAX_ATTEMPTS - 1:
                raise TlsFetchError(f"tls-browser got HTTP {last_status} for {url!r}")
            # Warm-up beat before the retry: give the wall's cookie a breath,
            # exactly one, then present the same session again.
            time.sleep(0.4)
        raise TlsFetchError(  # pragma: no cover - loop always returns/raises
            f"tls-browser exhausted {_MAX_ATTEMPTS} attempts for {url!r} (last HTTP {last_status})"
        )
    finally:
        # The session and its warmed cookies die here; nothing persists.
        with contextlib.suppress(Exception):
            session.close()
