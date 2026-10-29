"""Tests for PDF/DOCX adapters, rich metadata extraction, and Markdown conversion."""

import sys
import types
import unittest
from unittest import mock

from freegem.adapters import parse_document, parse_docx, parse_pdf
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

    def test_parse_document_routes_docx_mime_type(self) -> None:
        # The standard DOCX MIME type contains no "docx" substring -- it used
        # to fall through to the PDF parser and return PDF garbage instead of
        # a DOCX parse attempt.
        res = parse_document(
            b"not a real zip",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        self.assertEqual(res.doc_type, "docx")
        self.assertIn("Failed", res.text)

    def test_parse_document_pdf_extension_wins_over_docx_substring(self) -> None:
        # A PDF filename that merely contains "docx" must stay on the PDF
        # path (extension decides first).
        res = parse_document(b"%PDF-1.4 ... (Hello World) Tj ...", "quarterly-docx-report.pdf")
        self.assertEqual(res.doc_type, "pdf")
        self.assertIn("Hello World", res.text)

    def test_parse_document_none_hint_defaults_to_pdf(self) -> None:
        # Job params pass .get("filename", "") which is None when the key is
        # present-but-null; the hint must not crash on .lower().
        res = parse_document(b"%PDF-1.4 ... (Hello World) Tj ...", None)  # type: ignore[arg-type]
        self.assertEqual(res.doc_type, "pdf")
        self.assertIn("Hello World", res.text)

    def test_pdf_markdown_page_labels_keep_original_page_numbers(self) -> None:
        # A blank (or unextractable) page must not shift the "## Page N"
        # labels of the following pages; hermetic fake pypdf reader.
        fake_pypdf = types.ModuleType("pypdf")

        class _Page:
            def __init__(self, text: str) -> None:
                self._text = text

            def extract_text(self) -> str:
                return self._text

        class _Reader:
            def __init__(self, stream: object) -> None:
                _ = stream
                self.pages = [_Page(""), _Page("Second page body")]
                self.metadata = None

        fake_pypdf.PdfReader = _Reader  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"pypdf": fake_pypdf}):
            res = parse_pdf(b"%PDF-fake")
        self.assertEqual(res.doc_type, "pdf")
        self.assertEqual(res.page_count, 2)
        self.assertEqual(res.text, "Second page body")
        self.assertIn("## Page 2", res.markdown)
        self.assertNotIn("## Page 1", res.markdown)

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

