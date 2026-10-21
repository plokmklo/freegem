"""Selenium Grid capacity management (2.9.0, P3 #4).

``fetch_with_selenium`` renders one page on ONE browser endpoint; a fleet of
Grid nodes needs a dispatcher that owns the CAPACITY: which node has a free
session, what happens when every node is saturated, and what happens when a
node dies mid-fleet. This module is that dispatcher.

Design:

* Nodes register explicitly with a session budget (``max_sessions``). A node
  also declares its concurrency honesty -- the pool never assumes a node can
  take more sessions than it advertised.
* ``checkout`` returns a :class:`GridLease` for the least-loaded active node
  (lowest in-flight/max-sessions ratio, ties broken by registration order).
  When every node is saturated the caller BLOCKS on a condition variable up
  to ``checkout_timeout_sec`` and then gets :class:`GridPoolTimeout` -- an
  honest backpressure signal, not a queue that grows forever.
* ``release`` returns the session to the budget and wakes one waiter.
* A node can be disabled (``set_active``) for drain/maintenance: new leases
  skip it; existing leases finish naturally.
* ``fetch_with_grid`` composes the pool with the existing renderer: lease a
  node, point ``opts.grid_url`` at its endpoint, render, release -- including
  on failure paths.

No selenium import here: the pool is pure scheduling, so capacity logic is
testable without a browser or the selenium package.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from .selenium_fetcher import SeleniumOptions, SeleniumResult, fetch_with_selenium

__all__ = [
    "GridLease",
    "GridNode",
    "GridPool",
    "GridPoolTimeout",
    "fetch_with_grid",
]


class GridPoolTimeout(Exception):
    """Every node sat saturated for the whole checkout window."""


@dataclass
class GridNode:
    """One Grid endpoint and its honest session budget."""

    name: str
    endpoint: str
    max_sessions: int = 1
    active: bool = True


@dataclass
class GridLease:
    """A claimed browser session on one node; return it with release()."""

    node_name: str
    endpoint: str
    acquired_at: float = field(default_factory=time.monotonic)
    released: bool = False


class GridPool:
    """Thread-safe least-loaded dispatcher over a fixed set of Grid nodes."""

    def __init__(
        self, nodes: list[GridNode] | None = None, *, checkout_timeout_sec: float = 30.0
    ) -> None:
        self._cond = threading.Condition()
        self._nodes: dict[str, GridNode] = {}
        self._in_flight: dict[str, int] = {}
        self._order: list[str] = []
        self._checkout_timeout_sec = checkout_timeout_sec
        self.checkouts_total = 0
        self.timeouts_total = 0
        for node in nodes or []:
            self.add_node(node.name, node.endpoint, node.max_sessions)

    # -- fleet management ----------------------------------------------------

    def add_node(self, name: str, endpoint: str, max_sessions: int = 1) -> None:
        if not name or not endpoint:
            raise ValueError("node needs a name and an endpoint")
        if max_sessions < 1:
            raise ValueError("max_sessions must be >= 1")
        with self._cond:
            if name in self._nodes:
                raise ValueError(f"node {name!r} already registered")
            self._nodes[name] = GridNode(name=name, endpoint=endpoint, max_sessions=max_sessions)
            self._in_flight[name] = 0
            self._order.append(name)
            self._cond.notify_all()

    def remove_node(self, name: str) -> bool:
        with self._cond:
            if name not in self._nodes:
                return False
            if self._in_flight.get(name, 0) > 0:
                raise ValueError(
                    f"node {name!r} still holds {self._in_flight[name]} lease(s); drain first"
                )
            del self._nodes[name]
            del self._in_flight[name]
            self._order.remove(name)
            self._cond.notify_all()
            return True

    def set_active(self, name: str, active: bool) -> None:
        with self._cond:
            node = self._nodes.get(name)
            if node is None:
                raise KeyError(name)
            node.active = active
            if active:
                self._cond.notify_all()

    # -- session lifecycle -----------------------------------------------------

    def _pick_node(self) -> GridNode | None:
        best: GridNode | None = None
        best_ratio = 2.0
        for name in self._order:
            node = self._nodes[name]
            if not node.active:
                continue
            in_flight = self._in_flight[name]
            if in_flight >= node.max_sessions:
                continue
            ratio = in_flight / node.max_sessions
            if ratio < best_ratio:
                best, best_ratio = node, ratio
        return best

    def checkout(self, timeout_sec: float | None = None) -> GridLease:
        """Claim a session on the least-loaded active node."""
        window = self._checkout_timeout_sec if timeout_sec is None else timeout_sec
        deadline = time.monotonic() + window
        with self._cond:
            while True:
                node = self._pick_node()
                if node is not None:
                    self._in_flight[node.name] += 1
                    self.checkouts_total += 1
                    return GridLease(node_name=node.name, endpoint=node.endpoint)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.timeouts_total += 1
                    raise GridPoolTimeout(
                        f"all {len(self._nodes)} grid node(s) saturated for the checkout window "
                        f"({window:.1f}s); raise capacity or the timeout"
                    )
                self._cond.wait(timeout=remaining)

    def release(self, lease: GridLease) -> None:
        with self._cond:
            # Test the flag INSIDE the lock: an unlocked check lets two threads
            # racing to release the SAME lease both pass it and decrement
            # twice, silently overbooking the node past max_sessions (the
            # max(0, ...) clamp hides the underflow but steals real capacity).
            if lease.released:
                return
            lease.released = True
            if lease.node_name in self._in_flight:
                self._in_flight[lease.node_name] = max(0, self._in_flight[lease.node_name] - 1)
            self._cond.notify()

    @contextmanager
    def session(self, timeout_sec: float | None = None) -> Iterator[GridLease]:
        """checkout/release as a block; release runs even on exception."""
        lease = self.checkout(timeout_sec)
        try:
            yield lease
        finally:
            self.release(lease)

    # -- observability ---------------------------------------------------------

    def stats(self) -> dict:
        with self._cond:
            per_node = {
                name: {
                    "endpoint": self._nodes[name].endpoint,
                    "max_sessions": self._nodes[name].max_sessions,
                    "active": self._nodes[name].active,
                    "in_flight": self._in_flight[name],
                }
                for name in self._order
            }
            return {
                "nodes": per_node,
                "capacity": sum(n["max_sessions"] for n in per_node.values() if n["active"]),
                "in_flight_total": sum(n["in_flight"] for n in per_node.values()),
                "checkouts_total": self.checkouts_total,
                "timeouts_total": self.timeouts_total,
            }


def fetch_with_grid(
    pool: GridPool, opts: SeleniumOptions, *, timeout_sec: float | None = None
) -> SeleniumResult:
    """Render one URL through the pool: lease -> render -> release."""
    started = time.monotonic()
    try:
        lease = pool.checkout(timeout_sec)
    except GridPoolTimeout as exc:
        return SeleniumResult(
            url=opts.url,
            final_url=opts.url,
            status=0,
            ok=False,
            error=f"grid capacity: {exc}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    try:
        opts.grid_url = lease.endpoint
        return fetch_with_selenium(opts)
    finally:
        pool.release(lease)
