"""PDF and DOCX document adapters.

Converts PDF and DOCX binary streams or files into clean text and Markdown format,
with graceful fallbacks using standard library modules when external packages
(pypdf, python-docx) are missing.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Any


@dataclass
class DocumentResult:
    """Output from parsing a PDF or DOCX document."""

    text: str
    markdown: str
    metadata: dict[str, Any]
    page_count: int = 1
    doc_type: str = "pdf"


def parse_pdf(data_or_path: bytes | str) -> DocumentResult:
    """Parse a PDF document into text/markdown and metadata."""
    pdf_bytes: bytes
    if isinstance(data_or_path, str):
        with open(data_or_path, "rb") as f:
            pdf_bytes = f.read()
    else:
        pdf_bytes = data_or_path

    # Try pypdf / PyPDF2 if available
    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        # (page_no, text): keep the ORIGINAL page number so the markdown
        # labels stay right when a page has no extractable text (blank or
        # scanned-only pages used to shift every following "## Page N").
        pages_text: list[tuple[int, str]] = []
        for page_no, p in enumerate(reader.pages, 1):
            t = p.extract_text() or ""
            if t.strip():
                pages_text.append((page_no, t.strip()))
        full_text = "\n\n".join(t for _, t in pages_text)
        meta = {}
        if reader.metadata:
            meta = {k.lstrip("/"): str(v) for k, v in reader.metadata.items() if v}

        md_lines = [f"# Document ({len(reader.pages)} pages)", ""]
        for page_no, page_t in pages_text:
            md_lines.append(f"## Page {page_no}")
            md_lines.append(page_t)
            md_lines.append("")

        return DocumentResult(
            text=full_text,
            markdown="\n".join(md_lines),
            metadata=meta,
            page_count=len(reader.pages),
            doc_type="pdf",
        )
    except ImportError:
        pass
    except Exception:
        # pypdf is installed but the bytes are not a parseable PDF (truncated,
        # encrypted, garbage): degrade to the stdlib fallback below instead of
        # letting PdfStreamError escape to the caller.
        pass

    # Basic stdlib PDF text extraction fallback
    raw_str = pdf_bytes.decode("latin1", errors="ignore")
    text_fragments = re.findall(r"\((.*?)\)\s*Tj", raw_str)
    if not text_fragments:
        text_fragments = re.findall(r"\[(.*?)\]\s*TJ", raw_str)

    clean_text = " ".join(text_fragments)
    clean_text = re.sub(r"\s+", " ", clean_text).strip()
    if not clean_text:
        clean_text = "[PDF text extraction requires pypdf package for complex PDFs]"

    return DocumentResult(
        text=clean_text,
        markdown=f"# PDF Document\n\n{clean_text}",
        metadata={},
        page_count=1,
        doc_type="pdf",
    )


def parse_docx(data_or_path: bytes | str) -> DocumentResult:
    """Parse a DOCX document into text/markdown and metadata."""
    docx_bytes: bytes
    if isinstance(data_or_path, str):
        with open(data_or_path, "rb") as f:
            docx_bytes = f.read()
    else:
        docx_bytes = data_or_path

    # Try python-docx if available
    try:
        import docx

        doc = docx.Document(io.BytesIO(docx_bytes))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        full_text = "\n\n".join(paragraphs)

        md_paragraphs = []
        for p in doc.paragraphs:
            text = p.text.strip()
            if not text:
                continue
            if p.style and p.style.name and p.style.name.startswith("Heading"):
                level = p.style.name.replace("Heading", "").strip()
                try:
                    h_num = int(level)
                except ValueError:
                    h_num = 1
                md_paragraphs.append(f"{'#' * h_num} {text}")
            else:
                md_paragraphs.append(text)

        meta = {}
        if hasattr(doc, "core_properties"):
            cp = doc.core_properties
            if cp.title:
                meta["title"] = cp.title
            if cp.author:
                meta["author"] = cp.author
            if cp.created:
                meta["created"] = str(cp.created)

        return DocumentResult(
            text=full_text,
            markdown="\n\n".join(md_paragraphs),
            metadata=meta,
            page_count=1,
            doc_type="docx",
        )
    except ImportError:
        pass
    except Exception:
        # python-docx is installed but the bytes are not a parseable DOCX (not
        # a zip, corrupt package): degrade to the stdlib fallback below instead
        # of letting BadZipFile escape to the caller.
        pass

    # Stdlib docx ZIP + XML extraction
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
            xml_content = z.read("word/document.xml")
            tree = ET.fromstring(xml_content)

            # w:p represents paragraphs, w:t represents text
            paragraphs = []
            for p in tree.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                texts = [
                    t.text
                    for t in p.iter(
                        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
                    )
                    if t.text
                ]
                p_text = "".join(texts).strip()
                if p_text:
                    paragraphs.append(p_text)

            full_text = "\n\n".join(paragraphs)
            return DocumentResult(
                text=full_text,
                markdown="\n\n".join(paragraphs),
                metadata={},
                page_count=1,
                doc_type="docx",
            )
    except Exception as exc:
        return DocumentResult(
            text=f"Failed to parse DOCX: {exc}",
            markdown=f"Failed to parse DOCX: {exc}",
            metadata={},
            page_count=0,
            doc_type="docx",
        )


def parse_document(data_or_path: bytes | str, filename_or_mime: str = "") -> DocumentResult:
    """Parse document (PDF or DOCX) based on filename/mime type."""
    # None-tolerant: job params pass ``.get("filename", "")`` which is None
    # when the key is present-but-null.
    hint = str(filename_or_mime or "").lower()
    # Routing order: a real extension or the DOCX MIME token decides; the
    # historical bare-"docx" substring only routes when the hint is not
    # clearly a PDF. (The DOCX MIME type --
    # application/vnd.openxmlformats-officedocument.wordprocessingml.document --
    # contains no "docx" substring, so it used to fall into the PDF parser.)
    if (
        hint.endswith(".docx")
        or "wordprocessingml" in hint
        or ("docx" in hint and not hint.endswith(".pdf"))
    ):
        return parse_docx(data_or_path)
    return parse_pdf(data_or_path)
