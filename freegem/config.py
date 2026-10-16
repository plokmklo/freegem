"""Configuration. Frozen dataclass, no global mutable state.

Why not a dict like the original?

The original used a module-level ``CONFIG`` dict that every module mutated in
place. Under a threaded HTTP server that is a data race: one request setting
``CONFIG['cookie_file']`` for a per-request override can be observed by
another request mid-flight. Cloudflare Workers port of this project hit the
same bug in production (cookie cross-contamination between concurrent
requests) and fixed it with request-scoped config copies.

This module makes that impossible: :class:`BridgeConfig` is a frozen dataclass,
so the only way to change it is :meth:`BridgeConfig.replace`, which returns a
new instance. Callers hold their own copy.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

__all__ = ["DEFAULT_CONFIG", "BridgeConfig", "find_config", "load_config"]

# Defaults are read-only. MappingProxyType ENFORCES the immutability the
# comment always claimed: a plain dict annotated ``Mapping`` was still one
# assignment away from being mutated by any importer. Use
# BridgeConfig.replace() to derive variants.
DEFAULT_CONFIG: Mapping[str, Any] = MappingProxyType(
    {
        "port": 8081,
        "host": "127.0.0.1",  # loopback by default: do not expose your session
        "retry_attempts": 3,
        "retry_delay_sec": 2,
        "request_timeout_sec": 180,
        "gemini_bl": "boq_assistant-bard-web-server_20260716.08_p0",
        "auth_user": None,
        "xsrf_token": None,
        "default_model": "gemini-3.6-flash",
        "cookie_file": None,
        "proxy": None,
        "temporary_chats": False,
        "log_requests": True,
        # Concurrent fetches per research() call (bounded pool, per-host locks).
        "research_concurrency": 3,
        # robots.txt cache lifetime per origin; entries revalidate conditionally
        # (ETag / Last-Modified) after this many seconds.
        "robots_cache_ttl_sec": 600,
        # Ask the research answer to append an evidence block quoting the
        # passages that back its claims; every quote is verified against the
        # text actually fetched and the result carries per-quote flags.
        "research_evidence": True,
        # > 0 enables the server's short admission queue: requests beyond the
        # concurrency cap wait up to this many seconds for a slot instead of
        # getting an immediate 503. 0 keeps today's refuse-fast behavior.
        "request_queue_wait_sec": 0.0,
        # Claim verification's optional URL discovery searches this wiki through
        # the Wikimedia API portal (or the site's own OpenSearch API for
        # non-Wikimedia MediaWiki hosts). robots.txt is honored either way.
        "discover_wiki": "en.wikipedia.org",
        # Discovery backend for verify --discover: "wiki" searches the MediaWiki
        # site above; "serp" searches the web through the DuckDuckGo HTML
        # endpoint (better for claims about software, companies, current events).
        "discover_backend": "wiki",
        # Smart-fetch fallback ladder: when the direct, self-identifying fetch
        # is refused (403/405/401/...), retry through legitimate read paths
        # (browser-identifying headers, r.jina.ai public reader, Internet
        # Archive, Gemini search) and remember what worked per host. No CAPTCHA
        # solving, no challenge answering, no identity rotation -- a site that
        # insists on a CAPTCHA stays unread and the failure says so.
        "smart_fetch": True,
        # The last ladder step is AI-mediated (Gemini search asked for the
        # page's content, cited and labeled via="ai-search"). Needs a session.
        "smart_fetch_ai": True,
        # Optional browser-render rung in the smart-fetch ladder (#14): a
        # headless Chromium render through Playwright, for pages whose text
        # only exists after JavaScript. OFF by default -- a browser launch
        # costs seconds and is an explicit choice, never a silent fallback.
        # When on, the rung sits between the reader and the Internet Archive
        # (a fresh render beats an old snapshot) and is a plain headless
        # browser: SSRF-guarded per request, no stealth, NON_GOALS hold.
        "smart_fetch_browser_render": False,
        # Optional bearer key for the server's own job/result/artifact REST API
        # and the MCP adapter. None keeps the loopback-only, unauthenticated
        # behavior; a non-None value makes those endpoints demand
        # ``Authorization: Bearer <key>``. The Gemini session (cookies/xsrf) is
        # separate: this key gates OUR api, it never authenticates to Google.
        "api_key": None,
        "brave_api_key": "",
        # Optional r.jina.ai reader key. The reader serves anonymous queries
        # from well-reputed IPs; datacenter IPs get 401 and must authenticate.
        # Sent ONLY to r.jina.ai, only by the smart-fetch ladder.
        "reader_api_key": "",
        # Ceiling for batch-scrape concurrency (#15): ``freegem scrape``'s
        # --concurrency is clamped to this before the bounded pool is built.
        # The old code hard-clamped to 16 silently; the config key makes the
        # ceiling explicit and raisable, and a clamped request says so on
        # stderr (stdout stays pipeline-safe). Floor 1, whatever the value.
        "batch_max_concurrency": 16,
        # Bounded background-job execution (2.9): fixed worker pool, bounded
        # pending queue (saturation answers HTTP 429), per-job execution
        # timeout with a watchdog, capped retries, retention window.
        "job_max_workers": 4,
        "job_max_pending": 100,
        "job_timeout_sec": 600,
        "job_retention": 500,
        # Durable job status: SQLite by default ("memory" disables); the
        # special path keeps queued/processing jobs honest across restarts
        # (they come back "interrupted", never silently lost).
        "job_db": None,
        # Optional external execution queue (memory://, sqlite:///path,
        # redis://, amqp://). None keeps execution in-process.
        "job_queue_url": None,
    }
)


# ─── value coercion ──────────────────────────────────────────────────────────

#: JSON has no distinct integer/boolean/string types per field, and configs
#: are written by hand. Without coercion, "port": "9000" produced a string
#: field and broke the bind, and "temporary_chats": "false" -- a NON-EMPTY
#: string, therefore truthy -- silently enabled what the user asked to
#: disable. These sets mirror BridgeConfig's field annotations.
_INT_FIELDS = frozenset(
    {
        "port",
        "retry_attempts",
        "retry_delay_sec",
        "request_timeout_sec",
        "research_concurrency",
        "robots_cache_ttl_sec",
        "batch_max_concurrency",
        "job_max_workers",
        "job_max_pending",
        "job_timeout_sec",
        "job_retention",
    }
)
_FLOAT_FIELDS = frozenset({"request_queue_wait_sec"})
_BOOL_FIELDS = frozenset(
    {
        "temporary_chats",
        "log_requests",
        "research_evidence",
        "smart_fetch",
        "smart_fetch_ai",
        "smart_fetch_browser_render",
    }
)
_STR_FIELDS = frozenset(
    {
        "host",
        "gemini_bl",
        "default_model",
        "discover_wiki",
        "discover_backend",
        "cookie_file",
        "proxy",
        "auth_user",
        "xsrf_token",
        "brave_api_key",
        "reader_api_key",
        "api_key",
        "job_db",
        "job_queue_url",
    }
)
_NULLABLE_STR_FIELDS = frozenset(
    {"cookie_file", "proxy", "auth_user", "xsrf_token", "api_key", "job_db", "job_queue_url"}
)

#: Sentinel returned by _coerce_value when the value cannot be salvaged and
#: the documented default must apply instead.
_DROP = object()


def _coerce_value(name: str, value: Any) -> Any:
    """Coerce one config value to the type its BridgeConfig field declares.

    Booleans accept true/false/1/0/yes/no/on/off in any case; integers and
    floats accept numeric strings; strings accept scalar JSON values. A
    value that cannot be coerced returns the ``_DROP`` sentinel so the
    documented default applies -- a bad value must not crash startup NOR
    silently mean the opposite of what was written.
    """
    if name in _BOOL_FIELDS:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("1", "true", "yes", "on"):
                return True
            if low in ("0", "false", "no", "off", ""):
                return False
            return _DROP
        if isinstance(value, (int, float)):
            return bool(value)
        return _DROP
    if name in _INT_FIELDS:
        if isinstance(value, bool):  # bool is an int subclass; "port": true is nonsense
            return _DROP
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                return _DROP
        return _DROP
    if name in _FLOAT_FIELDS:
        if isinstance(value, bool):
            return _DROP
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                return _DROP
        return _DROP
    if name in _STR_FIELDS:
        if value is None:
            return None if name in _NULLABLE_STR_FIELDS else _DROP
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        return _DROP
    return value


@dataclass(frozen=True)
class BridgeConfig:
    """Immutable bridge configuration.

    ``api_key`` (optional) gates the server's own job/result/artifact API
    and MCP adapter behind a bearer token; when it is None those endpoints
    stay loopback-only and unauthenticated. It never authenticates to
    Google -- the Gemini session (cookie file + xsrf token) is separate.
    """

    port: int = DEFAULT_CONFIG["port"]
    host: str = DEFAULT_CONFIG["host"]
    retry_attempts: int = DEFAULT_CONFIG["retry_attempts"]
    retry_delay_sec: int = DEFAULT_CONFIG["retry_delay_sec"]
    request_timeout_sec: int = DEFAULT_CONFIG["request_timeout_sec"]
    gemini_bl: str = DEFAULT_CONFIG["gemini_bl"]
    auth_user: str | None = DEFAULT_CONFIG["auth_user"]
    xsrf_token: str | None = DEFAULT_CONFIG["xsrf_token"]
    default_model: str = DEFAULT_CONFIG["default_model"]
    cookie_file: str | None = DEFAULT_CONFIG["cookie_file"]
    proxy: str | None = DEFAULT_CONFIG["proxy"]
    temporary_chats: bool = DEFAULT_CONFIG["temporary_chats"]
    log_requests: bool = DEFAULT_CONFIG["log_requests"]
    research_concurrency: int = DEFAULT_CONFIG["research_concurrency"]
    robots_cache_ttl_sec: int = DEFAULT_CONFIG["robots_cache_ttl_sec"]
    batch_max_concurrency: int = DEFAULT_CONFIG["batch_max_concurrency"]
    request_queue_wait_sec: float = DEFAULT_CONFIG["request_queue_wait_sec"]
    research_evidence: bool = DEFAULT_CONFIG["research_evidence"]
    discover_wiki: str = DEFAULT_CONFIG["discover_wiki"]
    discover_backend: str = DEFAULT_CONFIG["discover_backend"]
    smart_fetch: bool = DEFAULT_CONFIG["smart_fetch"]
    smart_fetch_ai: bool = DEFAULT_CONFIG["smart_fetch_ai"]
    smart_fetch_browser_render: bool = DEFAULT_CONFIG["smart_fetch_browser_render"]
    api_key: str | None = DEFAULT_CONFIG["api_key"]
    brave_api_key: str = DEFAULT_CONFIG["brave_api_key"]
    reader_api_key: str = DEFAULT_CONFIG["reader_api_key"]
    job_max_workers: int = DEFAULT_CONFIG["job_max_workers"]
    job_max_pending: int = DEFAULT_CONFIG["job_max_pending"]
    job_timeout_sec: int = DEFAULT_CONFIG["job_timeout_sec"]
    job_retention: int = DEFAULT_CONFIG["job_retention"]
    job_db: str | None = DEFAULT_CONFIG["job_db"]
    job_queue_url: str | None = DEFAULT_CONFIG["job_queue_url"]

    # ---- construction -------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BridgeConfig:
        """Build from a mapping, coercing types and applying env overrides.

        Unknown keys are dropped rather than rejected so that a config file
        written for an older version still loads. Values are coerced to the
        type each field declares (``"9000"`` becomes ``9000``,
        ``"false"`` becomes ``False``); a value that cannot be coerced is
        dropped in favor of the documented default. Secrets (``xsrf_token``)
        may come from the environment instead of the file.
        """
        known: dict[str, Any] = {}
        for key, value in data.items():
            if key not in cls.__dataclass_fields__:
                continue
            coerced = _coerce_value(key, value)
            if coerced is not _DROP:
                known[key] = coerced
        cfg = cls(**{**dict(DEFAULT_CONFIG), **known})

        env = os.environ.get("FREEGEM_CONFIG")
        if env and not data:
            # no explicit config was passed: honour the env var
            pass

        # Environment can supply the session pieces without touching the file.
        if os.environ.get("FREEGEM_API_KEY"):
            cfg = cfg.replace(api_key=os.environ["FREEGEM_API_KEY"])
        if os.environ.get("GEMINI_XSRF_TOKEN"):
            cfg = cfg.replace(xsrf_token=os.environ["GEMINI_XSRF_TOKEN"])
        if os.environ.get("GEMINI_AUTH_USER"):
            cfg = cfg.replace(auth_user=os.environ["GEMINI_AUTH_USER"])
        if os.environ.get("GEMINI_BL"):
            cfg = cfg.replace(gemini_bl=os.environ["GEMINI_BL"])
        if os.environ.get("GEMINI_PROXY"):
            cfg = cfg.replace(proxy=os.environ["GEMINI_PROXY"])
        if os.environ.get("GEMINI_COOKIE_FILE"):
            cfg = cfg.replace(cookie_file=os.environ["GEMINI_COOKIE_FILE"])
        return cfg

    def replace(self, **changes: Any) -> BridgeConfig:
        """Return a copy with the named fields replaced. Never mutates self."""
        return replace(self, **changes)

    # ---- secrets -----------------------------------------------------------

    def has_session_cookie(self) -> bool:
        """True when a cookie file is configured and exists on disk."""
        return bool(self.cookie_file) and os.path.exists(str(self.cookie_file))

    def redacted(self) -> dict:
        """A dict safe to print: never include the xsrf token or cookie path secrets."""
        out = {
            "port": self.port,
            "host": self.host,
            "default_model": self.default_model,
            "gemini_bl": self.gemini_bl,
            "auth_user": self.auth_user,
            "temporary_chats": self.temporary_chats,
            "proxy": self.proxy or "system env",
            "retry_attempts": self.retry_attempts,
            "request_timeout_sec": self.request_timeout_sec,
            "log_requests": self.log_requests,
            "research_concurrency": self.research_concurrency,
            "robots_cache_ttl_sec": self.robots_cache_ttl_sec,
            "batch_max_concurrency": self.batch_max_concurrency,
            "request_queue_wait_sec": self.request_queue_wait_sec,
            "research_evidence": self.research_evidence,
            "discover_wiki": self.discover_wiki,
            "discover_backend": self.discover_backend,
            "smart_fetch": self.smart_fetch,
            "smart_fetch_ai": self.smart_fetch_ai,
            "smart_fetch_browser_render": self.smart_fetch_browser_render,
            # Booleans and paths only; never the token itself.
            "xsrf_token_set": bool(self.xsrf_token),
            "api_key_set": bool(self.api_key),
            "cookie": "configured" if self.cookie_file else "none (anonymous)",
            "cookie_exists": self.has_session_cookie(),
        }
        return out


def redact_proxy(proxy: str | None) -> str:
    """Show a proxy without any embedded credentials.

    Proxies are commonly configured as http://user:pass@host:port; printing
    that verbatim puts the password in terminal scrollback, docker logs, and
    tool output.
    """
    if not proxy:
        return "system env"
    try:
        parts = urllib.parse.urlsplit(proxy)
    except ValueError:
        return "<unparseable proxy url>"
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urllib.parse.urlunsplit(
            (parts.scheme, host, parts.path, parts.query, parts.fragment)
        )
    return proxy


def load_config(path: str | None) -> BridgeConfig:
    """Load a JSON config file into a BridgeConfig.

    A missing file is not an error: defaults plus env overrides apply.
    Neither is a corrupt or unreadable one: every command (doctor included)
    must come up running, so the problem is reported on stderr and defaults
    apply -- a crashed CLI cannot even tell the user WHY it crashed.
    """
    data: dict = {}
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
        except (OSError, ValueError) as exc:
            # ValueError covers json.JSONDecodeError and its subclasses.
            print(f"warning: ignoring unreadable config {path!r}: {exc}", file=sys.stderr)
            loaded = None
        if isinstance(loaded, dict):
            data = loaded
    return BridgeConfig.from_dict(data)


def find_config() -> str | None:
    """Search the usual places for a config file."""
    for candidate in (
        os.environ.get("FREEGEM_CONFIG"),
        "./config.json",
        "./gemini-auth.json",  # exported by the cookie-sync extension
        os.path.expanduser("~/.config/freegem/config.json"),
    ):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def config_search_paths() -> tuple[str, ...]:
    """The paths :func:`find_config` checks, for ``--config-search`` diagnostics."""
    return (
        os.environ.get("FREEGEM_CONFIG") or "(unset)",
        "./config.json",
        "./gemini-auth.json",
        os.path.expanduser("~/.config/freegem/config.json"),
    )
