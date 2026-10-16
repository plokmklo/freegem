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
2. ``reader``           -- ``https://r.jina.ai/<url>``, a public reader
   service that fetches the page itself and returns markdown. The page is
   read through a service the site can see and rate-limit like any client.
3. ``wayback``          -- the Internet Archive's availability API, then
   the closest snapshot. Published, permanent, and legal: the archive is
   meant to be read.
4. ``ai-search``        -- the bridge's own Gemini search tool is asked for
   the page's content, with citations. AI-mediated and clearly labeled
   (``via="ai-search"``): never presented as a verbatim fetch. Requires a
   configured session; skipped, never half-tried, without one.

What is deliberately NOT here, and stays not here (per NON_GOALS): CAPTCHA
solving, challenge answering, IP/identity rotation, TLS-fingerprint
spoofing, headless-browser stealth. A page whose only door is a CAPTCHA
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

from .config import BridgeConfig
from .web_scraper import ScrapeError, ScrapeResult, scrape_page

__all__ = [
    "STRATEGIES",
    "StrategyMemory",
    "classify_failure",
    "default_memory_path",
    "fallback_scrape",
]

#: The ladder, in default order. ``ai-search`` is gated at runtime (needs a
#: session and the ``smart_fetch_ai`` config flag).
STRATEGIES = ("browser-headers", "reader", "wayback", "ai-search")

#: Pause between ladder steps. One failed request per strategy, spaced out:
#: the ladder is a polite reader, not a hammer.
_PAUSE_SEC = 0.4

#: A response that is tiny AND challenge-shaped is not content.
_CHALLENGE_RE = re.compile(
    r"captcha|just a moment|challenge|verify you are human|enable javascript "
    r"and cookies|access denied|attention required",
    re.IGNORECASE,
)
_CHALLENGE_MAX_CHARS = 800

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
    guard, robots opt-in) and ``cap`` (size limits) are OUR refusals, not
    the site's -- no alternate path may bypass them, so the original error
    is re-raised as-is.
    """
    if "SSRF guard" in error or "robots.txt" in error:
        return "refused"
    if "exceeds" in error and "cap" in error:
        return "cap"
    if "refusing content type" in error:
        return "type"
    if _HTTP_STATUS_RE.search(error):
        return "http"
    return "network"


def _looks_like_challenge(text: str) -> bool:
    """True when a 'successful' fetch actually returned a bot wall."""
    stripped = text.strip()
    if len(stripped) > _CHALLENGE_MAX_CHARS:
        return False
    return bool(_CHALLENGE_RE.search(stripped[:_CHALLENGE_MAX_CHARS]))


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


def _strategy_order(host: str) -> list[str]:
    """The ladder with any remembered winning step moved to the front."""
    order = list(STRATEGIES)
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
) -> ScrapeResult:
    """One ladder step. Raises ScrapeError when that step cannot read the page."""
    if strategy == "browser-headers":
        return scrape_page(
            cfg,
            url,
            max_chars=max_chars,
            ignore_robots=ignore_robots,
            headers_override=_BROWSER_HEADERS,
        )
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
    if strategy == "wayback":
        snapshot = _wayback_snapshot(cfg, url)
        result = scrape_page(cfg, snapshot, max_chars=max_chars, ignore_robots=True)
        return replace(result, via="wayback")
    if strategy == "ai-search":
        return _ai_read(cfg, url, max_chars)
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
    return snapshot


def _ai_read(cfg: BridgeConfig, url: str, max_chars: int) -> ScrapeResult:
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
    from .mcp_tools import run_tool

    prompt = (
        f"Find and return the main text content of the page {url}. Quote the "
        "article body as fully and verbatim as you can, and cite the source "
        "URL. If you cannot access the page content, say exactly that "
        "instead of summarizing from memory."
    )
    answer = run_tool(cfg, "gemini_search", {"query": prompt}).strip()
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
        for strategy in _strategy_order(host):
            if progress is not None:
                progress(f"smart-fetch: direct failed ({first_error}); trying {strategy}")
            time.sleep(_PAUSE_SEC)
            try:
                result = _try_strategy(cfg, url, strategy, max_chars, ignore_robots)
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
