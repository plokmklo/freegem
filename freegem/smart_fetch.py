"""Smart fetch: a fallback ladder for pages that refuse the first request.

The first attempt is always the honest one: a self-identifying freegem
client, GET, no tricks. When the origin answers with 403 / 405 / 401 / 407 /
429 / 5xx, a timeout, or a connection reset, this module walks a ladder of
LEGITIMATE alternate read paths instead of giving up -- and remembers which
path worked for that host, so the next fetch goes straight there.

The ladder, in order:

1. ``browser-headers``  -- the same URL fetched with a standard
   browser-identifying client. Many 403s are naive UA matching, not a
   policy: a real browser would be served. If the site still refuses a
   browser-shaped request, nothing browser-shaped is faked beyond this.
2. ``tls-browser``      -- OPTIONAL, off by default (config
   ``smart_fetch_tls_browser``): the same URL fetched through curl_cffi
   with Chrome's exact TLS/HTTP2 fingerprint, so the client presenting
   browser headers IS the client whose hello the site fingerprints
   (v2.12.0, docs/ANTIBOT_RESEARCH.md). No header overrides -- a
   mismatched header/TLS pair is itself a detection signal -- one warm-up
   retry carrying the wall's own cookies, session discarded afterwards.
   An explicit choice like browser-render; needs the optional curl_cffi
   dependency.
3. ``reader``           -- ``https://r.jina.ai/<url>``, a public reader
   service that fetches the page itself and returns markdown. The page is
   read through a service the site can see and rate-limit like any client.
4. ``browser-render``   -- OPTIONAL, off by default (config
   ``smart_fetch_browser_render``): a plain headless Chromium render
   through the Playwright fetcher, for pages whose text only exists after
   JavaScript. Positioned here because a FRESH render beats an OLD
   archive; a browser launch is an explicit choice, never a silent
   fallback, and no stealth is involved (NON_GOALS hold).
5. ``flaresolverr``     -- OPTIONAL, off by default (config
   ``smart_fetch_flaresolverr``): an adapter to a FlareSolverr instance
   the user runs themselves -- the community's standard self-hosted
   answer to managed challenge walls. FreeGem ships no solver (NON_GOALS
   hold): the rung only exists when the user configured an endpoint, and
   its result is labeled ``via="flaresolverr"``.
6. ``wayback``          -- the Internet Archive's availability API, then
   the closest snapshot. Published, permanent, and legal: the archive is
   meant to be read.
7. ``ai-search``        -- the bridge's own Gemini search tool is asked for
   the page's content, with citations. AI-mediated and clearly labeled
   (``via="ai-search"``): never presented as a verbatim fetch. Requires a
   configured session; skipped, never half-tried, without one.

What is deliberately NOT here, and stays not here (per NON_GOALS): CAPTCHA
solving, challenge answering, IP/identity rotation, headless-browser
stealth. The tls-browser and flaresolverr rungs (v2.12.0) are the two
OPT-IN exceptions -- each exists only when the user switches it on, and
neither answers CAPTCHAs. A page whose only door is a CAPTCHA
stays unread, and the error says so. The taxonomy of every failed ladder is
honest -- each attempted path and its exact refusal -- so callers (and
humans) can tell "the site demands a login" from "our network is broken".

Per-host memory lives in ``~/.cache/freegem/smart_fetch_memory.json``
(respect ``FREEGEM_CACHE_DIR``). It stores one thing per host: the name of
the ladder step that last worked. Nothing else -- no cookies, no tokens,
no page data.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import flaresolverr_fetcher, tls_fetcher
from .config import BridgeConfig
from .web_scraper import ScrapeError, ScrapeResult, scrape_page

__all__ = [
    "STRATEGIES",
    "StrategyMemory",
    "ai_clean",
    "classify_failure",
    "default_memory_path",
    "fallback_scrape",
]

#: The ladder, in default order. ``browser-render``, ``tls-browser`` and
#: ``flaresolverr`` are the OPTIONAL rungs (config
#: ``smart_fetch_browser_render`` / ``smart_fetch_tls_browser`` /
#: ``smart_fetch_flaresolverr``, all default off: gated inside the
#: rungs themselves and dropped from the order by ``_gated_off``, like
#: ai-search's flag gate). ``ai-search`` is gated at
#: runtime (needs a session and the ``smart_fetch_ai`` config flag).
STRATEGIES = (
    "browser-headers",
    "tls-browser",
    "reader",
    "browser-render",
    "flaresolverr",
    "wayback",
    "ai-search",
)

#: Pause between ladder steps. One failed request per strategy, spaced out:
#: the ladder is a polite reader, not a hammer.
_PAUSE_SEC = 0.4

#: Total wall-clock the ladder may spend walking rungs (#25). Every rung has
#: socket timeouts, but the rungs are SEQUENTIAL: four slow rungs would stack
#: into minutes of silent waiting inside one call. Past the budget the
#: remaining rungs are skipped and recorded honestly in the taxonomy.
_LADDER_BUDGET_SEC = 60.0

#: Wall-clock bound for the ai-search rung's one model call (#25). RESIDUAL
#: (documented, deliberate): the generation thread is abandoned, not killed
#: -- threads cannot be killed -- so a timed-out request keeps running in the
#: background until gemini's own socket and retry bounds end it; its answer
#: is discarded. The bound exists so a wedged generation cannot hold a ladder
#: slot (and, inside a batch, a pool worker) hostage.
_AI_SEARCH_TIMEOUT_SEC = 30.0

#: Wall-clock bound for the optional browser-render rung (#14). The render
#: runs in a daemon worker thread (the #25 discipline): OUR wait is bounded
#: so a wedged launch cannot hold the ladder slot. Playwright's own
#: navigation wait (``_BROWSER_RENDER_WAIT_MS``) fires first for the normal
#: slow-page case; this bound additionally covers everything around it.
#: RESIDUAL (documented, deliberate): the thread is abandoned, not killed --
#: threads cannot be killed -- until Playwright's own bounds end it; the
#: thread is a DAEMON now (#58), so even a driver wedged past every bound
#: can no longer block interpreter exit.
_BROWSER_RENDER_TIMEOUT_SEC = 20.0

#: Playwright navigation wait for the render rung: headroom under the rung
#: wall-clock bound above.
_BROWSER_RENDER_WAIT_MS = 15_000

#: (#61) Phase-one navigation wait. ``networkidle`` is the semantically
#: "fully rendered" wait, but on pages with long-polling, websockets, or
#: periodic XHR it NEVER fires -- the one rung designed for JS-heavy pages
#: timed out hardest on exactly them. The rung now renders in two phases:
#: a SHORT quiet-phase attempt (quiet pages settle in a second or two; if
#: networkidle fires here, this is the best render), then a
#: ``domcontentloaded`` phase with a settle beat for chatty pages whose DOM
#: is ready but whose network never goes quiet.
_BROWSER_RENDER_QUIET_MS = 6_000

#: (#61) The domcontentloaded phase's settle beat: SPA frameworks hydrate
#: after the event fires; this gives them one breath before extraction.
_BROWSER_RENDER_SETTLE_MS = 1_200

#: Wall-clock bound for the tls-browser rung (v2.12.0). curl_cffi is a
#: plain HTTP client -- no launch, no render -- so its bound is the cheap
#: one, and it covers both GETs of the cookie warm-up pair.
_TLS_TIMEOUT_SEC = 15.0

#: Wall-clock bound for the flaresolverr rung (v2.12.0). Solving a managed
#: challenge takes a real browser real seconds; the service's own
#: ``maxTimeout`` gets this minus one second of socket headroom.
_FLARESOLVERR_TIMEOUT_SEC = 45.0

#: (#58) Process-wide cap on CONCURRENT headless renders. Every render is a
#: whole Chromium process; a concurrent batch scrape (up to 16 workers) used
#: to be able to stack 16 of them outside any tracking. Two slots keep the
#: orphan ceiling at two abandoned processes per 20s window while leaving
#: real parallelism for batch runs.
_MAX_CONCURRENT_RENDERS = 2
_RENDER_SLOTS = threading.BoundedSemaphore(_MAX_CONCURRENT_RENDERS)

#: A response that is tiny AND challenge-shaped is not content.
_CHALLENGE_RE = re.compile(
    r"captcha|just a moment|challenge|verify you are human|enable javascript "
    r"and cookies|access denied|attention required",
    re.IGNORECASE,
)
_CHALLENGE_MAX_CHARS = 800

#: (#57) A LONG page that is still mostly challenge boilerplate must not
#: pass the gate just because it cleared the length check. An interstitial
#: carrying >800 chars of explanatory/legalese text repeats its phrases:
#: "Just a moment", "Enable JavaScript and cookies", "Verify you are
#: human". Three or more marker hits inside the head window is boilerplate
#: repetition, not an article that happened to mention a word.
_CHALLENGE_DENSITY_HITS = 3

#: The AI step's "I could not read that page" phrasing. An answer that says
#: it could not access the page is a HONEST non-answer -- the ladder records
#: it as a failure instead of presenting it as content.
_AI_NONANSWER_RE = re.compile(
    r"wasn't able to access|weren't able to access|unable to access|"
    r"could not access|couldn't access|cannot access|"
    r"couldn't retrieve|could not retrieve|unable to retrieve|"
    r"don't have access|do not have access|couldn't find any(?:\s+\w+)? content|"
    r"no (?:search results|snippets|access)",
    re.IGNORECASE,
)
_AI_NONANSWER_MAX_CHARS = 2000

#: Standard browser-identifying request headers. This is what any normal
#: browser sends on every request; reusing it is not fingerprint evasion
#: (no TLS mimicry, no header-order tricks, no rotating identities).
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")

ProgressFn = Callable[[str], None]


# ─── failure classification ──────────────────────────────────────────────────


def classify_failure(error: str) -> str:
    """Bucket one ScrapeError message for the ladder's gate.

    ``http`` / ``network`` / ``type`` engage the ladder; ``refused`` (SSRF
    guard, robots opt-in, non-http schemes) and ``cap`` (size limits) are
    OUR refusals, not the site's -- no alternate path may bypass them, so
    the original error is re-raised as-is.
    """
    if "SSRF guard" in error or "robots.txt" in error:
        return "refused"
    if "refusing scheme" in error:
        return "refused"
    if "exceeds" in error and "cap" in error:
        return "cap"
    if "refusing content type" in error:
        return "type"
    if _HTTP_STATUS_RE.search(error):
        return "http"
    return "network"


def _looks_like_challenge(text: str) -> bool:
    """True when a 'successful' fetch actually returned a bot wall.

    (#57) The old rule was a hard fail-open past 800 chars: a rendered JS
    interstitial with a long explanatory body passed as content, got
    ``remember()``ed as a browser-render WIN, and poisoned that host's
    ladder position for every future fetch. The gate now works in two
    stages over the head window:

    * no challenge language at all in the head -> content, whatever the
      length (a 50k article mentioning nothing challenge-shaped never
      scans further);
    * challenge language within ``_CHALLENGE_MAX_CHARS``: a SHORT page is
      a wall (the original rule -- those pages are only ever walls), a
      LONG one is decided by phrase DENSITY. One or two hits in 800 chars
      is an article about challenges, CAPTCHAs, or bot protection; three
      or more is an interstitial repeating its boilerplate.
    """
    stripped = text.strip()
    if not stripped:
        return False
    head = stripped[:_CHALLENGE_MAX_CHARS]
    hits = len(_CHALLENGE_RE.findall(head))
    if hits == 0:
        return False
    if len(stripped) <= _CHALLENGE_MAX_CHARS:
        return True
    return hits >= _CHALLENGE_DENSITY_HITS


def _looks_like_ai_nonanswer(text: str) -> bool:
    """True when the AI step admitted it could not read the page.

    Checked only against short answers: a long quote of real article text
    that happens to CONTAIN "could not access" is content, not a refusal.
    """
    stripped = text.strip()
    if len(stripped) > _AI_NONANSWER_MAX_CHARS:
        return False
    return bool(_AI_NONANSWER_RE.search(stripped))


# ─── per-host strategy memory ────────────────────────────────────────────────


def default_memory_path() -> Path:
    """``$FREEGEM_CACHE_DIR`` if set, else ``~/.cache/freegem/smart_fetch_memory.json``."""
    base = os.environ.get("FREEGEM_CACHE_DIR")
    root = Path(base) if base else Path.home() / ".cache" / "freegem"
    return root / "smart_fetch_memory.json"


class StrategyMemory:
    """Host -> ladder-step-that-worked, persisted as a small JSON file.

    One entry per host, one string per entry. Loading is lazy and forgiving:
    a missing or corrupt file is an empty memory. Writing is best-effort --
    a read-only cache directory must not turn a successful page fetch into
    an error. Thread-safe via a module-level lock; every instance shares the
    same file, and instances within one process share the loaded dict.
    """

    _shared: dict[str, dict[str, str]] | None = None
    # RLock, not Lock: remember() holds this while _load() re-acquires it.
    _shared_lock = threading.RLock()

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else default_memory_path()

    def _load(self) -> dict[str, dict[str, str]]:
        with StrategyMemory._shared_lock:
            if StrategyMemory._shared is not None:
                return StrategyMemory._shared
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            shared = data if isinstance(data, dict) else {}
            StrategyMemory._shared = {
                host: value
                for host, value in shared.items()
                if isinstance(host, str) and isinstance(value, dict)
            }
            return StrategyMemory._shared

    def get(self, host: str) -> str | None:
        entry = self._load().get(host)
        if not entry:
            return None
        strategy = entry.get("strategy")
        return strategy if strategy in STRATEGIES else None

    def remember(self, host: str, strategy: str) -> None:
        if strategy not in STRATEGIES or not host:
            return
        with StrategyMemory._shared_lock:
            data = self._load()
            data[host] = {"strategy": strategy, "at": str(int(time.time()))}
            StrategyMemory._shared = data
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass  # best-effort persistence: the in-process memory still works


_MEMORY = StrategyMemory()


def clear_memory() -> None:
    """Drop every remembered host strategy (tests, --refresh paths)."""
    with StrategyMemory._shared_lock:
        StrategyMemory._shared = {}
    with contextlib.suppress(OSError):
        _MEMORY.path.unlink()


# ─── per-host serialization ──────────────────────────────────────────────────

_ladder_locks: dict[str, threading.Lock] = {}
_ladder_locks_guard = threading.Lock()


def _ladder_lock(host: str) -> threading.Lock:
    with _ladder_locks_guard:
        lock = _ladder_locks.get(host)
        if lock is None:
            lock = threading.Lock()
            _ladder_locks[host] = lock
        return lock


# ─── strategies ──────────────────────────────────────────────────────────────


#: Strategies whose config gate is open. (#60) The ladder must not spend a
#: slot (or front-rank a remembered win) on a rung that would refuse
#: instantly: ``_strategy_order`` drops gated-off rungs the same way it
#: drops unknown remembered strategies, and a config flip no longer needs
#: memory invalidation -- the stale entry simply stops ranking while the
#: gate is closed and comes back if the gate reopens.
def _gated_off(cfg: BridgeConfig) -> set[str]:
    off: set[str] = set()
    if not getattr(cfg, "smart_fetch_browser_render", False):
        off.add("browser-render")
    if not getattr(cfg, "smart_fetch_tls_browser", False):
        off.add("tls-browser")
    if not flaresolverr_fetcher.is_configured(cfg):
        off.add("flaresolverr")
    if not getattr(cfg, "smart_fetch_ai", True):
        off.add("ai-search")
    return off


def _strategy_order(host: str, cfg: BridgeConfig) -> list[str]:
    """The ladder with gated-off rungs dropped and any remembered winning
    step (among the rungs still allowed) moved to the front."""
    order = [s for s in STRATEGIES if s not in _gated_off(cfg)]
    remembered = _MEMORY.get(host)
    if remembered in order:
        order.remove(remembered)
        order.insert(0, remembered)
    return order


def _try_strategy(
    cfg: BridgeConfig,
    url: str,
    strategy: str,
    max_chars: int,
    ignore_robots: bool,
    budget: float | None = None,
) -> ScrapeResult:
    """One ladder step. Raises ScrapeError when that step cannot read the page.

    ``budget`` (#59) is the ladder's remaining wall-clock in seconds (None
    when the caller does not track one). The expensive rungs size their own
    wait by it, so a rung entered at deadline-epsilon can no longer add its
    full 20-30s on top of an exhausted budget.
    """
    if strategy == "browser-headers":
        return scrape_page(
            cfg,
            url,
            max_chars=max_chars,
            ignore_robots=ignore_robots,
            headers_override=_BROWSER_HEADERS,
        )
    if strategy == "tls-browser":
        return _tls_browser_read(cfg, url, max_chars, budget=budget)
    if strategy == "reader":
        reader_url = "https://r.jina.ai/" + url
        result = scrape_page(
            cfg,
            reader_url,
            # 2x budget: parse HEADROOM for the envelope lines and the
            # reader's own markdown chrome, never a license to return more
            # than the caller asked for (#22).
            max_chars=max_chars * 2,
            ignore_robots=True,
            headers_override=_reader_headers(cfg),
        )
        return _parse_reader_output(result, url, max_chars)
    if strategy == "browser-render":
        return _browser_render_read(cfg, url, max_chars, budget=budget)
    if strategy == "flaresolverr":
        return _flaresolverr_read(cfg, url, max_chars, budget=budget)
    if strategy == "wayback":
        snapshot = _wayback_snapshot(cfg, url)
        result = scrape_page(cfg, snapshot, max_chars=max_chars, ignore_robots=True)
        return replace(result, via="wayback")
    if strategy == "ai-search":
        return _ai_read(cfg, url, max_chars, budget=budget)
    raise ScrapeError(f"unknown smart-fetch strategy {strategy!r}")


def _reader_headers(cfg: BridgeConfig) -> dict[str, str]:
    """Reader request headers: an API key when configured, else anonymous.

    r.jina.ai serves anonymous queries from well-reputed IPs; datacenter
    IPs get 401 AuthenticationRequiredError and must authenticate. The key
    is the user's own (config ``reader_api_key``); it is sent ONLY to
    r.jina.ai and nowhere else.
    """
    headers = dict(_BROWSER_HEADERS)
    key = getattr(cfg, "reader_api_key", "") or ""
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _parse_reader_output(result: ScrapeResult, original_url: str, max_chars: int) -> ScrapeResult:
    """Pull title and body out of the r.jina.ai response envelope.

    The reader prefixes content with ``Title: ...`` / ``URL Source: ...`` /
    ``Markdown Content:`` header lines. When the shape is absent (the
    service evolves), the raw text is kept rather than discarded.

    The output is truncated to the CALLER's ``max_chars`` (#22): the rung
    fetches with ``max_chars * 2`` purely as parse headroom, and the flag
    ``truncated`` reports the caller's budget, not the internal one.
    """
    text = result.text
    title = result.title
    match = re.match(
        r"Title:\s*(.*?)\s*\nURL Source:.*?\nMarkdown Content:\s*\n?(.*)",
        text,
        re.DOTALL,
    )
    if match:
        title = match.group(1).strip() or title
        text = match.group(2).strip()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rstrip()
    return replace(
        result,
        url=original_url,
        title=title or original_url,
        text=text,
        truncated=truncated,
        via="reader",
    )


_WAYBACK_PATH_RE = re.compile(r"^/web/\d{4,14}[a-z_]*/(?:https?://)?(.+)$", re.IGNORECASE)


def _render_once(opts: Any, wall_sec: float) -> Any:
    """One render attempt in a DAEMON thread, our wait bounded by ``wall_sec``.

    (#58) The old rung submitted to a ``ThreadPoolExecutor`` whose threads
    are non-daemon and joined at interpreter exit: a wedged
    ``sync_playwright()``/driver blocked PROCESS EXIT entirely, and the
    abandoned chromium stacked up unbounded across a batch. A plain daemon
    thread fixes the exit-blocking half (the interpreter never joins
    daemons), the semaphore around the rung fixes the stacking half, and a
    result box replaces the future. Playwright's own navigation timeout
    still fires first for the normal slow-page case; the join bound covers
    everything around it. RESIDUAL (documented, deliberate): a timed-out
    render's thread is abandoned, not killed -- threads cannot be killed --
    until Playwright's own bounds end it and its ``finally`` closes the
    browser.
    """
    import threading

    from .playwright_fetcher import fetch_with_playwright

    box: dict[str, Any] = {}

    def _work() -> None:
        try:
            box["result"] = fetch_with_playwright(opts)
        except BaseException as exc:  # re-raised in the caller below
            box["error"] = exc

    worker = threading.Thread(target=_work, name="freegem-browser-render", daemon=True)
    worker.start()
    worker.join(timeout=wall_sec)
    if worker.is_alive():
        raise ScrapeError(f"browser-render timed out after {wall_sec:g}s for {opts.url!r}")
    if "error" in box:
        exc = box["error"]
        raise ScrapeError(
            f"browser-render failed for {opts.url!r}: {type(exc).__name__}: {exc}"
        ) from exc
    return box["result"]


def _browser_render_read(
    cfg: BridgeConfig, url: str, max_chars: int, *, budget: float | None = None
) -> ScrapeResult:
    """Render the page in a headless browser and extract its text (#14).

    The optional ladder rung, OFF by default: only runs when config
    ``smart_fetch_browser_render`` is set, because a browser launch costs
    seconds and is an explicit choice. The browser is a PLAIN headless
    Chromium through :func:`freegem.playwright_fetcher.fetch_with_playwright`
    (the same public entry the background-job API uses): default identity,
    SSRF-guarded per request inside the fetcher, no stealth flags.

    Two structural bounds (#58): a process-wide semaphore caps CONCURRENT
    renders (Chromium processes are heavy; a batch used to stack them
    unbounded), and the rung's wall-clock is sized by the ladder's
    REMAINING budget (#59) instead of always spending its full 20s cap.

    Two render phases (#61): first a SHORT ``networkidle`` attempt (quiet
    pages settle fast and get the best render), then -- only if the page's
    network never goes quiet (SPA long-polling, websockets) -- a
    ``domcontentloaded`` attempt with a settle beat. The old single
    ``networkidle`` attempt failed hardest on exactly the JS-heavy pages
    this rung exists for. Both attempts are recorded honestly on total
    failure.

    Every failure is a clean ScrapeError so the ladder records it and moves
    on to wayback; nothing raises through.
    """
    if not getattr(cfg, "smart_fetch_browser_render", False):
        raise ScrapeError("browser-render disabled (smart_fetch_browser_render=false)")

    from .playwright_fetcher import PlaywrightOptions, is_playwright_available
    from .web_scraper import _clean_text, _parse_html

    if not is_playwright_available():
        raise ScrapeError(f"browser-render unavailable for {url!r}: playwright is not installed")

    now = time.monotonic()
    wall = (
        _BROWSER_RENDER_TIMEOUT_SEC if budget is None else min(_BROWSER_RENDER_TIMEOUT_SEC, budget)
    )
    if wall < 1.0:
        raise ScrapeError(
            f"browser-render skipped for {url!r}: only {max(wall, 0.0):.2f}s of ladder budget remain"
        )

    # (#58) Bounded slot wait: a batch worker whose two render slots are held
    # waits at most ``wall`` seconds for one, then reports honestly instead
    # of stacking a seventeenth chromium.
    if not _RENDER_SLOTS.acquire(timeout=wall):
        raise ScrapeError(
            f"browser-render busy: {_MAX_CONCURRENT_RENDERS} concurrent renders hold "
            f"all slots (waited {wall:g}s) for {url!r}"
        )

    failures: list[str] = []
    try:
        # Phase 1: the quiet render. Six seconds of networkidle -- enough
        # for every well-behaved page, cheap enough to leave phase-2 room.
        quiet_ms = min(_BROWSER_RENDER_QUIET_MS, int(wall * 1000))
        try:
            rendered = _render_once(
                PlaywrightOptions(url=url, wait_until="networkidle", wait_timeout_ms=quiet_ms),
                wall,
            )
        except ScrapeError as exc:
            failures.append(f"networkidle: {exc}")
            rendered = None
        if rendered is None:
            remaining = wall - (time.monotonic() - now)
            if remaining < 1.0:
                raise ScrapeError(
                    f"browser-render failed for {url!r}: {'; '.join(failures)}"
                ) from None
            # Phase 2: the chatty page's render. The DOM is there; give the
            # SPA one settle beat, then extract.
            try:
                rendered = _render_once(
                    PlaywrightOptions(
                        url=url,
                        wait_until="domcontentloaded",
                        wait_timeout_ms=min(_BROWSER_RENDER_WAIT_MS, int(remaining * 1000)),
                        settle_ms=_BROWSER_RENDER_SETTLE_MS,
                    ),
                    remaining,
                )
            except ScrapeError as exc:
                failures.append(f"domcontentloaded: {exc}")
                raise ScrapeError(
                    f"browser-render failed for {url!r}: {'; '.join(failures)}"
                ) from exc
    finally:
        _RENDER_SLOTS.release()

    if rendered.error:
        raise ScrapeError(f"browser-render refused for {url!r}: {rendered.error}")
    if not rendered.ok:
        raise ScrapeError(f"browser-render got HTTP {rendered.status} for {url!r}")
    if not (rendered.text or "").strip():
        raise ScrapeError(f"browser-render returned no content for {url!r}")

    # The rendered DOM is HTML: the ordinary extractor strips scripts and
    # chrome, exactly like the direct path.
    extractor = _parse_html(cfg, url, rendered.text)
    text = _clean_text(extractor.text_parts)
    if not text:
        raise ScrapeError(f"browser-render extracted zero readable text from {url!r}")
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rstrip()
    return ScrapeResult(
        url=url,
        title=extractor.title or rendered.title or url,
        text=text,
        truncated=truncated,
        via="browser-render",
    )


def _ext_html_read(
    cfg: BridgeConfig, via: str, url: str, html: str, max_chars: int
) -> ScrapeResult:
    """Extract text from a rung's raw HTML exactly like the direct path does.

    Shared by the rungs that obtain RAW HTML from somewhere else
    (tls-browser, flaresolverr): the ordinary extractor strips scripts and
    chrome, so a rung win differs from a direct win by its ``via`` label
    and nothing else.
    """
    from .web_scraper import _clean_text, _parse_html

    extractor = _parse_html(cfg, url, html)
    text = _clean_text(extractor.text_parts)
    if not text:
        raise ScrapeError(f"{via} extracted zero readable text from {url!r}")
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars].rstrip()
    return ScrapeResult(
        url=url,
        title=extractor.title or url,
        text=text,
        truncated=truncated,
        via=via,
    )


def _tls_browser_read(
    cfg: BridgeConfig, url: str, max_chars: int, *, budget: float | None = None
) -> ScrapeResult:
    """The tls-browser rung (v2.12.0): Chrome's TLS hello through curl_cffi.

    OFF by default (config ``smart_fetch_tls_browser``): it presents the
    TLS fingerprint of the browser whose headers the browser-headers rung
    already sends, which is the point where naive UA matching ends and
    fingerprinting begins -- an explicit user choice, never a silent
    fallback. Honors ``cfg.proxy``; no header overrides; at most two GETs
    (one warm-up retry); the session and its cookies are discarded.
    """
    if not getattr(cfg, "smart_fetch_tls_browser", False):
        raise ScrapeError("tls-browser disabled (smart_fetch_tls_browser=false)")

    wall = _TLS_TIMEOUT_SEC if budget is None else min(_TLS_TIMEOUT_SEC, budget)
    if wall < 1.0:
        raise ScrapeError(
            f"tls-browser skipped for {url!r}: only {max(wall, 0.0):.2f}s of ladder budget remain"
        )
    try:
        html = tls_fetcher.tls_fetch_html(cfg, url, timeout=wall)
    except tls_fetcher.TlsFetchError as exc:
        raise ScrapeError(str(exc)) from exc
    return _ext_html_read(cfg, "tls-browser", url, html, max_chars)


def _flaresolverr_read(
    cfg: BridgeConfig, url: str, max_chars: int, *, budget: float | None = None
) -> ScrapeResult:
    """The flaresolverr rung (v2.12.0): solve through the user's own service.

    Exists only when config ``smart_fetch_flaresolverr`` holds an endpoint
    (the ladder's gate drops it otherwise; the client itself refuses too).
    The solved HTML goes through the ordinary extractor; the result is
    labeled ``via="flaresolverr"`` end to end.
    """
    wall = _FLARESOLVERR_TIMEOUT_SEC if budget is None else min(_FLARESOLVERR_TIMEOUT_SEC, budget)
    if wall < 5.0:
        raise ScrapeError(
            f"flaresolverr skipped for {url!r}: only {max(wall, 0.0):.2f}s of ladder budget remain"
        )
    try:
        html = flaresolverr_fetcher.flaresolverr_html(cfg, url, timeout=wall)
    except flaresolverr_fetcher.FlareSolverrError as exc:
        raise ScrapeError(str(exc)) from exc
    return _ext_html_read(cfg, "flaresolverr", url, html, max_chars)


def _snapshot_matches(snapshot: str, url: str) -> bool:
    """True when the Wayback snapshot URL embeds the SAME page as ``url`` (#23).

    The availability API answers with ``.../web/<stamp>/<target>``; a
    snapshot whose embedded target points at a different host or path would
    silently serve the WRONG page as the archived copy of ``url``. Scheme,
    port, capture timestamp, and query string are deliberately not compared:
    Wayback normalizes those between capture and replay.
    """
    try:
        snap = urllib.parse.urlsplit(snapshot)
        orig = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    match = _WAYBACK_PATH_RE.match(snap.path or "")
    if not match:
        return False
    target_host, _, target_path = match.group(1).partition("/")
    orig_host = (orig.hostname or "").lower()
    if not orig_host or target_host.lower() != orig_host:
        return False
    # strip("/") on both sides: the split above consumes the target's leading
    # slash, and bare domains have none at all.
    return target_path.strip("/") == (orig.path or "/").strip("/")


def _wayback_snapshot(cfg: BridgeConfig, url: str) -> str:
    """The closest Wayback Machine snapshot URL for ``url``, via the
    availability API (fetched through the guarded opener like everything else)."""
    from .web_scraper import _decode_body, _fetch, _read_capped

    api = "https://archive.org/wayback/available?" + urllib.parse.urlencode({"url": url})
    response = _fetch(cfg, api, timeout=15)
    try:
        data = json.loads(_decode_body(_read_capped(response), ""))
    except ValueError as exc:
        raise ScrapeError(f"wayback availability API returned unusable data: {exc}") from exc
    closest = (data.get("archived_snapshots") or {}).get("closest") or {}
    snapshot = closest.get("url")
    if not isinstance(snapshot, str) or not snapshot.startswith("http"):
        raise ScrapeError(f"no Wayback Machine snapshot exists for {url!r}")
    if snapshot.startswith("http://"):
        snapshot = "https://" + snapshot[len("http://") :]
    if not _snapshot_matches(snapshot, url):
        # (#23) The API sometimes answers with a snapshot of a DIFFERENT page
        # (a redirect target, a close-match). Serving it as the archived copy
        # of ``url`` would be a lie; refuse the rung and let the ladder move
        # on to the next one.
        raise ScrapeError(
            f"wayback availability API returned a snapshot for a different page than {url!r}"
        )
    return snapshot


def _ai_search_answer(
    cfg: BridgeConfig, prompt: str, url: str, *, budget: float | None = None
) -> str:
    """One bounded model call for the ai-search rung (#25).

    A thread is the only way to bound a synchronous call: ``run_tool`` ->
    ``generate`` carries its own retry/backoff budget, but nothing a caller
    can wait on. ``result(timeout=...)`` bounds OUR wait; the generation
    thread itself is abandoned (see ``_AI_SEARCH_TIMEOUT_SEC``).

    (#59) ``budget`` (the ladder's remaining wall-clock, when the caller
    tracks one) sizes that wait together with the fixed cap, so a rung
    entered near the ladder deadline can no longer overrun it by up to the
    full 30s.
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    from .mcp_tools import run_tool

    timeout = _AI_SEARCH_TIMEOUT_SEC if budget is None else min(_AI_SEARCH_TIMEOUT_SEC, budget)
    if timeout < 1.0:
        raise ScrapeError(
            f"ai-search skipped for {url!r}: only {max(timeout, 0.0):.2f}s of ladder budget remain"
        )
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-search")
    try:
        future = pool.submit(run_tool, cfg, "gemini_search", {"query": prompt})
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout as exc:
            raise ScrapeError(f"ai-search timed out after {timeout:g}s for {url!r}") from exc
    finally:
        # Do NOT wait: the whole point is that a wedged generation cannot
        # hold the ladder (or a batch worker) hostage.
        pool.shutdown(wait=False, cancel_futures=True)


def _ai_read(
    cfg: BridgeConfig, url: str, max_chars: int, *, budget: float | None = None
) -> ScrapeResult:
    """Ask the bridge's own Gemini search tool for the page's content.

    The result is an AI-mediated answer with citations, labeled
    ``via="ai-search"`` so callers never confuse it with a verbatim fetch.
    Non-deterministic by nature -- exactly what NON_GOALS says LLM
    extraction is -- which is why it is the LAST ladder step, not the first.
    Anonymous (cookie-less) sessions are ATTEMPTED, not assumed: Gemini
    works without a cookie on many networks, and if the bridge refuses,
    the ladder records that honestly and moves on.
    """
    if not getattr(cfg, "smart_fetch_ai", True):
        raise ScrapeError("ai-search disabled (smart_fetch_ai=false)")
    prompt = (
        f"Find and return the main text content of the page {url}. Quote the "
        "article body as fully and verbatim as you can, and cite the source "
        "URL. If you cannot access the page content, say exactly that "
        "instead of summarizing from memory."
    )
    answer = _ai_search_answer(cfg, prompt, url, budget=budget).strip()
    if not answer or len(answer) < 80:
        raise ScrapeError(f"ai-search produced no usable content for {url!r}")
    text = answer[:max_chars]
    return ScrapeResult(
        url=url,
        title=f"AI-assisted read of {url}",
        text=text,
        truncated=len(answer) > max_chars,
        via="ai-search",
    )


# ─── the ladder ──────────────────────────────────────────────────────────────


def fallback_scrape(
    cfg: BridgeConfig,
    url: str,
    *,
    max_chars: int,
    ignore_robots: bool = True,
    first_error: str,
    progress: ProgressFn | None = None,
) -> ScrapeResult:
    """Walk the ladder after ``first_error``; raise an honest taxonomy on total failure.

    The caller has already tried the direct fetch and it failed. This
    function never re-tries the direct path (the site answered; hammering
    it again would be an attack pattern, not reading) and never engages
    when the failure was OUR refusal (SSRF guard, size cap).
    """
    kind = classify_failure(first_error)
    if kind in ("refused", "cap"):
        # Our guard, not the site's answer. No alternate path may bypass it.
        raise ScrapeError(first_error)

    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        host = ""

    with _ladder_lock(host):
        attempts: list[str] = [f"direct: {first_error}"]
        deadline = time.monotonic() + _LADDER_BUDGET_SEC
        for strategy in _strategy_order(host, cfg):
            if time.monotonic() >= deadline:
                # (#25) The budget exists so four slow rungs cannot stack
                # into minutes of silence. Skipped rungs are recorded just
                # like failed ones: the taxonomy stays honest.
                attempts.append(
                    f"{strategy}: skipped, the {_LADDER_BUDGET_SEC:g}s ladder budget was exhausted"
                )
                continue
            if progress is not None:
                progress(f"smart-fetch: direct failed ({first_error}); trying {strategy}")
            time.sleep(_PAUSE_SEC)
            try:
                result = _try_strategy(
                    cfg,
                    url,
                    strategy,
                    max_chars,
                    ignore_robots,
                    # (#59) The rung sizes its own expensive wait by what is
                    # actually left, so entering at deadline-epsilon cannot
                    # add a fresh 20-30s overrun past the budget.
                    budget=max(0.0, deadline - time.monotonic()),
                )
            except ScrapeError as exc:
                attempts.append(f"{strategy}: {exc}")
                continue
            if not result.text.strip():
                attempts.append(f"{strategy}: fetched but extracted zero readable text")
                continue
            if _looks_like_challenge(result.text):
                attempts.append(f"{strategy}: got a bot-challenge page, not content")
                continue
            if strategy == "ai-search" and _looks_like_ai_nonanswer(result.text):
                attempts.append(f"{strategy}: the bridge could not read the page either")
                continue
            _MEMORY.remember(host, strategy)
            if progress is not None:
                progress(f"smart-fetch: {strategy} read {host or url} ({len(result.text)} chars)")
            return result
    taxonomy = "; ".join(attempts)
    raise ScrapeError(
        f"every read path refused {url!r} — honest failure taxonomy [{kind}]: {taxonomy}"
    )


# ─── AI-assisted content cleanup ──────────────────────────────────────────


#: One bounded model call for ai-clean (#25 discipline).
_AI_CLEAN_TIMEOUT_SEC = 30.0

#: Below this there is nothing worth cleaning: the extractor already
#: returned a page small enough to read by hand.
_AI_CLEAN_MIN_CHARS = 400

#: Page text fed to the model, head-trimmed: a 50k dump would spend the
#: model's attention on boilerplate the extractor already mis-ranked.
_AI_CLEAN_MAX_INPUT_CHARS = 20_000


def ai_clean(cfg: BridgeConfig, url: str, text: str, *, budget: float | None = None) -> str:
    """One bounded model pass that returns the page's MAIN readable content.

    AI-assisted scraping, stated honestly. The input is an already-fetched
    page's extracted text -- navigation menus, cookie notices, and related-
    link walls often surround the article. The bridge's own session is
    asked to return the article body verbatim, dropping the boilerplate.
    The output is an AI-mediated VIEW of the page: labeled ``ai-clean`` by
    the callers, never written to the fetch cache (cache entries hold
    verbatim extractions, #21), never presented as a verbatim fetch, and
    probabilistic by nature -- exactly what NON_GOALS says LLM extraction
    is. OFF by default (config ``smart_fetch_ai_clean``): a transformation
    of a successful read must be an explicit choice, like browser-render.

    Bounded like every model call here (#25): our wait is capped at
    ``_AI_CLEAN_TIMEOUT_SEC`` (or the caller's remaining ``budget``), the
    generation thread is abandoned on timeout, and an "I could not read
    that" answer is a clean ScrapeError, never content.
    """
    if not getattr(cfg, "smart_fetch_ai_clean", False):
        raise ScrapeError("ai-clean disabled (smart_fetch_ai_clean=false)")
    stripped = text.strip()
    if len(stripped) < _AI_CLEAN_MIN_CHARS:
        raise ScrapeError(
            f"ai-clean skipped for {url!r}: page text is only {len(stripped)} chars "
            f"(minimum {_AI_CLEAN_MIN_CHARS}) -- nothing worth cleaning"
        )
    prompt = (
        "The following is the extracted text of the web page " + url + ".\n"
        "Return ONLY the page's main readable content (the article, post, or "
        "documentation body), as verbatim as you can, in reading order. Drop "
        "navigation menus, cookie banners, subscription prompts, related-link "
        "walls, and footer boilerplate. Do not summarize, do not add commentary, "
        "do not answer questions about the text. If the text below is only "
        "boilerplate with no main content, say exactly: NO MAIN CONTENT.\n\n"
        "[PAGE TEXT]\n" + stripped[:_AI_CLEAN_MAX_INPUT_CHARS] + "\n[/PAGE TEXT]"
    )
    timeout = _AI_CLEAN_TIMEOUT_SEC if budget is None else min(_AI_CLEAN_TIMEOUT_SEC, budget)
    if timeout < 1.0:
        raise ScrapeError(
            f"ai-clean skipped for {url!r}: only {max(timeout, 0.0):.2f}s of budget remain"
        )
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    from .llm_extract import BridgeProvider

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ai-clean")
    try:
        future = pool.submit(BridgeProvider(cfg).complete, prompt)
        try:
            answer = future.result(timeout=timeout)
        except FuturesTimeout as exc:
            raise ScrapeError(f"ai-clean timed out after {timeout:g}s for {url!r}") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    answer = (answer or "").strip()
    # Refusal shapes FIRST: "NO MAIN CONTENT" is short BY DESIGN (the prompt
    # tells the model to say exactly that), so the length gate must not
    # reclassify an honest "nothing here" as a production failure.
    if _looks_like_ai_nonanswer(answer) or answer == "NO MAIN CONTENT":
        raise ScrapeError(f"ai-clean found no main content in {url!r}")
    if not answer or len(answer) < _AI_CLEAN_MIN_CHARS // 2:
        raise ScrapeError(f"ai-clean produced no usable content for {url!r}")
    return answer
