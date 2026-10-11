"""Output writers: JSONL / JSON / CSV, redaction, atomic rename."""

from __future__ import annotations

import csv
import json
import os
import tempfile
import unittest

from freegem.crawler.output import make_writer, redact_secrets_json


class WriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "out")

    def read(self, suffix: str = "") -> str:
        with open(self.path + suffix, encoding="utf-8") as fh:
            return fh.read()

    def test_jsonl_writes_and_renames(self) -> None:
        writer = make_writer(self.path, "jsonl")
        writer.write({"url": "https://e.com", "title": "ok"})
        writer.write({"url": "https://e.com/2", "title": "café ☕"})
        writer.close()
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        lines = self.read().strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1])["title"], "café ☕")

    def test_jsonl_appends(self) -> None:
        w1 = make_writer(self.path, "jsonl", append=True)
        w1.write({"n": 1})
        w1.close()
        w2 = make_writer(self.path, "jsonl", append=True)
        w2.write({"n": 2})
        w2.close()
        lines = self.read().strip().splitlines()
        self.assertEqual([json.loads(line)["n"] for line in lines], [1, 2])

    def test_json_document(self) -> None:
        writer = make_writer(self.path, "json")
        writer.write({"a": 1})
        writer.write({"a": 2})
        writer.close()
        data = json.loads(self.read())
        self.assertEqual([d["a"] for d in data], [1, 2])

    def test_json_rejects_append(self) -> None:
        with self.assertRaises(ValueError):
            make_writer(self.path, "json", append=True)

    def test_csv_columns_and_flatten(self) -> None:
        writer = make_writer(self.path, "csv", ["title", "tags"])
        writer.write(
            {
                "title": "one\ntwo",
                "tags": ["a", "b"],
                "url": "https://e.com",
                "fetched_at": "t",
                "content_hash": "h",
                "via": "crawl",
                "_index": 0,
            }
        )
        writer.close()
        with open(self.path, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(
            rows[0], ["title", "tags", "url", "fetched_at", "content_hash", "via", "_index"]
        )
        self.assertEqual(rows[1][0], "one two")
        self.assertEqual(rows[1][1], "a;b")

    def test_secrets_redacted_in_output(self) -> None:
        writer = make_writer(self.path, "jsonl")
        writer.write({"config": 'api_key = "a1b2c3d4e5f6g7h8"'})
        writer.close()
        content = self.read()
        self.assertIn("[REDACTED:bearer]", content)
        self.assertNotIn("a1b2c3d4e5f6g7h8", content)

    def test_unknown_format_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_writer(self.path, "parquet")


class RedactJsonTests(unittest.TestCase):
    def test_nested_structures(self) -> None:
        item = {
            "note": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
            "nested": {"secret": "password = hunter2hunter2"},
            "list": ["x", "Bearer a1b2c3d4e5f6g7h8i9j0"],
            "num": 5,
        }
        out = redact_secrets_json(item)
        self.assertIn("[REDACTED:", out["note"])
        self.assertIn("[REDACTED:", out["nested"]["secret"])
        self.assertIn("[REDACTED:", out["list"][1])
        self.assertEqual(out["num"], 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
