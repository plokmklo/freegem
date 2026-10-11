"""Metrics: request IDs, stage timing, counters, latency percentiles, memory.

Two layers, deliberately separate:

* :class:`RunMetrics` -- per-operation, created and owned by one call
  (``research()``, ``verify()``). No global state, so concurrent runs never
  see each other's numbers and the frozen-config threading rules hold. A run
  records named stage durations (discover / fetch / generate / verify), a
  request id that travels into logs and JSON output, and monotonic counters
  (fetches ok/failed, cache hits, quotes verified/unverified, schema
  repairs).
* :class:`ServerMetrics` -- process-wide, owned by the HTTP server. A bounded
  latency window (last N completed POSTs) with p50/p95/p99, response-status
  counts, queue-refusal counts, and uptime. Rendered by the ``GET /metrics``
  endpoint. Percentiles over a bounded window are an operational read, not a
  statistical guarantee; the window size is stated in the output.

Plus two process-wide helpers with no dependencies, safe to import from
anywhere (gemini.py uses one to count upstream retries):

* :func:`note_gemini_retry` / :func:`gemini_retries_total`
* :func:`memory_rss_kb` -- resident set size on POSIX, ``None`` elsewhere
  (Windows has no cheap portable RSS; reporting nothing beats reporting a
  wrong number).
"""

from __future__ import annotations

import math
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "DEFAULT_LATENCY_WINDOW",
    "LatencyWindow",
    "RunMetrics",
    "ServerMetrics",
    "gemini_retries_total",
    "memory_rss_kb",
    "new_request_id",
    "note_gemini_retry",
    "percentile",
]

#: How many completed-request durations the server keeps for percentile
#: math. 512 covers several minutes of a busy local bridge; anything older
#: stops influencing the percentiles.
DEFAULT_LATENCY_WINDOW = 512


def new_request_id() -> str:
    """Short, log-friendly request id (12 hex chars of one UUID4)."""
    return uuid.uuid4().hex[:12]


@contextmanager
def _stage_sink(metrics: RunMetrics, name: str) -> Iterator[None]:
    start = time.monotonic()
    try:
        yield
    finally:
        metrics.stages[name] = metrics.stages.get(name, 0.0) + (time.monotonic() - start)


@dataclass
class RunMetrics:
    """Stage timings and counters for ONE research/verify operation."""

    request_id: str = field(default_factory=new_request_id)
    started: float = field(default_factory=time.monotonic)
    #: stage name -> seconds spent there (accumulates across repeats)
    stages: dict[str, float] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)

    def stage(self, name: str) -> Any:
        """``with metrics.stage("fetch"):`` -- times the block, accumulates."""
        return _stage_sink(self, name)

    def bump(self, name: str, n: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + n

    @property
    def elapsed_sec(self) -> float:
        return time.monotonic() - self.started

    def snapshot(self) -> dict[str, Any]:
        """JSON-ready summary: id, stages, counters, memory, total time."""
        return {
            "request_id": self.request_id,
            "elapsed_sec": round(self.elapsed_sec, 3),
            "stages": {name: round(sec, 3) for name, sec in sorted(self.stages.items())},
            "counters": dict(sorted(self.counters.items())),
            "memory_rss_kb": memory_rss_kb(),
        }


def percentile(samples: list[float], pct: float) -> float:
    """Nearest-rank percentile of an ascending-sortable sample list.

    ``percentile([..], 95)`` is the value at index ceil(0.95 * n) - 1 of the
    sorted copy. Nearest-rank (not interpolation) because the output is an
    ACTUAL observed latency, which is easier to reason about when the sample
    count is small.
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


class LatencyWindow:
    """Bounded ring of durations with percentile reads."""

    def __init__(self, maxlen: int = DEFAULT_LATENCY_WINDOW) -> None:
        self._samples: deque[float] = deque(maxlen=max(1, int(maxlen)))

    def record(self, seconds: float) -> None:
        if seconds >= 0:
            self._samples.append(float(seconds))

    def __len__(self) -> int:
        return len(self._samples)

    def snapshot(self) -> dict[str, Any]:
        samples = list(self._samples)
        return {
            "count": len(samples),
            "p50_sec": round(percentile(samples, 50), 3),
            "p95_sec": round(percentile(samples, 95), 3),
            "p99_sec": round(percentile(samples, 99), 3),
        }


@dataclass
class ServerMetrics:
    """Process-wide counters for the HTTP server. Single-threaded owner is
    the server; handlers on worker threads bump counters through a lock --
    ``+=`` and dict get-then-set are NOT atomic under the GIL (each is a
    load/add/store triplet), so lock-free counting silently lost updates
    under concurrent requests. ``deque.append`` for latency samples is
    thread-safe on its own."""

    started_at: float = field(default_factory=time.time)
    requests_total: int = 0
    by_status: dict[int, int] = field(default_factory=dict)
    queue_refusals: int = 0
    latency: LatencyWindow = field(default_factory=LatencyWindow)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def note_status(self, status: int) -> None:
        with self._lock:
            self.requests_total += 1
            self.by_status[status] = self.by_status.get(status, 0) + 1

    def note_refusal(self) -> None:
        with self._lock:
            self.queue_refusals += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            by_status = {str(code): n for code, n in sorted(self.by_status.items())}
            requests_total = self.requests_total
            refusals = self.queue_refusals
        return {
            "uptime_sec": round(time.time() - self.started_at, 1),
            "requests_total": requests_total,
            "by_status": by_status,
            "queue_refusals": refusals,
            "latency": self.latency.snapshot(),
            "gemini_retries_total": gemini_retries_total(),
            "memory_rss_kb": memory_rss_kb(),
        }


# ─── process-wide helpers ─────────────────────────────────────────────────────

_GEMINI_RETRIES = {"total": 0}
_GEMINI_RETRIES_LOCK = threading.Lock()


def note_gemini_retry() -> None:
    """Count one upstream retry (any face). Cheap enough for hot paths."""
    with _GEMINI_RETRIES_LOCK:
        _GEMINI_RETRIES["total"] += 1


def gemini_retries_total() -> int:
    with _GEMINI_RETRIES_LOCK:
        return _GEMINI_RETRIES["total"]


def memory_rss_kb() -> int | None:
    """Resident set size in KiB on POSIX; ``None`` where unavailable.

    Uses ``resource.getrusage`` -- no tracemalloc (its accounting overhead
    belongs in an explicit debugging session, not every request). ru_maxrss
    is KiB on Linux, BYTES on macOS; normalize by page size heuristics is
    overkill, so: report max(KiB value, bytes/1024 sanity-normalized) via
    the documented per-platform unit. Attribute access goes through
    ``getattr`` because the ``resource`` stubs are platform-conditional:
    on Windows the module (and its attributes) do not exist for the type
    checker either, and a hard attribute reference would fail mypy there.
    """
    try:
        import resource
    except ImportError:  # Windows
        return None
    getrusage = getattr(resource, "getrusage", None)
    rusage_self = getattr(resource, "RUSAGE_SELF", None)
    if getrusage is None or rusage_self is None:
        return None
    try:
        rss = int(getrusage(rusage_self).ru_maxrss)
    except Exception:
        return None
    if rss <= 0:
        return None
    if sys.platform == "darwin":
        rss = rss // 1024  # macOS reports bytes
    return rss
