"""JS/XHR endpoint discovery and best-first URL scoring (2.8.0).

Two jobs the HTML link extractor cannot do:

* modern pages hide their data behind ``fetch("/api/v1/...")`` calls, XHR
  ``open("GET", url)``, and axios calls. :func:`discover_js_endpoints`
  reads the inline and referenced scripts the page ALREADY delivered and
  lifts out those endpoints — no headless browser, no execution, just the
  strings the page itself volunteers;
* a frontier ordered by discovery time wastes the budget on whatever was
  seen first. :func:`score_url` + :class:`BestFirstPrioritizer` rank the
  frontier: same-origin pages near the root, documentation-flavored paths,
  and fresh content first; media files, login walls, and deep junk last.

Both are pure functions over text/URLs, so they test without network and
fail closed (an unparseable script yields no endpoints, not a crash).
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "BestFirstPrioritizer",
    "ScoreWeights",
    "discover_js_endpoints",
    "score_url",
]

# fetch("/api/x") or fetch("https://api.host/x"), axios.get('/api/x'),
# $.getJSON("/x"), open("GET", "/x") — root-relative and absolute, quoted
# with ' " or ` (template literal). Concatenated/multipart strings are
# deliberately skipped: half of them are per-page dynamic junk.
_URL_BODY = r"""(?:https?://[^"'`\s]{1,300}|/[^"'`\s]{1,300})"""
_FETCH_RE = re.compile(
    r"""(?:\bfetch|\baxios(?:\.(?:get|post|put|delete|patch))?|\$\.(?:get|getJSON|ajax)|
        \bopen\s*\(\s*["'](?:GET|POST|PUT|DELETE|HEAD)["']\s*,)
    \s*\(\s*(?:event\.preventDefault\(\)\s*,\s*)?
    ["'`]("""
    + _URL_BODY
    + r""")["'`]""",
    re.VERBOSE,
)
_OPEN_RE = re.compile(
    r"""\bopen\s*\(\s*["'](GET|POST|PUT|DELETE|HEAD)["']\s*,\s*["'`](""" + _URL_BODY + r""")["'`]"""
)
_SRCDOC_HREF_RE = re.compile(r"""(?:href|action|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

#: Path segments that usually mean "the good stuff". Kept lowercase.
_POSITIVE_SEGMENTS = (
    "docs",
    "documentation",
    "api",
    "reference",
    "guide",
    "guides",
    "tutorial",
    "manual",
    "blog",
    "article",
    "articles",
    "post",
    "research",
    "paper",
    "whitepaper",
    "spec",
    "specs",
    "faq",
    "help",
    "kb",
    "release",
    "changelog",
)
#: Segments that almost never help a reading crawl.
_NEGATIVE_SEGMENTS = (
    "login",
    "signin",
    "signup",
    "register",
    "logout",
    "cart",
    "checkout",
    "wp-admin",
    "admin",
    "account",
    "search",
    "tag",
    "tags",
    "category",
    "print",
    "reply",
    "share",
    "trackback",
    "calendar",
    "feed",
    "attachment",
)
#: File extensions not worth fetching as documents.
_SKIP_EXTENSIONS = (
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".css",
    ".js",
    ".zip",
    ".tar",
    ".gz",
    ".rar",
    ".7z",
    ".exe",
    ".dmg",
    ".mp4",
    ".mp3",
    ".avi",
    ".mov",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
)


def discover_js_endpoints(html: str, *, base_url: str = "") -> list[str]:
    """Extract API/XHR endpoints from inline JS and HTML in one page.

    Returns ABSOLUTE URLs (``base_url`` required for relative paths),
    deduplicated, in first-seen order. Fragments and non-http(s) schemes are
    dropped; ``//cdn`` protocol-relative URLs are resolved against base_url.
    """
    if not html:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        raw = raw.strip().strip("\"'`").rstrip(",")
        if not raw or raw.startswith(("javascript:", "mailto:", "tel:", "data:")):
            return
        if "${" in raw or ("{" in raw and "}" in raw):  # unfilled template literal
            return
        abs_url = _absolutize(raw, base_url)
        if abs_url and abs_url not in seen:
            seen.add(abs_url)
            found.append(abs_url)

    for m in _FETCH_RE.finditer(html):
        add(m.group(1))
    for m in _OPEN_RE.finditer(html):
        add(m.group(2))
    for m in _SRCDOC_HREF_RE.finditer(html):
        candidate = m.group(1)
        if candidate.startswith(("#", "?")):
            continue
        add(candidate)
    return found


def _absolutize(raw: str, base_url: str) -> str:
    if raw.startswith("//"):
        if not base_url:
            return ""
        parts = urllib.parse.urlsplit(base_url)
        return f"{parts.scheme}:{raw.split('#', 1)[0]}"  # fragments dropped like http(s)
    if raw.startswith(("http://", "https://")):
        return raw.split("#", 1)[0]
    if raw.startswith("/"):
        if not base_url:
            return ""
        parts = urllib.parse.urlsplit(base_url)
        return f"{parts.scheme}://{parts.netloc}{raw.split('#', 1)[0]}"
    return ""  # relative-without-slash needs page context; skip (too noisy)


@dataclass(frozen=True)
class ScoreWeights:
    """Knobs of the best-first score. Higher score = fetch sooner."""

    positive_segment: float = 3.0
    negative_segment: float = -4.0
    same_origin: float = 2.0
    depth_penalty: float = 0.5  # per path segment beyond the second
    root_bonus: float = 2.0  # shallow paths near the root
    query_penalty: float = -0.5  # querystrings are usually junk params
    mobile_penalty: float = -2.0  # m., amp. duplicates of canonical content
    base: float = 1.0


def score_url(url: str, *, origin: str = "", weights: ScoreWeights | None = None) -> float:
    """Best-first score for one URL. Pure, deterministic, explainable.

    Never negative (floored at 0): a bad URL should sink to the back, not
    go below the neutral base of an unclassified one, or priority-ordering
    inversions get confusing to debug.
    """
    w = weights or ScoreWeights()
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return 0.0
    path = parts.path or "/"
    segments = [s for s in path.lower().split("/") if s]

    score = w.base
    lowered = path.lower()
    for seg in _POSITIVE_SEGMENTS:
        if seg in segments:
            score += w.positive_segment
    for seg in _NEGATIVE_SEGMENTS:
        if seg in segments:
            score += w.negative_segment

    if origin and parts.netloc == urllib.parse.urlsplit(origin).netloc:
        score += w.same_origin
    host = (parts.hostname or "").lower()
    if host.startswith("m.") or host.startswith("amp.") or ".amp." in host:
        score += w.mobile_penalty

    score -= max(0, len(segments) - 2) * w.depth_penalty
    if len(segments) <= 1:
        score += w.root_bonus
    if parts.query:
        score += w.query_penalty

    for ext in _SKIP_EXTENSIONS:
        if lowered.endswith(ext):
            return 0.0

    return max(0.0, round(score, 2))


@dataclass
class BestFirstPrioritizer:
    """Ranks a batch of discovered URLs, best first.

    ``top_k`` bounds the output so a page linking to 10,000 URLs cannot
    flood the frontier (the runner's budgets still apply downstream). Ties
    break by shorter path, then alphabetically — deterministic output makes
    reruns comparable.
    """

    origin: str
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    top_k: int = 200

    def prioritize(self, urls: list[str]) -> list[tuple[str, float]]:
        scored = [(u, score_url(u, origin=self.origin, weights=self.weights)) for u in urls]
        scored.sort(key=lambda t: (-t[1], self._path_len(t[0]), t[0]))
        return scored[: self.top_k]

    def enqueue_plan(self, urls: list[str]) -> list[dict[str, Any]]:
        """Frontier-ready records: url + computed priority (1 best .. 9 worst)."""
        plan: list[dict[str, Any]] = []
        for url, score in self.prioritize(urls):
            priority = max(1, min(9, 10 - int(score)))
            plan.append({"url": url, "priority": priority, "score": score})
        return plan

    @staticmethod
    def _path_len(url: str) -> int:
        try:
            return len(urllib.parse.urlsplit(url).path)
        except ValueError:
            return 0
