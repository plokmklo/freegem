"""HTML table extraction: data frames out of <table>, GFM out of nodes.

Tables are the number-one structured payload on public pages (price lists,
schedules, spec sheets, comparison matrices), and the markdown converter
used to flatten them into word soup. This module gives them two honest
shapes:

* :class:`TableData` -- the table as a rectangular frame (header row + body
  rows), extracted from the crawler's DOM. ``colspan``/``rowspan`` are
  deliberately NOT expanded: GFM cannot express spans, and silently
  duplicating or shifting cells would fabricate data that was never there.
  Cells that span land in their first cell position; the source HTML is the
  audit trail.
* ``to_markdown()`` -- GitHub-flavored markdown (the shape every LLM and
  renderer already understands), with cell text flattened, pipes escaped,
  and ragged rows padded so the pipe structure stays truthful.
* ``to_csv()`` -- the spreadsheet shape, stdlib ``csv``.

Nodes are duck-typed (``.tag``, ``.children``, ``.text_parts``, ``.attrs``)
so the module has no import of the DOM parser and can be tested with any
tree that walks like the crawler's HtmlDom.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

__all__ = ["TableData", "extract_tables", "table_to_markdown"]

#: Inline-level tags whose text stays inside a cell; everything block-level
#: (p, li, div...) still contributes its text but becomes a space.
_CELL_FLATTEN_TAGS = frozenset(
    {"a", "b", "strong", "em", "i", "code", "span", "small", "sub", "sup", "u", "s"}
)


@dataclass
class TableData:
    """One extracted table: optional caption, header row, body rows."""

    header: list[str]
    rows: list[list[str]]
    caption: str = ""
    #: Cells spanning columns/rows are NOT expanded; this counter tells the
    #: caller how much rectangularization the source HTML resisted.
    spanned_cells: int = 0
    attrs: dict[str, str] = field(default_factory=dict)

    @property
    def ncols(self) -> int:
        return (
            max([len(self.header), *(len(r) for r in self.rows)]) if self.header or self.rows else 0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "caption": self.caption,
            "header": self.header,
            "rows": self.rows,
            "ncols": self.ncols,
            "nrows": len(self.rows),
            "spanned_cells": self.spanned_cells,
        }

    def to_markdown(self) -> str:
        """GitHub-flavored markdown; ragged rows are padded, never trimmed."""
        ncols = max(self.ncols, 1)
        header = self.header or [""] * ncols
        header = [_pad_row(header, ncols)[i] for i in range(ncols)]
        lines = []
        if self.caption:
            lines.append(f"*{self.caption}*")
            lines.append("")
        lines.append("| " + " | ".join(_cell(header[i]) for i in range(ncols)) + " |")
        lines.append("| " + " | ".join("---" for _ in range(ncols)) + " |")
        for row in self.rows:
            padded = _pad_row(row, ncols)
            lines.append("| " + " | ".join(_cell(padded[i]) for i in range(ncols)) + " |")
        return "\n".join(lines)

    def to_csv(self) -> str:
        """RFC 4180 CSV (header row included when present, padded to the
        frame width so every line has the same field count)."""
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        if self.header:
            writer.writerow(_pad_row(self.header, self.ncols))
        for row in self.rows:
            writer.writerow(_pad_row(row, self.ncols))
        return buf.getvalue()


def _cell(text: str) -> str:
    """One GFM cell: pipes escaped, newlines flattened, empty stays empty."""
    flat = " ".join(str(text).split())
    return flat.replace("|", "\\|") or " "


def _pad_row(row: list[str], ncols: int) -> list[str]:
    if len(row) >= ncols:
        return list(row[:ncols]) if len(row) > ncols else list(row)
    return list(row) + [""] * (ncols - len(row))


def _cell_text(node: Any) -> str:
    """Flatten one <td>/<th> subtree to inline text, spaces for blocks."""
    parts: list[str] = []

    def walk(n: Any) -> None:
        tag = (n.tag or "").lower()
        if tag in ("script", "style", "noscript", "svg"):
            return
        if n.text_parts:
            parts.append(" ".join("".join(n.text_parts).split()))
        for child in n.children:
            if (child.tag or "").lower() not in _CELL_FLATTEN_TAGS:
                parts.append(" ")
            walk(child)

    walk(node)
    return " ".join(" ".join(parts).split())


def _split_rows(node: Any) -> tuple[list[list[str]], list[str], str, int]:
    """Walk a <table> subtree: body rows, header row, caption, span count."""
    rows: list[list[str]] = []
    header: list[str] = []
    caption_parts: list[str] = []
    spans = 0
    in_header_section = False

    def walk(n: Any) -> None:
        nonlocal in_header_section, spans
        tag = (n.tag or "").lower()
        if tag == "caption":
            caption_parts.append(_cell_text(n))
            return
        if tag == "thead":
            in_header_section = True
            for child in n.children:
                walk(child)
            in_header_section = False
            return
        if tag == "tr":
            cells: list[str] = []
            is_header_row = in_header_section
            for child in n.children:
                child_tag = (child.tag or "").lower()
                if child_tag in ("td", "th"):
                    if child_tag == "th":
                        is_header_row = True
                    if child.attrs.get("colspan") or child.attrs.get("rowspan"):
                        spans += 1
                    cells.append(_cell_text(child))
            if is_header_row and not header:
                header.extend(cells)
            else:
                rows.append(cells)
            return
        for child in n.children:
            walk(child)

    walk(node)
    return rows, header, " ".join(caption_parts).strip(), spans


def extract_tables(root: Any) -> list[TableData]:
    """Every <table> under ``root`` (including nested-in-td -- rare, kept)."""
    tables: list[TableData] = []

    def walk(n: Any) -> None:
        if (n.tag or "").lower() == "table":
            rows, header, caption, spans = _split_rows(n)
            tables.append(TableData(header=header, rows=rows, caption=caption, spanned_cells=spans))
            # A table nested inside another table is its own TableData too;
            # its cells were already flattened into this table's cells above,
            # so keep descending only to find it.
            for child in n.children:
                walk(child)
            return
        for child in n.children:
            walk(child)

    walk(root)
    return tables


def table_to_markdown(node: Any) -> str:
    """Render one <table> node straight to GFM (the converter's hook)."""
    rows, header, caption, _spans = _split_rows(node)
    return TableData(header=header, rows=rows, caption=caption).to_markdown()
