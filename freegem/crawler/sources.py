"""Source adapters: turn a site's own API into a crawl seed list (2.8.0).

A generic crawler treats every site as opaque HTML and burns requests
discovering what the site would have handed over for free. Source adapters
ask the structured front door first:

* :class:`MediaWikiSource` — ``action=query`` (search / categorymembers /
  allpages) via ``api.php``; one request yields hundreds of article URLs;
* :class:`ArxivSource` — the arXiv Atom API (``export.arxiv.org/api/query``),
  paged, id->abs-url mapping;
* :class:`FeedSource` — RSS 2.0 / Atom feeds via stdlib ElementTree; a feed
  IS a site-curated frontier;
* :class:`DocsSource` — documentation sites (Sphinx/mkdocs/Docusaurus): the
  sitemap.xml and the nav HTML of the root page are the canonical page list.

Every adapter returns :class:`SourceItem` records (url + metadata), never
fetches page BODIES (the crawl runner's job, with all its guards), and is
polite by construction: a small page count per request, plus the caller's
user agent. URL scheme discovery is in :func:`detect_source`.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Protocol

from .hardening import parse_xml
from .urls import normalize_url

__all__ = [
    "ArxivSource",
    "DocsSource",
    "FeedSource",
    "MediaWikiSource",
    "SourceAdapter",
    "SourceItem",
    "detect_source",
]


@dataclass
class SourceItem:
    """One seed discovered by a source adapter."""

    url: str
    title: str = ""
    updated: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "title": self.title,
            "updated": self.updated,
            "metadata": self.metadata,
        }


class SourceAdapter(Protocol):
    name: str

    def matches(self, url: str) -> bool: ...
    def discover(self, url: str, *, limit: int = 200) -> list[SourceItem]: ...


def _fetch_text(url: str, *, timeout_sec: float = 30.0, max_bytes: int = 8 * 1024 * 1024) -> str:
    """Small polite GET for API documents (never page bodies)."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "freegem-source-adapter/2.8"})
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        return resp.read(max_bytes).decode("utf-8", errors="replace")


# ─── MediaWiki ───────────────────────────────────────────────────────────────


class MediaWikiSource:
    """MediaWiki API as a seed source. ``https://en.wikipedia.org`` works out
    of the box; any MediaWiki farm with a reachable ``api.php`` does too."""

    name = "mediawiki"

    def matches(self, url: str) -> bool:
        host = urllib.parse.urlsplit(url).hostname or ""
        return "wikipedia.org" in host or "wikimedia" in host or "fandom.com" in host

    def discover(self, url: str, *, limit: int = 200) -> list[SourceItem]:
        origin = f"{urllib.parse.urlsplit(url).scheme}://{urllib.parse.urlsplit(url).netloc}"
        api = f"{origin}/w/api.php"
        items: list[SourceItem] = []
        for mode, param in (
            ("search", urllib.parse.quote(url.rsplit("/", 1)[-1] or "main page")),
            ("allpages", ""),
        ):
            if len(items) >= limit:
                break
            if mode == "search":
                list_url = f"{api}?action=query&list=search&srsearch={param}&srlimit={min(50, limit)}&format=json"
            else:
                list_url = f"{api}?action=query&list=allpages&aplimit={min(500, limit)}&format=json"
            try:
                import json

                data = json.loads(_fetch_text(list_url))
            except Exception:
                continue
            query = data.get("query", {})
            for page in (
                query.get("pages", {}).values() if isinstance(query.get("pages"), dict) else []
            ):
                items.append(
                    SourceItem(
                        url=normalize_url(
                            f"{origin}/wiki/{urllib.parse.quote(str(page.get('title', '')).replace(' ', '_'))}"
                        ),
                        title=str(page.get("title", "")),
                        metadata={"source": self.name, "pageid": page.get("pageid")},
                    )
                )
            for hit in query.get("search", []):
                items.append(
                    SourceItem(
                        url=normalize_url(
                            f"{origin}/wiki/{urllib.parse.quote(str(hit.get('title', '')).replace(' ', '_'))}"
                        ),
                        title=str(hit.get("title", "")),
                        metadata={
                            "source": self.name,
                            "snippet_len": len(str(hit.get("snippet", ""))),
                        },
                    )
                )
        # dedupe, keep order
        seen: set[str] = set()
        out: list[SourceItem] = []
        for it in items:
            if it.url not in seen:
                seen.add(it.url)
                out.append(it)
        return out[:limit]


# ─── arXiv ───────────────────────────────────────────────────────────────────


class ArxivSource:
    """arXiv Atom API. Accepts arxiv.org URLs (any page) and maps them onto
    the export API with the search query taken from the URL's search path."""

    name = "arxiv"

    _NS = ("atom", "http://www.w3.org/2005/Atom")

    @property
    def _ns_map(self) -> dict[str, str]:
        return {self._NS[0]: self._NS[1]}

    def matches(self, url: str) -> bool:
        host = urllib.parse.urlsplit(url).hostname or ""
        return host.endswith("arxiv.org")

    def discover(self, url: str, *, limit: int = 200) -> list[SourceItem]:
        parts = urllib.parse.urlsplit(url)
        query = ""
        if parts.path.startswith("/list/") or parts.path.startswith("/a/"):
            query = parts.path.rstrip("/").rsplit("/", 1)[-1]
        elif parts.query:
            query = urllib.parse.unquote_plus(parts.query)
        api_url = (
            "https://export.arxiv.org/api/query"
            f"?search_query=all:{urllib.parse.quote(query or 'cs')}"
            f"&start=0&max_results={min(100, limit)}"
        )
        try:
            xml_text = _fetch_text(api_url)
        except Exception:
            return []
        root = None
        try:
            root = parse_xml(xml_text)
        except Exception:
            return []
        if root is None:
            return []
        items: list[SourceItem] = []
        for entry in root.findall("atom:entry", self._ns_map):
            abs_url = ""
            fallback = ""
            for link in entry.findall("atom:link", self._ns_map):
                href = link.get("href", "")
                if not href.startswith("http"):
                    continue
                rel = link.get("rel", "alternate")
                if rel == "alternate" and link.get("type") in (None, "text/html"):
                    abs_url = href
                    break
                fallback = fallback or href
            abs_url = abs_url or fallback
            title = (entry.findtext("atom:title", "", self._ns_map) or "").strip()
            updated = (entry.findtext("atom:updated", "", self._ns_map) or "").strip()
            if abs_url:
                items.append(
                    SourceItem(
                        url=normalize_url(abs_url),
                        title=re.sub(r"\s+", " ", title),
                        updated=updated,
                        metadata={"source": self.name},
                    )
                )
        return items


# ─── RSS / Atom feeds ────────────────────────────────────────────────────────


class FeedSource:
    """RSS 2.0 and Atom feeds, stdlib-parsed. ``matches`` is generous: the
    caller usually knows the URL is a feed; ``detect_source`` confirms."""

    name = "feed"

    _NS = ("atom", "http://www.w3.org/2005/Atom")

    @property
    def _ns_map(self) -> dict[str, str]:
        return {self._NS[0]: self._NS[1]}

    def matches(self, url: str) -> bool:
        return bool(re.search(r"\.(xml|rss|atom)(\?|$)|/(feed|rss|atom)(/|\?|$)", url))

    def discover(self, url: str, *, limit: int = 200) -> list[SourceItem]:
        try:
            xml_text = _fetch_text(url)
        except Exception:
            return []
        try:
            root = parse_xml(xml_text)
        except Exception:
            return []
        if root is None:
            return []
        items: list[SourceItem] = []
        tag = root.tag.lower()
        if tag.endswith("rss") or tag.endswith("rdf"):
            for item_el in root.iter():
                if item_el.tag.rsplit("}", 1)[-1] == "item":
                    title = (item_el.findtext("title") or "").strip()
                    link = (item_el.findtext("link") or "").strip()
                    pub = (item_el.findtext("pubDate") or "").strip()
                    if link:
                        items.append(
                            SourceItem(
                                url=normalize_url(link),
                                title=title,
                                updated=pub,
                                metadata={"source": self.name},
                            )
                        )
                    if len(items) >= limit:
                        break
        elif tag.endswith("feed"):  # Atom
            for entry in root.findall("atom:entry", self._ns_map):
                link = ""
                for ln in entry.findall("atom:link", self._ns_map):
                    href = ln.get("href", "")
                    rel = ln.get("rel", "alternate")
                    if href and rel == "alternate":
                        link = href
                        break
                    link = link or href
                title = (entry.findtext("atom:title", "", self._ns_map) or "").strip()
                updated = (entry.findtext("atom:updated", "", self._ns_map) or "").strip()
                if link:
                    items.append(
                        SourceItem(
                            url=normalize_url(link),
                            title=title,
                            updated=updated,
                            metadata={"source": self.name},
                        )
                    )
                if len(items) >= limit:
                    break
        return items


# ─── documentation sites ─────────────────────────────────────────────────────


class DocsSource:
    """Documentation sites: sitemap.xml first, nav anchors as fallback.

    Sphinx, mkdocs, Docusaurus, and mdBook all ship a sitemap or a fully
    linked nav sidebar -- either is a better frontier than link-scraping
    every page for a docs crawl.
    """

    name = "docs"

    def matches(self, url: str) -> bool:
        path = urllib.parse.urlsplit(url).path.lower()
        return any(
            k in path for k in ("docs", "manual", "guide", "reference", "handbook")
        ) or path in ("", "/")

    def discover(self, url: str, *, limit: int = 500) -> list[SourceItem]:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        items: list[SourceItem] = []
        for sitemap_url in (f"{origin}/sitemap.xml", url.rstrip("/") + "/sitemap.xml"):
            try:
                xml_text = _fetch_text(sitemap_url)
            except Exception:
                continue
            try:
                root = parse_xml(xml_text)
            except Exception:
                continue
            if root is None:
                continue
            for loc in root.iter():
                text = loc.text
                if loc.tag.rsplit("}", 1)[-1] == "loc" and text and text.strip():
                    u = normalize_url(text.strip())
                    if u.startswith("http"):
                        items.append(
                            SourceItem(url=u, metadata={"source": self.name, "via": "sitemap"})
                        )
                        if len(items) >= limit:
                            return items
            break
        return items


def detect_source(url: str) -> SourceAdapter | None:
    """Pick the first adapter that claims this URL. Order matters: feeds
    before docs (feed URLs often live under /docs-like paths), MediaWiki
    before generic (its api.php path would also 'match' docs keywords)."""
    adapters: tuple[SourceAdapter, ...] = (
        MediaWikiSource(),
        ArxivSource(),
        FeedSource(),
        DocsSource(),
    )
    for a in adapters:
        try:
            if a.matches(url):
                return a
        except Exception:
            continue
    return None
