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
        pages_text: list[str] = []
        for p in reader.pages:
            t = p.extract_text() or ""
            if t.strip():
                pages_text.append(t.strip())
        full_text = "\n\n".join(pages_text)
        meta = {}
        if reader.metadata:
            meta = {k.lstrip("/"): str(v) for k, v in reader.metadata.items() if v}

        md_lines = [f"# Document ({len(reader.pages)} pages)", ""]
        for idx, page_t in enumerate(pages_text, 1):
            md_lines.append(f"## Page {idx}")
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

    # Stdlib docx ZIP + XML extraction
    try:
        with zipfile.ZipFile(io.BytesIO(docx_bytes)) as z:
            xml_content = z.read("word/document.xml")
            tree = ET.fromstring(xml_content)

            # w:p represents paragraphs, w:t represents text
            paragraphs = []
            for p in tree.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                texts = [
                    t.text for t in p.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t") if t.text
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
    lower_hint = filename_or_mime.lower()
    if "docx" in lower_hint or lower_hint.endswith(".docx"):
        return parse_docx(data_or_path)
    return parse_pdf(data_or_path)


def parse_document_with_ai(
    data_or_path: bytes | str,
    filename_or_mime: str = "",
    *,
    cfg: Any | None = None,
    question: str | None = None,
    ai_prompt: str | None = None,
) -> tuple[DocumentResult, str | None]:
    """Parse document and optionally run AI assistance on its content."""
    doc_res = parse_document(data_or_path, filename_or_mime)
    ai_answer = None

    prompt_task = ai_prompt or question
    if prompt_task and cfg is not None:
        try:
            from .gemini import generate
            from .models import DEFAULT_MODEL, resolve_model

            prompt = (
                f"You are an AI document analysis assistant.\n\n"
                f"Task: {prompt_task}\n\n"
                f"Document Type: {doc_res.doc_type.upper()}\n"
                f"Document Text:\n{doc_res.markdown[:50000]}\n"
            )
            _name, mode_id, think, _err, _extra = resolve_model(DEFAULT_MODEL)
            ai_answer = generate(cfg, prompt, mode_id or 0, think or 0).strip()
        except Exception as exc:
            ai_answer = f"AI extraction error: {exc}"

    return doc_res, ai_answer
