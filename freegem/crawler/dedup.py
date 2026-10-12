"""Similarity deduplication: MinHash, SimHash, and Jaccard distance.

Provides near-duplicate detection for crawled web pages to prevent redundant indexing or processing.
"""

from __future__ import annotations

import hashlib
import re


def _shingles(text: str, k: int = 3) -> set[str]:
    """Generate k-shingles (word k-grams) from text."""
    tokens = re.findall(r"\b\w+\b", text.lower())
    if len(tokens) < k:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}


def jaccard_similarity(text1: str, text2: str, k: int = 3) -> float:
    """Compute Jaccard similarity between two texts based on k-shingles."""
    s1 = _shingles(text1, k=k)
    s2 = _shingles(text2, k=k)
    if not s1 or not s2:
        return 1.0 if s1 == s2 else 0.0
    intersection = len(s1.intersection(s2))
    union = len(s1.union(s2))
    return intersection / union if union > 0 else 0.0


def simhash(text: str, hashbits: int = 64) -> int:
    """Compute 64-bit SimHash value for text."""
    tokens = re.findall(r"\b\w+\b", text.lower())
    if not tokens:
        return 0

    v = [0] * hashbits
    for token in tokens:
        # Generate hash bitmask
        h = int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16)
        for i in range(hashbits):
            bitmask = 1 << i
            if h & bitmask:
                v[i] += 1
            else:
                v[i] -= 1

    fingerprint = 0
    for i in range(hashbits):
        if v[i] >= 0:
            fingerprint |= (1 << i)
    return fingerprint


def hamming_distance(hash1: int, hash2: int) -> int:
    """Compute Hamming distance between two SimHash integers."""
    x = hash1 ^ hash2
    tot = 0
    while x:
        tot += 1
        x &= x - 1
    return tot


class Deduplicator:
    """In-memory near-duplicate index using SimHash / Jaccard."""

    def __init__(self, simhash_threshold: int = 3) -> None:
        self.simhash_threshold = simhash_threshold
        self._seen_hashes: list[tuple[str, int]] = []

    def is_duplicate(self, url: str, text: str) -> tuple[bool, str | None]:
        """Check if text is near-duplicate of previously seen documents."""
        sh = simhash(text)
        for prev_url, prev_sh in self._seen_hashes:
            if hamming_distance(sh, prev_sh) <= self.simhash_threshold:
                return True, prev_url
        self._seen_hashes.append((url, sh))
        return False, None
