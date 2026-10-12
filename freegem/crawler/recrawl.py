"""Content-change recrawling and adaptive crawl scheduling.

Detects document updates via HTTP headers (ETag, Last-Modified) and content hashes (SHA-256),
and dynamically adjusts fetch recheck intervals based on change frequency.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass


def compute_content_hash(content: bytes | str) -> str:
    """Compute SHA-256 hex digest for document body."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


@dataclass
class RecrawlState:
    url: str
    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None
    last_checked_at: float = 0.0
    change_count: int = 0
    check_count: int = 0
    current_interval_sec: float = 86400.0  # Default 1 day
    min_interval_sec: float = 3600.0        # Min 1 hour
    max_interval_sec: float = 604800.0      # Max 7 days


class AdaptiveScheduler:
    """Adaptive crawl interval scheduler."""

    def __init__(self, default_interval: float = 86400.0, min_interval: float = 3600.0, max_interval: float = 604800.0) -> None:
        self.default_interval = default_interval
        self.min_interval = min_interval
        self.max_interval = max_interval

    def update_and_reschedule(
        self,
        state: RecrawlState,
        new_etag: str | None,
        new_last_modified: str | None,
        new_content: bytes | str,
        now: float | None = None,
    ) -> tuple[bool, RecrawlState]:
        """Check if content changed and calculate next re-crawl interval.

        Returns (changed, updated_state).
        """
        now = now or time.time()
        new_hash = compute_content_hash(new_content)

        changed = False
        if (state.content_hash and state.content_hash != new_hash) or (state.etag and new_etag and state.etag != new_etag):
            changed = True

        state.check_count += 1
        state.last_checked_at = now
        state.etag = new_etag or state.etag
        state.last_modified = new_last_modified or state.last_modified
        state.content_hash = new_hash

        if changed:
            state.change_count += 1
            # Content changed: decrease interval (check more frequently)
            state.current_interval_sec = max(
                self.min_interval,
                state.current_interval_sec * 0.5,
            )
        else:
            # Content unchanged: decay interval (check less frequently)
            state.current_interval_sec = min(
                self.max_interval,
                state.current_interval_sec * 1.5,
            )

        return changed, state

    def is_due(self, state: RecrawlState, now: float | None = None) -> bool:
        """Check if URL is due for a recrawl."""
        now = now or time.time()
        return (now - state.last_checked_at) >= state.current_interval_sec
