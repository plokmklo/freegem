"""External search/SERP integrations with an explicit service boundary (2.9.0).

This module ANSWERS QUERIES -- it returns ranked web results as
:class:`SearchHit` records. It deliberately does NOT fetch those results:
handing a URL to the fetch layer is the caller's decision (that separation is
the service boundary -- search never quietly turns into a crawl).

Two providers ship:

* ``duckduckgo``  -- the HTML endpoint, no API key, no account. Fragile by
  nature (unofficial markup), fine for interactive use and tests.
* ``brave``       -- the official Web Search API. Requires a subscription
  token; the provider refuses to run without one (ProviderNotConfigured)
  rather than silently degrading, so a missing key is a loud event, not an
  empty result page.

Providers are opt-in BY CONSTRUCTION: nothing registers or enables one until
a caller asks for it by name via :func:`get_provider`, and no key material is
read from the environment implicitly -- the Brave token comes from the
freegem config file, where the user put it on purpose.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from .config import BridgeConfig
from .ssrf import build_guarded_opener

__all__ = [
    "BraveSearchProvider",
    "DuckDuckGoHTMLProvider",
    "ProviderNotConfigured",
    "SearchHit",
    "get_provider",
    "provider_names",
]

_DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
_BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_UA = "Mozilla/5.0 (X11; Linux x86_64) freegem-serp/2.9"


class ProviderNotConfigured(Exception):
    """The provider needs credentials the config does not carry."""


@dataclass
class SearchHit:
    """One ranked result from a SERP provider."""

    title: str
    url: str
    snippet: str
    position: int
    source: str  # provider name


class SearchProvider:
    """Common face: ``search(query, max_results) -> list[SearchHit]``."""

    name = "base"

    def search(self, query: str, max_results: int = 10) -> list[SearchHit]:
        raise NotImplementedError


def _opener(cfg: BridgeConfig | None) -> urllib.request.OpenerDirector:
    if cfg is None:
        return urllib.request.build_opener()
    return build_guarded_opener(cfg.proxy, None)


class DuckDuckGoHTMLProvider(SearchProvider):
    """Unofficial HTML endpoint. No key; markup is scraped, not contracted."""

    name = "duckduckgo"

    def __init__(self, cfg: BridgeConfig | None = None) -> None:
        self.cfg = cfg

    def search(self, query: str, max_results: int = 10) -> list[SearchHit]:
        if not query.strip():
            return []
        body = urlencode({"q": query, "kl": "wt-wt"}).encode()
        request = urllib.request.Request(
            _DDG_ENDPOINT,
            data=body,
            headers={"User-Agent": _UA, "Content-Type": "application/x-www-form-urlencoded"},
        )
        with _opener(self.cfg).open(request, timeout=20) as response:
            html = response.read(2 << 20).decode("utf-8", errors="replace")
        return _parse_ddg(html, max_results)


def _parse_ddg(html: str, max_results: int) -> list[SearchHit]:
    """Parse the html.duckduckgo.com results page.

    Results: ``a.result__a`` anchors whose href is ``/l/?uddg=<encoded-url>``
    (or occasionally a plain URL), snippets in ``a.result__snippet``. Regex
    over the markup is intentional: the endpoint is unofficial and changes
    without notice, and a full DOM pass buys nothing for two stable patterns.
    """
    hits: list[SearchHit] = []
    anchors = re.findall(
        r"<a[^>]+class=\"result__a\"[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", html, re.DOTALL
    )
    snippets = re.findall(r"<a[^>]+class=\"result__snippet\"[^>]*>(.*?)</a>", html, re.DOTALL)
    for pos, (href, title) in enumerate(anchors[: max_results or 10], 1):
        url = _ddg_real_url(href)
        if not url:
            continue
        snippet = re.sub(r"<[^>]+>", "", snippets[pos - 1]) if pos - 1 < len(snippets) else ""
        hits.append(
            SearchHit(
                title=re.sub(r"<[^>]+>", "", title).strip(),
                url=unquote(url),
                snippet=unquote(re.sub(r"\s+", " ", snippet)).strip(),
                position=pos,
                source="duckduckgo",
            )
        )
    return hits


def _ddg_real_url(href: str) -> str:
    if href.startswith("//duckduckgo.com/l/") or href.startswith("/l/") or "uddg=" in href:
        parsed = urlparse(href if href.startswith("http") else "https://duckduckgo.com" + href)
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        return target.strip()
    if href.startswith("http"):
        return href.strip()
    return ""


class BraveSearchProvider(SearchProvider):
    """Official Brave Web Search API. Requires a subscription token."""

    name = "brave"

    def __init__(self, api_key: str = "", cfg: BridgeConfig | None = None) -> None:
        self.api_key = api_key.strip()
        self.cfg = cfg

    def search(self, query: str, max_results: int = 10) -> list[SearchHit]:
        if not self.api_key:
            raise ProviderNotConfigured(
                'brave search needs an API token: put it in config.json as "brave_api_key" '
                "(https://api-dashboard.search.brave.com) -- the provider refuses to run half-configured"
            )
        if not query.strip():
            return []
        url = f"{_BRAVE_ENDPOINT}?{urlencode({'q': query, 'count': min(max_results or 10, 20)})}"
        request = urllib.request.Request(
            url, headers={"User-Agent": _UA, "X-Subscription-Token": self.api_key}
        )
        with _opener(self.cfg).open(request, timeout=20) as response:
            data = json.loads(response.read(2 << 20).decode("utf-8"))
        return _parse_brave(data, max_results)


def _parse_brave(data: dict[str, Any], max_results: int) -> list[SearchHit]:
    web = data.get("web") or {}
    results = web.get("results") or []
    hits: list[SearchHit] = []
    for pos, item in enumerate(results[: max_results or 10], 1):
        url = str(item.get("url", "")).strip()
        if not url:
            continue
        hits.append(
            SearchHit(
                title=str(item.get("title", "")).strip(),
                url=url,
                snippet=re.sub(r"\s+", " ", str(item.get("description", ""))).strip(),
                position=pos,
                source="brave",
            )
        )
    return hits


def provider_names() -> list[str]:
    return ["duckduckgo", "brave"]


def get_provider(name: str, cfg: BridgeConfig | None = None) -> SearchProvider:
    """Build one provider BY NAME -- the explicit opt-in point."""
    if name == "duckduckgo":
        return DuckDuckGoHTMLProvider(cfg=cfg)
    if name == "brave":
        key = getattr(cfg, "brave_api_key", "") or ""
        return BraveSearchProvider(api_key=key, cfg=cfg)
    raise ValueError(f"unknown search provider {name!r}; known: {', '.join(provider_names())}")
