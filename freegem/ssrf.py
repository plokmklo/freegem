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

Two injection points keep the guard single-source-of-truth while letting the
resilient ladder extend reach WITHOUT weakening it:

``resolver`` replaces :func:`pick_public_ips` as the resolve-and-check step.
The ladder uses it for its DoH fallback: when the local resolver cannot
answer for a name, the ladder resolves via DNS-over-HTTPS and hands the
addresses to the SAME public-only check (``_checked_addresses``) before any
dial. A custom resolver must raise :class:`SSRFRefused` for anything it
cannot vouch for.

``cookie_jar`` attaches an :class:`http.cookiejar.CookieJar` so a host's own
anonymous-entitlement flows work (a 302 that sets a cookie and loops back is
a normal browser pattern, and the ladder's shared jar resolves it on the
next attempt). The jar never carries credentials we did not receive from
the origin itself.

Proxy mode, precisely: DNS belongs to the proxy, so the dial cannot be
pinned. What the client CAN still do is refuse hosts that locally resolve
to a non-public address -- :class:`PreflightedProxyHandler` runs that check
before every proxied request (initial URL included). A name that does not
resolve locally is allowed through, because in a proxy-only deployment the
proxy is the only resolver; refusing unresolvable names would break split-
horizon setups. That residual -- the proxy resolving to an address we could
not pre-check -- is documented in the README security section. Redirect
hops stay strict: :class:`SafeRedirect` refuses unresolvable names outright,
because a redirect is attacker-influenced, not user intent.

Browsers (Playwright, Selenium) cannot be pinned at all -- the browser does
its own DNS and dials its own sockets for the main frame AND every
subresource. :mod:`freegem.browser_guard` layers the next-best controls on
top of this module's policy: strict resolve-and-check on the initial URL,
per-request validation of every navigation and subresource (Playwright
routes), a CDP blocklist plus resolver rules (Selenium), and a final-URL
re-validation before any content is returned.
"""

from __future__ import annotations

import http.client
import http.cookiejar
import ipaddress
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

__all__ = [
    "PinnedHTTPHandler",
    "PinnedHTTPSHandler",
    "PreflightedProxyHandler",
    "SSRFRefused",
    "SafeRedirect",
    "build_guarded_opener",
    "host_public_or_unresolvable",
    "host_public_strict",
    "ip_is_nonpublic",
    "pick_public_ip",
    "pick_public_ips",
    "url_allowed",
]

#: Resolve-and-check step: hostname -> the only IPs a dial may use.
Resolver = Callable[[str], list[str]]


class SSRFRefused(Exception):
    """A URL or host failed the SSRF guard and must not be contacted."""


# stdlib ``is_private`` misses two legacy ranges that are not routable on the
# public internet and must never be dialable: CGNAT (RFC 6598, the address
# space every carrier-grade NAT hands out) and IPv6 site-local (RFC 3879,
# deprecated but still configured on old gear). Verified on CPython 3.12:
# ipaddress classes neither as private nor reserved, so they are explicit.
_EXTRA_NONPUBLIC_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT / RFC 6598
    ipaddress.ip_network("fec0::/10"),  # deprecated IPv6 site-local
)

#: Hostnames that must never be contacted even when DNS calls them public:
#: cloud metadata services that resolve by name on their own networks.
_METADATA_HOSTNAMES = frozenset(
    {
        "metadata",  # GCE short name via its own resolver
        "metadata.google.internal",  # GCP
        "metadata.goog",  # GCP alternate
        "instance-data",  # generic
        "metadata.azure.internal",
    }
)

#: Special-use suffixes a public-web fetch has no business resolving
#: (RFC 6761 localhost, RFC 6762 .local mDNS, RFC 8375 .home.arpa, and the
#: ICANN-reserved .internal). Fail closed -- the public web does not use them.
_SPECIAL_SUFFIXES = (".internal", ".local", ".localhost", ".home.arpa")


def _is_special_name(bare: str) -> bool:
    """True for localhost, cloud-metadata names, and special-use suffixes."""

    low = bare.lower().rstrip(".")
    if low == "localhost" or low in _METADATA_HOSTNAMES:
        return True
    return any(low.endswith(suffix) for suffix in _SPECIAL_SUFFIXES)


def _ip_literal_nonpublic(bare: str) -> bool:
    """True when ``bare`` is an IP literal we refuse. Non-IP strings are False."""

    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip_is_nonpublic(ip)


def ip_is_nonpublic(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True when ``ip`` must not be dialed.

    Covers everything ``ipaddress`` already refuses (private, loopback,
    link-local, reserved, multicast, unspecified), IPv4-mapped IPv6 (the
    wrapper is unwrapped before checking), plus the explicit legacy ranges
    above. One source of truth for every guard call site: the pinned
    handlers here AND the multimodal image fetcher's pre-check.
    """
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
        return True
    return any(ip in net for net in _EXTRA_NONPUBLIC_NETS)


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
        if ip_is_nonpublic(ip):
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


def host_public_or_unresolvable(host: str) -> str:
    """Proxy-mode preflight: refuse hosts that RESOLVE non-public, allow the rest.

    Proxy mode cannot pin the dial, so this is the strongest client-side check
    that does not break proxy-only deployments: a host that locally resolves
    to a non-public address (or IS one) is refused; a host that does not
    resolve locally is allowed through, because the proxy owns DNS there.
    Metadata/special-use hostnames are refused unconditionally -- no proxy
    deployment has a legitimate reason to scrape its own cloud metadata.
    Used by :class:`PreflightedProxyHandler` before every proxied request.
    """
    bare = _host_only(host)
    if not bare:
        raise SSRFRefused("refusing empty host")
    if _is_special_name(bare):
        raise SSRFRefused(f"{bare!r} is a metadata/special-use hostname")
    if _ip_literal_nonpublic(bare):
        raise SSRFRefused(f"{bare!r} is a non-public address")
    try:
        ipaddress.ip_address(bare)
        return bare  # a public IP literal: nothing to resolve
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(bare, None)
    except OSError:
        return bare  # local DNS silent -> the proxy resolves; allowed here
    _checked_addresses(bare, infos)  # raises on ANY non-public address
    return bare


def host_public_strict(host: str) -> str:
    """Browser-mode preflight: strict local resolve-and-check.

    For browser fetches the local resolver IS the resolver the browser uses,
    so there is no split-horizon excuse: an unresolvable name is refused the
    same as a non-public one (it is a failed fetch either way -- refusing
    keeps the classification honest). Metadata/special-use hostnames and
    non-public IP literals are refused. Playwright's route guard and
    Selenium's final-URL check run this per request.
    """
    bare = _host_only(host)
    if not bare:
        raise SSRFRefused("refusing empty host")
    if _is_special_name(bare):
        raise SSRFRefused(f"{bare!r} is a metadata/special-use hostname")
    if _ip_literal_nonpublic(bare):
        raise SSRFRefused(f"{bare!r} is a non-public address")
    try:
        ipaddress.ip_address(bare)
        return bare  # a public IP literal: nothing to resolve
    except ValueError:
        pass
    pick_public_ips(bare)  # raises on unresolvable OR any non-public address
    return bare


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
        if parsed.username or parsed.password:
            # The first URL is refused for embedded credentials (url_allowed);
            # a redirect must not become the loophole that reintroduces them.
            raise urllib.error.URLError("redirect refused: target URL carries embedded credentials")
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

    def __init__(self, resolver: Resolver | None = None) -> None:
        super().__init__()
        self._resolver = resolver or pick_public_ips

    def http_open(self, req: urllib.request.Request) -> Any:
        conn_class = _pinned_http_class(self._resolver(req.host))
        return self.do_open(conn_class, req)


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """https_open resolves, checks, and pins in one step: no rebind window."""

    def __init__(self, context: ssl.SSLContext, resolver: Resolver | None = None) -> None:
        super().__init__(context=context)
        self._pinned_context = context
        self._resolver = resolver or pick_public_ips

    def https_open(self, req: urllib.request.Request) -> Any:
        # The context is baked into the class by the factory below; passing it
        # through do_open as well lands two `context=` kwargs on the
        # constructor and raises TypeError. One place, and only one.
        conn_class = _pinned_https_class(self._resolver(req.host), self._pinned_context)
        return self.do_open(conn_class, req)


class PreflightedProxyHandler(urllib.request.ProxyHandler):
    """Proxy-mode opening with a client-side public-IP preflight.

    Subclasses :class:`urllib.request.ProxyHandler` so the proxy semantics
    are untouched -- the ONLY change is that :meth:`proxy_open` (which every
    ``<scheme>_open`` lambda installed by ``ProxyHandler.__init__``
    dispatches through) runs :func:`host_public_or_unresolvable` on the
    request host BEFORE the proxy is contacted. That covers the initial URL
    and everything stdlib re-dispatches through the opener; redirect hops
    get the stricter :class:`SafeRedirect` check on top.
    """

    def __init__(self, proxy: str) -> None:
        super().__init__({"http": proxy, "https": proxy})

    def proxy_open(self, req: urllib.request.Request, proxy: Any = None, type: Any = None) -> Any:
        host_public_or_unresolvable(req.host)
        return super().proxy_open(req, proxy, type)


def build_guarded_opener(
    proxy: str | None = None,
    context: ssl.SSLContext | None = None,
    resolver: Resolver | None = None,
    cookie_jar: http.cookiejar.CookieJar | None = None,
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

    ``resolver`` swaps the resolve-and-check step (see module docstring); it
    is ignored in proxy mode, where DNS belongs to the proxy. ``cookie_jar``
    attaches a cookie processor so origin-granted cookies (anonymous
    entitlement tokens, consent marks) ride along on retries and redirects.
    """
    tls = context if context is not None else ssl.create_default_context()
    # NOTE: `is not None`, not truthiness -- an EMPTY CookieJar is falsy
    # (it implements __len__/__iter__), and `if cookie_jar` would silently
    # drop the processor exactly when the jar starts fresh.
    cookie_handler: list[urllib.request.BaseHandler] = (
        [urllib.request.HTTPCookieProcessor(cookie_jar)] if cookie_jar is not None else []
    )
    # (#63) A HAND-BUILT OpenerDirector, not build_opener: stdlib
    # build_opener silently installs the default handler stack -- FTPHandler,
    # FileHandler, DataHandler, UnknownHandler -- none of which run the
    # pinned/checked dial. `freegem scrape ftp://169.254.169.254/` used to
    # reach the cloud metadata service over FTP with NO resolve-check and NO
    # pin, in BOTH modes, because guard=False skips only the URL policy
    # pre-flight while the pinned-dial guarantee lives in these handlers.
    # A manual opener installs ONLY audited handlers: http/https resolve,
    # check, and pin (direct mode) or the preflighted proxy (proxy mode),
    # redirect checks, the HTTP error machinery, and an optional cookie
    # processor. Every other scheme fails with an honest URLError.
    if proxy:
        opener = urllib.request.OpenerDirector()
        for handler in (
            urllib.request.HTTPErrorProcessor(),
            urllib.request.HTTPDefaultErrorHandler(),
            PreflightedProxyHandler(proxy),
            # (#63) In proxy mode the ProxyHandler REWRITES the request to
            # the proxy and defers ("let other handlers take care of it") --
            # something must actually dial. build_opener used to supply the
            # default HTTP(S)Handler silently; installed explicitly now.
            # The proxy is operator-configured (trusted dial target); the
            # SSRF posture in proxy mode is the PreflightedProxyHandler
            # preflight plus SafeRedirect, both present.
            urllib.request.HTTPHandler(),
            urllib.request.HTTPSHandler(context=tls),
            SafeRedirect(),
            *cookie_handler,
        ):
            opener.add_handler(handler)
        return opener
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.HTTPErrorProcessor(),
        urllib.request.HTTPDefaultErrorHandler(),
        PinnedHTTPHandler(resolver),
        PinnedHTTPSHandler(tls, resolver),
        SafeRedirect(),
        *cookie_handler,
    ):
        opener.add_handler(handler)
    return opener
