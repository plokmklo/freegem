"""freegem.adaptive - outcome-driven rung selection for the smart-fetch ladder.

The ladder today is a fixed order plus "remember the last winner per host".
That has three holes (see issues #57, #60, #25):

  1. it only learns from wins, never from failures;
  2. a bot wall that slips through a content gate gets remembered as a win;
  3. nothing bounds how long a walk may take.

This module is the missing policy layer. It does NOT add new ways around a
wall: it only decides the ORDER in which the existing, legitimate rungs are
tried, learns that order from verified outcomes, and keeps the same honesty
rules (no CAPTCHA solving, no identity rotation, 402/451 never bypassed).

Policy, per (host, wall_class, rung):
  * Laplace/prior-smoothed success rate, decayed with a 14-day half-life;
  * the prior comes from the same (wall_class, rung) across ALL hosts, so a
    brand-new host inherits what worked on similar walls elsewhere;
  * a small UCB exploration bonus so a rung is not written off after one miss;
  * utility = optimistic_success / sqrt(cost), so cheap rungs win ties;
  * 3 consecutive failures put a rung on cooldown for that host (1h, doubling,
    capped at 24h) - but cooldown never empties the ladder;
  * a "win" that fails the caller's content gate is recorded as a LOSS.

Optional `advisor` hook (e.g. one Gemini call): consulted ONLY while a
host/wall pair is cold (< advisor_min_obs observations). Its answer is a
prior, validated against the known rung names; any error falls back to the
statistical policy. Gemini calls are rate-limited (~4-6/burst), so this is
off unless you pass one.

Stdlib only. Not wired into smart_fetch.py yet: see `AdaptiveLadder.walk`
for the drop-in runner and `plan` for a drop-in `_strategy_order`.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

T = TypeVar("T")

# Matches the documented default ladder order (README "Smart fetching").
DEFAULT_ORDER = (
    "browser-headers",
    "tls-browser",
    "reader",
    "browser-render",
    "flaresolverr",
    "wayback",
    "ai-search",
)

# Relative cost weights, monotone non-decreasing in DEFAULT_ORDER so a cold
# start reproduces the default order exactly. Tune freely; only ratios matter.
RUNG_COST = {
    "browser-headers": 1.0,
    "tls-browser": 2.0,
    "reader": 2.0,
    "browser-render": 4.0,
    "flaresolverr": 5.0,
    "wayback": 5.0,
    "ai-search": 6.0,
}

NO_BYPASS_STATUS = frozenset({402, 451})

CHALLENGE_MARKERS = (
    "just a moment",
    "checking your browser",
    "attention required",
    "verify you are human",
    "are you a robot",
    "captcha",
    "access denied",
    "enable javascript and cookies",
    "unusual traffic",
)

HALF_LIFE_SEC = 14 * 86400
PRIOR_WEIGHT = 4.0
EXPLORE_C = 0.25
COOLDOWN_AFTER = 3
COOLDOWN_BASE_SEC = 3600
COOLDOWN_MAX_SEC = 86400
MAX_ENTRIES = 5000


def looks_like_challenge(text: str) -> bool:
    """Phrase-density test, length-independent for dense pages (issue #57).

    One marker in a short page, or three distinct markers in any page.
    """
    low = (text or "").lower()
    hits = sum(1 for m in CHALLENGE_MARKERS if m in low)
    return (hits >= 1 and len(low) < 800) or hits >= 3


def wall_class(status: int | None, text: str = "", error: str = "") -> str:
    """Bucket the FIRST failure of a fetch into a coarse wall class."""
    if status in NO_BYPASS_STATUS:
        return "no_bypass"
    if status == 429:
        return "rate_limited"
    if looks_like_challenge(text):
        return "challenge"
    if status in (401, 403, 406, 407):
        return "forbidden"
    if status is not None and status >= 500:
        return "server_error"
    low_err = (error or "").lower()
    if any(
        w in low_err for w in ("timed out", "timeout", "reset", "resolve", "refused")
    ):
        return "network"
    if len((text or "").strip()) < 400:
        return "thin"
    return "other"


@dataclass
class _Stat:
    wins: float = 0.0
    losses: float = 0.0
    ema_sec: float = 0.0
    last: float = 0.0
    streak: int = 0
    cooldown_until: float = 0.0

    def to_dict(self) -> dict:
        return {
            "wins": self.wins,
            "losses": self.losses,
            "ema_sec": self.ema_sec,
            "last": self.last,
            "streak": self.streak,
            "cooldown_until": self.cooldown_until,
        }

    @classmethod
    def from_dict(cls, d: Mapping) -> _Stat:
        return cls(
            wins=max(0.0, float(d.get("wins", 0))),
            losses=max(0.0, float(d.get("losses", 0))),
            ema_sec=max(0.0, float(d.get("ema_sec", 0))),
            last=float(d.get("last", 0)),
            streak=max(0, int(d.get("streak", 0))),
            cooldown_until=float(d.get("cooldown_until", 0)),
        )


@dataclass
class Attempt:
    rung: str
    ok: bool
    seconds: float
    error: str = ""


Advisor = Callable[[str, str, Sequence[str]], Sequence[str] | None]


def default_path() -> str:
    base = os.environ.get("FREEGEM_CACHE_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "freegem"
    )
    return os.path.join(base, "adaptive_ladder.json")


class AdaptiveLadder:
    def __init__(
        self,
        path: str | None = None,
        *,
        advisor: Advisor | None = None,
        advisor_min_obs: float = 3.0,
        clock: Callable[[], float] = time.time,
        order: Sequence[str] = DEFAULT_ORDER,
        costs: Mapping[str, float] = RUNG_COST,
    ) -> None:
        self.path = path
        self.advisor = advisor
        self.advisor_min_obs = advisor_min_obs
        self.clock = clock
        self.order = tuple(order)
        self.costs = dict(costs)
        self._lock = threading.Lock()
        self._stats: dict[str, _Stat] = {}
        self._dirty = False
        if path:
            self._load()

    # ---- persistence -----------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:  # type: ignore[arg-type]
                raw = json.load(fh)
            entries = raw["stats"]
            if not isinstance(entries, dict):
                return
        except (OSError, ValueError, KeyError, TypeError):
            return  # missing or corrupt: start fresh, never crash a fetch
        for key, d in entries.items():
            try:
                self._stats[str(key)] = _Stat.from_dict(d)
            except (TypeError, ValueError):
                continue

    def flush(self) -> None:
        if not self.path:
            return
        with self._lock:
            if not self._dirty:
                return
            items = sorted(self._stats.items(), key=lambda kv: kv[1].last, reverse=True)
            payload = {
                "version": 1,
                "stats": {k: s.to_dict() for k, s in items[:MAX_ENTRIES]},
            }
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=directory, prefix=".adaptive-", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                os.replace(tmp, self.path)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                return
            self._dirty = False

    # ---- statistics ------------------------------------------------------

    @staticmethod
    def _key(host: str, wall: str, rung: str) -> str:
        return f"{host.lower()}|{wall}|{rung}"

    def _decayed(
        self, host: str, wall: str, rung: str, now: float
    ) -> tuple[float, float]:
        s = self._stats.get(self._key(host, wall, rung))
        if s is None:
            return 0.0, 0.0
        f = 0.5 ** (max(0.0, now - s.last) / HALF_LIFE_SEC) if s.last else 1.0
        return s.wins * f, s.losses * f

    def record(
        self,
        host: str,
        wall: str,
        rung: str,
        ok: bool,
        seconds: float = 0.0,
        *,
        verified: bool = True,
    ) -> None:
        """Record one attempt. A fetch that 'worked' but failed the caller's
        content gate (verified=False) counts as a LOSS - never a win."""
        win = bool(ok and verified)
        now = self.clock()
        with self._lock:
            for h in (host, "*"):
                s = self._stats.setdefault(self._key(h, wall, rung), _Stat())
                if s.last:
                    f = 0.5 ** (max(0.0, now - s.last) / HALF_LIFE_SEC)
                    s.wins *= f
                    s.losses *= f
                s.last = now
                s.ema_sec = (
                    seconds if s.ema_sec == 0 else 0.7 * s.ema_sec + 0.3 * seconds
                )
                if win:
                    s.wins += 1
                else:
                    s.losses += 1
                if h == "*":
                    continue  # streak/cooldown are per host only
                if win:
                    s.streak = 0
                    s.cooldown_until = 0.0
                else:
                    s.streak += 1
                    if s.streak >= COOLDOWN_AFTER:
                        span = COOLDOWN_BASE_SEC * 2 ** (s.streak - COOLDOWN_AFTER)
                        s.cooldown_until = now + min(span, COOLDOWN_MAX_SEC)
            self._dirty = True

    def stats(self, host: str, wall: str) -> dict[str, dict]:
        """Inspectable view: rung -> wins/losses/streak/cooldown (for reports)."""
        now = self.clock()
        out: dict[str, dict] = {}
        with self._lock:
            for rung in self.order:
                s = self._stats.get(self._key(host, wall, rung))
                if s is None:
                    continue
                w, lo = self._decayed(host, wall, rung, now)
                out[rung] = {
                    "wins": round(w, 3),
                    "losses": round(lo, 3),
                    "streak": s.streak,
                    "cooling_down": s.cooldown_until > now,
                    "avg_sec": round(s.ema_sec, 3),
                }
        return out

    # ---- planning --------------------------------------------------------

    def _rank(self, host: str, wall: str, pool: Sequence[str], now: float) -> list[str]:
        obs = {r: self._decayed(host, wall, r, now) for r in pool}
        total = sum(w + lo for w, lo in obs.values())

        def key(rung: str) -> tuple[float, int]:
            w, lo = obs[rung]
            gw, gl = self._decayed("*", wall, rung, now)
            prior = (gw + 1.0) / (gw + gl + 2.0)
            n = w + lo
            p = (w + PRIOR_WEIGHT * prior) / (n + PRIOR_WEIGHT)
            optimistic = p + EXPLORE_C * math.sqrt(math.log(total + 2.0) / (n + 1.0))
            utility = optimistic / math.sqrt(self.costs.get(rung, 5.0))
            return (-utility, self.order.index(rung))

        return sorted(pool, key=key)

    def plan(
        self,
        host: str,
        wall: str,
        *,
        gated_off: Iterable[str] = (),
        available: Iterable[str] | None = None,
    ) -> list[str]:
        """Ordered rungs to try. Drop-in for smart_fetch._strategy_order."""
        if wall == "no_bypass":
            return []  # 402/451: never bypassed, ladder must not engage
        off = set(gated_off)
        avail = set(available) if available is not None else None
        cands = [
            r for r in self.order if r not in off and (avail is None or r in avail)
        ]
        if not cands:
            return []
        now = self.clock()
        with self._lock:
            active = [
                r
                for r in cands
                if getattr(
                    self._stats.get(self._key(host, wall, r)), "cooldown_until", 0.0
                )
                <= now
            ]
            pool = active or cands  # cooldown demotes; it never empties the ladder
            ranked = self._rank(host, wall, pool, now)
            seen = sum(sum(self._decayed(host, wall, r, now)) for r in cands)
        if self.advisor is not None and seen < self.advisor_min_obs:
            ranked = self._apply_advice(host, wall, ranked)
        return ranked

    def _apply_advice(self, host: str, wall: str, ranked: list[str]) -> list[str]:
        try:
            suggestion = self.advisor(host, wall, tuple(ranked))  # type: ignore[misc]
        except Exception:
            return ranked
        if not suggestion:
            return ranked
        valid: list[str] = []
        for name in suggestion:
            if isinstance(name, str) and name in ranked and name not in valid:
                valid.append(name)
        return valid + [r for r in ranked if r not in valid]

    # ---- runner ----------------------------------------------------------

    def walk(
        self,
        host: str,
        wall: str,
        rungs: Mapping[str, Callable[[], T]],
        verify: Callable[[T], bool],
        *,
        budget_sec: float = 60.0,
        gated_off: Iterable[str] = (),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> tuple[T | None, list[Attempt]]:
        """Try rungs in planned order; return (first verified result, trace).

        `rungs[name]()` returns a result or raises; `verify(result)` is the
        caller's honest content gate (challenge/zero-text detectors). Every
        attempt is recorded, wins AND losses. The deadline is checked between
        rungs; it cannot interrupt a rung already in flight (issue #25), so
        keep per-rung timeouts too.
        """
        order = self.plan(host, wall, gated_off=gated_off, available=rungs.keys())
        deadline = monotonic() + budget_sec
        attempts: list[Attempt] = []
        result: T | None = None
        for rung in order:
            if monotonic() >= deadline:
                attempts.append(Attempt(rung, False, 0.0, "budget exhausted"))
                break
            t0 = monotonic()
            err = ""
            out: T | None = None
            try:
                out = rungs[rung]()
            except Exception as exc:  # a rung failing is data, not a crash
                err = f"{type(exc).__name__}: {exc}"
            dt = monotonic() - t0
            good = False
            if out is not None:
                try:
                    good = bool(verify(out))
                except Exception as exc:
                    err = f"verify raised {type(exc).__name__}: {exc}"
                if not good and not err:
                    err = "failed content gate"
            self.record(host, wall, rung, out is not None, dt, verified=good)
            attempts.append(Attempt(rung, good, dt, err))
            if good:
                result = out
                break
        self.flush()
        return result, attempts
