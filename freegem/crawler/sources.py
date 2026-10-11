"""Source adapters: MediaWiki API, Documentation sites, arXiv API, and RSS/Atom feeds.

Provides structured ingestion from specific domain sources into crawler records.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

from ..ssrf import url_allowed


@dataclass
class SourceArticle:
    url: str
    title: str
    content: str
    metadata: dict[str, Any]


class MediaWikiAdapter:
    """Adapter for Wikipedia / MediaWiki REST & Action APIs."""

    def __init__(self, domain: str = "en.wikipedia.org") -> None:
        self.domain = domain

    def fetch_page(self, title: str) -> SourceArticle | None:
        """Fetch article by title from MediaWiki REST API."""
        encoded_title = urllib.parse.quote(title.replace(" ", "_"))
        url = f"https://{self.domain}/api/rest_v1/page/summary/{encoded_title}"

        allowed, _reason = url_allowed(url)
        if not allowed:
            return None

        req = urllib.request.Request(url, headers={"User-Agent": "FreeGemCrawler/2.7.0"})
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return SourceArticle(
                    url=data.get("content_urls", {}).get("desktop", {}).get("page", url),
                    title=data.get("title", title),
                    content=data.get("extract", ""),
                    metadata={"page_id": data.get("pageid"), "wikibase_item": data.get("wikibase_item")},
                )
        except Exception:
            return None


class DocumentationAdapter:
    """Adapter for documentation sites (Sphinx, MkDocs, Docusaurus)."""

    @staticmethod
    def parse_sitemap(sitemap_xml: str) -> list[str]:
        """Extract documentation page URLs from sitemap.xml."""
        urls = []
        try:
            root = ET.fromstring(sitemap_xml)
            for elem in root.iter():
                if elem.tag.endswith("loc") and elem.text:
                    urls.append(elem.text.strip())
        except Exception:
            # Fallback regex search
            urls = re.findall(r"<loc>\s*(https?://[^\s<]+)\s*</loc>", sitemap_xml)
        return urls


class ArxivAdapter:
    """Adapter for arXiv API paper metadata."""

    @staticmethod
    def search(query: str, max_results: int = 5) -> list[SourceArticle]:
        """Search arXiv API for papers."""
        encoded_q = urllib.parse.quote(query)
        url = f"http://export.arxiv.org/api/query?search_query=all:{encoded_q}&start=0&max_results={max_results}"

        allowed, _reason = url_allowed(url)
        if not allowed:
            return []

        req = urllib.request.Request(url, headers={"User-Agent": "FreeGemCrawler/2.7.0"})
        articles = []
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                xml_data = resp.read().decode("utf-8")
                root = ET.fromstring(xml_data)
                ns = {"atom": "http://www.w3.org/2005/Atom"}

                for entry in root.findall("atom:entry", ns):
                    title_elem = entry.find("atom:title", ns)
                    summary_elem = entry.find("atom:summary", ns)
                    id_elem = entry.find("atom:id", ns)

                    title = title_elem.text.strip() if title_elem is not None and title_elem.text else "Untitled"
                    summary = summary_elem.text.strip() if summary_elem is not None and summary_elem.text else ""
                    paper_id = id_elem.text.strip() if id_elem is not None and id_elem.text else ""

                    articles.append(
                        SourceArticle(
                            url=paper_id,
                            title=title,
                            content=summary,
                            metadata={"source": "arxiv"},
                        )
                    )
        except Exception:
            pass
        return articles


class FeedAdapter:
    """Adapter for RSS 2.0 / Atom feed URLs."""

    @staticmethod
    def parse_feed(feed_xml: str) -> list[SourceArticle]:
        """Parse RSS or Atom feed XML into articles."""
        articles = []
        try:
            root = ET.fromstring(feed_xml)

            # Check RSS 2.0
            channel = root.find("channel")
            if channel is not None:
                for item in channel.findall("item"):
                    title = item.findtext("title", "Untitled")
                    link = item.findtext("link", "")
                    desc = item.findtext("description", "")
                    articles.append(SourceArticle(url=link, title=title, content=desc, metadata={"type": "rss"}))
                return articles

            # Check Atom
            if root.tag.endswith("feed"):
                ns = {"atom": "http://www.w3.org/2005/Atom"}
                for entry in root.findall("atom:entry", ns):
                    title = entry.findtext("atom:title", "Untitled", namespaces=ns)
                    summary = entry.findtext("atom:summary", "", namespaces=ns)
                    link_elem = entry.find("atom:link", ns)
                    link = link_elem.attrib.get("href", "") if link_elem is not None else ""
                    articles.append(SourceArticle(url=link, title=title, content=summary, metadata={"type": "atom"}))
                return articles

        except Exception:
            pass
        return articles
