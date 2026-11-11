"""A capacity-bounded "have I seen this URL?" set for discovery storms (2.13.0).

The frontier is SQLite-backed and exact: ``push()`` is ``INSERT OR IGNORE``,
so a duplicate costs a row-level check but never a re-fetch. During a deep
crawl the discovery phase offers every link on every page to the frontier --
the same pagination links, the same nav bars, ten thousand times over -- and
each offer pays a database round trip just to be told "already known".

A Bloom filter sits in front of the insert and answers "definitely not seen"
for the overwhelming majority of new URLs and "probably seen" for repeats,
using a fixed bit array whose size depends on the *expected* URL count, not
on how many arrived. This is the same trade Heritrix's ``BloomUriUniqFilter``
made, and it comes with the same honest cost:

* a "probably seen" answer is *usually* a true repeat (saving the insert) but
  can be a false positive -- a genuinely new URL quietly dropped at rate
  ``fp_rate``. Opt-in only; the default crawl stays exact.
* the filter never decides alone: a positive answer means *skip the insert*,
  not *skip the URL forever*. Restarting without the flag re-examines
  everything; the frontier on disk remains the source of truth.
* it is strictly a discovery pre-filter. Fetching, scheduling, and politeness
  are untouched -- nothing that reaches the frontier changes behavior.

Standard-library only: two 64-bit halves of one blake2b digest drive the
Kirsch-Mitzenmacher double-hashing scheme (h_i = h1 + i*h2 mod m), which
needs k hash positions from a single digest. ``stats()`` reports the design
and the observed load so receipts can show exactly what was delegated.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

__all__ = ["BloomSeenSet"]


def _optimal(capacity: int, fp_rate: float) -> tuple[int, int]:
    """(bits, hashes) for the classic capacity/false-positive design point."""
    bits = max(8, math.ceil(-capacity * math.log(fp_rate) / (math.log(2) ** 2)))
    hashes = max(1, round((bits / capacity) * math.log(2)))
    return bits, hashes


@dataclass
class BloomSeenSet:
    """Fixed-size Bloom filter over URL strings.

    ``capacity`` is the expected number of distinct URLs (design point, not a
    hard cap: exceeding it only raises the real false-positive rate above
    ``fp_rate``). ``add_if_absent(url)`` returns True when the URL was
    probably-new (now recorded) and False when probably-seen; ``__contains__``
    is the read-only probe. False positives are possible by design; false
    negatives are impossible (a URL once added always probes its bits).
    """

    capacity: int
    fp_rate: float = 0.001
    _bits: bytearray = field(init=False, repr=False)
    _num_hashes: int = field(init=False, repr=False)
    _bit_count: int = field(init=False, repr=False)
    _count: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError(f"capacity must be >= 1, got {self.capacity}")
        if not 0.0 < self.fp_rate < 1.0:
            raise ValueError(f"fp_rate must be in (0, 1), got {self.fp_rate}")
        bits, hashes = _optimal(self.capacity, self.fp_rate)
        self._bits = bytearray((bits + 7) // 8)
        self._num_hashes = hashes
        self._bit_count = bits
        self._count = 0

    def _positions(self, item: str) -> list[int]:
        digest = hashlib.blake2b(item.encode("utf-8"), digest_size=16).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:], "big") | 1  # odd, so the step coprime-ish
        return [(h1 + i * h2) % self._bit_count for i in range(self._num_hashes)]

    def add_if_absent(self, item: str) -> bool:
        """True when probably-new (and now recorded); False when probably-seen."""
        positions = self._positions(item)
        if all(self._bits[p >> 3] & (1 << (p & 7)) for p in positions):
            return False
        for p in positions:
            self._bits[p >> 3] |= 1 << (p & 7)
        self._count += 1
        return True

    def __contains__(self, item: str) -> bool:
        return all(self._bits[p >> 3] & (1 << (p & 7)) for p in self._positions(item))

    def __len__(self) -> int:
        return self._count

    def stats(self) -> dict[str, Any]:
        """Design and load, for the crawl receipts."""
        return {
            "capacity": self.capacity,
            "fp_rate": self.fp_rate,
            "items": self._count,
            "hashes": self._num_hashes,
            "bits": self._bit_count,
            "memory_bytes": len(self._bits),
        }
