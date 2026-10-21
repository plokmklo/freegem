"""URL normalization and crawl-scope rules for the crawler.

Everything that decides "is this URL worth fetching, and what exactly is it?"
lives here so the frontier stores one canonical spelling per page and the
runner never argues about scope twice.

Normalization is deliberately conservative:

* the fragment is dropped (crawlers fetch representations, not scroll
  positions);
* scheme and host are lowercased; an IDN host is stored as punycode so
  ``ñandu.example`` and ``xn--andu-hra.example`` dedupe to one entry;
* an explicit default port (``:80`` on http, ``:443`` on https) is dropped,
  and a trailing FQDN dot on the host (``example.com.``) is dropped so both
  spellings dedupe to one entry;
* percent-encoding is normalized: unreserved characters are decoded and the
  hex of everything else is uppercased, so ``%2f`` and ``%2F`` compare equal
  while a genuinely encoded ``/`` stays encoded; characters that are not
  legal path/query octets (spaces, non-ASCII, ...) are percent-encoded so
  ``/café`` and ``/caf%C3%A9`` are one entry;
* a small, fixed list of tracking parameters (``utm_*``, ``fbclid``,
  ``gclid``, ...) is removed. Query ORDER is preserved and no other
  parameter is touched: some URLs are signed and reordering or dropping a
  signature would silently change the page being fetched.
"""

from __future__ import annotations

import re
import string
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass, field

__all__ = [
    "URL_NORMALIZING_ERROR",
    "Budget",
    "Scope",
    "ScopeVerdict",
    "normalize_url",
    "registered_domain",
    "strip_tracking_params",
    "url_key",
    "url_origin",
]

URL_NORMALIZING_ERROR = "url did not normalize to a fetchable http(s) address"

#: Percent-triplets, matched case-insensitively so ``%2f`` and ``%2F`` unify.
_PCT = re.compile(r"%([0-9A-Fa-f]{2})")

#: RFC 3986 unreserved characters: safe to decode a percent-triplet into.
_UNRESERVED = frozenset(string.ascii_letters + string.digits + "-._~")

#: Tracking parameters stripped during normalization. Fixed on purpose: a
#: heuristic ("any param that looks useless") eats signed parameters.
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "yclid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "vero_id",
        "oly_anon_id",
        "oly_enc_id",
        "_openstat",
        "igshid",
        "wickedid",
        "msclkid",
        "twclid",
        "ttclid",
    }
)

#: Parameters whose NAME (case-insensitively) starts with these prefixes are
#: tracking too: utm_source, utm_medium, utm_campaign, ...
_TRACKING_PREFIXES = ("utm_", "hsa_", "pk_")


def _pct_normalize(text: str) -> str:
    """Decode percent-encoded unreserved characters; uppercase the rest."""

    def sub(match: re.Match[str]) -> str:
        raw = chr(int(match.group(1), 16))
        if raw in _UNRESERVED:
            return raw
        return "%" + match.group(1).upper()

    return _PCT.sub(sub, text)


#: Characters RFC 3986 leaves alone inside a path/query. ``quote`` with
#: this safe-set only encodes what RFC 3986 forbids there (spaces,
#: non-ASCII, controls, ``"<>``{}\|^``), so already-valid spellings --
#: percent triplets (``%`` is safe) and sub-delims kept for signed URLs --
#: stay byte-identical.
_RFC_SAFE = "%/?:@!$&'()*+,;="


def _quote_unsafe(text: str) -> str:
    """Percent-encode characters that are not legal path/query octets."""
    return urllib.parse.quote(text, safe=_RFC_SAFE)


def strip_tracking_params(query: str) -> str:
    """Drop tracking parameters from a raw query string, preserving order
    and the exact spelling of every parameter that is kept.

    Kept pairs are returned verbatim (not parse/re-encoded): a re-encode
    only when something is stripped would make ``?a=b%20c&utm_source=x``
    normalize differently from ``?a=b%20c`` (``+`` vs ``%20``, ``flag`` vs
    ``flag=``), splitting one resource into two dedup keys. The NAME used
    for the tracking decision IS percent-decoded, so ``utm%5Fsource`` is
    recognized as ``utm_source`` and dropped like its plain spelling.
    """
    if not query:
        return query
    kept: list[str] = []
    changed = False
    for part in query.split("&"):
        name = urllib.parse.unquote_plus(part.split("=", 1)[0]).lower()
        if name in _TRACKING_PARAMS or name.startswith(_TRACKING_PREFIXES):
            changed = True
            continue
        kept.append(part)
    if not changed:
        return query
    return "&".join(kept)


def normalize_url(url: str, *, base: str | None = None) -> str:
    """Canonical http(s) spelling of ``url``; raises ValueError when hopeless.

    ``base`` resolves relative URLs (links found on a page). Anything that is
    not http(s) after resolution -- mailto:, javascript:, data:, ftp: -- is a
    ValueError, because the crawler only speaks HTTP.
    """
    raw = url.strip()
    if not raw:
        raise ValueError(URL_NORMALIZING_ERROR)
    if base:
        raw = urllib.parse.urljoin(base, raw)
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError as exc:  # malformed port, bad IPv6 literal, ...
        raise ValueError(URL_NORMALIZING_ERROR) from exc
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(URL_NORMALIZING_ERROR)
    host = (parts.hostname or "").lower()
    if host.endswith("."):
        # FQDN root label: example.com. is the same host as example.com --
        # without this the two spellings never dedupe.
        host = host.rstrip(".")
    if not host:
        raise ValueError(URL_NORMALIZING_ERROR)
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]  # bracketed IPv6 literal: keep the bare address
    try:
        host.encode("ascii")
    except UnicodeEncodeError:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError(URL_NORMALIZING_ERROR) from exc
    try:
        port = parts.port
    except ValueError as exc:  # "Port out of range", "could not be cast"...
        raise ValueError(URL_NORMALIZING_ERROR) from exc
    if port is not None and not (0 < port < 65536):
        raise ValueError(URL_NORMALIZING_ERROR)
    default_port = port is not None and (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    )
    netloc = host if (port is None or default_port) else f"{host}:{port}"
    # urlsplit keeps userinfo out of hostname; a URL with credentials in the
    # path to the crawler is refused downstream by the SSRF guard anyway, but
    # normalization must not SILENTLY carry them either.
    if parts.username or parts.password:
        raise ValueError(URL_NORMALIZING_ERROR)
    path = _quote_unsafe(_pct_normalize(parts.path or "/"))
    query = _quote_unsafe(_pct_normalize(strip_tracking_params(parts.query)))
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def url_key(url: str) -> str:
    """The dedup key for ``url``: its normalized spelling, fragments gone.

    Raises ValueError for URLs that cannot normalize; callers decide whether
    that is fatal (a seed) or skippable (a link on a page).
    """
    return normalize_url(url)


def url_origin(url: str) -> str:
    """``scheme://host[:port]`` of a normalized URL (no path, no default port)."""
    parts = urllib.parse.urlsplit(url)
    port = parts.port
    default = port is not None and (
        (parts.scheme == "http" and port == 80) or (parts.scheme == "https" and port == 443)
    )
    netloc = parts.hostname or "" if port is None or default else f"{parts.hostname}:{port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, "", "", ""))


def registered_domain(host: str) -> str:
    """Last two labels of a host -- ``www.news.example.co.uk`` is not solved
    perfectly (a real PSL would keep ``co.uk`` together); the crawler's
    default scope is same-HOST, not same-domain, so this helper only serves
    allow/deny matching where a suffix match at a dot boundary is what the
    user asked for."""
    labels = host.strip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    return ".".join(labels[-2:])


# ─── scope ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScopeVerdict:
    """Why a URL was admitted or refused. ``reason`` is a stable short code
    (``ok``, ``out_of_scope_host``, ``deny_path``, ...) used in counters and
    structured logs; ``detail`` carries the offending value."""

    ok: bool
    reason: str
    detail: str = ""


_OK = ScopeVerdict(True, "ok")


@dataclass(frozen=True)
class Scope:
    """What the crawl may touch.

    ``allow_domains`` empty + ``same_host_only`` means "stay on the seed
    hosts" -- the safe default. Listing ``allow_domains`` widens the crawl to
    those domains (suffix-matched at a dot boundary, so ``example.org`` also
    admits ``www.example.org``); ``deny_domains`` and ``deny_paths`` always
    win over anything else. ``allow_paths``/``deny_paths`` are plain path
    prefixes: ``/docs`` admits ``/docs`` and ``/docs/x``, not ``/documents``.
    """

    allow_domains: tuple[str, ...] = ()
    deny_domains: tuple[str, ...] = ()
    allow_paths: tuple[str, ...] = ()
    deny_paths: tuple[str, ...] = ()
    max_depth: int = 2
    max_pages: int = 100
    same_host_only: bool = True

    def _host_allowed(self, host: str) -> bool:
        if self.deny_domains and any(
            host == d or host.endswith("." + d) for d in self.deny_domains
        ):
            return False
        if self.allow_domains:
            return any(host == d or host.endswith("." + d) for d in self.allow_domains)
        return True

    def admits(self, url: str, *, depth: int, seed_hosts: Iterable[str]) -> ScopeVerdict:
        """Full scope decision for a normalized URL at ``depth``."""
        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            return ScopeVerdict(False, "malformed_url")
        host = (parts.hostname or "").lower()
        path = parts.path or "/"
        if not host:
            return ScopeVerdict(False, "malformed_url")
        if depth > self.max_depth:
            return ScopeVerdict(False, "too_deep", f"depth={depth}>{self.max_depth}")
        if any(host == d or host.endswith("." + d) for d in self.deny_domains):
            return ScopeVerdict(False, "deny_domain", host)
        if self.allow_domains:
            if not any(host == d or host.endswith("." + d) for d in self.allow_domains):
                return ScopeVerdict(False, "out_of_scope_host", host)
        elif self.same_host_only:
            seeds = {h.lower() for h in seed_hosts}
            if host not in seeds:
                return ScopeVerdict(False, "out_of_scope_host", host)
        if any(path.startswith(p) for p in self.deny_paths):
            return ScopeVerdict(False, "deny_path", path)
        if self.allow_paths and not any(path.startswith(p) for p in self.allow_paths):
            return ScopeVerdict(False, "out_of_scope_path", path)
        return _OK


@dataclass
class Budget:
    """Mutable page budget the runner spends as it crawls.

    Kept separate from Scope because Scope is a frozen *rule* ("what is in
    scope") while the budget is *state* ("how much is left"). Both bound the
    crawl; the budget is what a checkpoint resumes against.
    """

    max_pages: int = 100
    fetched: int = 0
    failed: int = 0
    queued: int = 0
    #: pages whose bytes were already spent before this run (resume)
    fetched_before: int = 0

    @property
    def spent(self) -> int:
        return self.fetched + self.failed

    @property
    def remaining(self) -> int:
        return max(0, self.max_pages - self.fetched_before - self.spent)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    def as_dict(self) -> dict[str, int]:
        return {
            "max_pages": self.max_pages,
            "fetched": self.fetched,
            "failed": self.failed,
            "queued": self.queued,
            "remaining": self.remaining,
        }

    def note(self, key: str, amount: int = 1) -> None:
        setattr(self, key, getattr(self, key) + amount)


@dataclass
class Seed:
    """One starting URL with its bookkeeping."""

    url: str
    depth: int = 0
    priority: int = 0
    source: str = "seed"  # seed | sitemap | link | resume
    tags: frozenset[str] = field(default_factory=frozenset)
