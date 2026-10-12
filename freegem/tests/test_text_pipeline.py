"""Tests for the text pipeline (chunking, relevance, summary, telemetry)."""

from __future__ import annotations

from freegem.text_pipeline import (
    Chunk,
    CostQualityTelemetry,
    build_idf,
    chunk_text,
    estimate_tokens,
    extractive_summary,
    relevance_score,
    select_relevant,
    split_sentences,
)

LONG_DOC = (
    "Freegem is a local bridge that speaks both OpenAI-compatible and Google-native "
    " dialects over a Gemini web session.\n\n"
    "The crawler respects per-host politeness delays and never consults robots.txt "
    " because the operators of this project decided that reading pages is what the "
    " web is for.\n\n"
    "Escalation starts with plain HTTP, then retries through the resilient ladder "
    " with shared cookies, then falls back to a browser when the page needs "
    " JavaScript to render its main content.\n\n"
) * 4


def test_chunk_text_short_input_single_chunk() -> None:
    chunks = chunk_text("a short page body")
    assert len(chunks) == 1
    assert chunks[0].index == 0
    assert chunks[0].size == len("a short page body")


def test_chunk_text_empty_input() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n  ") == []


def test_chunk_text_respects_paragraph_boundaries() -> None:
    doc = "para one.\n\n" + "x" * 500 + "\n\n" + "y" * 500 + "\n\n" + "z" * 500
    chunks = chunk_text(doc, max_chars=600, overlap=0)
    assert len(chunks) >= 3
    # every chunk begins exactly at a paragraph start (doc head or after \n\n)
    para_starts = {0}
    i = doc.find("\n\n")
    while i != -1:
        para_starts.add(i + 2)
        i = doc.find("\n\n", i + 1)
    for c in chunks:
        assert doc.index(c.text) in para_starts


def test_chunk_text_overlaps_neighbors() -> None:
    doc = ("sentence one is here. " * 40) + "\n\n" + ("sentence two is there. " * 40)
    chunks = chunk_text(doc, max_chars=300, overlap=60)
    assert len(chunks) >= 2
    import itertools

    for prev, nxt in itertools.pairwise(chunks):
        tail = prev.text[-60:].strip()
        head = nxt.text[:80].strip()
        assert any(w in head for w in tail.split()[-3:]) or overlap_ok(prev, nxt)


def overlap_ok(prev: Chunk, nxt: Chunk) -> bool:
    # hard-cut chunks must still advance monotonically
    return nxt.start_char < prev.end_char


def test_chunk_indices_are_monotonic() -> None:
    chunks = chunk_text(LONG_DOC, max_chars=400, overlap=80)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert c.size <= 420  # small slack for boundary rounding


def test_relevance_scores_match_over_miss() -> None:
    q = "politeness delays crawler"
    hit = "the crawler honors per-host politeness delays between requests"
    miss = "the bakery sells sourdough and croissants every morning"
    assert relevance_score(q, hit) > relevance_score(q, miss)
    assert relevance_score(q, miss) == 0.0
    assert relevance_score("", hit) == 0.0


def test_relevance_uses_idf_to_downweight_common_terms() -> None:
    idf = {"the": 0.1, "crawler": 3.0}
    a = "the crawler crawls the pages the crawler wants"
    b = "the the the the the crawler"
    assert relevance_score("crawler", a, idf=idf) >= relevance_score("crawler", b, idf=idf)


def test_select_relevant_returns_document_order_top_k() -> None:
    chunks = [
        Chunk("cooking pasta with tomatoes", 0, 0, 26),
        Chunk("crawler politeness delays per host", 1, 26, 60),
        Chunk("unrelated weather report", 2, 60, 84),
        Chunk("the crawler sleeps between hits politely", 3, 84, 124),
    ]
    sel = select_relevant(chunks, "crawler politeness", k=2)
    assert [c.index for c in sel] == [1, 3]  # document order, not score order
    assert len(sel) == 2
    assert select_relevant(chunks, "quantum physics", k=2) == []


def test_select_relevant_with_idf_from_sample() -> None:
    docs = ["crawler politeness delays", "crawler request budgets", "cooking pasta tonight"]
    idf = build_idf(docs)
    # 'crawler' appears in 2 of 3 docs (less discriminative) than 'politeness' in 1
    assert idf["politeness"] > idf["crawler"]


def test_split_sentences_basic() -> None:
    s = split_sentences("First sentence here. Second one follows! A question? Yes.")
    assert len(s) == 4
    assert s[0] == "First sentence here."


def test_extractive_summary_picks_real_sentences_in_order() -> None:
    doc = (
        "The bridge runs locally on loopback and speaks two API dialects. "
        "It has no cloud dependency and keeps your session cookies on disk. "
        "Bananas are yellow fruit that grow in bunches on tall plants. "
        "The crawler politeness layer spaces requests per host automatically. "
    )
    summary = extractive_summary(doc, max_sentences=2)
    assert summary
    # every summary sentence must literally exist in the input
    for sentence in summary.split(". "):
        assert sentence.strip(". ") in doc
    # bridge-related sentence outranks the banana one
    assert "bridge" in summary
    assert "Bananas" not in summary


def test_extractive_summary_deterministic_and_bounded() -> None:
    doc = ("Alpha sentence explains the first mechanism in detail here. " * 3) + (
        "Beta sentence covers the second mechanism with different words. " * 3
    )
    a = extractive_summary(doc, max_sentences=3)
    b = extractive_summary(doc, max_sentences=3)
    assert a == b
    assert a.count(".") <= 3


def test_estimate_tokens_matches_documented_len_over_four() -> None:
    assert estimate_tokens("x" * 400) == 100
    assert estimate_tokens("") == 0


def test_telemetry_snapshot_counts_and_reduction() -> None:
    t = CostQualityTelemetry()
    t.note_doc(10_000, truncated=False)
    t.note_doc(5_000, truncated=True)
    t.note_chunks(made=9, selected=3, chars_out=1_500)
    t.note_summary()
    snap = t.snapshot()
    assert snap["docs_in"] == 2
    assert snap["chars_in"] == 15_000
    assert snap["docs_truncated"] == 1
    assert snap["chunks_made"] == 9
    assert snap["chunks_selected"] == 3
    assert snap["tokens_in_est"] == 15_000 // 4
    assert snap["tokens_out_est"] == 1_500 // 4
    assert snap["reduction_ratio"] == round(1_500 / 15_000, 4)


def test_telemetry_zero_state() -> None:
    snap = CostQualityTelemetry().snapshot()
    assert snap["reduction_ratio"] == 0.0
    assert snap["docs_in"] == 0
