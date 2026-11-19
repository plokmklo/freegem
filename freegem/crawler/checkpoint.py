"""Campaign checkpoint manifest: an interrupt-safe, digest-bound snapshot.

The SQLite state file already survives ``kill -9`` (every outcome is
committed the moment it lands), but "the data survived" is not the same as
"a resume is safe": nothing bound a seeds list to a state file, nothing
recorded how far the run got, and a plain re-run would re-fetch everything
-- including the seeds that already succeeded. The checkpoint manifest is
the missing contract between two campaign runs (issue #93):

- **Written at the end of every campaign run** -- normal completion AND
  SIGINT interrupt -- atomically (tmp file + ``os.replace``, so a crash
  mid-write leaves the previous manifest intact). A SIGTERM/SIGKILL death
  writes no manifest; the SQLite state is still the recovery path, and the
  subset rule below is what makes that honest.
- **Digest-bound**: the manifest carries a sha-256 over the canonical
  seeds set and over the queue, so ``--resume`` refuses to mix a state file
  with seeds it was never run against, or with a ``--limit`` that changed
  (the queue is ``seeds[:limit]`` -- a different limit is a different
  campaign identity).
- **A lower bound, not an equality snapshot**: every outcome recorded in
  the manifest must still match the state exactly (same kind, same status);
  outcome rows ADDED after the manifest was written are allowed, because
  they are exactly what a killed resume left behind. That asymmetry is the
  difference between "stale manifest" (usable) and "edited history"
  (refused) -- the evidence chain stays loud, like the WACZ digest chain.
- **Versioned**: ``format`` names the manifest dialect; an unknown format
  is a hard refusal, not a best-effort guess.

Manifest shape (JSON, ``sort_keys=True`` so it is diff-friendly)::

    {
      "format": "freegem-campaign-checkpoint/1.0",
      "written_at": 1760000000.0,
      "interrupted": false,
      "resumed_from": null,
      "seeds_count": 3,
      "seeds_digest": "sha256 over sorted canonical seed lines",
      "params": {"concurrency": 8, "limit": 1000, "timeout_sec": 20.0,
                  "host_delay_sec": 1.0, "retry_failed": false,
                  "distill": false},
      "counters": { ... CampaignResult.as_dict() ... },
      "state": {
        "queue_count": 3, "queue_digest": "sha256 ...",
        "results_count": 2, "results_digest": "sha256 ...",
        "results_rows": {"https://a.test/": ["ok", 200], ...}
      }
    }

``results_rows`` is per-URL ``[kind, status]`` -- the subset rule's data.
All digests here are integrity/self-check hashes over freegem's own
records (accident detection, as with the WACZ chain); they are not a
defense against a determined forger who recomputes them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from typing import Any

__all__ = [
    "CHECKPOINT_FORMAT",
    "CheckpointError",
    "queue_digest",
    "read_checkpoint",
    "results_digest",
    "seeds_digest",
    "verify_checkpoint",
    "write_checkpoint",
]

#: Bumped only when the manifest shape changes incompatibly; ``verify_checkpoint``
#: refuses anything else instead of guessing (issue #93: "version-incompatible
#: manifests fail clearly").
CHECKPOINT_FORMAT = "freegem-campaign-checkpoint/1.0"


class CheckpointError(ValueError):
    """A checkpoint manifest is unreadable, stale, or incompatible.

    Raised BEFORE the resumed run touches anything -- a refused resume
    leaves the state file exactly as it was (issue #93: "fail clearly
    without destructive side effects").
    """


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seeds_digest(seeds: list[dict[str, str]]) -> str:
    """sha-256 over the canonical seeds set, order-independent.

    The same seeds passed in a different order are the same campaign, so
    the canonical form sorts the per-seed JSON lines before hashing.
    """
    lines = sorted(
        json.dumps(
            {
                "url": str(seed.get("url", "")),
                "category": str(seed.get("category", "uncategorized")),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        for seed in seeds
    )
    return _sha256("".join(line + "\n" for line in lines))


def results_digest(db: sqlite3.Connection) -> str:
    """sha-256 over every recorded outcome, ordered by url."""
    rows = db.execute("SELECT url, kind, status FROM results ORDER BY url").fetchall()
    return _sha256(
        "".join(f"{row[0]}\t{row[1]}\t{'' if row[2] is None else row[2]}\n" for row in rows)
    )


def queue_digest(db: sqlite3.Connection) -> str:
    """sha-256 over the campaign frontier (queue table), ordered by url."""
    rows = db.execute("SELECT url, category FROM queue ORDER BY url").fetchall()
    return _sha256("".join(f"{row[0]}\t{row[1]}\n" for row in rows))


def _results_rows(db: sqlite3.Connection) -> dict[str, list[Any]]:
    return {
        str(row[0]): [str(row[1]), row[2]]
        for row in db.execute("SELECT url, kind, status FROM results").fetchall()
    }


def _canonical_rows_text(rows: dict[str, list[Any]]) -> str:
    lines = [f"{url}\t{kv[0]}\t{'' if kv[1] is None else kv[1]}\n" for url, kv in rows.items()]
    return "".join(sorted(lines))


def write_checkpoint(
    path: str,
    *,
    seeds: list[dict[str, str]],
    db: sqlite3.Connection,
    params: dict[str, Any],
    counters: dict[str, Any],
    interrupted: bool = False,
    resumed_from: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the manifest atomically and return what was written.

    ``params`` is the run configuration snapshot (concurrency, limit,
    timeouts); ``counters`` is the live ``CampaignResult`` summary; the
    state section (queue/results digests + per-row outcomes) is derived
    from the state db directly so the manifest cannot disagree with it.
    """
    rows = _results_rows(db)
    manifest: dict[str, Any] = {
        "format": CHECKPOINT_FORMAT,
        "written_at": round(time.time(), 3),
        "interrupted": bool(interrupted),
        "resumed_from": resumed_from,
        "seeds_count": len(seeds),
        "seeds_digest": seeds_digest(seeds),
        "params": dict(params),
        "counters": dict(counters),
        "state": {
            "queue_count": int(db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]),
            "queue_digest": queue_digest(db),
            "results_count": len(rows),
            "results_digest": _sha256(_canonical_rows_text(rows)),
            "results_rows": rows,
        },
    }
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".freegem-ckpt-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return manifest


def read_checkpoint(path: str) -> dict[str, Any]:
    """Load a manifest, failing clearly on missing/corrupt files.

    Read-only by construction: a refused read never touches the state.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except OSError as exc:
        raise CheckpointError(f"checkpoint manifest unreadable: {path}: {exc}") from exc
    except ValueError as exc:
        raise CheckpointError(f"checkpoint manifest is not valid JSON: {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise CheckpointError(f"checkpoint manifest must be a JSON object: {path}")
    return manifest


def verify_checkpoint(
    manifest: dict[str, Any],
    *,
    seeds: list[dict[str, str]],
    db: sqlite3.Connection,
) -> None:
    """Check a manifest against the seeds list and the state db.

    Hard refusals (each names what disagrees, so a resume never silently
    guesses):

    - unknown ``format`` -- version-incompatible manifest;
    - ``seeds_digest`` mismatch -- this state was built from different
      seeds;
    - ``queue_digest`` mismatch -- the frontier changed (different
      ``--limit``, edited queue);
    - manifest self-integrity -- the recorded ``results_rows`` do not hash
      to the recorded ``results_digest`` (hand-edited manifest);
    - any recorded outcome missing from the state or disagreeing on
      kind/status -- the state was edited (or rolled back) after the
      manifest was written.

    Outcomes present in the state but NOT in the manifest are allowed:
    they are the progress a killed resume made without rewriting the
    manifest (the manifest is a lower bound, see module docstring).
    """
    if not isinstance(manifest, dict):
        raise CheckpointError("checkpoint manifest must be a JSON object")
    if manifest.get("format") != CHECKPOINT_FORMAT:
        raise CheckpointError(
            f"checkpoint format mismatch: manifest says {manifest.get('format')!r}, "
            f"this freegem speaks {CHECKPOINT_FORMAT!r} -- refusing to resume"
        )
    recorded_seeds = manifest.get("seeds_digest")
    computed_seeds = seeds_digest(seeds)
    if recorded_seeds != computed_seeds:
        raise CheckpointError(
            f"seeds digest mismatch: manifest {recorded_seeds} vs computed {computed_seeds} "
            "-- these seeds were never run against this state; refusing to resume"
        )
    state = manifest.get("state")
    if not isinstance(state, dict):
        raise CheckpointError("checkpoint manifest missing the 'state' section")
    recorded_queue = state.get("queue_digest")
    computed_queue = queue_digest(db)
    if recorded_queue != computed_queue:
        raise CheckpointError(
            f"queue digest mismatch: manifest {recorded_queue} vs computed {computed_queue} "
            "-- the frontier changed since the manifest (different --limit or edited queue); "
            "refusing to resume"
        )
    recorded_rows = state.get("results_rows")
    if not isinstance(recorded_rows, dict):
        raise CheckpointError("checkpoint manifest missing 'state.results_rows'")
    computed_rows_digest = _sha256(_canonical_rows_text(recorded_rows))
    if state.get("results_digest") != computed_rows_digest:
        raise CheckpointError(
            f"manifest self-integrity failed: recorded results_rows do not hash to the "
            f"recorded results_digest ({state.get('results_digest')} vs {computed_rows_digest}); "
            "the manifest was edited after it was written; refusing to resume"
        )
    rows_now = _results_rows(db)
    for url, recorded in sorted(recorded_rows.items()):
        current = rows_now.get(url)
        if current is None:
            raise CheckpointError(
                f"state digest drift: outcome for {url} recorded in the manifest "
                "is missing from the state db; refusing to resume"
            )
        if current != recorded:
            raise CheckpointError(
                f"state digest drift: {url} recorded as {recorded!r} in the manifest "
                f"but the state db says {current!r}; refusing to resume"
            )
