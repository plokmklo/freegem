"""The escalation ladder: one URL, several honest attempts.

:cfunc:`crawl_fetch` reports WHAT happened. This module decides WHAT TO DO
NEXT: switch to browser-grade request headers, wait out a 429, give a slow
origin a longer timeout, fall back to the origin root when a path 404s or
5xxes, swap scheme (https<->http) or host shape (www<->apex) when the
transport itself is broken, keep the cookies an origin itself grants (a 302
that sets an anonymous entitlement cookie and loops back is a normal browser
pattern), resolve names the local resolver cannot answer via DNS-over-HTTPS,
and -- ONLY for broken certificate chains -- retry once with TLS verification
relaxed, flagged ``tls_unverified`` in the outcome.

The ladder's LAST resort for a page an origin still refuses to serve (hard
bot walls, dead domains, legal blocks) is the Internet Archive's public
snapshot (``web.archive.org``, ``id_`` raw-capture modifier): reading a
publicly archived copy of a published page attacks no one. Those outcomes
carry ``via="wayback"`` and ``fallback="wayback"`` so reports can count them
honestly -- they are reads of a copy, not of the live origin.

Nothing here attacks an origin: no authentication bypass, no captcha or
JS-challenge solving, no forgery beyond standard browser-grade request
headers and origin-granted cookies, and per-host pacing is honored before
every single attempt.
"""

from __future__ import annotations

import http.cookiejar
import ipaddress
import json
import random
import ssl
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace

from ..config import BridgeConfig
from ..ssrf import Resolver, SSRFRefused, _checked_addresses, _host_only, pick_public_ips
from .fetch import FetchOutcome, _is_dns_failure, crawl_fetch
from .hardening import ResourceLimits

__all__ = ["PROFILES", "HostPacer", "fetch_resilient"]

# ─── browser-grade header profiles ───────────────────────────────────────────
# Two full profiles, applied in order. Accept-Encoding stays gzip-only: the
# streaming reader decodes gzip/zlib and nothing else, and asking for brotli
# would buy bodies we cannot cut. Client hints (sec-ch-ua) are what many CDN
# bot walls actually score on, so the first profile sends them; the second
# profile omits them because some older Java/PHP stacks choke on unknown
# headers or on the Chrome UA itself.

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_FIREFOX_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0"
_SAFARI_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)

PROFILES: tuple[dict[str, str], ...] = (
    {
        "User-Agent": _CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": "none",
        "sec-fetch-mode": "navigate",
        "sec-fetch-user": "?1",
        "sec-fetch-dest": "document",
        "upgrade-insecure-requests": "1",
    },
    {
        "User-Agent": _FIREFOX_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip",
    },
    {
        # Safari: minimal header set, no client hints -- some WAFs score the
        # ABSENCE of Chrome-isms differently, and older stacks choke on them.
        "User-Agent": _SAFARI_UA,
        "Accept": "*/*",
        "Accept-Language": "*",
        "Accept-Encoding": "gzip",
    },
)

_CERT_MARKERS = (
    "certificate verify failed",
    "certificate is not valid",
    "certificate has expired",
    "ssl: certificate",
    "hostname mismatch",
)

_TRANSIENT_NET_MARKERS = (
    "connection refused",
    "network is unreachable",
    "connection reset",
    "temporary failure",
    "cannot resolve",
    "name resolution",
    "connection aborted",
    "remote end closed",
    "wrong version number",
)

_BACKOFF_BASE_SEC = 1.5
_BACKOFF_CAP_SEC = 20.0
_RETRY_AFTER_CAP_SEC = 60.0
_TIMEOUT_CAP_SEC = 90.0
#: How many times the ladder will DOUBLE the timeout and re-dial a URL that
#: keeps timing out (issue #45: it used to escalate forever, so a persistently
#: timing-out URL burned the whole attempt budget at up to 90s a dial and
#: could never reach the wayback last resort).
_MAX_TIMEOUT_ESCALATIONS = 2
#: The Internet Archive's raw-capture view: timestamp "2" redirects to the
#: newest snapshot, ``id_`` serves the original bytes without the toolbar.
_WAYBACK_PREFIX = "https://web.archive.org/web/2id_/"
_WAYBACK_TIMEOUT_SEC = 20.0
_DOH_ENDPOINTS = (
    "https://dns.google/resolve?name={host}&type={rtype}",
    "https://cloudflare-dns.com/dns-query?name={host}&type={rtype}",
)
# The ladder owns the SHORT retry horizon; the frontier's own retry
# schedule owns the long one (across resume). Sleep budgets are capped so
# a hopeless origin costs seconds per URL, not minutes.


def _is_cert_error(outcome: FetchOutcome) -> bool:
    lowered = (outcome.error or "").lower()
    return any(marker in lowered for marker in _CERT_MARKERS)


def _is_transient_net(outcome: FetchOutcome) -> bool:
    lowered = (outcome.error or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_NET_MARKERS)


def _sleep_backoff(attempt: int) -> float:
    """Exponential backoff with jitter, capped."""
    delay = min(_BACKOFF_CAP_SEC, _BACKOFF_BASE_SEC * (2 ** max(0, attempt - 1)))
    return delay * (0.7 + 0.6 * random.random())


class HostPacer:
    """Per-host minimum spacing between request starts, thread-safe.

    The campaign runs many workers; without this, eight threads hammer one
    origin simultaneously and a 429 is manufactured where none was coming.
    A 429 (or a polite server with a long Retry-After) pushes the host's
    next slot out via :meth:`penalize`.
    """

    def __init__(self, default_delay_sec: float = 1.0) -> None:
        self.default_delay_sec = max(0.0, default_delay_sec)
        self._lock = threading.Lock()
        self._next_slot: dict[str, float] = {}
        self._delay: dict[str, float] = {}

    def reserve(self, host: str) -> float:
        """Claim the host's next free slot; return how long to sleep FIRST."""
        now = time.monotonic()
        with self._lock:
            delay = self._delay.get(host, self.default_delay_sec)
            slot = max(now, self._next_slot.get(host, 0.0))
            self._next_slot[host] = slot + delay
        return max(0.0, slot - now)

    def penalize(self, host: str, seconds: float) -> None:
        """Push the host's next slot out (429 Retry-After, server overload)."""
        wait = min(max(0.0, seconds), _RETRY_AFTER_CAP_SEC)
        with self._lock:
            key = host
            self._next_slot[key] = max(self._next_slot.get(key, 0.0), time.monotonic() + wait)
            self._delay[key] = max(self._delay.get(key, self.default_delay_sec), min(wait, 5.0))


@dataclass
class _LadderState:
    """What the ladder has already tried for this URL."""

    timeout_sec: float = 0.0
    #: How many of PROFILES have been tried (0 = honest freegem identity).
    profiles_tried: int = 0
    tried_root: bool = False
    tried_scheme_swap: bool = False
    tried_host_swap: bool = False
    tried_tls_relax: bool = False
    tried_doh: bool = False
    tried_wayback: bool = False
    actions: list[str] = field(default_factory=list)
    attempts: int = 0
    #: Backoff sleeps used so far / allowed (from the caller's max_retries).
    sleeps_used: int = 0
    max_sleeps: int = 2
    #: 429s seen for this URL: persistent rate limiting (no Retry-After,
    #: IP-keyed) is not fixable in-run -- stop early and leave the rest to a
    #: later --retry-failed pass, when the limit window has likely reset.
    seen_429: int = 0
    #: Timeout escalations used so far; capped at ``_MAX_TIMEOUT_ESCALATIONS``
    #: so a wall of silence falls through to the remaining rungs instead of
    #: consuming the entire attempt budget (issue #45).
    timeout_escalations: int = 0
    #: Caller-supplied headers that must ride along on every attempt
    #: (conditional validators); profile headers merge OVER these.
    caller_headers: dict[str, str] = field(default_factory=dict)

    def next_profile(self) -> dict[str, str] | None:
        """The next browser profile to try, or None when all were tried."""
        if self.profiles_tried >= len(PROFILES):
            return None
        profile = PROFILES[self.profiles_tried]
        self.profiles_tried += 1
        return dict(profile)


def fetch_resilient(
    cfg: BridgeConfig,
    url: str,
    *,
    limits: ResourceLimits | None = None,
    pacer: HostPacer | None = None,
    spill_dir: str = "",
    max_attempts: int = 16,
    extra_headers: dict[str, str] | None = None,
    cookie_jar: http.cookiejar.CookieJar | None = None,
) -> FetchOutcome:
    """Fetch ``url`` and, on failure, work the ladder until the action budget
    is spent. The returned outcome carries ``attempts`` (how many requests
    were made) and ``fallback`` (which escalation produced the content, ""
    for a plain first-try success). ``extra_headers`` ride along on EVERY
    attempt (conditional-request validators), merged UNDER the ladder's
    profile headers so a profile can replace identity headers but never a
    caller's validators. ``cookie_jar`` (share one per campaign) keeps
    origin-granted cookies across attempts so anonymous-entitlement loops
    resolve the way a browser resolves them. Never raises for fetch-level
    problems.
    """
    base = limits or ResourceLimits()
    pacer = pacer or HostPacer()
    host = _host_of(url)
    # A browser always carries a cookie jar, so the ladder does too: a 302
    # that grants an anonymous-entitlement cookie and loops back must resolve
    # the way it does in a browser. Callers may share ONE jar across the
    # whole campaign (that is what run_campaign does); per-call isolation is
    # the default.
    if cookie_jar is None:
        cookie_jar = http.cookiejar.CookieJar()
    state = _LadderState(
        timeout_sec=base.timeout_sec,
        max_sleeps=max(1, min(base.max_retries, 4)),
        caller_headers=extra_headers or {},
    )
    outcome = _attempt(cfg, url, base, state, pacer, spill_dir, cookie_jar=cookie_jar)

    while not _happy(outcome) and state.attempts < max_attempts:
        nxt = _next_action(url, outcome, state, host, pacer)
        if nxt is None:
            break
        action, target_url, extra_headers, relaxed_tls = nxt
        if action == "sleep":
            time.sleep(_sleep_backoff(state.attempts))
            outcome = _attempt(
                cfg, target_url, base, state, pacer, spill_dir, cookie_jar=cookie_jar
            )
            continue
        state.actions.append(action)
        if action == "wayback":
            # A dead-wall read of the archive must not inherit a 90s timeout:
            # unresponsive networks cost seconds, not minutes.
            state.timeout_sec = min(state.timeout_sec or _WAYBACK_TIMEOUT_SEC, _WAYBACK_TIMEOUT_SEC)
        resolver: Resolver | None = None
        if action == "doh":
            resolver = _doh_resolver_for(host)
        if action.startswith("profile_") or action in (
            "tls_unverified",
            "origin_root",
            "scheme_swap",
            "host_swap",
            "wayback",
            "doh",
        ):
            outcome = _attempt(
                cfg,
                target_url,
                base,
                state,
                pacer,
                spill_dir,
                extra_headers=extra_headers,
                relaxed_tls=relaxed_tls,
                resolver=resolver,
                cookie_jar=cookie_jar,
            )
        else:  # plain retry (429 waited out, timeout escalated, 5xx backed off)
            outcome = _attempt(
                cfg, target_url, base, state, pacer, spill_dir, cookie_jar=cookie_jar
            )
        if action == "wayback" and outcome.ok:
            outcome.via = "wayback"

    outcome.attempts = max(1, state.attempts)
    outcome.fallback = _fallback_of(state.actions)
    return outcome


def _happy(outcome: FetchOutcome) -> bool:
    return outcome.ok or outcome.not_modified


def _attempt(
    cfg: BridgeConfig,
    url: str,
    base: ResourceLimits,
    state: _LadderState,
    pacer: HostPacer,
    spill_dir: str,
    extra_headers: dict[str, str] | None = None,
    relaxed_tls: bool = False,
    resolver: Resolver | None = None,
    cookie_jar: http.cookiejar.CookieJar | None = None,
) -> FetchOutcome:
    """One paced attempt at ``url`` with the ladder's current knobs."""
    wait = pacer.reserve(_host_of(url) or "")
    if wait > 0:
        time.sleep(wait)
    state.attempts += 1
    limits = replace(base, timeout_sec=state.timeout_sec)
    tls_context: ssl.SSLContext | None = None
    if relaxed_tls:
        tls_context = ssl.create_default_context()
        tls_context.check_hostname = False
        tls_context.verify_mode = ssl.CERT_NONE
    headers: dict[str, str] = dict(state.caller_headers)
    if extra_headers:
        headers.update(extra_headers)
    outcome = crawl_fetch(
        cfg,
        url,
        limits=limits,
        extra_headers=headers or None,
        spill_dir=spill_dir,
        tls_context=tls_context,
        resolver=resolver,
        cookie_jar=cookie_jar,
    )
    if relaxed_tls:
        outcome.tls_unverified = True
    return outcome


def _next_action(
    original_url: str,
    outcome: FetchOutcome,
    state: _LadderState,
    host: str,
    pacer: HostPacer,
) -> tuple[str, str, dict[str, str] | None, bool] | None:
    """Decide the next ladder step, or None to stop and report honestly.

    Returns ``(action, url, extra_headers, relaxed_tls)``. ``action ==
    "sleep"`` means retry the SAME url after a backoff sleep.
    """
    kind, status = outcome.kind, outcome.status

    if kind == "ssrf" or kind == "type_refused" or kind == "too_large":
        return None  # guard refusals and shape problems are final

    if status == 429:
        state.seen_429 += 1
        if state.seen_429 <= 3:
            wait = outcome.retry_after or 15.0
            pacer.penalize(host, wait)
            return ("wait_429", original_url, None, False)
        # fall through: after three waited-out windows, the archived copy
        # (wayback, below) beats hammering an origin that is IP-limiting us.

    if status in (403, 406):
        profile = state.next_profile()
        if profile is not None:
            return (f"profile_{state.profiles_tried}", original_url, profile, False)

    if status in (500, 502, 503, 504):
        # A path-specific origin error often leaves the origin root healthy
        # (sohu.com/about 502s while / serves): one backoff, then the root.
        if state.sleeps_used >= 1 and not state.tried_root and _has_path(original_url):
            state.tried_root = True
            return ("origin_root", _origin_root(original_url), None, False)
        step = _sleep_step(state, original_url)
        if step is not None:
            return step

    if kind == "timeout" and state.timeout_escalations < _MAX_TIMEOUT_ESCALATIONS:
        state.timeout_escalations += 1
        state.timeout_sec = min(_TIMEOUT_CAP_SEC, max(state.timeout_sec * 2, 30.0))
        return ("timeout_up", original_url, None, False)
    # A timeout past its escalation cap falls through from here (issue #45):
    # two escalations are the honest budget -- a URL that still times out at
    # the doubled timeout must not burn the remaining attempts on identical
    # dials. The remaining rungs below decide (the archive decides last).

    if status in (404, 410) and not state.tried_root and _has_path(original_url):
        state.tried_root = True
        return ("origin_root", _origin_root(original_url), None, False)

    if kind == "network" and _is_cert_error(outcome) and not state.tried_tls_relax:
        # Keep verification when a host-shape swap can fix the name first.
        if not state.tried_host_swap and _host_swap(original_url):
            state.tried_host_swap = True
            return ("host_swap", _host_swap(original_url) or "", None, False)
        state.tried_tls_relax = True
        return ("tls_unverified", original_url, None, True)

    if kind == "network" and _is_dns_failure(outcome.error or "") and not state.tried_doh:
        # The local resolver cannot answer; ask DNS-over-HTTPS, keep the
        # public-only address check. Dead domains fail here too -- honestly.
        state.tried_doh = True
        return ("doh", original_url, None, False)

    if kind == "network" and _is_transient_net(outcome):
        if not state.tried_scheme_swap and _scheme_swap(original_url):
            state.tried_scheme_swap = True
            return ("scheme_swap", _scheme_swap(original_url) or "", None, False)
        if not state.tried_host_swap and _host_swap(original_url):
            state.tried_host_swap = True
            return ("host_swap", _host_swap(original_url) or "", None, False)
        step = _sleep_step(state, original_url)
        if step is not None:
            return step

    if kind in ("network", "timeout"):
        # A timeout past its escalation cap gains nothing from another
        # identical dial after a nap (the escalation WAS that retry, at a
        # bigger timeout); the rungs below decide instead. Network errors
        # and pre-cap timeouts keep the backoff semantics.
        timeout_walled = kind == "timeout" and state.timeout_escalations >= _MAX_TIMEOUT_ESCALATIONS
        if not timeout_walled:
            step = _sleep_step(state, original_url)
            if step is not None:
                return step

    if status in (301, 302, 303, 307, 308):
        # A final 3xx means urllib declined to follow (usually a loop where
        # the origin sets a cookie mid-redirect). The shared cookie jar holds
        # what the origin granted; one retry usually closes the loop.
        step = _sleep_step(state, original_url)
        if step is not None:
            return step

    if status in (401, 402, 451):
        # Refusals of auth/payment/law: one profile switch -- some of these
        # walls are UA filters in disguise. After that, the archive decides.
        profile = state.next_profile()
        if profile is not None:
            return (f"profile_{state.profiles_tried}", original_url, profile, False)

    if kind == "http_error" and state.attempts < 3:
        # Odd statuses (400, 405, 418, ...): one polite retry, then archive.
        step = _sleep_step(state, original_url)
        if step is not None:
            return step

    if not state.tried_wayback and kind in ("network", "timeout", "http_error"):
        # LAST RESORT: the Internet Archive's public snapshot of the page.
        # Reading a published page's public copy attacks no one; the outcome
        # is flagged via="wayback" so reports count it as a copy, not a live
        # origin read.
        state.tried_wayback = True
        return ("wayback", _wayback_url(original_url), None, False)

    return None  # unknown: stop rather than loop forever


def _sleep_step(state: _LadderState, url: str) -> tuple[str, str, None, bool] | None:
    """A backoff retry, within the caller's sleep budget; None = give up."""
    if state.sleeps_used >= state.max_sleeps:
        return None
    state.sleeps_used += 1
    return ("sleep", url, None, False)


def _fallback_of(actions: list[str]) -> str:
    """The escalation that produced the final content ('' = none needed)."""
    for name in ("origin_root", "scheme_swap", "host_swap", "tls_unverified", "doh", "wayback"):
        if name in actions:
            return name
    for name in actions:
        if name.startswith("profile_"):
            return name
    return ""


def _wayback_url(url: str) -> str:
    """The Internet Archive's raw-capture view of ``url``."""
    return _WAYBACK_PREFIX + url


def _doh_ips(host: str) -> list[str]:
    """Resolve ``host`` via DNS-over-HTTPS (A + AAAA across two resolvers).

    ``host`` is a bare hostname (or ``req.host``-style value with an optional
    port). Raises :class:`SSRFRefused` when no resolver returns a usable
    record -- the caller treats that exactly like a local resolution failure,
    and the public-only check in :func:`_checked_addresses` still applies
    before any dial. CNAME answers are skipped (their A records appear as
    their own answers).
    """
    bare = _host_only(host)
    if not bare:
        raise SSRFRefused("refusing empty host")
    ips: list[str] = []
    seen: set[str] = set()
    last_error: Exception | None = None
    for endpoint in _DOH_ENDPOINTS:
        for rtype in ("A", "AAAA"):
            if len(ips) >= 8:
                break
            url = endpoint.format(host=urllib.parse.quote(bare, safe=""), rtype=rtype)
            try:
                req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
                with urllib.request.urlopen(req, timeout=8) as resp:
                    payload = json.loads(resp.read(64 * 1024).decode("utf-8", "replace"))
            except (OSError, ValueError) as exc:
                last_error = exc
                continue
            if int(payload.get("Status", 1)) != 0:
                continue
            for answer in payload.get("Answer") or []:
                data = str(answer.get("data", "")).strip()
                try:
                    ipaddress.ip_address(data)
                except ValueError:
                    continue  # CNAME chain links are not dialable
                if data not in seen:
                    seen.add(data)
                    ips.append(data)
    if not ips:
        detail = f": {last_error}" if last_error else ""
        raise SSRFRefused(f"cannot resolve {bare!r} via DoH{detail}")
    return ips


def _doh_resolver_for(target_host: str) -> Resolver:
    """A resolver that answers ONLY ``target_host`` from DoH; every other
    name (redirect hops) uses the normal local resolution path."""

    def resolver(h: str) -> list[str]:
        # h is a req.host-style value (no scheme, maybe a port): _host_only,
        # NOT _host_of -- urlsplit reads 'name:port' as scheme:path.
        if _host_only(h) != target_host:
            return pick_public_ips(h)
        infos = [(0, 0, 0, "", (ip, 0)) for ip in _doh_ips(h)]
        return _checked_addresses(_host_only(h), infos)

    return resolver


def _has_path(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return bool(parsed.path.strip("/"))


def _origin_root(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


def _scheme_swap(url: str) -> str | None:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https":
        return urllib.parse.urlunsplit(("http", parsed.netloc, parsed.path, parsed.query, ""))
    if parsed.scheme == "http":
        return urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))
    return None


def _host_swap(url: str) -> str | None:
    """www.example.com <-> example.com, keeping everything else."""
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    if not host:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    swapped = host[4:] if host.startswith("www.") else f"www.{host}"
    try:
        swapped.encode("idna")
    except UnicodeError:
        return None
    return urllib.parse.urlunsplit(
        (parsed.scheme, f"{swapped}{port}", parsed.path, parsed.query, "")
    )


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
