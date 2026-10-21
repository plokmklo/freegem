"""CSS / XPath-subset / JSON-path extraction, typed fields, provenance."""

from __future__ import annotations

import json
import unittest

from freegem.crawler.extract import (
    ExtractionStats,
    Field,
    Schema,
    css_select,
    dom_to_etree,
    extract_items,
    json_get,
    xpath_select,
)
from freegem.crawler.hardening import HtmlDom

PAGE = """
<html><body>
  <div class="results">
    <article class="hit"><h2><a href="/a1">Alpha</a></h2><span class="score">12</span>
      <a class="dl" href="/files/a1.pdf">pdf</a></article>
    <article class="hit"><h2><a href="/a2">Beta</a></h2><span class="score">7</span></article>
    <article><h2><a href="/a3">Gamma</a></h2><span class="score">nope</span></article>
  </div>
  <a id="top" href="https://example.com/">home</a>
</body></html>
"""


def _dom(page: str = PAGE) -> HtmlDom:
    dom = HtmlDom()
    dom.feed(page)
    return dom


class CssTests(unittest.TestCase):
    def test_tag_selector(self) -> None:
        self.assertEqual(len(css_select(_dom(), "article")), 3)

    def test_class_selector(self) -> None:
        self.assertEqual(len(css_select(_dom(), ".hit")), 2)

    def test_compound_class(self) -> None:
        dom = HtmlDom()
        dom.feed('<i class="a b"></i><i class="a"></i>')
        self.assertEqual(len(css_select(dom, ".a.b")), 1)

    def test_id_selector(self) -> None:
        self.assertEqual(len(css_select(_dom(), "#top")), 1)

    def test_attribute_selectors(self) -> None:
        dom = _dom()
        self.assertEqual(len(css_select(dom, "a[href]")), 5)
        self.assertEqual(len(css_select(dom, "a[href^='/files/']")), 1)
        self.assertEqual(len(css_select(dom, "a[href$='.pdf']")), 1)
        self.assertEqual(len(css_select(dom, "[class*=esu]")), 1)  # .results

    def test_descendant_and_child(self) -> None:
        dom = _dom()
        self.assertEqual(len(css_select(dom, "article h2 a")), 3)
        self.assertEqual(len(css_select(dom, ".results > article")), 3)
        self.assertEqual(len(css_select(dom, "article > h2 > a")), 3)

    def test_grouping_comma(self) -> None:
        dom = HtmlDom()
        dom.feed("<p>one</p><em>two</em>")
        self.assertEqual(len(css_select(dom, "p, em")), 2)

    def test_unsupported_syntax_raises(self) -> None:
        for bad in ("p > :first-child", "::before", "a + b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                css_select(_dom(), bad)


class XpathTests(unittest.TestCase):
    def test_double_slash_tag(self) -> None:
        self.assertEqual(len(xpath_select(_dom(), "//article")), 3)

    def test_star(self) -> None:
        self.assertGreater(len(xpath_select(_dom(), "//*")), 3)

    def test_findall_subset(self) -> None:
        dom = _dom()
        self.assertEqual(len(xpath_select(dom, ".//article//a")), 4)

    def test_unsupported_path_raises(self) -> None:
        with self.assertRaises(ValueError):
            xpath_select(_dom(), "//article[@class='hit']//x:foo")

    def test_dom_to_etree_roundtrip(self) -> None:
        tree = dom_to_etree(_dom())
        articles = tree.findall(".//article")
        self.assertEqual(len(articles), 3)


class JsonPathTests(unittest.TestCase):
    def test_dotted_and_index(self) -> None:
        data = {"store": {"book": [{"title": "a"}, {"title": "b"}]}}
        self.assertEqual(json_get(data, "store.book.1.title"), (True, "b"))
        self.assertEqual(json_get(data, "$.store.book[0].title"), (True, "a"))

    def test_missing_paths(self) -> None:
        found, value = json_get({"a": 1}, "a.b.c")
        self.assertFalse(found)
        self.assertIsNone(value)
        found, value = json_get([1, 2], "5")
        self.assertFalse(found)

    def test_null_is_found(self) -> None:
        self.assertEqual(json_get({"a": None}, "a"), (True, None))

    def test_empty_path_returns_whole(self) -> None:
        self.assertEqual(json_get({"a": 1}, ""), (True, {"a": 1}))


class ExtractItemsTests(unittest.TestCase):
    SCHEMA = Schema(
        item_selector="article",
        fields=(
            Field(
                name="title",
                selector="h2 a",
                kind="css",
                required=True,
                normalize=("strip", "collapse"),
            ),
            Field(name="url", selector="h2 a", kind="css", attr="href", type="url", required=True),
            Field(name="score", selector="span.score", kind="css", type="int", required=False),
            Field(name="pdf", selector="a.dl", kind="css", attr="href", type="url", multi=True),
        ),
    )

    def test_records_typed_and_provenanced(self) -> None:
        records, errors = extract_items(_dom(), None, self.SCHEMA, base_url="https://e.com/list")
        self.assertEqual(len(records), 3)
        self.assertEqual([r["title"] for r in records], ["Alpha", "Beta", "Gamma"])
        self.assertEqual(records[0]["url"], "https://e.com/a1")
        self.assertEqual(records[0]["score"], 12)
        self.assertEqual(records[1]["pdf"], [])
        self.assertEqual(records[0]["pdf"], ["https://e.com/files/a1.pdf"])
        # Gamma's score is not an int: absent, not a crash
        self.assertNotIn("score", records[2])
        self.assertTrue(any("score" in e.field for e in errors))

    def test_required_missing_drops_record(self) -> None:
        schema = Schema(
            item_selector="article",
            fields=(Field(name="isbn", selector="span.isbn", required=True),),
        )
        records, errors = extract_items(_dom(), None, schema, base_url="https://e.com")
        self.assertEqual(records, [])
        self.assertEqual(len(errors), 3)

    def test_default_used_when_absent(self) -> None:
        schema = Schema(
            item_selector="article",
            fields=(Field(name="lang", selector="span.lang", default="en"),),
        )
        records, _ = extract_items(_dom(), None, schema, base_url="https://e.com")
        self.assertEqual([r["lang"] for r in records], ["en", "en", "en"])

    def test_single_record_per_page(self) -> None:
        schema = Schema(single=True, fields=(Field(name="title", selector="title", kind="css"),))
        page = HtmlDom()
        page.feed("<html><title>Doc Title</title><body>x</body></html>")
        records, _ = extract_items(page, None, schema, base_url="https://e.com")
        self.assertEqual(records, [{"title": "Doc Title", "_index": 0}])

    def test_json_items(self) -> None:
        schema = Schema(
            item_kind="json",
            item_selector="items",
            fields=(
                Field(name="name", selector="name", kind="json"),
                Field(name="count", selector="count", kind="json", type="int"),
            ),
        )
        payload = {"items": [{"name": "a", "count": 1}, {"name": "b", "count": "x"}]}
        records, errors = extract_items(None, payload, schema, base_url="https://e.com")
        self.assertEqual(records[0], {"name": "a", "count": 1, "_index": 0})
        self.assertNotIn("count", records[1])
        self.assertEqual(len(errors), 1)

    def test_json_whole_document_as_item(self) -> None:
        schema = Schema(
            item_kind="json",
            item_selector="$",
            fields=(Field(name="total", selector="total", kind="json", type="int"),),
        )
        records, _ = extract_items(None, {"total": 4}, schema, base_url="https://e.com")
        self.assertEqual(records[0]["total"], 4)

    def test_validation_pattern(self) -> None:
        schema = Schema(
            single=True, fields=(Field(name="version", selector=r"v\d+\.\d+", kind="text"),)
        )
        page = HtmlDom()
        page.feed("<p>version v2.6 here</p>")
        records, _ = extract_items(page, None, schema, base_url="https://e.com")
        self.assertEqual(records[0]["version"], "v2.6")

    def test_schema_validation_problems(self) -> None:
        bad = Schema(item_kind="xml", fields=())
        problems = bad.validate_spec()
        self.assertTrue(any("item kind" in p for p in problems))
        self.assertTrue(any("at least one field" in p for p in problems))
        field = Field(name="bad name!", selector="x", kind="lua", type="dict")
        problems = field.validate_spec()
        self.assertEqual(len(problems), 3)

    def test_max_items_cap(self) -> None:
        schema = Schema(item_selector="article", fields=(Field(name="t", selector="h2 a"),))
        records, _ = extract_items(_dom(), None, schema, base_url="https://e.com", max_items=2)
        self.assertEqual(len(records), 2)

    def test_required_type_failure_recorded_once(self) -> None:
        """A present-but-invalid required value earns ONE error (the
        validation failure), not a second 'required value missing'."""
        dom = HtmlDom()
        dom.feed('<article><span class="s">NaN</span></article>')
        schema = Schema(
            item_selector="article",
            fields=(Field(name="score", selector="span.s", type="int", required=True),),
        )
        records, errors = extract_items(dom, None, schema, base_url="https://e.com")
        self.assertEqual(records, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("failed", errors[0].error)

    def test_missing_url_attribute_is_absent_not_phantom(self) -> None:
        """<a> without href must not resolve type=url to the page URL."""
        dom = HtmlDom()
        dom.feed("<article><a>no href</a></article>")
        schema = Schema(
            item_selector="article",
            fields=(Field(name="u", selector="a", attr="href", type="url"),),
        )
        records, errors = extract_items(dom, None, schema, base_url="https://page.test/list")
        self.assertEqual(records, [])  # value absent -> record empty -> dropped
        self.assertEqual(errors, [])  # absence is not an error

    def test_missing_url_attribute_required_drops_record(self) -> None:
        dom = HtmlDom()
        dom.feed("<article><a>no href</a></article>")
        schema = Schema(
            item_selector="article",
            fields=(Field(name="u", selector="a", attr="href", type="url", required=True),),
        )
        records, errors = extract_items(dom, None, schema, base_url="https://page.test/list")
        self.assertEqual(records, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("required", errors[0].error)

    def test_multi_missing_attr_yields_empty_list(self) -> None:
        dom = HtmlDom()
        dom.feed('<article><a class="dl">no href</a></article>')
        schema = Schema(
            item_selector="article",
            fields=(Field(name="pdf", selector="a.dl", attr="href", type="url", multi=True),),
        )
        records, _ = extract_items(dom, None, schema, base_url="https://e.com")
        self.assertEqual(records[0]["pdf"], [])

    def test_text_field_applies_normalize_and_type(self) -> None:
        schema = Schema(
            single=True,
            fields=(
                Field(name="v", selector=r"v\d+\.\d+", kind="text", normalize=("upper",)),
                Field(name="n", selector=r"(?<=build )\d+", kind="text", type="int"),
            ),
        )
        page = HtmlDom()
        page.feed("<p>version v2.6 build 42</p>")
        records, _ = extract_items(page, None, schema, base_url="https://e.com")
        self.assertEqual(records[0]["v"], "V2.6")
        self.assertEqual(records[0]["n"], 42)

    def test_pages_with_schema_counts_calls(self) -> None:
        stats = ExtractionStats()
        schema = Schema(item_selector="article", fields=(Field(name="t", selector="h2 a"),))
        extract_items(_dom(), None, schema, base_url="https://e.com", stats=stats)
        self.assertEqual(stats.pages_with_schema, 1)
        self.assertEqual(stats.as_dict()["pages_with_schema"], 1)


class SchemaFileTests(unittest.TestCase):
    def test_load_schema_from_file(self) -> None:
        import os
        import tempfile

        doc = {
            "item_selector": "article",
            "fields": [
                {"name": "title", "selector": "h2 a", "required": True},
                {"name": "score", "selector": "span.score", "type": "int"},
            ],
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            from freegem.crawler.runner import load_schema

            schema = load_schema(path)
            self.assertEqual(len(schema.fields), 2)
        finally:
            os.unlink(path)

    def test_load_schema_rejects_bad(self) -> None:
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"fields": [{"name": "x", "kind": "bogus"}]}, fh)
            from freegem.crawler.runner import load_schema

            with self.assertRaises(ValueError):
                load_schema(path)
        finally:
            os.unlink(path)

    def test_empty_schema_path_is_passthrough(self) -> None:
        from freegem.crawler.runner import load_schema

        schema = load_schema("")
        self.assertTrue(schema.single)
        self.assertEqual(schema.fields, ())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
