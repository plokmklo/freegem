"""Shared SSRF guard: resolve, check, and pin the connection to the checked IP.

The original guard (``multimodal._host_resolves_public``) resolved the host,
checked that every resolved address was public, and then let urllib connect --
which resolves the host a *second* time. In that window an attacker-controlled
DNS server can answer with a private address the second time (DNS rebinding).
This module closes the gap: the handler resolves once, checks that address,
and dials exactly that IP, while TLS keeps using the original hostname for
SNI and certificate verification.

Every fetch of an untrusted, user-supplied URL should go through
:func:`build_guarded_opener`. Redirects stay covered: urllib redispatches each
hop through the same handlers, so every hop is resolved, checked, and pinned
again.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

__all__ = [
    "PinnedHTTPHandler",
    "PinnedHTTPSHandler",
    "SSRFRefused",
    "SafeRedirect",
    "build_guarded_opener",
    "pick_public_ip",
    "pick_public_ips",
    "url_allowed",
]


class SSRFRefused(Exception):
    """A URL or host failed the SSRF guard and must not be contacted."""


def _host_only(host: str) -> str:
    """Bare hostname out of a ``req.host``-style value.

    ``req.host`` can carry a port (``example.com:8080``) or bracketed IPv6
    (``[::1]:443``). ``parsed.hostname`` arrives here already bare.
    """
    host = host.strip()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end != -1 else host.lstrip("[")
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def _checked_addresses(host: str, infos: list[tuple[Any, ...]]) -> list[str]:
    """Return every resolved address after verifying ALL of them are public.

    The whole-resolution contract matches ``multimodal._host_resolves_public``:
    if ANY resolved address is loopback, private, link-local, reserved,
    multicast, or unspecified, the host is refused -- not just the dialed one.
    A resolution failure is refusal, never "allow".

    The list is deduplicated (getaddrinfo repeats entries per socket type) but
    keeps the resolver's order: the dialer will try them in sequence, staying
    inside the checked set the whole time.
    """
    checked: list[str] = []
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise SSRFRefused(f"{host!r} resolved an unparseable address {raw!r}") from exc
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
            raise SSRFRefused(f"{host!r} resolves to a non-public address ({ip})")
        if str(ip) not in checked:
            checked.append(str(ip))
    if not checked:
        raise SSRFRefused(f"no usable addresses resolved for {host!r}")
    return checked


def pick_public_ips(host: str) -> list[str]:
    """Resolve ``host`` and return its verified-public IPs to dial, in order.

    Callers must dial ONLY addresses from this list (that is the whole point)
    while keeping the original hostname for TLS SNI and certificate
    verification. Trying the list in order -- instead of betting on the first
    entry -- mirrors what stdlib ``create_connection`` does for a hostname,
    without ever letting it re-resolve: dual-stack hosts work on IPv4-only and
    IPv6-only networks alike.
    """
    bare = _host_only(host)
    if not bare:
        raise SSRFRefused("refusing empty host")
    try:
        infos = socket.getaddrinfo(bare, None)
    except OSError as exc:
        raise SSRFRefused(f"cannot resolve {bare!r}: {exc}") from exc
    if not infos:
        raise SSRFRefused(f"no addresses resolved for {bare!r}")
    return _checked_addresses(bare, infos)


def pick_public_ip(host: str) -> str:
    """The first checked-public IP of ``host`` (see :func:`pick_public_ips`).

    Handy for point checks -- redirect validation -- that only need "would we
    allow this host at all".
    """
    return pick_public_ips(host)[0]


def url_allowed(url: str) -> str:
    """Pre-flight a user-supplied URL: http(s) only, no credentials, a host.

    This is the cheap syntactic half of the guard; the network half (public-IP
    resolution and pinning) happens inside the handlers at connect time.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise SSRFRefused(f"unparseable URL: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise SSRFRefused(f"refusing scheme {parsed.scheme or 'none'!r}")
    if parsed.username or parsed.password:
        raise SSRFRefused("refusing URL with embedded credentials")
    if not (parsed.hostname or "").strip():
        raise SSRFRefused("refusing URL without a host")
    if any(ch.isspace() for ch in url):
        raise SSRFRefused("refusing URL containing whitespace")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Re-validate the scheme and the host of every redirect hop.

    In direct mode each hop is ALSO re-resolved, re-checked, and re-pinned by
    the pinned handlers, so this class is belt and suspenders there. In proxy
    mode there are no pinned handlers (the proxy resolves DNS), so THIS is the
    per-hop check.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: Any,
        msg: Any,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in ("http", "https"):
            raise urllib.error.URLError(f"redirect to scheme {parsed.scheme!r} refused")
        try:
            pick_public_ip(parsed.hostname or "")
        except SSRFRefused as exc:
            raise urllib.error.URLError(f"redirect refused: {exc}") from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _pinned_http_class(pinned_ips: list[str]) -> type[http.client.HTTPConnection]:
    """An HTTPConnection that dials the pre-checked IPs instead of re-resolving."""

    class PinnedHTTPConnection(http.client.HTTPConnection):
        def __init__(self, host: str, **kwargs: Any) -> None:
            super().__init__(host, **kwargs)
            self._pinned_ips = pinned_ips

        def connect(self) -> None:
            # Dial the CHECKED IPs, in order, and nothing else. self.host is
            # kept for the Host header but is never resolved again -- that is
            # the anti-rebinding point. Trying the whole list (rather than the
            # first entry) is what makes dual-stack hosts work on IPv4-only
            # and IPv6-only networks alike.
            last_error: OSError | None = None
            for ip in self._pinned_ips:
                try:
                    self.sock = socket.create_connection((ip, self.port), self.timeout)
                    return
                except OSError as exc:
                    last_error = exc
            if last_error is not None:
                raise last_error
            raise OSError("no checked addresses to dial")

    return PinnedHTTPConnection


def _pinned_https_class(
    pinned_ips: list[str], context: ssl.SSLContext
) -> type[http.client.HTTPSConnection]:
    """An HTTPSConnection that dials the pinned IPs with hostname-verified TLS."""

    class PinnedHTTPSConnection(http.client.HTTPSConnection):
        def __init__(self, host: str, **kwargs: Any) -> None:
            super().__init__(host, context=context, **kwargs)
            self._pinned_ips = pinned_ips

        def connect(self) -> None:
            last_error: OSError | None = None
            sock: socket.socket | None = None
            for ip in self._pinned_ips:
                try:
                    sock = socket.create_connection((ip, self.port), self.timeout)
                    break
                except OSError as exc:
                    last_error = exc
            if sock is None:
                if last_error is not None:
                    raise last_error
                raise OSError("no checked addresses to dial")
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # SNI and certificate verification stay on the ORIGINAL hostname
            # even though TCP dials a pinned IP, so the pinning cannot
            # downgrade TLS: the server still has to prove the hostname.
            self.sock = context.wrap_socket(sock, server_hostname=self.host)

    return PinnedHTTPSConnection


class PinnedHTTPHandler(urllib.request.HTTPHandler):
    """http_open resolves, checks, and pins in one step: no rebind window."""

    def http_open(self, req: urllib.request.Request) -> Any:
        conn_class = _pinned_http_class(pick_public_ips(req.host))
        return self.do_open(conn_class, req)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """https_open resolves, checks, and pins in one step: no rebind window."""

    def __init__(self, context: ssl.SSLContext) -> None:
        super().__init__(context=context)
        self._pinned_context = context

    def https_open(self, req: urllib.request.Request) -> Any:
        # The context is baked into the class by the factory below; passing it
        # through do_open as well lands two `context=` kwargs on the
        # constructor and raises TypeError. One place, and only one.
        conn_class = _pinned_https_class(pick_public_ips(req.host), self._pinned_context)
        return self.do_open(conn_class, req)


def build_guarded_opener(
    proxy: str | None = None, context: ssl.SSLContext | None = None
) -> urllib.request.OpenerDirector:
    """An opener for untrusted, user-supplied URLs.

    Direct mode (no proxy): every request -- the first URL and every redirect
    hop -- is resolved, checked, and pinned inside the handler, closing the
    resolve-check-reconnect window (DNS rebinding).

    Proxy mode: DNS is resolved by the proxy, so the client cannot pin the
    dial target; the client-side public-IP check still runs on the first URL
    (callers) and on every redirect hop (SafeRedirect).

    ``context`` lets callers share one SSLContext (gemini._ssl_ctx caches one
    process-wide; building a fresh context per fetch re-parses the CA bundle
    every time). Defaults to a private ``create_default_context()`` so this
    module stays safe to use standalone.
    """
    tls = context if context is not None else ssl.create_default_context()
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
            SafeRedirect(),
        )
    return urllib.request.build_opener(
        PinnedHTTPHandler(),
        PinnedHTTPSHandler(tls),
        SafeRedirect(),
    )
