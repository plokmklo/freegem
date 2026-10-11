"""Chunking, relevance selection, NLP heuristics, and cost/quality telemetry.

Provides tools for splitting document text into semantic chunks, scoring chunk relevance against a query,
extracting key terms, and tracking model token/cost and extraction quality telemetry.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are", "aren't",
    "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but", "by", "can't",
    "cannot", "could", "couldn't", "did", "didn't", "do", "does", "doesn't", "doing", "don't", "down", "during",
    "each", "few", "for", "from", "further", "had", "hadn't", "has", "hasn't", "have", "haven't", "having",
    "he", "he'd", "he'll", "he's", "her", "here", "here's", "hers", "herself", "him", "himself", "his", "how",
    "how's", "i", "i'd", "i'll", "i'm", "i've", "if", "in", "into", "is", "isn't", "it", "it's", "its", "itself",
    "let's", "me", "more", "most", "mustn't", "my", "myself", "no", "nor", "not", "of", "off", "on", "once",
    "only", "or", "other", "ought", "our", "ours", "ourselves", "out", "over", "own", "same", "shan't", "she",
    "she'd", "she'll", "she's", "should", "shouldn't", "so", "some", "such", "than", "that", "that's", "the",
    "their", "theirs", "them", "themselves", "then", "there", "there's", "these", "they", "they'd", "they'll",
    "they're", "they've", "this", "those", "through", "to", "too", "under", "until", "up", "very", "was",
    "wasn't", "we", "we'd", "we'll", "we're", "we've", "were", "weren't", "what", "what's", "when", "when's",
    "where", "where's", "which", "while", "who", "who's", "whom", "why", "why's", "with", "won't", "would",
    "wouldn't", "you", "you'd", "you'll", "you're", "you've", "your", "yours", "yourself", "yourselves"
}


# --- Chunking Strategies ---

def chunk_text(text: str, chunk_size: int = 1000, overlap: int = 200, strategy: str = "paragraph") -> list[str]:
    """Chunk text into segments with overlapping context.

    Strategies:
    - 'character': split strictly by character limit with overlap
    - 'paragraph': split by double-newlines, merging up to chunk_size
    - 'sentence': split by sentence boundaries (.!?)
    """
    if not text:
        return []

    if strategy == "paragraph":
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        chunks: list[str] = []
        current: list[str] = []
        curr_len = 0

        for p in paragraphs:
            if curr_len + len(p) > chunk_size and current:
                chunks.append("\n\n".join(current))
                # Keep tail for overlap if possible
                current = [p]
                curr_len = len(p)
            else:
                current.append(p)
                curr_len += len(p)
        if current:
            chunks.append("\n\n".join(current))
        return chunks

    elif strategy == "sentence":
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        chunks = []
        current = []
        curr_len = 0

        for s in sentences:
            if curr_len + len(s) > chunk_size and current:
                chunks.append(" ".join(current))
                current = [s]
                curr_len = len(s)
            else:
                current.append(s)
                curr_len += len(s)
        if current:
            chunks.append(" ".join(current))
        return chunks

    else:  # character strategy
        chunks = []
        start = 0
        n = len(text)
        step = max(1, chunk_size - overlap)
        while start < n:
            end = min(n, start + chunk_size)
            chunks.append(text[start:end])
            start += step
        return chunks


# --- Relevance Selection (TF-IDF / BM25) ---

def tokenize(text: str) -> list[str]:
    """Tokenize and remove stopwords/punctuation."""
    tokens = re.findall(r"\b\w+\b", text.lower())
    return [t for t in tokens if t not in STOPWORDS and not t.isdigit()]


def score_relevance(chunk: str, query: str) -> float:
    """Compute TF-IDF keyword overlap relevance score between chunk and query."""
    query_tokens = tokenize(query)
    if not query_tokens:
        return 0.0

    chunk_tokens = tokenize(chunk)
    if not chunk_tokens:
        return 0.0

    tf = {}
    for t in chunk_tokens:
        tf[t] = tf.get(t, 0) + 1

    score = 0.0
    for q in set(query_tokens):
        if q in tf:
            # Term frequency log-weighted
            score += (1 + math.log(tf[q]))
    return score / math.sqrt(len(chunk_tokens))


def select_relevant_chunks(text: str, query: str, top_k: int = 3, chunk_size: int = 1000) -> list[str]:
    """Filter top-k relevant text chunks for a query."""
    chunks = chunk_text(text, chunk_size=chunk_size, strategy="paragraph")
    if not chunks:
        return []

    scored = [(score_relevance(c, query), i, c) for i, c in enumerate(chunks)]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [c for _, _, c in scored[:top_k]]


# --- NLP Heuristics ---

def extract_keywords(text: str, top_n: int = 10) -> list[tuple[str, int]]:
    """Extract top N non-stopword keywords by frequency."""
    tokens = tokenize(text)
    counts: dict[str, int] = {}
    for t in tokens:
        counts[t] = counts.get(t, 0) + 1
    sorted_keywords = sorted(counts.items(), key=lambda x: x[1], reverse=True)
    return sorted_keywords[:top_n]


# --- Cost & Quality Telemetry ---

MODEL_PRICING = {
    # per 1,000,000 tokens (input, output)
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
    "gpt-4o": (2.50, 10.00),
}


@dataclass
class Telemetry:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_cost_usd: float = 0.0
    items_extracted: int = 0
    schema_fields_matched: int = 0
    schema_fields_total: int = 0

    def record_usage(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        pricing = MODEL_PRICING.get(model, (0.10, 0.40))
        cost = (prompt_tokens / 1_000_000.0 * pricing[0]) + (completion_tokens / 1_000_000.0 * pricing[1])
        self.total_cost_usd += cost

    def quality_score(self) -> float:
        """Return schema coverage ratio (0.0 to 1.0)."""
        if self.schema_fields_total == 0:
            return 1.0
        return self.schema_fields_matched / self.schema_fields_total
