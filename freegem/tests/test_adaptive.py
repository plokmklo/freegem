"""Contract tests for freegem.adaptive. Hermetic: fake clock, no network."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from freegem.adaptive import (
    COOLDOWN_AFTER,
    DEFAULT_ORDER,
    AdaptiveLadder,
    looks_like_challenge,
    wall_class,
)


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def make(**kw: Any) -> AdaptiveLadder:
    return AdaptiveLadder(clock=kw.pop("clock", Clock()), **kw)


HOST, WALL = "example.com", "forbidden"


def test_clear_drops_every_statistic() -> None:
    a = make()
    a.record(HOST, WALL, "reader", True)
    a.record(HOST, WALL, "reader", False)
    a.clear()
    assert a.stats(HOST, WALL) == {}
    assert a.plan(HOST, WALL) == list(DEFAULT_ORDER)


def test_cold_start_reproduces_default_order() -> None:
    assert make().plan(HOST, WALL) == list(DEFAULT_ORDER)


def test_failures_and_wins_reorder_the_ladder() -> None:
    a = make()
    for _ in range(2):
        a.record(HOST, WALL, "browser-headers", False)
    for _ in range(3):
        a.record(HOST, WALL, "reader", True)
    assert a.plan(HOST, WALL)[0] == "reader"


def test_cooldown_demotes_but_ladder_is_never_empty() -> None:
    clock = Clock()
    a = make(clock=clock)
    for _ in range(COOLDOWN_AFTER):
        a.record(HOST, WALL, "browser-headers", False)
    plan = a.plan(HOST, WALL)
    assert "browser-headers" not in plan and len(plan) == len(DEFAULT_ORDER) - 1
    clock.t += 2 * 3600  # cooldown (1h) expires
    assert "browser-headers" in a.plan(HOST, WALL)
    for rung in DEFAULT_ORDER:
        for _ in range(COOLDOWN_AFTER):
            a.record(HOST, WALL, rung, False)
    assert sorted(a.plan(HOST, WALL)) == sorted(DEFAULT_ORDER)


def test_unverified_win_is_recorded_as_a_loss() -> None:
    a = make()
    for _ in range(3):
        a.record(HOST, WALL, "browser-render", True, verified=False)
    s = a.stats(HOST, WALL)["browser-render"]
    assert s["wins"] == 0 and s["losses"] > 0
    assert a.plan(HOST, WALL)[0] != "browser-render"


def test_gated_off_and_unavailable_rungs_are_excluded() -> None:
    a = make()
    plan = a.plan(HOST, WALL, gated_off={"browser-render", "flaresolverr"})
    assert "browser-render" not in plan and "flaresolverr" not in plan
    plan = a.plan(HOST, WALL, available={"reader", "wayback"})
    assert plan == ["reader", "wayback"]


def test_no_bypass_statuses_never_engage_the_ladder() -> None:
    for status in (402, 451):
        assert wall_class(status) == "no_bypass"
    assert make().plan(HOST, "no_bypass") == []


def test_new_host_inherits_prior_from_same_wall_class() -> None:
    a = make()
    for _ in range(6):
        a.record("a.example", WALL, "browser-headers", False)
        a.record("a.example", WALL, "reader", True)
    assert a.plan("b.example", WALL)[0] == "reader"


def test_decay_lets_old_evidence_fade() -> None:
    clock = Clock()
    a = make(clock=clock)
    for _ in range(8):
        a.record(HOST, WALL, "reader", False)
    assert a.stats(HOST, WALL)["reader"]["losses"] >= 7.9
    clock.t += 14 * 86400 * 4  # four half-lives
    assert a.stats(HOST, WALL)["reader"]["losses"] < 1.0


def test_persistence_round_trip_and_corrupt_file(tmp_path: Path) -> None:
    p = str(tmp_path / "adaptive.json")
    a = make(path=p)
    for _ in range(3):
        a.record(HOST, WALL, "reader", True)
    a.flush()
    b = make(path=p)
    assert b.stats(HOST, WALL)["reader"]["wins"] == pytest.approx(3.0, abs=0.01)
    (tmp_path / "adaptive.json").write_text("{not json")
    assert make(path=p).plan(HOST, WALL) == list(DEFAULT_ORDER)
    (tmp_path / "adaptive.json").write_text(json.dumps({"stats": "nope"}))
    assert make(path=p).plan(HOST, WALL) == list(DEFAULT_ORDER)


def test_advisor_is_cold_only_validated_and_failure_safe() -> None:
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    def advisor(host: str, wall: str, candidates: Sequence[str]) -> list[str]:
        calls.append((host, wall, tuple(candidates)))
        return ["wayback", "bogus", "wayback", "reader"]

    a = make(advisor=advisor)
    plan = a.plan(HOST, WALL)
    assert plan[:2] == ["wayback", "reader"]
    assert sorted(plan) == sorted(DEFAULT_ORDER) and "bogus" not in plan
    for _ in range(3):
        a.record(HOST, WALL, "reader", True)
    a.plan(HOST, WALL)
    assert len(calls) == 1  # warm pair: statistics decide, advisor not asked

    def boom(*_: object) -> list[str]:
        raise RuntimeError("gemini rate limited")

    assert make(advisor=boom).plan(HOST, WALL) == list(DEFAULT_ORDER)


def test_walk_stops_at_first_verified_win_and_records_everything() -> None:
    a = make()
    rungs: dict[str, Callable[[], str]] = {
        "browser-headers": lambda: (_ for _ in ()).throw(RuntimeError("403")),
        "reader": lambda: "real article text " * 30,
        "wayback": lambda: pytest.fail("must not run after a win"),
    }
    # Assign before unpacking: passing the tuple return straight into a
    # multi-target assignment makes mypy solve T as str | None (the None in the
    # return type joins T), and the verify lambda then sees str | None.
    out = a.walk(HOST, WALL, rungs, verify=lambda t: len(t) > 80)
    result, trace = out
    assert result and [t.rung for t in trace] == ["browser-headers", "reader"]
    assert trace[0].ok is False and "RuntimeError" in trace[0].error
    assert trace[1].ok is True
    st = a.stats(HOST, WALL)
    assert st["browser-headers"]["losses"] == 1 and st["reader"]["wins"] == 1


def test_walk_rejects_challenge_shaped_wins() -> None:
    a = make()
    wall_page = "Just a moment... Checking your browser before accessing"
    rungs: dict[str, Callable[[], str]] = {
        "browser-headers": lambda: wall_page,
        "reader": lambda: "genuine content " * 40,
    }
    out = a.walk(
        HOST, WALL, rungs, verify=lambda t: not looks_like_challenge(t) and len(t) > 80
    )
    result, trace = out
    assert result is not None and result.startswith("genuine")
    assert trace[0].ok is False and trace[0].error == "failed content gate"
    assert a.stats(HOST, WALL)["browser-headers"]["wins"] == 0


def test_walk_respects_the_deadline_between_rungs() -> None:
    ticks = iter([0.0, 0.0, 1.0, 100.0, 100.0, 100.0, 100.0])
    a = make()
    rungs = {r: (lambda: None) for r in ("browser-headers", "reader", "wayback")}
    _, trace = a.walk(
        HOST, WALL, rungs, verify=bool, budget_sec=5.0, monotonic=lambda: next(ticks)
    )
    assert trace[-1].error == "budget exhausted"
    assert "budget exhausted" not in str(a.stats(HOST, WALL))


def test_wall_class_buckets() -> None:
    assert wall_class(429) == "rate_limited"
    assert wall_class(403) == "forbidden"
    assert wall_class(503) == "server_error"
    assert wall_class(200, "Just a moment...") == "challenge"
    assert wall_class(None, error="connection timed out") == "network"
    assert wall_class(200, "tiny") == "thin"
    assert wall_class(200, "x" * 2000) == "other"


def test_long_dense_interstitial_is_still_a_challenge() -> None:
    page = "Just a moment. Verify you are human. CAPTCHA required. " + "x " * 2000
    assert looks_like_challenge(page)
    assert not looks_like_challenge(
        "an article that mentions captcha once. " + "x " * 2000
    )
