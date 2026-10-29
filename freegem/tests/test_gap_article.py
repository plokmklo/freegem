"""Edge-case tests for the article adapter (freegem.article).

The preference-order happy paths live in test_article.py; these cover
JSON-LD edge shapes (@graph, list @type, author forms), DOM fallback
attributes (pubdate / data-timestamp / itemprop), body-container
selection, paragraph dedupe, and the doc_type rule.
"""

from __future__ import annotations

import unittest

from freegem.article import is_article, parse_article
from freegem.crawler.hardening import HtmlDom


def _dom(html: str) -> HtmlDom:
    dom = HtmlDom()
    dom.feed(html)
    return dom


class JsonLdShapeTests(unittest.TestCase):
    def test_graph_expansion(self) -> None:
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@context": "https://schema.org", "@graph": [
          {"@type": "WebSite", "name": "site"},
          {"@type": "NewsArticle", "headline": "Graph Headline",
           "datePublished": "2026-01-01"}
        ]}
        </script></head><body><article><p>Body paragraph long enough to be
        treated as substantive content for the adapter extraction pass.</p></article></body></html>
        """
        res = parse_article(html, "https://x.example/g")
        self.assertEqual(res.title, "Graph Headline")
        self.assertEqual(res.signals["title"], "json-ld")
        self.assertEqual(res.published_time, "2026-01-01")
        self.assertEqual(res.doc_type, "article")

    def test_list_payload_with_list_type(self) -> None:
        html = """
        <html><head>
        <script type="application/ld+json">
        [{"@type": ["NewsArticle", "Thing"], "headline": "Listy"}]
        </script></head><body></body></html>
        """
        res = parse_article(html, "https://x.example/l")
        self.assertEqual(res.title, "Listy")
        self.assertEqual(res.signals["title"], "json-ld")

    def test_non_article_ld_type_ignored(self) -> None:
        html = """
        <html><head>
        <script type="application/ld+json">{"@type": "Organization", "name": "Org"}</script>
        </head><body><article><h1>DOM Headline</h1></article></body></html>
        """
        res = parse_article(html, "https://x.example/o")
        self.assertEqual(res.title, "DOM Headline")
        self.assertEqual(res.signals["title"], "dom")

    def test_author_as_plain_string(self) -> None:
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@type": "NewsArticle", "headline": "H", "author": "Solo Writer"}
        </script></head><body></body></html>
        """
        res = parse_article(html, "https://x.example/s")
        self.assertEqual(res.authors, ["Solo Writer"])
        self.assertEqual(res.signals["authors"], "json-ld")

    def test_author_dict_without_name_is_dropped(self) -> None:
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@type": "NewsArticle", "headline": "H", "author": {"@type": "Person"}}
        </script>
        <meta name="author" content="Meta Author">
        </head><body></body></html>
        """
        res = parse_article(html, "https://x.example/n")
        self.assertEqual(res.authors, ["Meta Author"])  # ld contributed nothing
        self.assertEqual(res.signals["authors"], "meta")

    def test_image_dict_without_url_falls_through(self) -> None:
        html = """
        <html><head>
        <script type="application/ld+json">
        {"@type": "NewsArticle", "headline": "H", "image": {"@type": "ImageObject"}}
        </script>
        <meta property="og:image" content="https://cdn.example.com/og.jpg">
        </head><body></body></html>
        """
        res = parse_article(html, "https://x.example/i")
        self.assertEqual(res.image, "https://cdn.example.com/og.jpg")
        self.assertEqual(res.signals["image"], "opengraph")


class DomFallbackTests(unittest.TestCase):
    def test_time_pubdate_attribute(self) -> None:
        html = (
            "<html><body><article><h1>T</h1>"
            '<time pubdate="2026-02-03">Feb 3</time></article></body></html>'
        )
        res = parse_article(html, "https://x.example/t1")
        self.assertEqual(res.published_time, "2026-02-03")
        self.assertEqual(res.signals["published_time"], "dom")

    def test_time_data_timestamp_attribute(self) -> None:
        html = (
            "<html><body><article><h1>T</h1>"
            '<time data-timestamp="2026-03-04">Mar 4</time></article></body></html>'
        )
        res = parse_article(html, "https://x.example/t2")
        self.assertEqual(res.published_time, "2026-03-04")

    def test_itemprop_datepublished_content(self) -> None:
        html = (
            "<html><body><article><h1>T</h1>"
            '<span itemprop="datePublished" content="2026-04-05">Apr 5</span>'
            "</article></body></html>"
        )
        res = parse_article(html, "https://x.example/t3")
        self.assertEqual(res.published_time, "2026-04-05")

    def test_itemprop_author_when_no_rel_author(self) -> None:
        html = (
            "<html><body><article><h1>T</h1>"
            '<span itemprop="author">Item Author</span></article></body></html>'
        )
        res = parse_article(html, "https://x.example/t4")
        self.assertEqual(res.authors, ["Item Author"])
        self.assertEqual(res.signals["authors"], "dom")


class BodyContainerTests(unittest.TestCase):
    def test_role_main_container(self) -> None:
        html = """
        <html><body>
        <div role="main">
          <p>Paragraph inside role=main that is clearly longer than sixty characters total.</p>
          <p>A second substantive paragraph inside role=main, also long enough here.</p>
        </div>
        </body></html>
        """
        res = parse_article(html, "https://x.example/m")
        self.assertIn("role=main", res.text)
        self.assertEqual(res.word_count, len(res.text.split()))

    def test_div_with_most_substantive_paragraphs_wins(self) -> None:
        html = """
        <html><body>
        <div><p>tiny</p></div>
        <div>
          <p>Substantive paragraph number one with well over sixty characters of text.</p>
          <p>Substantive paragraph number two with well over sixty characters of text.</p>
        </div>
        </body></html>
        """
        res = parse_article(html, "https://x.example/d")
        self.assertIn("number one", res.text)
        self.assertIn("number two", res.text)

    def test_repeated_paragraphs_deduped(self) -> None:
        banner = "Cookie banner text repeated twice for the dedupe check scenario here."
        html = f"""
        <html><body><article>
        <p>{banner}</p>
        <p>{banner}</p>
        <p>Distinct second paragraph also longer than the sixty character floor.</p>
        </article></body></html>
        """
        res = parse_article(html, "https://x.example/u")
        self.assertEqual(res.text.count("Cookie banner"), 1)
        self.assertIn("Distinct second", res.text)

    def test_script_inside_article_excluded(self) -> None:
        html = """
        <html><body><article>
        <h1>T</h1>
        <script>var x = "this script text is quite long and should never leak into the body";</script>
        <p>Visible paragraph one that is definitely longer than sixty characters in total.</p>
        </article></body></html>
        """
        res = parse_article(html, "https://x.example/sc")
        self.assertNotIn("script text", res.text)
        self.assertIn("Visible paragraph", res.text)


class DocTypeAndMarkdownTests(unittest.TestCase):
    def test_body_without_any_date_is_page(self) -> None:
        # doc_type requires article signals: JSON-LD block or a publish time.
        html = """
        <html><body><article>
          <p>Substantive paragraph number one with well over sixty characters of text.</p>
          <p>Substantive paragraph number two with well over sixty characters of text.</p>
          <p>Substantive paragraph number three with well over sixty characters of text.</p>
        </article></body></html>
        """
        res = parse_article(html, "https://x.example/p")
        self.assertEqual(res.doc_type, "page")
        self.assertNotEqual(res.text, "")  # body still extracted

    def test_og_type_article_detects_page(self) -> None:
        html = """
        <html><head><meta property="og:type" content="article"></head>
        <body><p>tiny</p></body></html>
        """
        self.assertTrue(is_article(_dom(html)))

    def test_is_article_needs_three_substantive_paragraphs(self) -> None:
        two = (
            "<html><body>"
            "<p>Substantive paragraph number one with well over sixty characters of text.</p>"
            "<p>Substantive paragraph number two with well over sixty characters of text.</p>"
            "</body></html>"
        )
        three = two.replace(
            "</body>",
            "<p>Substantive paragraph number three with well over sixty characters of text.</p>"
            "</body>",
        )
        self.assertFalse(is_article(_dom(two)))
        self.assertTrue(is_article(_dom(three)))

    def test_markdown_byline_without_timestamp(self) -> None:
        html = """
        <html><head><meta name="author" content="Byline Only"></head>
        <body><article><h1>Headline Here</h1>
        <p>Body paragraph long enough for the adapter to include in extraction output.</p>
        </article></body></html>
        """
        res = parse_article(html, "https://x.example/b")
        lines = res.markdown.splitlines()
        self.assertEqual(lines[0], "# Headline Here")
        self.assertEqual(lines[2], "*Byline Only*")
        self.assertEqual(res.byline, "Byline Only")


if __name__ == "__main__":
    unittest.main()
