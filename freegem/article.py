"""Specialized article and news adapters (2.9.0).

A news article carries structure a generic readable-text pass throws away:
headline, byline, publish time, section, canonical URL, lead image. Downstream
consumers -- the evidence verifier that cites a publication date, the research
pipeline that wants the publisher name, the search sink that indexes by
section -- need those fields explicitly, with a record of WHERE each field
came from, because news HTML lies: OpenGraph tags can be stale, JSON-LD can
disagree with the visible headline, ``<meta name=author>`` can hold the site
brand rather than the writer.

``parse_article`` collects candidates from four independent signal families --
JSON-LD, OpenGraph, plain meta tags, and DOM heuristics (``<time datetime>``,
``rel=author``, byline classes) -- and resolves each field with a preference
order, keeping every contributing signal in ``signals`` so a caller can audit
the choice. The body is the paragraph text under the best article container
(``<article>``, ``[role=main]``, ``<main>``, or the div with the most
substantive paragraphs), filtered of nav/aside/form/script noise.

Everything here is offline HTML interpretation: no network, no Gemini.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .crawler.extract import css_select
from .crawler.hardening import HtmlDom, Node

__all__ = ["ArticleResult", "is_article", "parse_article"]

_ARTICLE_LD_TYPES = {
    "article",
    "newsarticle",
    "blogposting",
    "reportagenewsarticle",
    "techarticle",
    "scholarlyarticle",
}

_SKIP_BODY_TAGS = {
    "script",
    "style",
    "nav",
    "aside",
    "form",
    "footer",
    "header",
    "noscript",
    "svg",
    "button",
}
_MIN_PARA_CHARS = 60


@dataclass
class ArticleResult:
    """Structured fields extracted from one article/news page."""

    url: str
    title: str = ""
    byline: str = ""
    authors: list[str] = field(default_factory=list)
    published_time: str = ""
    modified_time: str = ""
    canonical_url: str = ""
    section: str = ""
    description: str = ""
    image: str = ""
    publisher: str = ""
    language: str = ""
    word_count: int = 0
    text: str = ""
    markdown: str = ""
    #: field name -> {"value": ..., "source": "json-ld" | "opengraph" | "meta" | "dom"}
    signals: dict[str, str] = field(default_factory=dict)
    #: "article" when article signals are present, "page" otherwise
    doc_type: str = "page"


def _attr(node: Node, name: str) -> str:
    return (node.attrs.get(name) or "").strip()


def _text_of(node: Node) -> str:
    parts: list[str] = []

    def walk(n: Node) -> None:
        if n.tag in _SKIP_BODY_TAGS:
            return
        parts.extend(n.text_parts)
        for child in n.children:
            walk(child)

    walk(node)
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()


def _content_of_meta(dom: HtmlDom, *, prop: str | None = None, name: str | None = None) -> str:
    for node in css_select(dom, "meta"):
        if prop is not None and _attr(node, "property") == prop:
            return _attr(node, "content")
        if name is not None and _attr(node, "name") == name:
            return _attr(node, "content")
    return ""


def _first_text(dom: HtmlDom, selector: str) -> str:
    nodes = css_select(dom, selector)
    for node in nodes:
        text = _text_of(node)
        if text:
            return text
    return ""


def _raw_text_of(node: Node) -> str:
    """Text of a node WITHOUT the body-skip filter -- for script contents."""
    parts: list[str] = list(node.text_parts)
    for child in node.children:
        parts.extend(child.text_parts)
    return re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()


def _jsonld_blocks(dom: HtmlDom) -> list[Any]:
    blocks: list[Any] = []
    for node in css_select(dom, 'script[type="application/ld+json"]'):
        raw = _raw_text_of(node)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        queue = data if isinstance(data, list) else [data]
        for item in queue:
            if isinstance(item, dict):
                blocks.append(item)
                graph = item.get("@graph")
                if isinstance(graph, list):
                    blocks.extend(g for g in graph if isinstance(g, dict))
    return blocks


def _ld_articles(dom: HtmlDom) -> list[dict[str, Any]]:
    out = []
    for block in _jsonld_blocks(dom):
        types = block.get("@type")
        type_names = (
            {types.lower()}
            if isinstance(types, str)
            else {str(t).lower() for t in types}
            if isinstance(types, list)
            else set()
        )
        if type_names & _ARTICLE_LD_TYPES:
            out.append(block)
    return out


def _ld_str(value: Any) -> str:
    """JSON-LD values arrive as str | list | {'name': str}; flatten to str."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        name = value.get("name")
        return name.strip() if isinstance(name, str) else ""
    if isinstance(value, list):
        for item in value:
            flat = _ld_str(item)
            if flat:
                return flat
    return ""


def _ld_authors(block: dict[str, Any]) -> list[str]:
    raw = block.get("author")
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    names: list[str] = []
    for item in items:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"].strip())
    return [n for n in names if n]


def _ld_image(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, str):
            return first.strip()
        if isinstance(first, dict):
            url = first.get("url")
            return url.strip() if isinstance(url, str) else ""
    if isinstance(value, dict):
        url = value.get("url")
        return url.strip() if isinstance(url, str) else ""
    return ""


def _pick(
    field: str, value: str, source: str, chosen: dict[str, str], signals: dict[str, str]
) -> None:
    """First source wins per field; later sources only fill still-empty fields."""
    if value and not chosen.get(field):
        chosen[field] = value
        signals[field] = source


def _dom_published(dom: HtmlDom) -> str:
    for node in css_select(dom, "time"):
        stamp = _attr(node, "datetime") or _attr(node, "pubdate") or _attr(node, "data-timestamp")
        if stamp:
            return stamp
    for node in css_select(dom, "[itemprop='datePublished']"):
        stamp = _attr(node, "content") or _text_of(node)
        if stamp:
            return stamp
    return ""


def _dom_byline(dom: HtmlDom) -> str:
    for selector in (
        "[rel='author']",
        "[itemprop='author']",
        ".byline",
        ".author",
        "[data-byline]",
    ):
        text = _first_text(dom, selector)
        if text:
            return text
    return ""


def _body_container(dom: HtmlDom) -> Node | None:
    for selector in ("article", "[role='main']", "main"):
        nodes = css_select(dom, selector)
        if nodes:
            return nodes[0]
    best: Node | None = None
    best_score = 0
    for node in css_select(dom, "div"):
        paras = [
            child
            for child in node.children
            if child.tag == "p" and len(_text_of(child)) >= _MIN_PARA_CHARS
        ]
        if len(paras) > best_score:
            best, best_score = node, len(paras)
    return best


def _body_text(dom: HtmlDom) -> str:
    container = _body_container(dom)
    if container is None:
        return ""
    paras: list[str] = []

    def walk(node: Node) -> None:
        if node.tag in _SKIP_BODY_TAGS:
            return
        if node.tag == "p":
            text = _text_of(node)
            if text:
                paras.append(text)
            return
        for child in node.children:
            walk(child)

    walk(container)
    # dedupe repeated boilerplate paragraphs (cookie banners etc.)
    seen: set[str] = set()
    unique: list[str] = []
    for para in paras:
        if para in seen:
            continue
        seen.add(para)
        unique.append(para)
    return "\n\n".join(unique)


def is_article(dom: HtmlDom) -> bool:
    """Heuristic: does this page carry article signals worth the adapter?"""
    if _ld_articles(dom):
        return True
    if _content_of_meta(dom, prop="og:type").lower() == "article":
        return True
    substantive = sum(1 for node in css_select(dom, "p") if len(_text_of(node)) >= _MIN_PARA_CHARS)
    return substantive >= 3


def parse_article(html: str, url: str) -> ArticleResult:
    """Extract structured article fields from one HTML page.

    Signal preference per field: JSON-LD, then OpenGraph, then plain meta,
    then DOM heuristics. ``result.signals`` records the winning source for
    every populated field.
    """
    dom = HtmlDom()
    dom.feed(html)
    chosen: dict[str, str] = {}
    signals: dict[str, str] = {}
    authors: list[str] = []

    ld = _ld_articles(dom)
    ld_block = ld[0] if ld else {}
    if ld_block:
        _pick("title", _ld_str(ld_block.get("headline")), "json-ld", chosen, signals)
        _pick("published_time", _ld_str(ld_block.get("datePublished")), "json-ld", chosen, signals)
        _pick("modified_time", _ld_str(ld_block.get("dateModified")), "json-ld", chosen, signals)
        _pick("section", _ld_str(ld_block.get("articleSection")), "json-ld", chosen, signals)
        _pick("description", _ld_str(ld_block.get("description")), "json-ld", chosen, signals)
        _pick("image", _ld_image(ld_block.get("image")), "json-ld", chosen, signals)
        _pick("publisher", _ld_str(ld_block.get("publisher")), "json-ld", chosen, signals)
        if not authors:
            authors = _ld_authors(ld_block)
            if authors:
                signals["authors"] = "json-ld"

    _pick("title", _content_of_meta(dom, prop="og:title"), "opengraph", chosen, signals)
    _pick("description", _content_of_meta(dom, prop="og:description"), "opengraph", chosen, signals)
    _pick("image", _content_of_meta(dom, prop="og:image"), "opengraph", chosen, signals)
    _pick(
        "published_time",
        _content_of_meta(dom, prop="article:published_time"),
        "opengraph",
        chosen,
        signals,
    )
    _pick(
        "modified_time",
        _content_of_meta(dom, prop="article:modified_time"),
        "opengraph",
        chosen,
        signals,
    )
    _pick("section", _content_of_meta(dom, prop="article:section"), "opengraph", chosen, signals)
    og_author = _content_of_meta(dom, prop="article:author")
    if og_author and not authors:
        authors = [og_author]
        signals["authors"] = "opengraph"

    _pick("description", _content_of_meta(dom, name="description"), "meta", chosen, signals)
    meta_author = _content_of_meta(dom, name="author")
    if meta_author and not authors:
        authors = [meta_author]
        signals["authors"] = "meta"
    _pick("publisher", _content_of_meta(dom, name="application-name"), "meta", chosen, signals)

    _pick("title", _first_text(dom, "h1"), "dom", chosen, signals)
    dom_published = _dom_published(dom)
    _pick("published_time", dom_published, "dom", chosen, signals)
    dom_byline = _dom_byline(dom)
    if dom_byline and not authors:
        authors = [dom_byline]
        signals["authors"] = "dom"

    canonical_nodes = css_select(dom, "link[rel='canonical']")
    if canonical_nodes:
        _pick("canonical_url", _attr(canonical_nodes[0], "href"), "dom", chosen, signals)
    lang_nodes = css_select(dom, "html")
    if lang_nodes:
        _pick("language", _attr(lang_nodes[0], "lang"), "dom", chosen, signals)

    text = _body_text(dom)
    title = chosen.get("title", "")
    byline = authors[0] if authors else ""
    word_count = len(text.split()) if text else 0

    md_lines: list[str] = []
    if title:
        md_lines.append(f"# {title}")
    if byline or chosen.get("published_time"):
        stamp = chosen.get("published_time", "")
        md_lines.append("")
        md_lines.append("*" + " · ".join(p for p in (byline, stamp) if p) + "*")
    if text:
        md_lines.append("")
        md_lines.append(text)
    markdown = "\n".join(md_lines)

    return ArticleResult(
        url=url,
        title=title,
        byline=byline,
        authors=authors,
        published_time=chosen.get("published_time", ""),
        modified_time=chosen.get("modified_time", ""),
        canonical_url=chosen.get("canonical_url", ""),
        section=chosen.get("section", ""),
        description=chosen.get("description", ""),
        image=chosen.get("image", ""),
        publisher=chosen.get("publisher", ""),
        language=chosen.get("language", ""),
        word_count=word_count,
        text=text,
        markdown=markdown,
        signals=signals,
        doc_type="article" if (ld_block or chosen.get("published_time")) else "page",
    )
