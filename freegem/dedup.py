"""Near-duplicate detection for crawled documents (2.8.0).

Why not exact hashes: mirrors, paginated reprints, and boilerplate-heavy
pages are never byte-identical -- a crawler that dedupes only on
``content_hash`` (which the frontier already keeps) still stores the same
article nine times with nine different ad banners.

Two complementary techniques, both stdlib-only:

* ``Simhash`` (Manku et al. 2007, re-derived): 64-bit fingerprint of word
  k-shingles; hamming distance <= 3 between 64-bit fingerprints is the
  standard near-duplicate band. O(words) to compute, O(n) index scan to
  query -- fine at crawl scale because the index is bounded by the budget,
  and exact enough to explain ("these two differ in 2 of 64 bits").
* ``jaccard`` over hashed k-shingles for the pairwise/verification path:
  called on simhash candidates only, so the O(n^2) blowup never happens in
  practice.

``Deduper`` is the thing callers use: ``add(doc_id, text)`` returns False
when the text is a near-duplicate of something already seen, and the
keeper's id is returned so output pipelines can point at the original.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

__all__ = ["Deduper", "Simhash", "hamming", "jaccard", "shingles"]

_WORD_RE = re.compile(r"\w+", re.UNICODE)
#: Default shingle width in WORDS. 5 is the classic near-dup band: short
#: enough to survive light rewording, long enough that random collisions
#: between unrelated pages are rare.
DEFAULT_SHINGLE_K = 5


def _hash64(data: bytes) -> int:
    return int.from_bytes(hashlib.blake2b(data, digest_size=8).digest(), "big")


def shingles(text: str, k: int = DEFAULT_SHINGLE_K) -> set[int]:
    """Hashed word k-shingles of a text. Deterministic across runs."""
    words = _WORD_RE.findall(text.lower())
    if len(words) < k:
        return {_hash64(" ".join(words).encode("utf-8"))} if words else set()
    return {_hash64(" ".join(words[i : i + k]).encode("utf-8")) for i in range(len(words) - k + 1)}


def jaccard(a: set[int], b: set[int]) -> float:
    """Jaccard similarity of two shingle sets: |A∩B| / |A∪B| in [0, 1]."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def hamming(a: int, b: int) -> int:
    """Bits differing between two fingerprints."""
    return (a ^ b).bit_count()


@dataclass(frozen=True)
class Simhash:
    """64-bit near-duplicate fingerprint."""

    value: int

    @classmethod
    def from_text(cls, text: str, k: int = DEFAULT_SHINGLE_K) -> Simhash:
        """Weighted-merge of shingle hashes: bit i is 1 when more shingles
        hash with bit i set than clear."""
        sh = shingles(text, k)
        if not sh:
            return cls(0)
        bits = [0] * 64
        for h in sh:
            for i in range(64):
                bits[i] += 1 if (h >> i) & 1 else -1
        value = 0
        for i in range(64):
            if bits[i] > 0:
                value |= 1 << i
        return cls(value)

    def distance(self, other: Simhash) -> int:
        return hamming(self.value, other.value)


@dataclass
class Deduper:
    """Streaming near-duplicate filter over simhashes (+ optional jaccard verify).

    ``add()`` returns True and records the doc when it is (near-)unique;
    False when it duplicates an earlier document. ``threshold`` is the max
    simhash hamming distance treated as a duplicate. The literature's 3-of-64
    band assumes web-scale documents with thousands of shingles; at the
    moderate sizes a crawl budget actually fetches, the observed band is
    wider, so the default is 8 -- calibrated so a reworded/tail-appended
    duplicate lands at 4-6 bits while unrelated pages sit at 25+.
    ``jaccard_verify`` re-checks candidates by actual shingle overlap, so a
    simhash collision between genuinely different pages does not drop data
    (the floor rejects anything under 50% real overlap regardless of bits).
    """

    threshold: int = 8
    jaccard_verify: bool = True
    jaccard_floor: float = 0.5
    shingle_k: int = DEFAULT_SHINGLE_K
    _kept: dict[str, Simhash] = field(default_factory=dict)
    _shingles_by_id: dict[str, set[int]] = field(default_factory=dict)
    _original_of: dict[str, str] = field(default_factory=dict)

    def add(self, doc_id: str, text: str) -> bool:
        """True when unique (kept); False when a near-duplicate of a kept doc.

        Re-adding a ``doc_id`` already in the index refreshes its record
        instead of comparing it against itself (a recrawl replaces -- the doc
        never becomes its own duplicate).
        """
        fp = Simhash.from_text(text, self.shingle_k)
        sh = shingles(text, self.shingle_k)
        for kept_id, kept_fp in self._kept.items():
            if kept_id == doc_id:
                continue
            if fp.distance(kept_fp) > self.threshold:
                continue
            if (
                self.jaccard_verify
                and jaccard(sh, self._shingles_by_id[kept_id])
                < self.jaccard_floor
            ):
                continue  # fingerprint collision, not a real duplicate
            self._original_of[doc_id] = kept_id
            return False
        self._kept[doc_id] = fp
        self._shingles_by_id[doc_id] = sh
        self._original_of.pop(doc_id, None)  # no longer a rejected duplicate
        return True

    def is_duplicate(self, text: str) -> bool:
        """Query-only check that never mutates the index."""
        fp = Simhash.from_text(text, self.shingle_k)
        sh = shingles(text, self.shingle_k)
        for kept_id, kept_fp in self._kept.items():
            if fp.distance(kept_fp) <= self.threshold:
                if not self.jaccard_verify:
                    return True
                if jaccard(sh, self._shingles_by_id[kept_id]) >= self.jaccard_floor:
                    return True
        return False

    def original_of(self, doc_id: str) -> str | None:
        """For a rejected duplicate, the id of the document it duplicated."""
        return self._original_of.get(doc_id)

    def stats(self) -> dict[str, Any]:
        return {"kept": len(self._kept), "threshold": self.threshold}
