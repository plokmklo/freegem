"""JavaScript / XHR endpoint discovery and best-first priority scoring.

Discovers API endpoints, AJAX request URLs, and fetch calls embedded in inline script blocks or JS bundles,
and calculates best-first priority scores for URL frontier scheduling.
"""

from __future__ import annotations

import re
import urllib.parse


def discover_xhr_endpoints(html_or_js: str, base_url: str = "") -> list[str]:
    """Extract XHR / fetch API endpoints from HTML or JavaScript code."""
    endpoints = set()

    # Pattern 1: fetch('/api/...') or fetch("http...")
    fetch_matches = re.findall(r"""fetch\s*\(\s*["']([^"']+)["']""", html_or_js)
    endpoints.update(fetch_matches)

    # Pattern 2: axios.get('/api/...') or axios('/api/...')
    axios_matches = re.findall(r"""axios(?:\.\w+)?\s*\(\s*["']([^"']+)["']""", html_or_js)
    endpoints.update(axios_matches)

    # Pattern 3: $.ajax({ url: '/api/...' }) or $.get('/api/...')
    ajax_matches = re.findall(r"""\$\.ajax\s*\(\s*\{\s*url\s*:\s*["']([^"']+)["']""", html_or_js)
    endpoints.update(ajax_matches)

    # Pattern 4: Explicit API path heuristics e.g., "/api/v1/users"
    api_paths = re.findall(r"""["'](/api/v\d+/[a-zA-Z0-9_\-/]+)["']""", html_or_js)
    endpoints.update(api_paths)

    resolved_urls = []
    for ep in endpoints:
        if ep.startswith(("http://", "https://")):
            resolved_urls.append(ep)
        elif (base_url and ep.startswith("/")) or base_url:
            resolved_urls.append(urllib.parse.urljoin(base_url, ep))

    return sorted(set(resolved_urls))


def compute_best_first_score(
    url: str,
    depth: int = 0,
    keywords: list[str] | None = None,
    yield_count: int = 0,
    domain_weight: float = 1.0,
) -> float:
    """Compute best-first priority score for frontier scheduling.

    Higher score = higher priority (dequeued first).
    Score factors:
    - Base score: 100
    - Depth penalty: -10 per depth level
    - Keyword boost: +20 per keyword match in URL path
    - Yield boost: +5 per extracted item yielded from domain
    - Domain weight multiplier
    """
    score = 100.0

    # Depth penalty
    score -= (depth * 10.0)

    # Keyword match boost
    if keywords:
        url_lower = url.lower()
        for kw in keywords:
            if kw.lower() in url_lower:
                score += 20.0

    # Historical extraction yield boost
    score += min(50.0, yield_count * 5.0)

    # Apply domain weight
    score *= domain_weight

    return max(1.0, score)
