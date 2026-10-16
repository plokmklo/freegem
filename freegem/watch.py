"""Change monitoring for one URL: ``freegem watch`` (2.9.0, issue #30).

The repo already knew how to tell two documents apart (:mod:`freegem.dedup`
simhashes) and how to schedule rechecks of changing pages
(:mod:`freegem.crawler.scheduling`). What it could not do was answer a
human's simplest question about a page: "what changed since I last looked?"

This module is that answer, local-first, no cloud:

* the first run fetches the page and stores a BASELINE -- the normalized
  readable text plus its 64-bit simhash -- as one JSON file under
  ``~/.config/freegem/watch/<sha256(url)[:16]>.json`` (override the
  directory with ``$FREEGEM_WATCH_DIR``; tests pass ``state_dir=``);
* every later run refetches and diffs. The change decision is the sha256 of
  the normalized text (exact), and the simhash distance explains HOW BIG the
  change is (0-3 bits: boilerplate-level churn, 25+: a different page);
* ``difflib`` over the normalized lines yields the human-readable blocks:
  added/removed/changed regions with char counts. A watch that can only say
  "different" is not worth running.

Deliberate choices:

* NO fetch cache. The research cache would make a poll within its TTL report
  "no change" because it re-read the page it cached half an hour ago -- the
  one answer a monitor must never fake. A watch always sees fresh bytes.
* Conditional GET (If-None-Match) is not wired: ``web_scraper``'s extraction
  path has no 304 notion, and a 304 saves bytes, not the diff we need. The
  watch diffs the EXTRACTED text, so layout churn and ad rotation do not
  show as changes -- only the words a reader sees.
* Whitespace-only churn is normalized away (blank lines dropped, per-line
  edges stripped) so a re-render that only reshuffles indentation is not a
  change.
* All IO is injectable: ``fetcher=`` replaces the network, ``state_dir=`` /
  ``state_path=`` replace the on-disk state, ``now=`` replaces the clock.
  Tests run hermetic with zero sleeps.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import BridgeConfig
from .dedup import Simhash

__all__ = [
    "DEFAULT_INTERVAL_SEC",
    "DiffBlock",
    "WatchResult",
    "WatchState",
    "normalize_text",
    "parse_interval",
    "render_text",
    "state_path_for",
    "watch_poll",
    "watch_url",
]

#: Poll cadence for ``freegem watch URL --interval`` loops (one hour).
DEFAULT_INTERVAL_SEC = 3600.0

#: Diff blocks the TEXT report shows before pointing at ``--delta``.
_SUMMARY_BLOCK_LIMIT = 5

_WORD_INTERVAL_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([smh]?)")
_UNIT_SECONDS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0}


# ─── state ────────────────────────────────────────────────────────────────────


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_text(text: str) -> str:
    """The text a watch actually tracks: stripped lines, no blank lines.

    Extraction output varies in whitespace that carries no meaning (a
    re-rendered page, a different trailing newline). Watching the normalized
    text keeps the signal on words, not indentation.
    """
    lines = (line.strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def resolve_state_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Where watch baselines live: the caller's dir, ``$FREEGEM_WATCH_DIR``,
    or ``~/.config/freegem/watch`` (the repo's config-dir convention)."""
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("FREEGEM_WATCH_DIR")
    if env:
        return Path(env)
    return Path.home() / ".config" / "freegem" / "watch"


def state_path_for(url: str, state_dir: str | os.PathLike[str] | None = None) -> Path:
    """One JSON file per watched URL, keyed by sha256(url)[:16].

    The hash (not the URL) is the filename: URLs carry characters no
    filesystem likes. The full URL is stored inside the file.
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return resolve_state_dir(state_dir) / f"{digest}.json"


@dataclass
class WatchState:
    """The baseline one watch compares against, persisted as one JSON file."""

    url: str
    text: str
    text_hash: str
    simhash: int
    checked_at: float
    first_seen: float
    last_changed_at: float | None = None
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "url": self.url,
            "text": self.text,
            "text_hash": self.text_hash,
            "simhash": self.simhash,
            "checked_at": self.checked_at,
            "first_seen": self.first_seen,
            "last_changed_at": self.last_changed_at,
        }

    def save(self, path: str | os.PathLike[str]) -> None:
        """Persist atomically (tmp + rename), the same way the research cache
        does: a crash mid-write leaves the previous baseline, never a half
        file that would masquerade as "no change"."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")
        tmp.replace(target)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> WatchState | None:
        """The stored state, or None when absent OR untrustworthy.

        A corrupt state file must degrade to a fresh baseline, never crash
        and never compare against garbage: missing file, unparseable JSON,
        wrong shapes, or a text whose stored hash does not match (a damaged
        or hand-edited entry) all load as None.
        """
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        url, text, digest = data.get("url"), data.get("text"), data.get("text_hash")
        simhash, checked_at, first_seen = (
            data.get("simhash"),
            data.get("checked_at"),
            data.get("first_seen"),
        )
        last_changed = data.get("last_changed_at")
        if not isinstance(url, str) or not isinstance(text, str) or not isinstance(digest, str):
            return None
        if not isinstance(simhash, int) or isinstance(simhash, bool):
            return None
        if not isinstance(checked_at, (int, float)) or not isinstance(first_seen, (int, float)):
            return None
        if last_changed is not None and not isinstance(last_changed, (int, float)):
            return None
        if _sha256(text) != digest:
            return None
        return cls(
            url=url,
            text=text,
            text_hash=digest,
            simhash=simhash,
            checked_at=float(checked_at),
            first_seen=float(first_seen),
            last_changed_at=None if last_changed is None else float(last_changed),
        )


# ─── diff ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DiffBlock:
    """One contiguous changed region between two snapshots."""

    #: "added" (new lines only), "removed" (old lines only), or "changed".
    kind: str
    before: str | None = None
    after: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"kind": self.kind, "before": self.before, "after": self.after}


def diff_blocks(old_text: str, new_text: str) -> tuple[list[DiffBlock], int, int]:
    """Diff two normalized snapshots: blocks + added/removed char counts.

    The counts are of the changed lines' joined text -- the size of what a
    reader would have to re-read, not a byte-level accounting of invisible
    churn.
    """
    old_lines, new_lines = old_text.splitlines(), new_text.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    blocks: list[DiffBlock] = []
    added_chars = 0
    removed_chars = 0
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op == "equal":
            continue
        before = "\n".join(old_lines[a1:a2]) if a2 > a1 else None
        after = "\n".join(new_lines[b1:b2]) if b2 > b1 else None
        kind = {"replace": "changed", "delete": "removed", "insert": "added"}[op]
        blocks.append(DiffBlock(kind=kind, before=before, after=after))
        if after is not None:
            added_chars += len(after)
        if before is not None:
            removed_chars += len(before)
    return blocks, added_chars, removed_chars


# ─── results ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WatchResult:
    """What one poll of one URL produced."""

    url: str
    #: True when this run CREATED the baseline (nothing to compare against).
    baseline: bool
    #: None on a baseline run, else whether the text changed.
    changed: bool | None
    simhash_before: int | None
    simhash_after: int
    simhash_distance: int | None
    added_chars: int = 0
    removed_chars: int = 0
    changed_blocks: tuple[DiffBlock, ...] = field(default_factory=tuple)
    checked_at: float = 0.0
    state_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        """The machine-readable shape of ``--format json``."""
        return {
            "url": self.url,
            "changed": self.changed,
            "baseline": self.baseline,
            "simhash_before": self.simhash_before,
            "simhash_after": self.simhash_after,
            "simhash_distance": self.simhash_distance,
            "added_chars": self.added_chars,
            "removed_chars": self.removed_chars,
            "changed_blocks": [block.to_dict() for block in self.changed_blocks],
            "checked_at": self.checked_at,
            "state_path": self.state_path,
        }


def render_text(result: WatchResult, *, delta: bool = False) -> str:
    """The human report: baseline confirmation, no-change line, or a diff.

    ``delta=True`` prints every diff block; without it only the first
    :data:`_SUMMARY_BLOCK_LIMIT` are shown and the rest are counted.
    """
    if result.baseline:
        return "\n".join(
            [
                f"baseline saved: {result.url}",
                f"  simhash 0x{result.simhash_after:016x}  state: {result.state_path}",
                "  future runs diff against this snapshot",
            ]
        )
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(result.checked_at))
    if result.changed is False:
        return f"no change: {result.url}  (checked {stamp})"
    distance = result.simhash_distance if result.simhash_distance is not None else 0
    lines = [
        f"CHANGED: {result.url}  (checked {stamp})",
        f"  simhash 0x{result.simhash_before:016x} -> 0x{result.simhash_after:016x}"
        f"  ({distance}/64 bits differ)",
        f"  +{result.added_chars} / -{result.removed_chars} chars,"
        f" {len(result.changed_blocks)} diff block(s)",
    ]
    shown = result.changed_blocks if delta else result.changed_blocks[:_SUMMARY_BLOCK_LIMIT]
    for index, block in enumerate(shown, 1):
        lines.append(f"  [{index}/{len(result.changed_blocks)}] {block.kind}:")
        if block.before is not None:
            lines.extend(f"    - {line}" for line in block.before.splitlines())
        if block.after is not None:
            lines.extend(f"    + {line}" for line in block.after.splitlines())
    hidden = len(result.changed_blocks) - len(shown)
    if hidden > 0:
        lines.append(f"  ... {hidden} more block(s); use --delta for the full diff")
    return "\n".join(lines)


# ─── polling ──────────────────────────────────────────────────────────────────


def _default_fetcher(cfg: BridgeConfig, url: str) -> str:
    """Fetch through the ordinary guarded extraction path (smart ladder on
    per config, NO cache -- see the module docstring). Raises ``ScrapeError``
    on refusal, which the CLI turns into an honest non-zero exit."""
    from .web_scraper import scrape_page

    page = scrape_page(cfg, url, smart=getattr(cfg, "smart_fetch", True))
    return page.text


def watch_url(
    url: str,
    cfg: BridgeConfig,
    *,
    fetcher: Callable[[str], str] | None = None,
    state_dir: str | os.PathLike[str] | None = None,
    state_path: str | os.PathLike[str] | None = None,
    now: float | None = None,
) -> WatchResult:
    """One check of one URL: fetch, compare to the baseline, report, persist.

    The first call stores a baseline (``result.baseline`` True,
    ``result.changed`` None). Later calls return the change summary. The
    fetcher returns the page's extracted text and raises on failure;
    injecting it (plus ``state_dir`` and ``now``) makes this hermetic.
    """
    ts = time.time() if now is None else now
    fetch: Callable[[str], str] = (
        fetcher if fetcher is not None else (lambda u: _default_fetcher(cfg, u))
    )
    path = Path(state_path) if state_path is not None else state_path_for(url, state_dir)
    text = normalize_text(fetch(url))
    fingerprint = Simhash.from_text(text)
    prior = WatchState.load(path)

    if prior is None:
        state = WatchState(
            url=url,
            text=text,
            text_hash=_sha256(text),
            simhash=fingerprint.value,
            checked_at=ts,
            first_seen=ts,
        )
        state.save(path)
        return WatchResult(
            url=url,
            baseline=True,
            changed=None,
            simhash_before=None,
            simhash_after=fingerprint.value,
            simhash_distance=None,
            checked_at=ts,
            state_path=str(path),
        )

    if prior.text_hash == _sha256(text):
        prior.checked_at = ts
        prior.save(path)
        return WatchResult(
            url=url,
            baseline=False,
            changed=False,
            simhash_before=prior.simhash,
            simhash_after=fingerprint.value,
            simhash_distance=Simhash(prior.simhash).distance(fingerprint),
            checked_at=ts,
            state_path=str(path),
        )

    blocks, added_chars, removed_chars = diff_blocks(prior.text, text)
    simhash_before = Simhash(prior.simhash)
    distance = simhash_before.distance(fingerprint)
    prior.text = text
    prior.text_hash = _sha256(text)
    prior.simhash = fingerprint.value
    prior.checked_at = ts
    prior.last_changed_at = ts
    prior.save(path)
    return WatchResult(
        url=url,
        baseline=False,
        changed=True,
        simhash_before=simhash_before.value,
        simhash_after=fingerprint.value,
        simhash_distance=distance,
        added_chars=added_chars,
        removed_chars=removed_chars,
        changed_blocks=tuple(blocks),
        checked_at=ts,
        state_path=str(path),
    )


def watch_poll(
    url: str,
    cfg: BridgeConfig,
    *,
    interval_sec: float = DEFAULT_INTERVAL_SEC,
    fetcher: Callable[[str], str] | None = None,
    state_dir: str | os.PathLike[str] | None = None,
    sleep: Callable[[float], None] | None = None,
    stop: Callable[[], bool] | None = None,
    max_polls: int | None = None,
) -> Iterator[WatchResult]:
    """Yield one :class:`WatchResult` per poll, sleeping between polls.

    A single check is ``max_polls=1``; a watch loop leaves it None and the
    consumer (the CLI) breaks on KeyboardInterrupt. ``sleep`` and ``stop``
    are injectable so tests never wait (``sleep=None`` falls back to
    :func:`time.sleep`, resolved at call time so test patches land). The
    first poll is immediate.
    """
    sleep_fn = time.sleep if sleep is None else sleep
    polls = 0
    while max_polls is None or polls < max_polls:
        yield watch_url(url, cfg, fetcher=fetcher, state_dir=state_dir)
        polls += 1
        if stop is not None and stop():
            return
        if max_polls is not None and polls >= max_polls:
            return
        sleep_fn(interval_sec)


# ─── interval parsing ─────────────────────────────────────────────────────────


def parse_interval(text: str) -> float:
    """``"45s"``/``"10m"``/``"2h"``/``"1h30m"``/bare seconds -> seconds.

    Raises ValueError on anything else (empty, negative, unknown unit,
    zero): an invalid interval must be a clean usage error, not a watch
    that silently polls at some accidental cadence.
    """
    stripped = text.strip().lower()
    if not stripped:
        raise ValueError("interval is empty")
    total = 0.0
    pos = 0
    for match in _WORD_INTERVAL_RE.finditer(stripped):
        if match.start() != pos:
            raise ValueError(f"cannot parse interval {text!r}")
        pos = match.end()
        value = float(match.group(1))
        total += value * _UNIT_SECONDS[match.group(2)]
    if pos != len(stripped):
        raise ValueError(f"cannot parse interval {text!r}")
    if total <= 0:
        raise ValueError(f"interval must be positive, got {text!r}")
    return total
