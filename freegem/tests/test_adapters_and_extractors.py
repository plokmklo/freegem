"""Tests for PDF/DOCX adapters, rich metadata extraction, and Markdown conversion."""

import unittest

from freegem.adapters import parse_docx, parse_pdf
from freegem.crawler.extract import extract_metadata, html_to_markdown
from freegem.crawler.hardening import HtmlDom


class TestAdaptersAndExtractors(unittest.TestCase):
    def test_pdf_adapter_fallback(self) -> None:
        sample_pdf_bytes = b"%PDF-1.4 ... (Hello World) Tj ..."
        res = parse_pdf(sample_pdf_bytes)
        self.assertEqual(res.doc_type, "pdf")
        self.assertIn("Hello World", res.text)

    def test_docx_adapter(self) -> None:
        res = parse_docx(b"not a real zip")
        self.assertEqual(res.doc_type, "docx")
        self.assertIn("Failed", res.text)

    def test_extract_metadata(self) -> None:
        html_doc = """
        <html lang="en">
          <head>
            <title>Sample Article</title>
            <meta name="description" content="A great test article.">
            <meta name="author" content="Jane Doe">
            <meta name="date" content="2025-01-01">
            <link rel="canonical" href="https://example.com/article">
            <meta property="og:title" content="Sample OG Title">
            <meta property="og:image" content="https://example.com/img.png">
            <meta name="twitter:card" content="summary_large_image">
            <script type="application/ld+json">
              {"@context": "https://schema.org", "@type": "NewsArticle", "headline": "Test"}
            </script>
          </head>
          <body><h1>Body</h1></body>
        </html>
        """
        dom = HtmlDom()
        dom.feed(html_doc)
        meta = extract_metadata(dom, base_url="https://example.com")

        self.assertEqual(meta["title"], "Sample Article")
        self.assertEqual(meta["description"], "A great test article.")
        self.assertEqual(meta["author"], "Jane Doe")
        self.assertEqual(meta["published_date"], "2025-01-01")
        self.assertEqual(meta["canonical_url"], "https://example.com/article")
        self.assertEqual(meta["language"], "en")
        self.assertEqual(meta["opengraph"]["title"], "Sample OG Title")
        self.assertEqual(meta["twitter_cards"]["card"], "summary_large_image")
        self.assertEqual(len(meta["json_ld"]), 1)
        self.assertEqual(meta["json_ld"][0]["headline"], "Test")

    def test_html_to_markdown(self) -> None:
        html_doc = """
        <html>
          <body>
            <h1>Main Title</h1>
            <p>This is a <strong>bold</strong> paragraph with a <a href="/link">link</a>.</p>
            <ul>
              <li>Item 1</li>
              <li>Item 2</li>
            </ul>
            <blockquote>A quote</blockquote>
          </body>
        </html>
        """
        md = html_to_markdown(html_doc, base_url="https://example.com")
        self.assertIn("# Main Title", md)
        self.assertIn("**bold**", md)
        self.assertIn("[link](https://example.com/link)", md)
        self.assertIn("- Item 1", md)
        self.assertIn("> A quote", md)


if __name__ == "__main__":
    unittest.main()
