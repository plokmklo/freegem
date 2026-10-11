"""Parser hardening, secret redaction, safe XML defaults, resource limits.

The crawler reads bytes produced by strangers. Every helper in this module
exists because a stranger can produce input that is:

* a secret you did not mean to save (a page that embeds an API key in a
  config example, a token in a query string) -- :func:`redact_secrets`;
* XML with an external DTD or entity expansion (billion laughs, exfil via
  SYSTEM entities) -- :func:`parse_xml` refuses DOCTYPE/ENTITY outright,
  which is the "safe XML default": the crawler never needs DTDs to read a
  sitemap or an RSS feed;
* deeper, wider, or longer than any legitimate page needs to be --
  :class:`ResourceLimits` and the caps inside :class:`HtmlDom`;
* encoded in anything other than UTF-8 -- :func:`decode_body` honors the
  header charset, then the BOM, then an HTML ``<meta charset>`` sniff, and
  only then falls back to UTF-8-with-replacement.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html.parser import HTMLParser

__all__ = [
    "HtmlDom",
    "Node",
    "ResourceLimits",
    "UnsafeXMLError",
    "decode_body",
    "parse_xml",
    "redact_secrets",
]

# ─── secret redaction ─────────────────────────────────────────────────────────

#: Shapes we treat as secrets. Order matters only for the label; each pattern
#: must be specific enough that redacting prose is rare, because a redaction
#: that fires on ordinary text corrupts output.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "private-key-block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    ),
    (
        "bearer",
        re.compile(
            r"\b(Bearer|token|password|passwd|pwd|secret|api[_-]?key)\b"
            r"\s*[=:]\s*[\"']?[A-Za-z0-9_\-./+=]{12,}",
            re.IGNORECASE,
        ),
    ),
    ("bearer-header", re.compile(r"\bBearer\s+[A-Za-z0-9_\-./+=]{12,}")),
    (
        "query-secret",
        re.compile(
            r"(?i)([?&](?:token|key|secret|password|sig|signature|credential)s?=)[^&\s\"']{8,}"
        ),
    ),
)


def redact_secrets(text: str) -> str:
    """Replace secret-shaped substrings with ``[REDACTED:<kind>]``.

    Applied to every line the crawler logs and every field it writes out.
    Deliberately pattern-based, not an LLM judgment call: it must never
    silently pass a real key because it "looked contextual".
    """
    out = text
    for kind, pattern in _SECRET_PATTERNS:
        out = pattern.sub(f"[REDACTED:{kind}]", out)
    return out


# ─── safe XML ─────────────────────────────────────────────────────────────────


class UnsafeXMLError(ValueError):
    """The document uses XML features the crawler refuses (DTD, entities)."""


_XML_FORBIDDEN = (
    "<!doctype",
    "<!entity",
    "<!element",
    "<!attlist",
    "<?xml-stylesheet",
)


def parse_xml(data: bytes | str, *, max_bytes: int = 8 * 1024 * 1024) -> ET.Element:
    """Parse XML with safe defaults: no DTD, no entities, size-capped.

    ElementTree itself never resolves external entities, but it will happily
    parse a DTD and then raise confusing errors halfway through an entity
    bomb. Scanning the prolog and refusing DOCTYPE/ENTITY notation makes the
    rejection explicit, early, and testable.
    """
    if isinstance(data, bytes):
        if len(data) > max_bytes:
            raise UnsafeXMLError(f"xml exceeds the {max_bytes}-byte cap")
        text = data.decode("utf-8", errors="replace")
    else:
        text = data
        if len(text.encode("utf-8", errors="replace")) > max_bytes:
            raise UnsafeXMLError(f"xml exceeds the {max_bytes}-byte cap")
    head = text[:65536].lower()
    for marker in _XML_FORBIDDEN:
        if marker in head:
            raise UnsafeXMLError(f"refusing XML with {marker.strip()} declaration")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise UnsafeXMLError(f"unparseable XML: {exc}") from exc


# ─── resource limits ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ResourceLimits:
    """Hard ceilings the runner enforces everywhere.

    Every value is a promise to the user's machine: this crawl cannot read
    more than ``max_bytes`` per page, follow more than ``max_redirects``
    hops (urllib's own ceiling is 10; ours is a refusal-reason), parse a DOM
    wider than ``max_dom_nodes``, or run longer than ``max_runtime_sec``.
    """

    timeout_sec: float = 20.0
    max_bytes: int = 5 * 1024 * 1024
    max_redirects: int = 8
    max_dom_nodes: int = 60_000
    max_dom_depth: int = 300
    max_links_per_page: int = 2_000
    max_items_per_page: int = 10_000
    max_sitemap_urls: int = 2_000
    max_runtime_sec: float = 900.0
    max_retries: int = 2


# ─── body decoding ────────────────────────────────────────────────────────────

_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    # 4-byte BOMs before 2-byte: utf-16le BOM is a prefix of utf-32le's.
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)

_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.IGNORECASE)


def decode_body(raw: bytes, content_type: str) -> str:
    """bytes -> str: header charset, BOM, meta charset, then UTF-8 replace.

    The meta sniff reads only the first 2 KiB, where the tag must legally
    appear, so a multi-megabyte page pays nothing for decoding. An unknown
    charset name falls through instead of raising -- a page that lies about
    being ``utf-88`` still renders with replacement characters.
    """
    match = re.search(r"charset=([\w\-]+)", content_type or "", re.IGNORECASE)
    if match:
        try:
            return raw.decode(match.group(1), errors="replace")
        except LookupError:
            pass
    for bom, encoding in _BOMS:
        if raw.startswith(bom):
            try:
                return raw.decode(encoding, errors="replace")
            except LookupError:  # pragma: no cover - stdlib encodings exist
                break
    sniff = raw[:2048]
    meta = _META_CHARSET.search(sniff)
    if meta:
        try:
            return raw.decode(meta.group(1).decode("ascii", "ignore"), errors="replace")
        except LookupError:
            pass
    return raw.decode("utf-8", errors="replace")


# ─── hardened HTML DOM ────────────────────────────────────────────────────────

_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
#: Implied-close rules kept deliberately tiny: the DOM exists for selectors,
#: not for rendering. Without these, unclosed <p>/<li> (extremely common in
#: old hand-written HTML) nest unboundedly until max_dom_depth trips and the
#: whole page is discarded -- worse than a slightly wrong tree.
_IMPLIED_CLOSE = {
    "p": {"p"},
    "li": {"li"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "tr": {"tr"},
    "option": {"option"},
}
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class Node:
    """One element in the crawl DOM: tag, attributes, children, text."""

    __slots__ = ("attrs", "children", "parent", "tag", "tail_text", "text_parts")

    def __init__(self, tag: str, attrs: dict[str, str], parent: Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[Node] = []
        self.parent = parent
        self.text_parts: list[str] = []
        self.tail_text: str = ""

    @property
    def text(self) -> str:
        """Concatenated descendant text (no tails)."""
        chunks: list[str] = []

        def walk(node: Node) -> None:
            for part in node.text_parts:
                chunks.append(part)
            for child in node.children:
                walk(child)

        walk(self)
        return "".join(chunks)

    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def iter(self) -> list[Node]:
        """Document-order list of self + descendants (bounded by build caps)."""
        out: list[Node] = [self]
        for child in self.children:
            out.extend(child.iter())
        return out


class HtmlDom:
    """A size-capped HTML document tree built with html.parser.

    Caps enforced during construction (not after): ``max_nodes`` stops the
    builder, ``max_depth`` refuses to nest deeper, attribute values and text
    nodes are length-capped. A page that trips a cap yields the tree built so
    far plus ``truncated=True`` -- selectors then run over a prefix of the
    page instead of the process ballooning.
    """

    def __init__(self, *, max_nodes: int = 60_000, max_depth: int = 300) -> None:
        self.root = Node("#document", {}, None)
        self.truncated = False
        self.node_count = 1
        self._max_nodes = max_nodes
        self._max_depth = max_depth
        self._stack: list[Node] = [self.root]
        self._last_text_node: Node | None = None

    # -- parsing -------------------------------------------------------------

    def feed(self, data: str) -> None:
        parser = _DomBuilder(self)
        try:
            parser.feed(_CONTROL_CHARS.sub("", data))
            parser.close()
        except Exception:
            # Malformed markup: keep whatever tree was built. Same posture as
            # the scraper's extractor -- a broken page must not raise.
            self.truncated = True

    def _append_text(self, owner: Node, piece: str) -> None:
        if not piece:
            return
        if len(piece) > 65536:
            piece = piece[:65536]
            self.truncated = True
        owner.text_parts.append(piece)
        self._last_text_node = owner


class _DomBuilder(HTMLParser):
    """HTMLParser that grows an :class:`HtmlDom`, respecting its caps."""

    def __init__(self, dom: HtmlDom) -> None:
        super().__init__(convert_charrefs=True)
        self._dom = dom

    def _cur(self) -> Node:
        return self._dom._stack[-1]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        dom = self._dom
        tag = tag.lower()
        # Implied end tags: a new <p> closes an open <p>, a new <li> closes
        # an open <li>, a <td>/<th>/<tr> closes an open cell/row.
        closers = _IMPLIED_CLOSE.get(tag)
        while len(dom._stack) > 1 and closers and dom._stack[-1].tag in closers:
            dom._stack.pop()
        if len(dom._stack) >= dom._max_depth:
            dom.truncated = True
            return
        if tag in _VOID_TAGS:
            parent = self._cur()
            node = Node(tag, _attrs_of(attrs), parent)
            parent.children.append(node)
            dom.node_count += 1
            dom._last_text_node = None
            return
        if dom.node_count >= dom._max_nodes:
            dom.truncated = True
            return
        parent = self._cur()
        node = Node(tag, _attrs_of(attrs), parent)
        parent.children.append(node)
        dom.node_count += 1
        dom._stack.append(node)
        dom._last_text_node = None

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        parent = self._dom._stack[-1]
        node = Node(tag.lower(), _attrs_of(attrs), parent)
        parent.children.append(node)
        self._dom.node_count += 1
        self._dom._last_text_node = None

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        stack = self._dom._stack
        if tag in _VOID_TAGS or len(stack) <= 1:
            return
        # Find the nearest matching open tag; if none, ignore (stray </div>).
        for idx in range(len(stack) - 1, 0, -1):
            if stack[idx].tag == tag:
                del stack[idx:]
                self._dom._last_text_node = None
                return

    def handle_data(self, data: str) -> None:
        cur = self._cur()
        # Whitespace-only text goes to the current node anyway; it keeps
        # inline text runs separated ("a <b>bold</b> c").
        self._dom._append_text(cur, data)

    def handle_entityref(self, name: str) -> None:  # pragma: no cover
        # convert_charrefs=True routes here only on old-Python edge cases.
        self._dom._append_text(self._cur(), html.unescape(f"&{name};"))


def _attrs_of(raw: list[tuple[str, str | None]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in raw:
        key = key.lower()
        if key not in out:
            out[key] = value if value is not None else ""
    return {k: (v[:4096] if len(v) > 4096 else v) for k, v in out.items()}
