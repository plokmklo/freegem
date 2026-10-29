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

    def test_all_three_writers_redact_secrets(self) -> None:
        """Module contract: EVERY record passes through redact_secrets before
        hitting disk, whichever format it lands in (issue #43 -- the CSV
        writer skipped it, so crawled secrets landed verbatim in exports)."""
        secret = {
            "url": "https://e.com/page",
            "title": "leaky page",
            "config": 'api_key = "a1b2c3d4e5f6g7h8"',
            "note": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
            "tags": ["x", "Bearer a1b2c3d4e5f6g7h8i9j0"],
        }
        raw_fragments = (
            "a1b2c3d4e5f6g7h8",
            "A1b2C3d4E5f6G7h8I9j0K1l2M3n4",
            "a1b2c3d4e5f6g7h8i9j0",
        )
        for fmt in ("jsonl", "json", "csv"):
            path = self.path + "." + fmt
            writer = make_writer(path, fmt, ["title", "config", "note", "tags"])
            writer.write(dict(secret))
            writer.close()
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            for fragment in raw_fragments:
                self.assertNotIn(fragment, content, f"{fmt} leaked a raw secret")
            self.assertIn("[REDACTED:", content, f"{fmt} did not redact")

    def test_unknown_format_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_writer(self.path, "parquet")

    def test_csv_append_writes_header_for_missing_file(self) -> None:
        """Resume onto a file that never existed must not produce a
        headerless CSV."""
        writer = make_writer(self.path, "csv", ["title"], append=True)
        writer.write({"title": "t", "url": "https://e.com"})
        writer.close()
        with open(self.path, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[0][0], "title")
        self.assertEqual(rows[1][0], "t")

    def test_append_repairs_truncated_tail(self) -> None:
        """A killed run can leave a partial last line; the resumed record
        must land on its own line, not concatenate onto the fragment."""
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"n": 1')  # no trailing newline: killed mid-write
        writer = make_writer(self.path, "jsonl", append=True)
        writer.write({"n": 2})
        writer.close()
        lines = self.read().strip().splitlines()
        self.assertEqual(len(lines), 2)  # the fragment stays isolated
        self.assertEqual(json.loads(lines[-1])["n"], 2)

    def test_csv_append_repairs_truncated_tail(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("partial")  # no trailing newline
        writer = make_writer(self.path, "csv", ["n"], append=True)
        writer.write({"n": 2})
        writer.close()
        with open(self.path, encoding="utf-8", newline="") as fh:
            rows = list(csv.reader(fh))
        self.assertEqual(rows[-1][0], "2")


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

    def test_redaction_reaches_nested_lists_and_tuples(self) -> None:
        """Every record passes through redact_secrets -- at ANY nesting
        depth, whichever container holds the secret."""
        secret = "Bearer a1b2c3d4e5f6g7h8i9j0"
        out = redact_secrets_json(
            {
                "deep": [[["x", secret]]],
                "pair": ("ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4", 7),
            }
        )
        self.assertIn("[REDACTED:", out["deep"][0][0][1])
        self.assertNotIn("a1b2c3d4e5f6g7h8i9j0", str(out))
        self.assertIn("[REDACTED:", out["pair"][0])
        self.assertEqual(out["pair"][1], 7)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
