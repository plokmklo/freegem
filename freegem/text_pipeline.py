"""Text pipeline: chunking, relevance selection, extractive NLP, telemetry.

Sits between extraction (crawler/extract.py) and consumption (LLM prompt,
index sink, research answer). The problem it owns: a 100 MB page is useless
to a model with a 32k window, and "the first N chars" is not selection, it
is amputation.

Pieces, all stdlib-only:

* :func:`chunk_text` -- paragraph-aware splitting with overlap. Chunks break
  at paragraph (then sentence, then hard) boundaries, and overlap keeps a
  fact straddling a boundary alive in both neighbors.
* :func:`relevance_score` / :func:`select_relevant` -- idf-weighted term
  overlap between a query and each chunk. Pure ranking, no model calls:
  the point is to spend the prompt budget on the right 5% of the page.
* :func:`extractive_summary` -- frequency-scored sentence selection, the
  "optional NLP" that needs no weights, no downloads, and explains itself
  (each summary sentence is a real sentence from the input).
* :class:`CostQualityTelemetry` -- per-run counters (chars in/out, chunks,
  selected, truncated docs, estimated tokens) so "how much did this page
  cost us" is a dict, not a guess. Mirrors RunMetrics conventions.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Chunk",
    "CostQualityTelemetry",
    "chunk_text",
    "estimate_tokens",
    "extractive_summary",
    "relevance_score",
    "select_relevant",
    "split_sentences",
]

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'\u00c0-\u024f])", re.UNICODE)
_DEFAULT_MAX_CHARS = 1_200
_DEFAULT_OVERLAP = 150


@dataclass(frozen=True)
class Chunk:
    """One chunk of a larger document."""

    text: str
    index: int
    start_char: int
    end_char: int

    @property
    def size(self) -> int:
        return len(self.text)


def split_sentences(text: str) -> list[str]:
    """Sentence split good enough for ranking (not for linguistics papers)."""
    parts = _SENT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def chunk_text(
    text: str,
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
    overlap: int = _DEFAULT_OVERLAP,
) -> list[Chunk]:
    """Split into overlapping, boundary-respecting chunks.

    Order of preference for a break: paragraph end > sentence end > hard cut
    at ``max_chars``. ``overlap`` characters of the previous chunk are
    repeated in the next (bounded to < max/2 so degenerate settings cannot
    produce non-advancing chunks). Empty input yields no chunks; a text
    smaller than ``max_chars`` yields exactly one.
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [Chunk(text=text, index=0, start_char=0, end_char=len(text))]

    overlap = min(overlap, max_chars // 2)
    chunks: list[Chunk] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            # prefer the last paragraph boundary inside the window
            cut = text.rfind("\n\n", start, end)
            if cut == -1 or cut <= start + max_chars // 3:
                cut = text.rfind("\n", start, end)
            if cut == -1 or cut <= start + max_chars // 3:
                last = None
                for match in _SENT_RE.finditer(text, start, end):
                    last = match
                cut = last.end() if last else -1
            if cut > start + max_chars // 3:
                end = cut
            # back off from a mid-word cut
            while end < len(text) and end > start and text[end - 1].isalnum() and text[end].isalnum():
                end -= 1
        piece = text[start:end].strip()
        if piece:
            chunks.append(Chunk(text=piece, index=len(chunks), start_char=start, end_char=end))
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _tokens(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


def relevance_score(query: str, text: str, *, idf: dict[str, float] | None = None) -> float:
    """Idf-weighted overlap between query terms and a chunk's terms.

    ``idf`` maps term -> weight (higher = rarer across the collection, so
    more discriminative). Without one, every term weighs the same -- still
    far better than raw position slicing. Coverage is normalized by query
    length so a long query does not automatically outscores a short one.
    """
    q = _tokens(query)
    if not q:
        return 0.0
    doc_terms = _tokens(text)
    doc_set = set(doc_terms)
    score = 0.0
    for term in q:
        if term not in doc_set:
            continue
        weight = (idf or {}).get(term, 1.0)
        tf = doc_terms.count(term)
        score += weight * (1.0 + math.log(tf))
    return score / math.sqrt(len(q))


def build_idf(documents: list[str]) -> dict[str, float]:
    """Idf weights from a sample of documents of the same collection."""
    n = len(documents)
    if n == 0:
        return {}
    df: dict[str, int] = {}
    for d in documents:
        for t in set(_tokens(d)):
            df[t] = df.get(t, 0) + 1
    return {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}


def select_relevant(
    chunks: list[Chunk],
    query: str,
    *,
    k: int = 5,
    idf: dict[str, float] | None = None,
) -> list[Chunk]:
    """Top-k chunks by relevance, returned in DOCUMENT order (not score order).

    Document order matters: feeding a model shuffled mid-page fragments
    destroys coherence for zero gain. Zero-score chunks are dropped -- when
    nothing matches, the caller should fall back to the head of the page,
    not to random chunks.
    """
    scored = [(relevance_score(query, c.text, idf=idf), c) for c in chunks]
    hits = [(s, c) for s, c in scored if s > 0]
    hits.sort(key=lambda sc: sc[0], reverse=True)
    chosen = [c for _, c in hits[:k]]
    chosen.sort(key=lambda c: c.index)
    return chosen


def estimate_tokens(text: str) -> int:
    """Same len//4 estimate the server face documents (not a tokenizer)."""
    return len(text or "") // 4


def extractive_summary(text: str, *, max_sentences: int = 5, min_sentence_len: int = 40) -> str:
    """Frequency-ranked extractive summary: real sentences, no generation.

    Sentences shorter than ``min_sentence_len`` (fragments, headings) are
    skipped unless nothing qualifies. Deterministic: equal scores keep
    document order, so re-running on the same text gives the same summary.
    """
    sentences = split_sentences(text)
    if not sentences:
        return ""
    freq: dict[str, int] = {}
    for w in _tokens(text):
        freq[w] = freq.get(w, 0) + 1
    max_freq = max(freq.values()) if freq else 1

    scored: list[tuple[float, int, str]] = []
    for i, s in enumerate(sentences):
        toks = _tokens(s)
        if not toks:
            continue
        score = sum(freq[w] / max_freq for w in toks) / math.sqrt(len(toks))
        scored.append((score, i, s))
    scored.sort(key=lambda t: (-t[0], t[1]))

    chosen: list[str] = []
    for _score, _i, sentence in scored:
        if len(chosen) >= max_sentences:
            break
        if len(sentence) < min_sentence_len:
            continue  # fragments and headings are noise when real sentences exist
        chosen.append(sentence)
    if not chosen and scored:
        chosen = [scored[0][2]]  # all-short input: best fragment beats nothing
    # restore document order
    order = {s: i for i, s in enumerate(sentences)}
    chosen.sort(key=lambda s: order[s])
    return " ".join(chosen)


@dataclass
class CostQualityTelemetry:
    """Per-run counters for what the pipeline did to the bytes.

    Thread-safe via a lock, like ServerMetrics: handlers on worker threads
    bump counters concurrently and ``+=`` is not atomic under the GIL.
    """

    started: float = field(default_factory=time.monotonic)
    docs_in: int = 0
    chars_in: int = 0
    chunks_made: int = 0
    chunks_selected: int = 0
    chars_out: int = 0
    docs_truncated: int = 0
    summaries_made: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def note_doc(self, chars: int, *, truncated: bool = False) -> None:
        with self._lock:
            self.docs_in += 1
            self.chars_in += chars
            if truncated:
                self.docs_truncated += 1

    def note_chunks(self, made: int, selected: int, chars_out: int) -> None:
        with self._lock:
            self.chunks_made += made
            self.chunks_selected += selected
            self.chars_out += chars_out

    def note_summary(self) -> None:
        with self._lock:
            self.summaries_made += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "elapsed_sec": round(time.monotonic() - self.started, 3),
                "docs_in": self.docs_in,
                "chars_in": self.chars_in,
                "chunks_made": self.chunks_made,
                "chunks_selected": self.chunks_selected,
                "chars_out": self.chars_out,
                "docs_truncated": self.docs_truncated,
                "summaries_made": self.summaries_made,
                "tokens_in_est": self.chars_in // 4,
                "tokens_out_est": self.chars_out // 4,
                "reduction_ratio": round(self.chars_out / self.chars_in, 4) if self.chars_in else 0.0,
            }
