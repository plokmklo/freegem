"""Field extraction: CSS selectors, an XPath subset, JSON paths, typed fields.

The DOM (:class:`freegem.crawler.hardening.HtmlDom`) is the source for CSS
and XPath queries; JSON documents (from a fetch that returned JSON) use
JSON paths. Every extracted item carries provenance: the page URL, fetch
time, content hash, and the selector that produced each field -- so an item
in a results file can always be traced back to where it came from and how
it was cut.

CSS support is a compiled subset, enough for real pages::

    tag, *, .class, .a.b, #id, [attr], [attr=v], [attr^=v], [attr$=v],
    [attr*=v], descendant " ", child ">", grouping ","

XPath support is ElementTree's documented subset (``tag``, ``*``, ``[@attr]``,
``[@attr='v']``, ``[tag]``, ``[tag='text']``, ``//``, ``.//``, ``[n]``) --
honest scope instead of a fake full XPath.
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .hardening import HtmlDom, Node

__all__ = [
    "ExtractionStats",
    "Field",
    "FieldError",
    "Schema",
    "css_select",
    "dom_to_etree",
    "extract_items",
    "json_get",
    "xpath_select",
]

# ─── CSS subset ───────────────────────────────────────────────────────────────

#: One atom of a simple selector: a tag, *, #id, .class, or [attr ...].
_ATOM = re.compile(
    r"""(?P<tag>^[a-zA-Z][\w-]*)|(?P<star>^\*)|(?P<id>^[\#][\w-]+)|(?P<class>^[\.][\w-]+)|(?P<attr>^\[[^\]]*\])"""
)


def _parse_compound(token: str) -> dict[str, Any]:
    """Parse ``div.x#y[href^='z']`` into one simple-selector dict.

    Atoms are consumed greedily left to right; anything left over is an
    error. A compound matches when EVERY atom matches.
    """
    simple: dict[str, Any] = {}
    rest = token
    while rest:
        match = _ATOM.match(rest)
        if not match:
            raise ValueError(f"unsupported CSS token {token!r}")
        kind = match.lastgroup
        text = match.group(0)
        if kind == "tag":
            simple["tag"] = text.lower()
        elif kind == "star":
            pass
        elif kind == "id":
            simple["id"] = text[1:]
        elif kind == "class":
            simple.setdefault("classes", []).append(text[1:])
        elif kind == "attr":
            inner = text[1:-1]
            op_match = re.search(r"(\^=|\$=|\*=|=)\s*(.*)$", inner)
            if op_match:
                simple["attr"] = inner[: op_match.start()].strip()
                simple["op"] = op_match.group(1)
                simple["val"] = op_match.group(2).strip().strip("\"'")
            else:
                simple["attr"] = inner.strip()
                simple["op"] = "exists"
                simple["val"] = ""
        rest = rest[match.end() :]
    return simple


def _match_simple(node: Node, simple: dict[str, Any]) -> bool:
    if simple.get("tag") and node.tag != simple["tag"]:
        return False
    if simple.get("id") and node.attrs.get("id") != simple["id"]:
        return False
    for cls in simple.get("classes", ()):
        if cls not in node.classes():
            return False
    attr = simple.get("attr")
    if attr is not None:
        actual = node.attrs.get(attr)
        op = simple.get("op") or ""
        val = simple.get("val") or ""
        if actual is None:
            return False
        if op == "=" and actual != val:
            return False
        if op == "^=" and not actual.startswith(val):
            return False
        if op == "$=" and not actual.endswith(val):
            return False
        if op == "*=" and val not in actual:
            return False
    return True


def _match_chain(node: Node, chain: list[dict[str, Any]], combinators: list[str]) -> bool:
    """Match the LAST simple selector at ``node``; walk combinators leftward."""
    if not _match_simple(node, chain[-1]):
        return False
    idx = len(chain) - 1
    cur = node
    while idx > 0:
        comb = combinators[idx - 1]
        if comb == ">":
            parent = cur.parent
            if parent is None or not _match_simple(parent, chain[idx - 1]):
                return False
            cur = parent
        else:  # descendant: some ancestor must match; try each
            ancestor = cur.parent
            while ancestor is not None and ancestor.tag != "#document":
                if _match_simple(ancestor, chain[idx - 1]):
                    cur = ancestor
                    break
                ancestor = ancestor.parent
            else:
                return False
        idx -= 1
    return True


def _compile_selector(selector: str) -> Callable[[Node], list[Node]]:
    """Compile one CSS selector string into a matcher over DOM nodes."""
    groups = [g.strip() for g in selector.split(",") if g.strip()]
    parsed: list[tuple[list[dict[str, Any]], list[str]]] = []
    for group in groups:
        tokens = re.split(r"\s*(>)\s*|\s+", group)
        tokens = [t for t in tokens if t]
        chain: list[dict[str, Any]] = []
        combinators: list[str] = []
        expect_simple = True
        for token in tokens:
            if token == ">":
                if not chain:
                    raise ValueError(f"selector starts with a combinator: {selector!r}")
                combinators.append(">")
                expect_simple = True
                continue
            if not expect_simple:
                combinators.append(" ")
            chain.append(_parse_compound(token))
            expect_simple = False
        if not chain:
            raise ValueError(f"empty selector group in {selector!r}")
        parsed.append((chain, combinators))

    def run(root: Node) -> list[Node]:
        out: list[Node] = []
        for node in root.iter():
            if node.tag == "#document":
                continue
            for chain, combinators in parsed:
                if _match_chain(node, chain, combinators):
                    out.append(node)
                    break
        return out

    return run


def css_select(dom: HtmlDom, selector: str) -> list[Node]:
    """Run a CSS-subset query; raises ValueError on unsupported syntax."""
    return _compile_selector(selector)(dom.root)


# ─── XPath (ElementTree subset) ───────────────────────────────────────────────


def dom_to_etree(dom: HtmlDom) -> ET.Element:
    """Re-attach the DOM to a real ElementTree for ``findall`` queries."""
    root_node = dom.root
    root = ET.Element("root")
    stack: list[tuple[Node, ET.Element]] = [(root_node, root)]
    while stack:
        node, et_parent = stack.pop()
        for child in node.children:
            et_child = ET.SubElement(et_parent, child.tag or "span", dict(child.attrs))
            if child.text_parts:
                et_child.text = "".join(child.text_parts)
            stack.append((child, et_child))
    return root


def xpath_select(dom: HtmlDom, path: str) -> list[Node]:
    """Evaluate an ElementTree-subset XPath against the DOM.

    Returns the matching DOM nodes (not ET elements) so ``attr`` extraction
    and text handling behave identically to CSS results. ``//tag`` works
    through a document-level scan; the remaining subset is delegated to
    ``ElementTree.findall`` and mapped back by document order.
    """
    path = path.strip()
    if path.startswith("//"):
        tag = path[2:]
        if not re.fullmatch(r"[\w-]+|\*", tag):
            raise ValueError(f"unsupported XPath {path!r} (only //tag is special here)")
        return [n for n in dom.root.iter() if n.tag != "#document" and (tag == "*" or n.tag == tag)]
    tree = dom_to_etree(dom)
    try:
        found = tree.findall(path)
    except (SyntaxError, KeyError) as exc:
        raise ValueError(f"unsupported XPath {path!r}: {exc}") from exc
    # Map ET elements back to DOM nodes by identity of (tag, attrs, text).
    candidates: dict[str, list[Node]] = {}
    for node in dom.root.iter():
        if node.tag == "#document":
            continue
        key = f"{node.tag}|{sorted(node.attrs.items())}|{''.join(node.text_parts)[:200]}"
        candidates.setdefault(key, []).append(node)
    out: list[Node] = []
    used: set[int] = set()
    for el in found:
        key = f"{el.tag}|{sorted(el.attrib.items())}|{(el.text or '')[:200]}"
        for node in candidates.get(key, []):
            if id(node) not in used:
                used.add(id(node))
                out.append(node)
                break
    return out


# ─── JSON paths ───────────────────────────────────────────────────────────────


def json_get(data: Any, path: str) -> tuple[bool, Any]:
    """Dotted path into JSON: ``$.store.book[0].title`` or ``store.book.0.title``.

    Returns ``(found, value)`` so a legitimate ``null`` is distinguishable
    from "the path went nowhere".
    """
    clean = path.strip()
    if clean.startswith("$"):
        clean = clean[1:]
    clean = clean.lstrip(".")
    if not clean:
        return True, data
    tokens = re.findall(r"([^.\[\]]+)|\[(\d+)\]", clean)
    cur: Any = data
    for key_token, idx_token in tokens:
        if idx_token:
            idx = int(idx_token)
            if not isinstance(cur, list) or idx >= len(cur):
                return False, None
            cur = cur[idx]
        elif isinstance(cur, list) and key_token.isdigit():
            idx = int(key_token)
            if idx >= len(cur):
                return False, None
            cur = cur[idx]
        elif isinstance(cur, dict) and key_token in cur:
            cur = cur[key_token]
        else:
            return False, None
    return True, cur


# ─── typed fields + schemas ───────────────────────────────────────────────────

_TYPES = ("str", "int", "float", "bool", "url")
_NORMALIZERS = ("strip", "collapse", "lower", "upper", "abs_url")


@dataclass(frozen=True)
class Field:
    """One typed, validated output field.

    ``selector`` is interpreted by ``kind``: ``css`` (relative to the item
    node), ``xpath``, ``json`` (relative to the item's JSON object), or
    ``text`` (regex against the item's text; the field value is the whole
    match). ``attr`` pulls an attribute instead of the element text.
    """

    name: str
    selector: str
    kind: str = "css"  # css | xpath | json | text
    type: str = "str"  # str | int | float | bool | url
    required: bool = False
    default: str | None = None
    attr: str | None = None
    normalize: tuple[str, ...] = ()
    pattern: str | None = None
    multi: bool = False

    def validate_spec(self) -> list[str]:
        problems: list[str] = []
        if not self.name or not re.fullmatch(r"[\w.-]+", self.name):
            problems.append(f"bad field name {self.name!r}")
        if self.kind not in ("css", "xpath", "json", "text"):
            problems.append(f"unknown selector kind {self.kind!r}")
        if self.type not in _TYPES:
            problems.append(f"unknown type {self.type!r}")
        bad_norm = [n for n in self.normalize if n not in _NORMALIZERS]
        if bad_norm:
            problems.append(f"unknown normalizers {bad_norm}")
        if self.pattern:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                problems.append(f"bad pattern: {exc}")
        return problems


@dataclass(frozen=True)
class FieldError:
    """A per-field, per-item failure recorded instead of raised."""

    item_index: int
    field: str
    error: str


@dataclass
class ExtractionStats:
    items: int = 0
    field_errors: int = 0
    pages_with_schema: int = 0
    errors: list[FieldError] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "items": self.items,
            "field_errors": self.field_errors,
            "pages_with_schema": self.pages_with_schema,
        }


@dataclass(frozen=True)
class Schema:
    """How to cut records out of one page kind."""

    item_selector: str = ""  # css or xpath when item_kind != json; json path when json
    item_kind: str = "css"  # css | xpath | json
    single: bool = False  # one record per page (item_selector optional)
    fields: tuple[Field, ...] = ()

    def validate_spec(self) -> list[str]:
        problems: list[str] = []
        if self.item_kind not in ("css", "xpath", "json"):
            problems.append(f"unknown item kind {self.item_kind!r}")
        if not self.single and not self.item_selector:
            problems.append("schema needs item_selector or single=true")
        if not self.fields:
            problems.append("schema needs at least one field")
        for f in self.fields:
            problems.extend(f.validate_spec())
        return problems


def _cast(value: str, ftype: str, base_url: str) -> tuple[bool, Any]:
    text = value
    try:
        if ftype == "int":
            return True, int(text.strip())
        if ftype == "float":
            return True, float(text.strip())
        if ftype == "bool":
            low = text.strip().lower()
            if low in ("true", "yes", "1", "on"):
                return True, True
            if low in ("false", "no", "0", "off"):
                return True, False
            return False, None
        if ftype == "url":
            absolute = urllib.parse.urljoin(base_url, text.strip())
            if not absolute.lower().startswith(("http://", "https://")):
                return False, None
            return True, absolute
    except (ValueError, TypeError):
        return False, None
    return True, text


def _normalize(text: str, steps: tuple[str, ...], base_url: str) -> str:
    out = text
    for step in steps:
        if step == "strip":
            out = out.strip()
        elif step == "collapse":
            out = re.sub(r"\s+", " ", out)
        elif step == "lower":
            out = out.lower()
        elif step == "upper":
            out = out.upper()
        elif step == "abs_url":
            out = urllib.parse.urljoin(base_url, out.strip())
    return out


def _text_of(node: Node) -> str:
    return re.sub(r"\s+", " ", node.text).strip()


def extract_items(
    dom: HtmlDom | None,
    payload: Any,
    schema: Schema,
    *,
    base_url: str,
    stats: ExtractionStats | None = None,
    max_items: int = 10_000,
) -> tuple[list[dict[str, Any]], list[FieldError]]:
    """Cut records per the schema; never raises for data problems.

    ``dom`` carries HTML queries (css/xpath), ``payload`` the parsed JSON for
    json queries -- pass whichever the page needs (both is fine). Field
    failures become :class:`FieldError` entries; a record with a failed
    ``required`` field is dropped, anything else keeps its extracted values.
    """
    st = stats if stats is not None else ExtractionStats()
    errors: list[FieldError] = []
    records: list[dict[str, Any]] = []
    unit_list: list[Any]

    if schema.item_kind == "json":
        found, items = json_get(payload, schema.item_selector)
        if not found:
            return records, errors
        unit_list = items if isinstance(items, list) else [items]
    elif schema.single:
        # One record per page: css/xpath fields query the whole document.
        unit_list = [dom.root if dom is not None else None]
    else:
        if dom is None:
            return records, errors
        try:
            if schema.item_kind == "xpath":
                unit_list = list(xpath_select(dom, schema.item_selector))
            else:
                unit_list = list(css_select(dom, schema.item_selector))
        except ValueError as exc:
            errors.append(FieldError(-1, "(schema)", str(exc)))
            st.field_errors += 1
            return records, errors

    for index, unit in enumerate(unit_list[:max_items]):
        record: dict[str, Any] = {}
        drop = False
        for f in schema.fields:
            try:
                ok, value = _extract_one(unit, payload, f, dom, base_url)
            except ValueError as exc:
                ok, value, _ = False, None, str(exc)
                errors.append(FieldError(index, f.name, str(exc)))
                st.field_errors += 1
            if not ok:
                if f.required:
                    drop = True
                    errors.append(FieldError(index, f.name, "required value missing"))
                    st.field_errors += 1
                elif f.default is not None:
                    record[f.name] = f.default
                continue
            record[f.name] = value
        if not drop and record:
            record["_index"] = index
            records.append(record)
    st.items += len(records)
    return records, errors


def _extract_one(
    unit: Any,
    payload: Any,
    f: Field,
    dom: HtmlDom | None,
    base_url: str,
) -> tuple[bool, Any]:
    """Produce one typed field value from the item unit.

    Returns ``(False, None)`` when the value is ABSENT; raises ValueError
    when a value is present but fails its type/pattern (the caller records
    that as a FieldError, so bad data is counted, not swallowed).
    """
    if f.kind == "json":
        source = unit if unit is not None else payload
        found, value = json_get(source, f.selector)
        if not found:
            return False, None
        ok, typed = _finish_raw(
            value if isinstance(value, str) else _json_scalar(value), f, base_url
        )
        if not ok:
            raise ValueError(f"{f.name}: value failed type/pattern validation")
        return True, typed
    if f.kind == "text":
        if isinstance(unit, Node):
            text = _text_of(unit)
        elif unit is None and dom is not None:
            text = _text_of(dom.root)  # single-record mode: the whole page
        else:
            text = str(unit)
        regex = f.pattern or f.selector  # for text fields the selector IS the regex
        if not regex:
            return True, text
        match = re.search(regex, text)
        if not match:
            raise ValueError(f"{f.name}: pattern {regex!r} not found")
        return True, match.group(0)
    if unit is None:
        return False, None
    nodes: list[Node]
    if f.kind == "xpath":
        nodes = xpath_select(_dom_of_unit(unit, dom), f.selector)
    else:
        nodes = css_select(_dom_of_unit(unit, dom), f.selector)
    if not nodes:
        if f.multi:
            return True, []  # a list field with no hits is empty, not absent
        return False, None
    chosen = nodes if f.multi else nodes[:1]
    values: list[Any] = []
    for node in chosen:
        raw = node.attrs.get(f.attr, "") if f.attr is not None else _text_of(node)
        ok, typed = _finish_raw(raw, f, base_url)
        if not ok:
            raise ValueError(f"{f.name}: value {raw[:60]!r} failed type/pattern")
        values.append(typed)
    return True, (values if f.multi else values[0])


def _dom_of_unit(unit: Node, dom: HtmlDom | None) -> HtmlDom:
    """A query context whose root is the item node (children only)."""
    wrapper = HtmlDom()
    wrapper.root = unit
    if dom is not None:
        wrapper.truncated = dom.truncated
    return wrapper


def _json_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _finish_raw(raw: str, f: Field, base_url: str) -> tuple[bool, Any]:
    text = _normalize(raw, f.normalize, base_url)
    if f.pattern and not re.search(f.pattern, text):
        return False, None
    return _cast(text, f.type, base_url)


# ─── Rich Metadata & Markdown Extraction ──────────────────────────────────────


def extract_metadata(dom: HtmlDom, *, base_url: str = "") -> dict[str, Any]:
    """Extract rich metadata (OpenGraph, Twitter cards, JSON-LD, canonical URL, language, author, publish date)."""
    meta_info: dict[str, Any] = {
        "title": "",
        "description": "",
        "canonical_url": "",
        "language": "",
        "author": "",
        "published_date": "",
        "site_name": "",
        "opengraph": {},
        "twitter_cards": {},
        "json_ld": [],
    }

    # Handled inside _walk when tag == "html"

    def _walk(node: Node) -> None:
        tag = node.tag.lower()
        if tag == "html" and node.attrs.get("lang"):
            meta_info["language"] = node.attrs["lang"].strip()
        if tag == "title" and not meta_info["title"]:
            meta_info["title"] = _text_of(node)
        elif tag == "link":
            rel = (node.attrs.get("rel") or "").lower()
            href = node.attrs.get("href") or ""
            if "canonical" in rel and href:
                meta_info["canonical_url"] = urllib.parse.urljoin(base_url, href)
        elif tag == "meta":
            name = (node.attrs.get("name") or node.attrs.get("property") or "").lower()
            content = node.attrs.get("content") or ""

            if name == "description":
                meta_info["description"] = content
            elif name in ("author", "article:author"):
                meta_info["author"] = content
            elif name in ("article:published_time", "date", "pubdate"):
                meta_info["published_date"] = content
            elif name == "og:site_name":
                meta_info["site_name"] = content

            if name.startswith("og:"):
                meta_info["opengraph"][name[3:]] = content
            elif name.startswith("twitter:"):
                meta_info["twitter_cards"][name[8:]] = content
        elif tag == "script":
            stype = (node.attrs.get("type") or "").lower()
            if "json" in stype and "ld" in stype:
                script_text = _text_of(node)
                if script_text:
                    try:
                        import json

                        data = json.loads(script_text)
                        meta_info["json_ld"].append(data)
                    except Exception:
                        pass

        for child in node.children:
            _walk(child)

    if dom.root:
        _walk(dom.root)

    # Fallbacks from OpenGraph
    if not meta_info["title"] and meta_info["opengraph"].get("title"):
        meta_info["title"] = meta_info["opengraph"]["title"]
    if not meta_info["description"] and meta_info["opengraph"].get("description"):
        meta_info["description"] = meta_info["opengraph"]["description"]

    return meta_info


def html_to_markdown(dom_or_html: HtmlDom | str, *, base_url: str = "") -> str:
    """Convert HTML DOM or raw HTML into clean structured Markdown."""
    if isinstance(dom_or_html, str):
        dom = HtmlDom()
        dom.feed(dom_or_html)
    else:
        dom = dom_or_html

    if not dom or not dom.root:
        return ""

    from ..tables import table_to_markdown

    def _render(node: Node) -> str:
        tag = node.tag.lower()
        if tag in ("script", "style", "noscript", "svg"):
            return ""

        # Tables bypass the generic recursion: the cell grid IS the meaning,
        # and flattening cells into running text destroys it (price lists,
        # schedules, spec sheets became word soup before GFM rendering).
        if tag == "table":
            rendered = table_to_markdown(node)
            return f"\n\n{rendered}\n\n" if rendered else ""

        children_texts = [_render(c) for c in node.children]
        inner = "".join(children_texts)

        # Append direct text if any
        if node.text_parts:
            direct_text = "".join(node.text_parts)
            inner = direct_text + inner

        inner = inner.strip()

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(tag[1])
            return f"\n\n{'#' * level} {inner}\n\n"
        elif tag == "p":
            return f"\n\n{inner}\n\n" if inner else ""
        elif tag == "a":
            href = node.attrs.get("href", "")
            if href:
                full_href = urllib.parse.urljoin(base_url, href)
                return (
                    f" [{inner or full_href}]({full_href}) "
                    if inner
                    else f" [{full_href}]({full_href}) "
                )
            return inner
        elif tag == "li":
            return f"\n- {inner}" if inner else ""
        elif tag in ("ul", "ol"):
            return f"\n{inner}\n" if inner else ""
        elif tag == "blockquote":
            return f"\n\n> {inner}\n\n" if inner else ""
        elif tag == "pre":
            return f"\n\n```\n{inner}\n```\n\n" if inner else ""
        elif tag == "code":
            return f"`{inner}`" if inner else ""
        elif tag in ("strong", "b"):
            return f"**{inner}**" if inner else ""
        elif tag in ("em", "i"):
            return f"*{inner}*" if inner else ""

        return inner if inner else ""

    raw_md = _render(dom.root)

    # Normalize excessive empty lines
    cleaned = re.sub(r"\n{3,}", "\n\n", raw_md).strip()
    return cleaned
