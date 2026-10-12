"""Tests for JS/XHR discovery and best-first scoring (crawler/discovery.py)."""

from __future__ import annotations

from freegem.crawler.discovery import (
    BestFirstPrioritizer,
    ScoreWeights,
    discover_js_endpoints,
    score_url,
)

PAGE = """
<html><head>
<script>
  const cfg = {api: "/api/v2"};
  fetch("/api/v1/items?limit=50").then(r => r.json());
  axios.get('/api/v2/user/profile', {headers: {x: 1}});
  $.getJSON("/widgets/list", cb);
  const xhr = new XMLHttpRequest();
  xhr.open("GET", "/legacy/data.xml", true);
  xhr.open('POST', `/api/${version}/submit`);   // template literal: skipped
</script>
</head><body>
<a href="/docs/reference">API reference</a>
<a href="https://cdn.other.org/lib.js">lib</a>
<a href="#section">anchor</a>
<img src="/img/logo.png">
</body></html>
"""


def test_discover_fetch_and_xhr_endpoints() -> None:
    eps = discover_js_endpoints(PAGE, base_url="https://example.org/home")
    assert "https://example.org/api/v1/items?limit=50" in eps
    assert "https://example.org/api/v2/user/profile" in eps
    assert "https://example.org/widgets/list" in eps
    assert "https://example.org/legacy/data.xml" in eps


def test_discover_skips_templates_anchors_and_relative() -> None:
    eps = discover_js_endpoints(PAGE, base_url="https://example.org/home")
    assert not any("${" in e for e in eps)
    assert not any(e.endswith("#section") for e in eps)
    # plain hrefs ARE picked up (page's own links, useful for API docs pages)
    assert "https://example.org/docs/reference" in eps


def test_discover_empty_and_protocol_relative() -> None:
    assert discover_js_endpoints("") == []
    eps = discover_js_endpoints('<a href="//cdn.example.org/x.js">x</a>', base_url="https://example.org/")
    assert "https://cdn.example.org/x.js" in eps
    # no base_url: relative candidates are dropped, absolute kept
    assert discover_js_endpoints('fetch("/a")') == []
    assert discover_js_endpoints('fetch("https://abs.example.org/a")') != []


def test_discover_dedupes() -> None:
    eps = discover_js_endpoints(
        'fetch("/a"); fetch("/a"); fetch("/a")', base_url="https://x.org/"
    )
    assert eps == ["https://x.org/a"]


# ─── scoring ─────────────────────────────────────────────────────────────────


def test_docs_and_api_paths_score_high() -> None:
    docs = score_url("https://x.org/docs/reference/index", origin="https://x.org")
    api = score_url("https://x.org/api/v2/users", origin="https://x.org")
    plain = score_url("https://x.org/a/b/c", origin="https://x.org")
    assert docs > plain
    assert api > plain


def test_login_and_media_score_lowest() -> None:
    assert score_url("https://x.org/login") < score_url("https://x.org/article")
    assert score_url("https://x.org/img/photo.jpg") == 0.0
    assert score_url("https://x.org/downloads/app.exe") == 0.0


def test_same_origin_bonus_and_mobile_penalty() -> None:
    same = score_url("https://x.org/page", origin="https://x.org")
    other = score_url("https://other.org/page", origin="https://x.org")
    assert same > other
    mobile = score_url("https://m.x.org/page", origin="https://x.org")
    assert mobile < same


def test_depth_penalty_and_root_bonus() -> None:
    shallow = score_url("https://x.org/", origin="https://x.org")
    deep = score_url("https://x.org/a/b/c/d/e/f", origin="https://x.org")
    assert shallow > deep
    assert deep > 0.0  # floored, never negative


def test_query_penalty_applies() -> None:
    no_q = score_url("https://x.org/docs/page", origin="https://x.org")
    with_q = score_url("https://x.org/docs/page?utm_source=x&ref=t", origin="https://x.org")
    assert with_q < no_q


def test_score_deterministic_and_never_negative() -> None:
    urls = [
        "https://x.org/login?a=1",
        "https://x.org/very/deep/path/that/goes/on/and/on",
        "https://amp.x.org/thing",
    ]
    for u in urls:
        assert score_url(u, origin="https://x.org") >= 0.0
    assert all(score_url(u, origin="https://x.org") == score_url(u, origin="https://x.org") for u in urls)


def test_weights_change_ordering() -> None:
    harsh = ScoreWeights(negative_segment=-100.0)
    assert score_url("https://x.org/login", weights=harsh) == 0.0


# ─── prioritizer ─────────────────────────────────────────────────────────────


def test_prioritizer_orders_and_truncates() -> None:
    p = BestFirstPrioritizer(origin="https://x.org", top_k=3)
    urls = [
        "https://x.org/img/a.png",
        "https://x.org/docs/api",
        "https://x.org/",
        "https://x.org/a/b/c/d/e",
        "https://x.org/guide",
    ]
    ranked = p.prioritize(urls)
    assert len(ranked) == 3
    scores = [s for _, s in ranked]
    assert scores == sorted(scores, reverse=True)
    # docs/api and guide outrank everything; media URL is last (or cut)
    ranked_full = BestFirstPrioritizer(origin="https://x.org", top_k=10).prioritize(urls)
    assert ranked_full[-1][0] == "https://x.org/img/a.png"


def test_enqueue_plan_maps_score_to_frontier_priority() -> None:
    p = BestFirstPrioritizer(origin="https://x.org")
    plan = p.enqueue_plan(["https://x.org/docs/api", "https://x.org/img/a.png"])
    by_url = {e["url"]: e for e in plan}
    assert by_url["https://x.org/docs/api"]["priority"] <= 6  # good stuff, low number
    assert by_url["https://x.org/img/a.png"]["priority"] == 9  # media, worst bucket
    for entry in plan:
        assert 1 <= entry["priority"] <= 9
        assert entry["score"] == score_url(entry["url"], origin="https://x.org")


def test_prioritizer_tiebreak_deterministic() -> None:
    p = BestFirstPrioritizer(origin="https://x.org")
    urls = ["https://x.org/b/doc", "https://x.org/a/doc"]
    first = p.prioritize(urls)
    second = p.prioritize(list(reversed(urls)))
    assert [u for u, _ in first] == [u for u, _ in second] == ["https://x.org/a/doc", "https://x.org/b/doc"]
