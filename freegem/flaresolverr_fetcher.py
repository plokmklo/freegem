"""The flaresolverr rung: an adapter for a FlareSolverr instance YOU run.

Research context (docs/ANTIBOT_RESEARCH.md, v2.12.0 sweep): FlareSolverr
(15.9k stars) is the community's standard self-hosted answer to managed
challenge walls -- it launches a real browser, waits out the challenge,
and returns the solved HTML plus cookies over a small HTTP API. The walls
that survive every other rung (TLS-impersonation-proof managed challenges,
"checking your browser" interstitials) are exactly this rung's population.

The boundary that keeps this honest, per NON_GOALS:

* FreeGem SHIPS no solver, no stealth, no challenge automation. This
  module is a thin API client; the solving (if any) happens inside a
  FlareSolverr process the user installed, configured, and exposed at an
  endpoint they chose. OFF by default (config
  ``smart_fetch_flaresolverr`` holds the endpoint URL; empty = the rung
  does not exist).
* The endpoint is trusted user configuration, exactly like ``cfg.proxy``
  and ``cfg.cookie_file``: it is not fetched based on page content, and
  the URL it is asked to solve is still subject to the ladder's own
  refusal rules upstream (our guards never delegate).
* The result is labeled ``via="flaresolverr"`` all the way up: a
  browser-mediated read obtained through a solver service is never
  presented as a plain verbatim fetch.

API (FlareSolverr v1, unchanged for years -- that stability is why it
became the standard): ``POST {endpoint}/v1`` with
``{"cmd": "request.get", "url": ..., "maxTimeout": ...}``; the reply's
``solution.response`` carries the solved page HTML, ``solution.status``
its HTTP status.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

__all__ = ["FlareSolverrError", "flaresolverr_html", "is_configured"]


class FlareSolverrError(Exception):
    """The flaresolverr rung could not read the page; message says exactly why."""


def is_configured(cfg: Any) -> bool:
    """True when the user configured a FlareSolverr endpoint."""
    endpoint = getattr(cfg, "smart_fetch_flaresolverr", "") or ""
    return bool(str(endpoint).strip())


def flaresolverr_html(cfg: Any, url: str, *, timeout: float) -> str:
    """Ask the configured FlareSolverr for ``url``; return the solved HTML.

    ``timeout`` bounds the whole call (FlareSolverr's own ``maxTimeout``
    gets almost all of it; the socket timeout gets the rest). Raises
    :class:`FlareSolverrError` with the service's own status/message on
    refusal, so the ladder's taxonomy records what the service said.
    """
    endpoint = str(getattr(cfg, "smart_fetch_flaresolverr", "") or "").strip().rstrip("/")
    if not endpoint:
        raise FlareSolverrError(
            f"flaresolverr disabled for {url!r} (smart_fetch_flaresolverr is empty)"
        )
    wall = max(5.0, float(timeout))
    payload = json.dumps(
        {"cmd": "request.get", "url": url, "maxTimeout": int(wall * 1000) - 1000}
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint + "/v1",
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=wall) as response:
            body = response.read(16 * 1024 * 1024)
    except Exception as exc:
        raise FlareSolverrError(f"flaresolverr request failed for {url!r}: {exc}") from exc
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise FlareSolverrError(
            f"flaresolverr at {endpoint} returned unusable JSON for {url!r}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise FlareSolverrError(
            f"flaresolverr at {endpoint} returned non-object JSON for {url!r}: {str(data)[:80]!r}"
        )
    if str(data.get("status")) != "ok":
        message = str(data.get("message") or data.get("error") or "unknown error")
        raise FlareSolverrError(f"flaresolverr refused {url!r}: {message}")
    solution = data.get("solution") or {}
    if not isinstance(solution, dict):
        # v1 success replies carry a solution OBJECT; a list/string/null here
        # must be refused by name -- `solution.get` would raise AttributeError
        # past the ladder's FlareSolverrError taxonomy.
        raise FlareSolverrError(
            f"flaresolverr at {endpoint} returned a malformed solution for {url!r}: "
            f"{str(solution)[:80]!r}"
        )
    html = str(solution.get("response") or "")
    try:
        status = int(solution.get("status") or 0)
    except (TypeError, ValueError):
        # A non-numeric status must not leak ValueError out of this client.
        raise FlareSolverrError(
            f"flaresolverr solved {url!r} but reported a malformed status "
            f"{solution.get('status')!r}"
        ) from None
    if status and not (200 <= status < 300):
        raise FlareSolverrError(f"flaresolverr solved {url!r} but got HTTP {status}")
    if not html.strip():
        raise FlareSolverrError(f"flaresolverr returned no content for {url!r}")
    return html
