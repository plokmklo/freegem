"""The 2.13.0 evidence round: WACZ export and the Bloom seen-set.

Contract style like test_evidence_round: every feature is pinned by the
observable bytes it produces. A WACZ built here must survive its own
verifier, and a tampered WACZ must fail loudly -- the collection is only
worth producing if its honesty is checkable without trusting the writer.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from typing import Any
from unittest import mock

from freegem.config import BridgeConfig
from freegem.crawler.bloom import BloomSeenSet
from freegem.crawler.frontier import Frontier
from freegem.crawler.runner import CrawlSpec, ResourceLimits, run
from freegem.crawler.urls import Scope
from freegem.crawler.wacz import WaczError, build_wacz, surt, verify_wacz
from freegem.crawler.warc import WarcWriter
from freegem.tests.test_evidence_round import (
    _FAKE_HOST,
    _CounterUuids,
    _EvidenceSite,
    _fake_create_connection,
    _fake_resolver,
)
from freegem.web_scraper import clear_robots_cache

_FIXED_CLOCK = 1_700_000_000.0  # 2023-11-14T22:13:20Z, deterministic


def _write_sample_warc(path: str) -> None:
    """A small deterministic WARC: one win, one 403 wall, one DNS dead end."""
    uuids = _CounterUuids()
    with WarcWriter(
        path,
        software="freegem/test",
        robots_stance="never consulted",
        uuid_gen=uuids,
        clock=lambda: _FIXED_CLOCK,
    ) as writer:
        writer.warcinfo({"freegem-delay-ms": "0"})
        request_id = writer.request("http://site.test/a", headers={"User-Agent": "freegem/test"})
        writer.response(
            "http://site.test/a",
            status=200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            body=b"<html><body>hello evidence</body></html>",
            request_id=request_id,
        )
        writer.response(
            "http://site.test/wall",
            status=403,
            headers={"Content-Type": "text/plain"},
            body=b"denied!!!",
        )
        writer.metadata("http://site.test/dead", {"freegem-outcome-kind": "dns_failure"})


class SurtTests(unittest.TestCase):
    def test_reverses_host_labels_keeps_path(self) -> None:
        self.assertEqual(surt("https://www.Example.com/a"), "com,example,www)/a")

    def test_query_is_kept(self) -> None:
        self.assertEqual(surt("https://www.example.com/a?b=1"), "com,example,www)/a?b=1")

    def test_deep_host(self) -> None:
        self.assertEqual(surt("http://sub.deep.example.org/"), "org,example,deep,sub)/")

    def test_bare_host_gets_root_path(self) -> None:
        self.assertEqual(surt("https://example.com"), "com,example)/")

    def test_port_is_stripped_and_documented(self) -> None:
        self.assertEqual(surt("http://example.com:8080/x"), "com,example)/x")

    def test_hostless_uri_indexed_as_is(self) -> None:
        self.assertEqual(surt("file:///etc/passwd"), "file:///etc/passwd")


class BuildWaczTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.warc = os.path.join(self.tmp.name, "evidence.warc")
        self.wacz = os.path.join(self.tmp.name, "collection.wacz")
        _write_sample_warc(self.warc)
        self.summary = build_wacz(self.warc, self.wacz, software="freegem/test")

    def test_zip_layout_is_exactly_the_spec_files(self) -> None:
        with zipfile.ZipFile(self.wacz) as zf:
            self.assertEqual(
                sorted(zf.namelist()),
                sorted(
                    [
                        "datapackage.json",
                        "datapackage-digest.json",
                        "archive/data.warc",
                        "pages/pages.jsonl",
                        "index/index.cdx",
                    ]
                ),
            )

    def test_datapackage_manifest_binds_every_resource(self) -> None:
        with zipfile.ZipFile(self.wacz) as zf:
            datapackage = json.loads(zf.read("datapackage.json"))
            self.assertEqual(datapackage["profile"], "wacz")
            self.assertEqual(datapackage["wacz_version"], "1.2.0")
            self.assertEqual(datapackage["software"], "freegem/test")
            self.assertEqual(len(datapackage["resources"]), 3)
            for resource in datapackage["resources"]:
                blob = zf.read(resource["path"])
                self.assertEqual(
                    resource["hash"], "sha256:" + hashlib.sha256(blob).hexdigest(), resource["path"]
                )
                self.assertEqual(resource["bytes"], len(blob), resource["path"])

    def test_datapackage_digest_closes_the_chain(self) -> None:
        with zipfile.ZipFile(self.wacz) as zf:
            digest_entry = json.loads(zf.read("datapackage-digest.json"))
            dp_bytes = zf.read("datapackage.json")
        self.assertEqual(digest_entry["path"], "datapackage.json")
        self.assertEqual(digest_entry["hash"], "sha256:" + hashlib.sha256(dp_bytes).hexdigest())

    def test_archive_is_byte_identical_to_the_source_warc(self) -> None:
        with zipfile.ZipFile(self.wacz) as zf:
            self.assertEqual(zf.read("archive/data.warc"), open(self.warc, "rb").read())

    def test_pages_lists_wins_but_not_walls(self) -> None:
        with zipfile.ZipFile(self.wacz) as zf:
            lines = zf.read("pages/pages.jsonl").decode().splitlines()
        self.assertEqual(json.loads(lines[0])["format"], "json-pages-1.0")
        pages = [json.loads(line) for line in lines[1:]]
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["url"], "http://site.test/a")
        self.assertEqual(pages[0]["ts"], "2023-11-14T22:13:20Z")
        self.assertTrue(pages[0]["id"])

    def test_index_lists_wins_and_walls_with_exact_offsets(self) -> None:
        with zipfile.ZipFile(self.wacz) as zf:
            lines = zf.read("index/index.cdx").decode().splitlines()
        self.assertEqual(len(lines), 2)
        rows = [line.split(" ", 2) for line in lines]
        by_url = {json.loads(row[2])["url"]: json.loads(row[2]) for row in rows}
        self.assertEqual(sorted(by_url), ["http://site.test/a", "http://site.test/wall"])
        self.assertEqual(by_url["http://site.test/a"]["status"], 200)
        self.assertEqual(by_url["http://site.test/wall"]["status"], 403)
        self.assertEqual(by_url["http://site.test/wall"]["filename"], "archive/data.warc")
        for annotation in by_url.values():
            self.assertTrue(annotation["digest"].startswith("sha-256:"))
        surts = {row[0] for row in rows}
        self.assertEqual(surts, {"test,site)/a", "test,site)/wall"})
        timestamps = {row[1] for row in rows}
        self.assertEqual(timestamps, {"20231114221320"})

    def test_verifier_accepts_its_own_collection(self) -> None:
        report = verify_wacz(self.wacz)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["cdx_entries"], 2)
        self.assertEqual(report["pages"], 1)
        self.assertEqual(report["resources"], 3)

    def test_summary_counts(self) -> None:
        self.assertEqual(self.summary["pages"], 1)
        self.assertEqual(self.summary["cdx_entries"], 2)
        self.assertTrue(self.summary["bytes"] > 0)


class TamperedWaczTests(unittest.TestCase):
    """A WACZ whose claims no longer match its bytes must fail verification."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.warc = os.path.join(self.tmp.name, "evidence.warc")
        self.good = os.path.join(self.tmp.name, "good.wacz")
        _write_sample_warc(self.warc)
        build_wacz(self.warc, self.good)

    def _rebuild(self, target: str, mutate: Any) -> None:
        with zipfile.ZipFile(self.good) as zf:
            entries = {name: zf.read(name) for name in zf.namelist()}
        mutate(entries)
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, blob in entries.items():
                zf.writestr(name, blob)

    def test_stale_resource_hash_is_refused(self) -> None:
        path = os.path.join(self.tmp.name, "stale.wacz")

        def flip_pages(entries: dict[str, bytes]) -> None:
            entries["pages/pages.jsonl"] += b'{"url": "http://site.test/injected", "ts": "x"}\n'

        self._rebuild(path, flip_pages)
        report = verify_wacz(path)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("pages/pages.jsonl hash mismatch" in p for p in report["problems"]),
            report["problems"],
        )

    def test_forged_datapackage_digest_is_refused(self) -> None:
        path = os.path.join(self.tmp.name, "forged.wacz")

        def forge_digest(entries: dict[str, bytes]) -> None:
            entries["datapackage.json"] = entries["datapackage.json"].replace(
                b'"profile": "wacz"', b'"profile": "data-package"'
            )

        self._rebuild(path, forge_digest)
        report = verify_wacz(path)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("datapackage.json digest mismatch" in p for p in report["problems"]),
            report["problems"],
        )

    def test_cdx_offset_pointing_elsewhere_is_refused(self) -> None:
        path = os.path.join(self.tmp.name, "shifted.wacz")

        def shift_offset(entries: dict[str, bytes]) -> None:
            lines = entries["index/index.cdx"].decode().splitlines()
            patched = []
            for line in lines:
                surt_key, ts, annotation = line.split(" ", 2)
                obj = json.loads(annotation)
                obj["offset"] = obj["offset"] + 7
                patched.append(f"{surt_key} {ts} " + json.dumps(obj, sort_keys=True))
            entries["index/index.cdx"] = ("\n".join(patched) + "\n").encode()

        self._rebuild(path, shift_offset)
        report = verify_wacz(path)
        self.assertFalse(report["ok"])
        self.assertTrue(
            any("not a WARC record" in p or "not at offset" in p for p in report["problems"]),
            report["problems"],
        )

    def test_missing_member_is_refused(self) -> None:
        path = os.path.join(self.tmp.name, "hole.wacz")
        with zipfile.ZipFile(self.good) as zf:
            entries = {name: zf.read(name) for name in zf.namelist() if name != "index/index.cdx"}
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, blob in entries.items():
                zf.writestr(name, blob)
        report = verify_wacz(path)
        self.assertFalse(report["ok"])
        self.assertTrue(any("missing index/index.cdx" in p for p in report["problems"]))


class EmptyWarcTests(unittest.TestCase):
    def test_warcinfo_only_warc_is_a_caller_mistake(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        warc = os.path.join(tmp.name, "empty.warc")
        with WarcWriter(
            warc,
            uuid_gen=_CounterUuids(),
            clock=lambda: _FIXED_CLOCK,
        ) as writer:
            writer.warcinfo({})
        with self.assertRaises(WaczError):
            build_wacz(warc, os.path.join(tmp.name, "out.wacz"))


class BloomTests(unittest.TestCase):
    def test_second_add_is_probably_seen(self) -> None:
        bloom = BloomSeenSet(capacity=1024)
        self.assertTrue(bloom.add_if_absent("http://a.test/1"))
        self.assertFalse(bloom.add_if_absent("http://a.test/1"))
        self.assertTrue("http://a.test/1" in bloom)
        self.assertFalse("http://a.test/2" in bloom)
        self.assertEqual(len(bloom), 1)

    def test_no_false_negatives(self) -> None:
        bloom = BloomSeenSet(capacity=5000)
        urls = [f"http://h.test/{i}" for i in range(2000)]
        for url in urls:
            bloom.add_if_absent(url)
        for url in urls:
            self.assertTrue(url in bloom)

    def test_false_positive_rate_stays_near_design(self) -> None:
        bloom = BloomSeenSet(capacity=5000, fp_rate=0.001)
        for i in range(5000):
            bloom.add_if_absent(f"http://seen.test/{i}")
        seen = ["http://unseen.test/x" for x in range(2000)]  # distinct probes
        false_positives = sum(1 for url in seen if url in bloom)
        self.assertLessEqual(false_positives, 20, f"fp {false_positives}/2000 at p=0.001")

    def test_stats_report_design_and_load(self) -> None:
        bloom = BloomSeenSet(capacity=1024, fp_rate=0.01)
        bloom.add_if_absent("http://a.test/")
        stats = bloom.stats()
        self.assertEqual(stats["capacity"], 1024)
        self.assertEqual(stats["fp_rate"], 0.01)
        self.assertEqual(stats["items"], 1)
        self.assertGreaterEqual(stats["hashes"], 1)
        self.assertGreater(stats["memory_bytes"], 0)

    def test_invalid_design_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            BloomSeenSet(capacity=0)
        with self.assertRaises(ValueError):
            BloomSeenSet(capacity=100, fp_rate=0.0)
        with self.assertRaises(ValueError):
            BloomSeenSet(capacity=100, fp_rate=1.0)


class _RunnerSite(_EvidenceSite):
    """Same routes as the evidence site; shared harness, new contract."""


class RunnerWaczEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_robots_cache()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = self.tmp.name
        resolver = mock.patch("freegem.ssrf.socket.getaddrinfo", side_effect=_fake_resolver)
        resolver.start()
        self.addCleanup(resolver.stop)
        dial = mock.patch(
            "freegem.ssrf.socket.create_connection", side_effect=_fake_create_connection
        )
        dial.start()
        self.addCleanup(dial.stop)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _RunnerSite)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        import threading

        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://{_FAKE_HOST}:{self.server.server_port}"
        self.cfg = BridgeConfig(guard=True, log_requests=False)
        self.events: list[dict[str, Any]] = []

    def _crawl(self, **kwargs: Any) -> Any:
        from freegem.crawler.runner import CrawlSpec as Spec

        self.state = os.path.join(self.dir, "state.db")
        self.warc_path = os.path.join(self.dir, "evidence.warc")
        self.wacz_path = os.path.join(self.dir, "collection.wacz")
        spec_kwargs: dict[str, Any] = {"warc_path": self.warc_path, "wacz_path": self.wacz_path}
        spec_kwargs.update(kwargs)
        spec = Spec(
            seeds=(f"{self.base}/",),
            scope=Scope(max_depth=1, max_pages=10, deny_paths=("/404",)),
            limits=ResourceLimits(timeout_sec=5, max_retries=0),
            delay_ms=0,
            concurrency=1,
            resilient=False,
            state_path=self.state,
            **spec_kwargs,
        )
        frontier = Frontier(self.state)
        try:
            return run(self.cfg, spec, frontier, on_event=self.events.append)
        finally:
            frontier.close()

    def _event(self, name: str) -> dict[str, Any] | None:
        return next((e for e in self.events if e.get("event") == name), None)

    def test_crawl_packages_a_verifiable_wacz(self) -> None:
        counters = self._crawl()
        self.assertGreater(counters.pages_ok, 0)
        wacz_event = self._event("wacz")
        assert wacz_event is not None
        self.assertGreater(wacz_event["pages"], 0)
        self.assertEqual(wacz_event["path"], self.wacz_path)
        report = verify_wacz(self.wacz_path)
        self.assertTrue(report["ok"], report["problems"])
        self.assertGreaterEqual(report["cdx_entries"], report["pages"])
        with zipfile.ZipFile(self.wacz_path) as zf:
            self.assertIn("freegem/", json.loads(zf.read("datapackage.json"))["software"])

    def test_wacz_requires_warc(self) -> None:
        with self.assertRaises(ValueError):
            self._crawl(warc_path="", wacz_path=os.path.join(self.dir, "solo.wacz"))

    def test_bloom_flag_keeps_the_crawl_exact_on_a_small_site(self) -> None:
        self.events.clear()
        with_bloom = self._crawl(bloom_seen=True)
        self.assertIsNotNone(self._event("bloom_enabled"))
        self.assertIsNotNone(self._event("bloom_stats"))
        bloom_stats = self._event("bloom_stats")
        assert bloom_stats is not None
        self.assertGreaterEqual(bloom_stats["items"], 1)  # discovery fed the filter
        # Same site, no bloom: identical crawl outcome (the filter must not
        # change what a small crawl finds).
        self.events.clear()
        dir2 = tempfile.TemporaryDirectory()
        self.addCleanup(dir2.cleanup)
        self.dir = dir2.name
        without_bloom = self._crawl()
        self.assertEqual(with_bloom.pages_ok, without_bloom.pages_ok)
        self.assertEqual(with_bloom.links_queued, without_bloom.links_queued)


class RunnerWaczErrorTests(unittest.TestCase):
    def test_seedless_crawl_reports_wacz_error_instead_of_crashing(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        events: list[dict[str, Any]] = []
        cfg = BridgeConfig(guard=True, log_requests=False)
        spec = CrawlSpec(
            seeds=("::not a url::",),
            scope=Scope(max_depth=0, max_pages=1),
            limits=ResourceLimits(timeout_sec=1, max_retries=0),
            delay_ms=0,
            concurrency=1,
            resilient=False,
            state_path=os.path.join(tmp.name, "state.db"),
            warc_path=os.path.join(tmp.name, "evidence.warc"),
            wacz_path=os.path.join(tmp.name, "collection.wacz"),
        )
        frontier = Frontier(spec.state_path)
        try:
            counters = run(cfg, spec, frontier, on_event=events.append)
        finally:
            frontier.close()
        self.assertEqual(counters.pages_ok, 0)
        wacz_error = next((e for e in events if e.get("event") == "wacz_error"), None)
        assert wacz_error is not None
        self.assertIn("no response records", wacz_error["error"])
        self.assertFalse(os.path.exists(os.path.join(tmp.name, "collection.wacz")))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
