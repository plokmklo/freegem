"""Tests for source adapters (crawler/sources.py) — parsing is exercised
against real XML fixtures; the network layer is stubbed."""

from __future__ import annotations

from typing import Any

import pytest

from freegem.crawler.sources import (
    ArxivSource,
    DocsSource,
    FeedSource,
    MediaWikiSource,
    SourceItem,
    detect_source,
)

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>ArXiv Query</title>
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <title>  A study of
        crawling politeness  </title>
    <updated>2024-01-01T00:00:00Z</updated>
    <link href="http://arxiv.org/abs/2401.00001v1" rel="alternate" type="text/html"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00002v1</id>
    <title>Second paper</title>
    <updated>2024-01-02T00:00:00Z</updated>
    <link rel="related" href="http://arxiv.org/pdf/2401.00002v1"/>
    <link rel="alternate" type="text/html" href="http://arxiv.org/abs/2401.00002v1"/>
  </entry>
</feed>
"""

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <title>Blog</title>
  <item><title>Post one</title><link>https://blog.example.org/2024/one</link><pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate></item>
  <item><title>Post two</title><link>https://blog.example.org/2024/two</link></item>
</channel></rss>
"""

ATOM_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Feed</title>
  <entry><title>Entry one</title><link rel="alternate" type="text/html" href="https://f.example.org/e1"/><updated>2024-01-01T00:00:00Z</updated></entry>
  <entry><title>Entry two</title><link href="https://f.example.org/e2"/><updated>2024-01-02T00:00:00Z</updated></entry>
</feed>
"""

SITEMAP = """<?xml version="1.0"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://docs.example.org/en/stable/</loc></url>
  <url><loc>https://docs.example.org/en/stable/api.html</loc></url>
  <url><loc>https://docs.example.org/en/stable/guide/start.html</loc></url>
</urlset>
"""

MW_SEARCH = """{"query": {"search": [
  {"title": "Politeness", "snippet": "web robots ..."},
  {"title": "Crawler", "snippet": "web crawler ..."}
]}}"""


@pytest.fixture()
def stub_net(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """_fetch_text serves from a dict keyed by URL prefix."""
    pages: dict[str, str] = {}

    def fake_fetch(url: str, **kw: Any) -> str:
        for prefix, body in pages.items():
            if url.startswith(prefix):
                return body
        raise OSError(f"no stub for {url}")

    monkeypatch.setattr("freegem.crawler.sources._fetch_text", fake_fetch)
    return pages


# ─── matching ────────────────────────────────────────────────────────────────


def test_detect_source_by_host() -> None:
    assert isinstance(detect_source("https://en.wikipedia.org/wiki/Robot"), MediaWikiSource)
    assert isinstance(detect_source("https://arxiv.org/list/cs/recent"), ArxivSource)


def test_detect_source_feed_before_docs() -> None:
    # a feed under a docs-ish path: FeedSource must win
    src = detect_source("https://example.org/docs/feed.xml")
    assert isinstance(src, FeedSource)


def test_detect_source_generic_path_is_docs() -> None:
    assert isinstance(detect_source("https://example.org/docs/"), DocsSource)
    assert detect_source("https://example.org/random/page") is None


# ─── MediaWiki ───────────────────────────────────────────────────────────────


def test_mediawiki_search_seeds_wiki_urls(stub_net: dict[str, str]) -> None:
    stub_net["https://en.wikipedia.org/w/api.php"] = MW_SEARCH
    items = MediaWikiSource().discover("https://en.wikipedia.org/wiki/Politeness", limit=10)
    assert len(items) == 2
    assert items[0].url == "https://en.wikipedia.org/wiki/Politeness"
    assert items[0].title == "Politeness"
    assert items[0].metadata["source"] == "mediawiki"


def test_mediawiki_title_quoting(stub_net: dict[str, str]) -> None:
    stub_net["https://en.wikipedia.org/w/api.php"] = (
        '{"query": {"search": [{"title": "Web crawler: a b", "snippet": ""}]}}'
    )
    items = MediaWikiSource().discover("https://en.wikipedia.org/wiki/X", limit=5)
    assert items[0].url == "https://en.wikipedia.org/wiki/Web_crawler%3A_a_b"


def test_mediawiki_unreachable_api_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(url: str, **kw: Any) -> str:
        raise OSError("down")

    monkeypatch.setattr("freegem.crawler.sources._fetch_text", fail)
    assert MediaWikiSource().discover("https://en.wikipedia.org/wiki/X") == []


# ─── arXiv ───────────────────────────────────────────────────────────────────


def test_arxiv_parses_atom_entries(stub_net: dict[str, str]) -> None:
    stub_net["https://export.arxiv.org/api/query"] = ARXIV_ATOM
    items = ArxivSource().discover("https://arxiv.org/list/cs/recent", limit=10)
    assert len(items) == 2
    assert items[0].url == "http://arxiv.org/abs/2401.00001v1"
    assert items[0].title == "A study of crawling politeness"  # whitespace collapsed
    assert items[0].updated == "2024-01-01T00:00:00Z"
    # second entry: the alternate (abs) link wins over the pdf link
    assert items[1].url == "http://arxiv.org/abs/2401.00002v1"


def test_arxiv_bad_xml_returns_empty(stub_net: dict[str, str]) -> None:
    stub_net["https://export.arxiv.org/api/query"] = "<not-xml"
    assert ArxivSource().discover("https://arxiv.org/list/cs/recent") == []


# ─── feeds ───────────────────────────────────────────────────────────────────


def test_feed_rss_items(stub_net: dict[str, str]) -> None:
    stub_net["https://blog.example.org/feed.xml"] = RSS
    src = FeedSource()
    assert src.matches("https://blog.example.org/feed.xml")
    items = src.discover("https://blog.example.org/feed.xml")
    assert [i.url for i in items] == [
        "https://blog.example.org/2024/one",
        "https://blog.example.org/2024/two",
    ]
    assert items[0].title == "Post one"
    assert items[0].updated.startswith("Mon")


def test_feed_atom_items(stub_net: dict[str, str]) -> None:
    stub_net["https://f.example.org/atom.xml"] = ATOM_FEED
    items = FeedSource().discover("https://f.example.org/atom.xml")
    assert [i.url for i in items] == ["https://f.example.org/e1", "https://f.example.org/e2"]


def test_feed_limit_is_honored(stub_net: dict[str, str]) -> None:
    many = "<rss version='2.0'><channel>" + "".join(
        f"<item><title>t{i}</title><link>https://b.org/{i}</link></item>" for i in range(50)
    ) + "</channel></rss>"
    stub_net["https://b.org/feed"] = many
    assert len(FeedSource().discover("https://b.org/feed", limit=7)) == 7


# ─── docs ────────────────────────────────────────────────────────────────────


def test_docs_sitemap_seeds(stub_net: dict[str, str]) -> None:
    stub_net["https://docs.example.org/sitemap.xml"] = SITEMAP
    items = DocsSource().discover("https://docs.example.org/en/stable/", limit=10)
    assert [i.url for i in items] == [
        "https://docs.example.org/en/stable/",
        "https://docs.example.org/en/stable/api.html",
        "https://docs.example.org/en/stable/guide/start.html",
    ]
    assert all(i.metadata["via"] == "sitemap" for i in items)


def test_docs_no_sitemap_returns_empty(stub_net: dict[str, str]) -> None:
    stub_net["https://other.example.org/sitemap.xml"] = "<broken"
    assert DocsSource().discover("https://other.example.org/manual/") == []


def test_source_item_dict_roundtrip() -> None:
    it = SourceItem(url="https://x", title="T", updated="u", metadata={"a": 1})
    d = it.to_dict()
    assert d == {"url": "https://x", "title": "T", "updated": "u", "metadata": {"a": 1}}
