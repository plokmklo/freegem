"""Tests for the article/news adapter (freegem.article)."""

from __future__ import annotations

import unittest

from freegem.article import ArticleResult, is_article, parse_article

NEWS_HTML = (
    """
<!DOCTYPE html>
<html lang="en">
<head>
  <title>Fallback Headline - Daily Signal</title>
  <meta name="description" content="Meta description of the story.">
  <meta name="author" content="Meta Author">
  <link rel="canonical" href="https://news.example.com/stories/one">
  <meta property="og:title" content="OG Headline">
  <meta property="og:description" content="OG description.">
  <meta property="og:image" content="https://cdn.example.com/og.jpg">
  <meta property="article:published_time" content="2026-10-11T08:00:00Z">
  <meta property="article:modified_time" content="2026-10-11T09:30:00Z">
  <meta property="article:section" content="Technology">
  <script type="application/ld+json">
  {"@type": "NewsArticle", "headline": "JSON-LD Headline",
   "author": [{"@type": "Person", "name": "Ada Lovelace"},
              {"@type": "Person", "name": "Grace Hopper"}],
   "datePublished": "2026-10-11T07:55:00Z",
   "dateModified": "2026-10-11T10:00:00Z",
   "articleSection": "Tech",
   "description": "JSON-LD description.",
   "image": ["https://cdn.example.com/ld.jpg"],
   "publisher": {"@type": "Organization", "name": "Daily Signal"}}
  </script>
</head>
<body>
  <nav>Home Sections Subscribe</nav>
  <article>
    <h1>Visible H1 Headline</h1>
    <p class="byline">By Ada Lovelace and Grace Hopper</p>
    <time datetime="2026-10-11T07:55:00Z">Oct 11, 2026</time>
    <p>__PARA1__</p>
    <p>__PARA2__</p>
    <aside>Related stories paid placement</aside>
    <p>__PARA3__</p>
  </article>
  <footer>Copyright Daily Signal</footer>
</body>
</html>
""".replace("__PARA1__", "The market for parsing tools grew again this quarter, according to people familiar with the matter.")
    .replace("__PARA2__", "Analysts said the change reflects a broader shift toward structured extraction over plain text dumps.")
    .replace("__PARA3__", "Several publishers have already adopted the format, citing clearer provenance for every field.")
)


class ParseArticleTests(unittest.TestCase):
    def test_full_news_page(self) -> None:
        res = parse_article(NEWS_HTML, "https://news.example.com/stories/one")
        self.assertEqual(res.doc_type, "article")
        self.assertEqual(res.title, "JSON-LD Headline")  # json-ld wins over og/h1
        self.assertEqual(res.signals["title"], "json-ld")
        self.assertEqual(res.authors, ["Ada Lovelace", "Grace Hopper"])
        self.assertEqual(res.byline, "Ada Lovelace")
        self.assertEqual(res.signals["authors"], "json-ld")
        self.assertEqual(res.published_time, "2026-10-11T07:55:00Z")
        self.assertEqual(res.modified_time, "2026-10-11T10:00:00Z")
        self.assertEqual(res.section, "Tech")
        self.assertEqual(res.publisher, "Daily Signal")
        self.assertEqual(res.canonical_url, "https://news.example.com/stories/one")
        self.assertEqual(res.language, "en")
        self.assertEqual(res.image, "https://cdn.example.com/ld.jpg")
        self.assertIn("parsing tools grew again", res.text)
        self.assertNotIn("Related stories", res.text)  # aside excluded
        self.assertNotIn("Subscribe", res.text)  # nav excluded
        self.assertEqual(res.markdown.splitlines()[0], "# JSON-LD Headline")
        self.assertGreater(res.word_count, 30)

    def test_opengraph_fills_when_no_jsonld(self) -> None:
        html = NEWS_HTML.split('<script type="application/ld+json">')[0] + "</head>" + NEWS_HTML.split("</head>")[1]
        res = parse_article(html, "https://news.example.com/stories/one")
        self.assertEqual(res.title, "OG Headline")
        self.assertEqual(res.signals["title"], "opengraph")
        self.assertEqual(res.published_time, "2026-10-11T08:00:00Z")
        self.assertEqual(res.signals["published_time"], "opengraph")
        self.assertEqual(res.authors, ["Meta Author"])
        self.assertEqual(res.signals["authors"], "meta")

    def test_dom_heuristics_last(self) -> None:
        html = """
        <html><head><title>T</title></head><body>
        <article><h1>Heuristic Headline</h1>
        <span rel="author">Dome Byline</span>
        <time datetime="2026-01-02">Jan 2</time>
        <p>One substantive paragraph with more than sixty characters to count here now.</p>
        <p>Second substantive paragraph with more than sixty characters to count as well.</p>
        <p>Third substantive paragraph with more than sixty characters to count here too.</p>
        </article></body></html>
        """
        res = parse_article(html, "https://x.example.com/a")
        self.assertEqual(res.title, "Heuristic Headline")
        self.assertEqual(res.signals["title"], "dom")
        self.assertEqual(res.authors, ["Dome Byline"])
        self.assertEqual(res.published_time, "2026-01-02")
        self.assertEqual(res.signals["published_time"], "dom")
        self.assertEqual(res.doc_type, "article")

    def test_ld_image_dict_and_string_forms(self) -> None:
        for image in ("https://c.example/i.jpg", {"url": "https://c.example/i.jpg"},
                      ["https://c.example/i.jpg"]):
            html = NEWS_HTML.replace('"image": ["https://cdn.example.com/ld.jpg"]',
                                     f'"image": {image!r}'.replace("'", '"'))
            res = parse_article(html, "https://news.example.com/one")
            self.assertEqual(res.image, "https://c.example/i.jpg", image)

    def test_non_article_page(self) -> None:
        html = "<html><head><title>Home</title></head><body><p>tiny</p></body></html>"
        res = parse_article(html, "https://x.example.com/")
        self.assertEqual(res.doc_type, "page")
        self.assertEqual(res.text, "")
        self.assertIsInstance(res, ArticleResult)

    def test_is_article(self) -> None:
        dom_html = NEWS_HTML
        from freegem.crawler.hardening import HtmlDom

        dom = HtmlDom()
        dom.feed(dom_html)
        self.assertTrue(is_article(dom))
        dom2 = HtmlDom()
        dom2.feed("<html><body><p>tiny</p></body></html>")
        self.assertFalse(is_article(dom2))

    def test_malformed_jsonld_does_not_crash(self) -> None:
        html = ('<script type="application/ld+json">{not json</script>'
                "<article><h1>Still Works</h1>"
                "<p>One paragraph long enough for body extraction to include it here.</p></article>")
        res = parse_article(html, "https://x.example.com/b")
        self.assertEqual(res.title, "Still Works")
        self.assertEqual(res.signals["title"], "dom")


if __name__ == "__main__":
    unittest.main()
