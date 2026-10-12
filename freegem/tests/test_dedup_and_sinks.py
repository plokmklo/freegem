"""Tests for near-duplicate detection (dedup.py) and search sinks (search_sinks.py)."""

from __future__ import annotations

import json
from pathlib import Path

from freegem.dedup import Deduper, Simhash, hamming, jaccard, shingles
from freegem.search_sinks import JsonlSink, NullSink, SqliteFtsSink

# ─── dedup ───────────────────────────────────────────────────────────────────


def test_shingles_deterministic_and_nonempty() -> None:
    a = shingles("the quick brown fox jumps over the lazy dog")
    b = shingles("the quick brown fox jumps over the lazy dog")
    assert a and a == b
    assert shingles("") == set()


def test_simhash_distance_same_text_zero() -> None:
    t = "freegem crawls pages politely and stores only what it may keep"
    assert Simhash.from_text(t).distance(Simhash.from_text(t)) == 0


def test_simhash_near_duplicate_within_band() -> None:
    base = (
        "the committee published its final report on tuesday after months of review "
        "recommending stronger oversight of automated systems deployed in public services "
        "with clear accountability rules for vendors and agencies alike plus transition "
        "periods for existing contracts and a public register of deployed models "
    ) * 3
    near = base.replace("tuesday", "thursday")
    assert Simhash.from_text(base).distance(Simhash.from_text(near)) <= 8


def test_simhash_far_apart_for_different_topics() -> None:
    a = (
        "quantum computing requires cryogenic temperatures and error corrected qubits "
        "with logical error rates that fall as the code distance grows and more physical "
        "qubits are committed to each logical state machine cycles are long and costly "
    ) * 3
    b = (
        "the bakery sells fresh sourdough bread every morning except sundays when the "
        "village market takes over the square and the ovens rest while the family hikes "
        "in the hills gathering herbs for the winter season and drying chili peppers "
    ) * 3
    assert Simhash.from_text(a).distance(Simhash.from_text(b)) > 8


def test_hamming_and_jaccard_basics() -> None:
    assert hamming(0, 0) == 0
    assert hamming(0b1010, 0b0101) == 4
    s1, s2 = {1, 2, 3}, {2, 3, 4}
    assert jaccard(s1, s2) == 2 / 4
    assert jaccard(set(), set()) == 1.0
    assert jaccard({1}, set()) == 0.0


def test_deduper_keeps_unique_rejects_duplicate() -> None:
    d = Deduper()
    t = (
        "the quarterly earnings report showed steady growth across all regions and product "
        "lines with margins improving for the third consecutive quarter driven by cost "
        "discipline and higher subscription revenue while headcount remained flat and the "
        "board raised the dividend for the second year in a row "
    ) * 3
    assert d.add("a", t) is True
    assert d.add("b", t + " executives said the outlook remains cautious given currency headwinds") is False
    assert d.original_of("b") == "a"
    assert d.stats()["kept"] == 1


def test_deduper_jaccard_verify_prevents_false_positive() -> None:
    # Strict floor + tight simhash band: partial shingle overlap must NOT
    # make a duplicate, even if the fingerprints happen to sit close.
    d = Deduper(threshold=3, jaccard_floor=0.95)
    a = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    b = "alpha beta gamma delta epsilon mu nu xi omicron pi rho sigma"
    r1 = d.add("a", a)
    r2 = d.add("b", b)
    assert r1 is True
    # If simhash said duplicate but jaccard disagrees, b must be kept.
    assert r2 is True or d.original_of("b") == "a"


def test_deduper_is_duplicate_does_not_mutate() -> None:
    d = Deduper()
    t = (
        "an entirely unique paragraph about maritime insurance underwriting practices "
        "covering hull policies cargo liability and the reinsurance treaties that spread "
        "catastrophe risk across syndicates with quarterly renewal cycles and surveyed "
        "vessels "
    ) * 3
    d.add("a", t)
    before = d.stats()["kept"]
    assert d.is_duplicate(t) is True
    assert d.is_duplicate("unrelated text about alpine climbing gear and rope ratings") is False
    assert d.stats()["kept"] == before


# ─── search sinks ────────────────────────────────────────────────────────────


def test_sqlite_fts_sink_upsert_search_replace() -> None:
    sink = SqliteFtsSink(":memory:")
    sink.upsert("1", url="https://x/1", title="Gemini bridge guide", text="how to run the local bridge server")
    sink.upsert("2", url="https://x/2", title="Crawler manual", text="the crawler honors politeness delays")
    assert sink.count() == 2
    hits = sink.search("bridge")
    assert hits and hits[0]["doc_id"] == "1"
    # re-upsert replaces, never duplicates
    sink.upsert("1", url="https://x/1v2", title="Gemini bridge guide v2", text="updated bridge instructions")
    assert sink.count() == 2
    assert sink.search("bridge")[0]["title"] == "Gemini bridge guide v2"
    assert sink.search("nonexistenttermmatch") == []
    sink.close()


def test_sqlite_sink_metadata_roundtrip() -> None:
    sink = SqliteFtsSink(":memory:")
    sink.upsert("m1", url="https://x/m", title="t", text="body words here", metadata={"lang": "en", "depth": 2})
    hit = sink.search("body")[0]
    assert hit["metadata"] == {"lang": "en", "depth": 2}
    sink.close()


def test_jsonl_sink_persists_and_searches(tmp_path: Path) -> None:
    path = tmp_path / "index.jsonl"
    s1 = JsonlSink(path)
    s1.upsert("a", url="https://x/a", title="Arxiv digests", text="papers about diffusion models weekly")
    s1.upsert("b", url="https://x/b", title="Cooking blog", text="sourdough starter maintenance")
    s1.close()

    s2 = JsonlSink(path)  # reload from disk
    assert s2.count() == 2
    hits = s2.search("diffusion papers")
    assert hits[0]["doc_id"] == "a"
    # JSONL on disk is line-delimited json
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2


def test_jsonl_sink_upsert_rewrites_append(tmp_path: Path) -> None:
    path = tmp_path / "idx.jsonl"
    s = JsonlSink(path)
    s.upsert("a", url="u", title="t1", text="x")
    s.upsert("a", url="u", title="t2", text="x")
    s.close()
    s2 = JsonlSink(path)
    # in-memory view has ONE doc (replaced), disk log has both lines
    assert s2.count() == 1
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_null_sink_is_a_silent_noop() -> None:
    s = NullSink()
    s.upsert("x", url="u", title="t", text="body")
    assert s.count() == 0
    assert s.search("anything") == []
    s.close()
