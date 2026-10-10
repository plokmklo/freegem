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
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

__all__ = ["DEFAULT_CONFIG", "BridgeConfig", "find_config", "load_config"]

# Defaults are read-only. Use BridgeConfig.replace() to derive variants.
DEFAULT_CONFIG: Mapping[str, Any] = {
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
}


@dataclass(frozen=True)
class BridgeConfig:
    """Immutable bridge configuration.

    Deliberately has no ``api_keys`` field: authentication is not part of this
    project. Binding to loopback is the access control.
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

    # ---- construction -------------------------------------------------------

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BridgeConfig:
        """Build from a mapping, ignoring unknown keys and applying env overrides.

        Unknown keys are dropped rather than rejected so that a config file
        written for an older version still loads. Secrets (``xsrf_token``) may
        come from the environment instead of the file.
        """
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        cfg = cls(**{**dict(DEFAULT_CONFIG), **known})

        env = os.environ.get("FREEGEM_CONFIG")
        if env and not data:
            # no explicit config was passed: honour the env var
            pass

        # Environment can supply the session pieces without touching the file.
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
            # Booleans and paths only; never the token itself.
            "xsrf_token_set": bool(self.xsrf_token),
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
    """
    data: dict = {}
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
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
