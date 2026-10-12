"""Per-request SSRF policy for real browsers (Playwright and Selenium).

A browser cannot be pinned the way :mod:`freegem.ssrf` pins stdlib dials --
it resolves DNS itself and dials its own sockets for the main frame AND for
every subresource. So the browser layer stacks several honest, best-effort
controls on top of the same public-only policy:

1. **Initial URL**: strict resolve-and-check (:func:`ssrf.host_public_strict`)
   BEFORE the browser launches -- loopback, private, link-local, metadata,
   CGNAT, and special-use names never reach navigation.
2. **Every request** (Playwright): a context-wide route handler validates
   each request the browser is about to make -- initial navigation, redirect
   hops, meta-refresh, JS ``location`` changes, and all subresources
   (img/script/fetch/XHR/WebSocket). Refused requests are aborted; a blocked
   NAVIGATION fails the whole fetch, a blocked subresource is dropped and
   the page still renders.
3. **Every request, pattern layer** (Selenium): the sync Selenium API has no
   event loop, so per-request validation is impossible; instead a CDP
   ``Network.setBlockedURLs`` blocklist (IP-range globs, localhost and
   metadata names, special-use suffixes, across http/https/ws/wss) plus
   ``--host-resolver-rules`` (kills name resolution for metadata/localhost/
   special suffixes at the browser resolver) carry the subresource layer.
4. **Final URL**: after navigation, the landed URL is re-validated before
   ANY content is returned -- a redirect that ends on an internal host
   yields ``ok=False`` and empty text, never the internal page.

Residual risks, stated plainly:

* The browser resolves and dials on its own; request-time validation
  narrows the DNS-rebinding window to the per-request one but cannot pin
  the IP the way the stdlib guard does.
* Selenium's pattern layer cannot enumerate every private DNS name;
  non-matching names that resolve private are caught only by the strict
  initial check (main frame) and the final-URL check (after the fact, with
  content withheld). Firefox has no CDP at all -- only checks 1 and 4.
* With a Grid, the browser runs on the NODE: its network position, not the
  client's, decides what is reachable.
"""

from __future__ import annotations

import ipaddress
import urllib.parse
from typing import Any

from .ssrf import SSRFRefused, host_public_strict, url_allowed

__all__ = [
    "BrowserRequestGuard",
    "browser_request_allowed",
    "install_playwright_route_guard",
    "selenium_blocked_url_patterns",
    "selenium_resolver_rules",
]


def browser_request_allowed(url: str) -> str:
    """Full policy for ONE browser request: syntax half + strict host half.

    Raises :class:`SSRFRefused` for anything a public-web browser fetch has
    no business contacting: non-http(s) schemes (file://, ftp://, data:,
    blob: -- a JS redirect to file:// would otherwise hand local files to
    ``page.content()``), embedded credentials, and hosts that fail the
    strict public-only check.
    """
    url_allowed(url)
    parsed = urllib.parse.urlparse(url)
    host_public_strict(parsed.hostname or "")
    return url


class BrowserRequestGuard:
    """Playwright route handler enforcing the policy on every request.

    Instances are callables registered with ``context.route("**/*", guard)``.
    Allowed requests continue; refused ones abort and are recorded by kind
    so the fetcher can fail hard on blocked navigations while tolerating
    blocked subresources.
    """

    def __init__(self) -> None:
        self.blocked_navigations: list[str] = []
        self.blocked_subresources: list[str] = []

    def __call__(self, route: Any) -> None:
        url = route.request.url
        try:
            browser_request_allowed(url)
        except SSRFRefused as exc:
            self._record(route, url, exc)
            route.abort()
            return
        route.continue_()

    def _record(self, route: Any, url: str, exc: SSRFRefused) -> None:
        entry = f"{url}: {exc}"
        navigation = False
        try:
            navigation = bool(route.request.is_navigation())
        except Exception:
            # Older/newer API shape: fall back to the resource-type probe.
            navigation = getattr(route.request, "resource_type", "") == "document"
        if navigation:
            self.blocked_navigations.append(entry)
        else:
            self.blocked_subresources.append(entry)

    @property
    def blocked_any_navigation(self) -> bool:
        return bool(self.blocked_navigations)


def install_playwright_route_guard(context: Any) -> BrowserRequestGuard:
    """Register a :class:`BrowserRequestGuard` on a Playwright browser context.

    Must run BEFORE the first navigation; the guard then sees every request
    the context makes, on every page created from it.
    """
    guard = BrowserRequestGuard()
    context.route("**/*", guard)
    return guard


# ---------------------------------------------------------------------------
# Selenium pattern layer
# ---------------------------------------------------------------------------

#: IPv4 ranges the CDP blocklist covers, as CIDRs expanded to octet globs
#: (defense-in-depth; the strict initial and final-URL checks carry the
#: main weight). Same policy set as ``ssrf.ip_is_nonpublic``.
_CDP_BLOCK_NETS = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",  # CGNAT
    "127.0.0.0/8",
    "169.254.0.0/16",  # link-local incl. cloud metadata (169.254.169.254)
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",  # TEST-NET-1
    "192.168.0.0/16",
    "198.18.0.0/15",  # benchmarking
    "198.51.100.0/24",  # TEST-NET-2
    "203.0.113.0/24",  # TEST-NET-3
    "224.0.0.0/4",  # multicast
    "240.0.0.0/4",  # reserved
)

#: IPv6 literal globs: loopback/unspecified, unique-local (fc00::/7), and
#: link/site-local (fe80::/10, fec0::/10). Global unicast starts 2xxx/3xxx,
#: so these prefixes cannot match a public address.
_CDP_BLOCK_IPV6_GLOBS = (
    "[::]*",
    "[fc*]*",
    "[fd*]*",
    "[fe8*]*",
    "[fe9*]*",
    "[fea*]*",
    "[feb*]*",
    "[fec*]*",
    "[fed*]*",
    "[fee*]*",
    "[fef*]*",
)

#: Special/metadata hostnames for the pattern layer (exact forms; the
#: resolver rules below cover suffixes at any depth).
_CDP_SPECIAL_NAMES = (
    "localhost",
    "*.localhost",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "metadata.azure.internal",
    "*.internal",
    "*.local",
    "*.home.arpa",
)

_CDP_SCHEMES = ("http", "https", "ws", "wss")


def _glob_for_network(network: ipaddress.IPv4Network) -> list[str]:
    """Octet globs covering ``network`` (CDP patterns are prefix-anchored)."""
    prefix = network.prefixlen
    addr = int(network.network_address)
    if prefix <= 8:
        return [f"{addr >> 24 & 0xFF}.*"]
    if prefix <= 16:
        count = 1 << (16 - prefix)  # /12 -> 16 globs, /10 -> 64, /15 -> 2
        base = addr >> 16
        return [f"{(base + i) >> 8 & 0xFF}.{(base + i) & 0xFF}.*" for i in range(count)]
    if prefix <= 24:
        count = 1 << (24 - prefix)
        base = addr >> 8
        return [
            f"{(base + i) >> 16 & 0xFF}.{(base + i) >> 8 & 0xFF}.{(base + i) & 0xFF}.*"
            for i in range(count)
        ]
    return [str(network.network_address)]  # finer than /24: single-host form


def selenium_blocked_url_patterns() -> list[str]:
    """CDP ``Network.setBlockedURLs`` patterns for the Selenium request layer.

    Blocks every special-use IPv4 range, the non-global IPv6 forms, and
    localhost/metadata/special-use hostnames -- across http/https/ws/wss,
    with and without ports. Returns a fresh list each call; callers may
    extend it (e.g. deployment-specific ranges).
    """
    host_globs: list[str] = []
    for cidr in _CDP_BLOCK_NETS:
        host_globs.extend(_glob_for_network(ipaddress.IPv4Network(cidr)))
    host_globs.extend(_CDP_BLOCK_IPV6_GLOBS)
    host_globs.extend(_CDP_SPECIAL_NAMES)

    patterns: list[str] = []
    for scheme in _CDP_SCHEMES:
        for glob in host_globs:
            if glob.endswith("*"):
                patterns.append(f"{scheme}://{glob}")
            else:
                patterns.append(f"{scheme}://{glob}/")
                patterns.append(f"{scheme}://{glob}:*")
    return patterns


def selenium_resolver_rules() -> str:
    """``--host-resolver-rules`` value killing special-use NAME resolution.

    Chromium matches the wildcards at any depth, so ``*.internal`` etc. are
    dead at the browser's resolver for main frames AND subresources -- the
    layer the CDP pattern list cannot fully cover. Applies to remote Grid
    nodes too: the options travel with the session capabilities.
    """
    rules = [
        "MAP localhost ~NOTFOUND",
        "MAP *.localhost ~NOTFOUND",
        "MAP *.local ~NOTFOUND",
        "MAP *.internal ~NOTFOUND",
        "MAP *.home.arpa ~NOTFOUND",
        "MAP metadata ~NOTFOUND",
        "MAP metadata.google.internal ~NOTFOUND",
        "MAP metadata.goog ~NOTFOUND",
        "MAP instance-data ~NOTFOUND",
    ]
    return ", ".join(rules)
