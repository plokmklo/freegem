"""Hardening: redaction, safe XML, decoding, DOM caps."""

from __future__ import annotations

import unittest

from freegem.crawler.hardening import (
    HtmlDom,
    ResourceLimits,
    UnsafeXMLError,
    decode_body,
    parse_xml,
    redact_secrets,
)


class RedactTests(unittest.TestCase):
    def test_common_tokens_redacted(self) -> None:
        cases = {
            "aws-access-key": "key AKIAIOSFODNN7EXAMPLE in config",
            "github-token": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
            "openai-style-key": "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0",
            "google-api-key": "AIza" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4o5p6q7r",
        }
        for kind, text in cases.items():
            with self.subTest(kind=kind):
                out = redact_secrets(f"page says {text} ok")
                self.assertIn(f"[REDACTED:{kind}]", out)
                self.assertNotIn(text, out)

    def test_bearer_assignment_redacted(self) -> None:
        out = redact_secrets('api_key = "a1b2c3d4e5f6g7h8"')
        self.assertIn("[REDACTED:bearer]", out)
        self.assertNotIn("a1b2c3d4e5f6g7h8", out)

    def test_query_secret_redacted(self) -> None:
        out = redact_secrets("https://e.com/x?token=supersecretvalue123&id=2")
        self.assertNotIn("token=supersecretvalue123", out)
        self.assertIn("[REDACTED:", out)
        self.assertIn("&id=2", out)

    def test_private_key_block_redacted(self) -> None:
        blob = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----"
        out = redact_secrets(blob)
        self.assertIn("[REDACTED:private-key-block]", out)
        self.assertNotIn("MIIEow", out)

    def test_ordinary_text_untouched(self) -> None:
        text = "The quick brown fox jumps over the lazy dog, 42 times."
        self.assertEqual(redact_secrets(text), text)


class SafeXmlTests(unittest.TestCase):
    def test_plain_sitemap_parses(self) -> None:
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://e.com/a</loc></url>"
            "<url><loc>https://e.com/b</loc></url></urlset>"
        )
        root = parse_xml(xml)
        locs = [el.text for el in root.iter() if el.tag.endswith("loc")]
        self.assertEqual(locs, ["https://e.com/a", "https://e.com/b"])

    def test_doctype_refused(self) -> None:
        with self.assertRaises(UnsafeXMLError):
            parse_xml('<!DOCTYPE sitemap SYSTEM "http://evil/x.dtd"><sitemap/>')

    def test_entity_declaration_refused(self) -> None:
        with self.assertRaises(UnsafeXMLError):
            parse_xml('<!ENTITY a "b"><r/>')

    def test_xml_stylesheet_refused(self) -> None:
        with self.assertRaises(UnsafeXMLError):
            parse_xml('<?xml-stylesheet href="e"?><r/>')

    def test_unparseable_refused(self) -> None:
        with self.assertRaises(UnsafeXMLError):
            parse_xml("<a><b></a>")

    def test_bytes_and_size_cap(self) -> None:
        root = parse_xml(b"<r/>")
        self.assertEqual(root.tag, "r")
        with self.assertRaises(UnsafeXMLError):
            parse_xml(b"<r/>" + b" " * 100, max_bytes=5)


class DecodeTests(unittest.TestCase):
    def test_header_charset_wins(self) -> None:
        raw = "café".encode("gbk")
        self.assertEqual(decode_body(raw, "text/html; charset=gbk"), "café")

    def test_unknown_header_charset_falls_through(self) -> None:
        raw = "café".encode()
        self.assertEqual(decode_body(raw, "text/html; charset=utf-88"), "café")

    def test_bom_detected(self) -> None:
        raw = "café".encode("utf-16")
        self.assertEqual(decode_body(raw, "text/html"), "café")

    def test_meta_charset_sniffed(self) -> None:
        raw = ('<html><head><meta charset="shift_jis"><title>日本語</title></head></html>').encode(
            "shift_jis"
        )
        self.assertIn("日本語", decode_body(raw, "text/html"))

    def test_meta_bare_charset_form(self) -> None:
        raw = '<meta http-equiv="Content-Type" content="text/html; charset=gb2312">'.encode("gbk")
        self.assertIn("中文", decode_body(raw + "中文".encode("gbk"), "text/html"))

    def test_fallback_is_utf8_replace(self) -> None:
        self.assertIn("\ufffd", decode_body(b"\xff\xfe\xfd", "text/html"))


class DomTests(unittest.TestCase):
    def test_basic_tree_and_text(self) -> None:
        dom = HtmlDom()
        dom.feed("<div id='a' class='x y'><p>hello <b>world</b></p><p>second</p></div>")
        nodes = [n for n in dom.root.iter() if n.tag == "p"]
        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[0].text, "hello world")
        self.assertIn("x", nodes[0].parent.classes() if nodes[0].parent else set())

    def test_void_elements_do_not_nest(self) -> None:
        dom = HtmlDom()
        dom.feed("<div><br><input type='checkbox'><span>ok</span></div>")
        spans = [n for n in dom.root.iter() if n.tag == "span"]
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0].parent.tag if spans[0].parent else "", "div")

    def test_implied_close_li_p(self) -> None:
        dom = HtmlDom()
        dom.feed("<ul><li>one<li>two<li>three</ul>")
        items = [n for n in dom.root.iter() if n.tag == "li"]
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertEqual(item.parent.tag if item.parent else "", "ul")

    def test_stray_end_tag_ignored(self) -> None:
        dom = HtmlDom()
        dom.feed("<div><span></div></span><p>x</p>")
        ps = [n for n in dom.root.iter() if n.tag == "p"]
        self.assertEqual(len(ps), 1)

    def test_node_cap_keeps_prefix(self) -> None:
        dom = HtmlDom(max_nodes=10)
        dom.feed("".join(f"<div>{i}</div>" for i in range(100)))
        self.assertTrue(dom.truncated)
        self.assertLessEqual(dom.node_count, 11)

    def test_depth_cap(self) -> None:
        dom = HtmlDom(max_depth=10)
        dom.feed("<div>" * 50 + "deep" + "</div>" * 50)
        self.assertTrue(dom.truncated)

    def test_control_chars_stripped(self) -> None:
        dom = HtmlDom()
        dom.feed("<p>a\x00b\x07c</p>")
        ps = [n for n in dom.root.iter() if n.tag == "p"]
        self.assertEqual(ps[0].text, "abc")


class LimitsTests(unittest.TestCase):
    def test_defaults_are_sane(self) -> None:
        limits = ResourceLimits()
        self.assertEqual(limits.timeout_sec, 20.0)
        self.assertEqual(limits.max_bytes, 5 * 1024 * 1024)
        self.assertGreater(limits.max_dom_nodes, 1000)
        self.assertGreaterEqual(limits.max_retries, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
