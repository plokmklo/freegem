"""The crawl loop: seeds -> frontier -> guarded fetch -> extract -> output.

Everything expensive or dangerous is bounded and everything observable is
recorded:

* a ``plan()`` pass that does NO network -- seeds are normalized, scope
  verdicts are shown per seed, budgets are mathed -- so ``crawl plan`` can
  be run before touching any server;
* ``run()`` spends the budget on the frontier, honoring robots.txt through
  the shared cache, per-host politeness delays, backoff-with-jitter
  retries, and a cancellation event that turns Ctrl-C into "finish the
  in-flight page, checkpoint, exit";
* artifacts: every successfully fetched body is stored content-addressed
  (``artifacts/ab/<sha256>.body``) with one manifest line per page, so a
  crawl's raw evidence survives independently of the frontier DB;
* counters and structured JSON logs cover pages, bytes, statuses, robots
  refusals, blocks, retries, extraction stats, and per-host failures.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from ..config import BridgeConfig
from ..web_scraper import RobotsCache
from .extract import ExtractionStats, Field, FieldError, Schema, extract_items
from .fetch import FetchOutcome, crawl_fetch
from .frontier import Frontier, FrontierItem
from .hardening import HtmlDom, ResourceLimits, parse_xml, redact_secrets
from .output import make_writer
from .urls import Budget, Scope, normalize_url, url_origin

__all__ = ["Counters", "CrawlSpec", "PlanReport", "load_schema", "plan", "run"]


@dataclass
class CrawlSpec:
    """Everything one crawl needs, CLI-constructible."""

    seeds: tuple[str, ...]
    scope: Scope = field(default_factory=Scope)
    limits: ResourceLimits = field(default_factory=ResourceLimits)
    delay_ms: int = 250
    concurrency: int = 2
    robots: bool = True
    sitemaps: bool = False
    output_path: str = ""
    output_format: str = "jsonl"
    append_output: bool = False
    schema_path: str = ""
    artifacts_dir: str = ""
    state_path: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)

    def seed_hosts(self) -> set[str]:
        hosts: set[str] = set()
        for seed in self.seeds:
            try:
                hosts.add((urllib.parse.urlsplit(seed).hostname or "").lower())
            except ValueError:
                continue
        return hosts


@dataclass
class Counters:
    """The crawl's scoreboard; the shape of the campaign report too."""

    pages_ok: int = 0
    pages_not_modified: int = 0
    pages_failed: int = 0
    robots_denied: int = 0
    ssrf_refused: int = 0
    blocked: int = 0
    retries: int = 0
    links_found: int = 0
    links_queued: int = 0
    sitemap_urls: int = 0
    bytes_in: int = 0
    items_extracted: int = 0
    field_errors: int = 0
    artifacts_stored: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_kind: dict[str, int] = field(default_factory=dict)
    failures_by_host: dict[str, int] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)

    def note_outcome(self, outcome: FetchOutcome) -> None:
        self.by_kind[outcome.kind] = self.by_kind.get(outcome.kind, 0) + 1
        if outcome.status is not None:
            key = str(outcome.status)
            self.by_status[key] = self.by_status.get(key, 0) + 1
        if outcome.blocked:
            self.blocked += 1
        if not outcome.ok and outcome.kind != "http_error":
            host = _host_of(outcome.url)
            if host:
                self.failures_by_host[host] = self.failures_by_host.get(host, 0) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "pages_ok": self.pages_ok,
            "pages_not_modified": self.pages_not_modified,
            "pages_failed": self.pages_failed,
            "robots_denied": self.robots_denied,
            "ssrf_refused": self.ssrf_refused,
            "blocked": self.blocked,
            "retries": self.retries,
            "links_found": self.links_found,
            "links_queued": self.links_queued,
            "sitemap_urls": self.sitemap_urls,
            "bytes_in": self.bytes_in,
            "items_extracted": self.items_extracted,
            "field_errors": self.field_errors,
            "artifacts_stored": self.artifacts_stored,
            "by_status": dict(sorted(self.by_status.items())),
            "by_kind": dict(sorted(self.by_kind.items())),
            "failures_by_host": dict(
                sorted(self.failures_by_host.items(), key=lambda kv: -kv[1])[:20]
            ),
            "runtime_sec": round(time.time() - self.started_at, 1),
        }


@dataclass
class PlanReport:
    """What ``crawl plan`` prints: no network, no state file, no surprises."""

    seeds: list[dict[str, str]]
    rejected: list[dict[str, str]]
    budget_pages: int
    max_depth: int
    hosts: list[str]
    sitemaps_planned: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "seeds": self.seeds,
            "rejected": self.rejected,
            "budget_pages": self.budget_pages,
            "max_depth": self.max_depth,
            "hosts": self.hosts,
            "sitemaps_planned": self.sitemaps_planned,
        }


def load_schema(path: str) -> Schema:
    """Load + validate a schema JSON file (empty schema when path is '')."""
    if not path:
        return Schema(single=True, fields=())
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("schema file must contain a JSON object")
    fields = []
    for raw in data.get("fields", []):
        fields.append(
            Field(
                name=str(raw.get("name", "")),
                selector=str(raw.get("selector", "")),
                kind=str(raw.get("kind", "css")),
                type=str(raw.get("type", "str")),
                required=bool(raw.get("required", False)),
                default=raw.get("default"),
                attr=raw.get("attr"),
                normalize=tuple(raw.get("normalize", ())),
                pattern=raw.get("pattern"),
                multi=bool(raw.get("multi", False)),
            )
        )
    schema = Schema(
        item_selector=str(data.get("item_selector", "")),
        item_kind=str(data.get("item_kind", "css")),
        single=bool(data.get("single", False)),
        fields=tuple(fields),
    )
    problems = schema.validate_spec()
    if problems:
        raise ValueError("invalid schema: " + "; ".join(problems))
    return schema


def plan(spec: CrawlSpec) -> PlanReport:
    """Normalize seeds and apply scope WITHOUT any network traffic."""
    seeds: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    hosts: set[str] = set()
    seed_hosts = spec.seed_hosts()
    for raw in spec.seeds:
        try:
            url = normalize_url(raw)
        except ValueError as exc:
            rejected.append({"seed": raw, "reason": str(exc)})
            continue
        verdict = spec.scope.admits(url, depth=0, seed_hosts=seed_hosts)
        if not verdict.ok:
            rejected.append({"seed": raw, "reason": verdict.reason})
            continue
        hosts.add(_host_of(url) or "")
        seeds.append({"url": url, "host": _host_of(url) or ""})
    sitemaps_planned = (
        [f"{origin}/sitemap.xml" for origin in sorted(_seed_origins(spec))] if spec.sitemaps else []
    )
    return PlanReport(
        seeds=seeds,
        rejected=rejected,
        budget_pages=spec.scope.max_pages,
        max_depth=spec.scope.max_depth,
        hosts=sorted(hosts),
        sitemaps_planned=sitemaps_planned,
    )


def _seed_origins(spec: CrawlSpec) -> set[str]:
    """Distinct scheme://host[:port] origins of the seeds -- sitemap.xml is
    probed per origin, not per bare host, because the scheme and port of the
    seed (http on 8080, https on 443, ...) is what the site actually serves
    its sitemap on."""
    origins: set[str] = set()
    for seed in spec.seeds:
        try:
            origins.add(url_origin(normalize_url(seed)))
        except ValueError:
            continue
    return origins


def run(
    cfg: BridgeConfig,
    spec: CrawlSpec,
    frontier: Frontier,
    *,
    cancel: threading.Event | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> Counters:
    """Crawl until the budget is spent, the frontier is dry, or cancel fires."""
    cancel = cancel or threading.Event()
    counters = Counters()
    budget = Budget(max_pages=spec.scope.max_pages)
    robots = RobotsCache() if spec.robots else None
    schema = load_schema(spec.schema_path)
    stats = ExtractionStats()
    writer = (
        make_writer(
            spec.output_path,
            spec.output_format,
            [f.name for f in schema.fields],
            append=spec.append_output,
        )
        if spec.output_path
        else None
    )
    started = time.monotonic()

    # Recover in-flight rows from a previous crashed/killed run.
    recovered = frontier.abandon_inflight()
    if recovered:
        _emit(on_event, {"event": "recovered", "inflight": recovered})

    frontier.set_meta("started_at", str(int(time.time())))
    frontier.set_meta("seeds", json.dumps(list(spec.seeds)))

    if spec.sitemaps:
        _seed_sitemaps(cfg, spec, frontier, robots, counters, cancel)

    for seed in spec.seeds:
        try:
            url = normalize_url(seed)
        except ValueError as exc:
            _emit(on_event, {"event": "seed_rejected", "seed": seed, "error": str(exc)})
            continue
        frontier.push(url, host=_host_of(url) or "", depth=0, priority=0)

    try:
        _crawl_loop(
            cfg,
            spec,
            frontier,
            robots,
            schema,
            stats,
            budget,
            counters,
            writer,
            cancel,
            on_event,
            started,
        )
    finally:
        frontier.set_meta("finished_at", str(int(time.time())))
        frontier.set_meta("counters", json.dumps(counters.as_dict()))
        frontier.checkpoint()
        if writer is not None:
            writer.close()
        _emit(
            on_event,
            {"event": "done", "counters": counters.as_dict(), "extraction": stats.as_dict()},
        )
    return counters


def _crawl_loop(
    cfg: BridgeConfig,
    spec: CrawlSpec,
    frontier: Frontier,
    robots: RobotsCache | None,
    schema: Schema,
    stats: ExtractionStats,
    budget: Budget,
    counters: Counters,
    writer: Any,
    cancel: threading.Event,
    on_event: Callable[[dict[str, Any]], None] | None,
    started: float,
) -> None:
    """Claim-fetch-process loop; single or small worker pool."""
    workers = max(1, min(spec.concurrency, 8))
    if workers == 1:
        while _keep_going(spec, frontier, budget, counters, cancel, started):
            batch = frontier.claim(1)
            if not batch:
                if not _wait_for_due(frontier, cancel, started, spec, budget):
                    return  # frontier drained: nothing pending, nothing inflight
                continue
            _process(
                cfg,
                spec,
                frontier,
                robots,
                schema,
                stats,
                budget,
                counters,
                writer,
                batch[0],
                cancel,
                on_event,
            )
        return
    lock = threading.Lock()
    progress = {"last_progress": time.monotonic()}

    def worker() -> None:
        while True:
            with lock:
                if not _keep_going(spec, frontier, budget, counters, cancel, started):
                    return
            batch = frontier.claim(1)
            if not batch:
                if frontier.inflight_count() == 0 and not frontier.pending_urls(limit=1):
                    return  # drained
                with lock:
                    quiet = time.monotonic() - progress["last_progress"]
                if quiet > 20.0:
                    # Everything left is a far-future retry: end the run; a
                    # later resume picks the retries up. The cap is generous
                    # enough for the default two-retry backoff window.
                    return
                time.sleep(0.25)
                continue
            with lock:
                progress["last_progress"] = time.monotonic()
            _process(
                cfg,
                spec,
                frontier,
                robots,
                schema,
                stats,
                budget,
                counters,
                writer,
                batch[0],
                cancel,
                on_event,
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in range(workers):
            pool.submit(worker)


def _keep_going(
    spec: CrawlSpec,
    frontier: Frontier,
    budget: Budget,
    counters: Counters,
    cancel: threading.Event,
    started: float,
) -> bool:
    if cancel.is_set():
        return False
    if budget.exhausted:
        return False
    return time.monotonic() - started <= spec.limits.max_runtime_sec


def _wait_for_due(
    frontier: Frontier,
    cancel: threading.Event,
    started: float,
    spec: CrawlSpec,
    budget: Budget,
) -> bool:
    """Nothing is due right now. Wait briefly for backoff retries to come
    due; return False when the frontier is DRAINED (no pending, no inflight),
    True when the loop should keep polling."""
    pending = frontier.pending_urls(limit=1)
    if not pending and frontier.inflight_count() == 0:
        return False
    deadline = time.monotonic() + 2.0
    while (
        time.monotonic() < deadline
        and not cancel.is_set()
        and not budget.exhausted
        and time.monotonic() - started <= spec.limits.max_runtime_sec
    ):
        time.sleep(0.1)
    return True


def _process(
    cfg: BridgeConfig,
    spec: CrawlSpec,
    frontier: Frontier,
    robots: RobotsCache | None,
    schema: Schema,
    stats: ExtractionStats,
    budget: Budget,
    counters: Counters,
    writer: Any,
    item: FrontierItem,
    cancel: threading.Event,
    on_event: Callable[[dict[str, Any]], None] | None,
) -> None:
    """Fetch one claimed URL, record every branch, queue its links."""
    url = item.url
    host = item.host
    wait = frontier.host_ready(host, delay_sec=spec.delay_ms / 1000.0)
    if wait > 0:
        _polite_sleep(wait, cancel)
    frontier.touch_host(host)

    verdict = spec.scope.admits(url, depth=item.depth, seed_hosts=spec.seed_hosts())
    if not verdict.ok:
        frontier.mark_skipped(url, verdict.reason)
        _log(cfg, {"event": "skipped", "url": url, "reason": verdict.reason})
        return

    outcome = crawl_fetch(
        cfg,
        url,
        limits=spec.limits,
        robots=robots,
        etag=item.etag,
        last_modified=item.last_modified,
        extra_headers=spec.extra_headers,
    )
    counters.note_outcome(outcome)
    counters.bytes_in += len(outcome.body or b"")
    _log(
        cfg,
        {
            "event": "fetched",
            "url": url,
            "status": outcome.status,
            "kind": outcome.kind,
            "ms": outcome.elapsed_ms,
            "bytes": len(outcome.body or b""),
        },
    )

    if outcome.kind == "robots":
        counters.robots_denied += 1
        frontier.mark_skipped(url, "robots_disallow")
        return
    if outcome.kind == "ssrf":
        counters.ssrf_refused += 1
        frontier.mark_skipped(url, "ssrf_refused")
        return
    if outcome.kind == "ok" and outcome.not_modified:
        counters.pages_not_modified += 1
        frontier.mark_done(url, http_status=304, etag=item.etag, last_modified=item.last_modified)
        return
    if not outcome.ok:
        counters.pages_failed += 1
        budget.failed += 1
        retryable = outcome.kind in ("network", "timeout", "too_large") or (
            outcome.status in (429, 500, 502, 503, 504)
        )
        will_retry = frontier.mark_error(
            url,
            outcome.error or outcome.kind,
            retry=retryable,
            max_retries=spec.limits.max_retries,
            retry_after=outcome.retry_after,
        )
        if will_retry:
            counters.retries += 1
        return

    counters.pages_ok += 1
    budget.fetched += 1
    content_hash = hashlib.sha256(outcome.body or b"").hexdigest()
    frontier.mark_done(
        url,
        http_status=outcome.status,
        etag=outcome.etag,
        last_modified=outcome.last_modified,
        content_hash=content_hash,
    )
    _store_artifact(spec, url, outcome, content_hash, counters)

    text = outcome.text()
    dom: HtmlDom | None = None
    payload: Any = None
    mime = outcome.content_type.split(";")[0].strip().lower()
    if "json" in mime:
        payload = outcome.json()
    else:
        dom = HtmlDom(max_nodes=spec.limits.max_dom_nodes, max_depth=spec.limits.max_dom_depth)
        dom.feed(text)

    if not schema.fields:
        # No schema given: one useful record per page -- title and capped
        # readable text (the same "what did this page say" cut the scraper
        # makes), so the default crawl output is not empty.
        records = [_default_record(dom, payload, url)]
        field_errors: list[FieldError] = []
    else:
        records, field_errors = extract_items(
            dom,
            payload,
            schema,
            base_url=url,
            stats=stats,
            max_items=spec.limits.max_items_per_page,
        )
    counters.items_extracted += len(records)
    counters.field_errors += len(field_errors)
    if writer is not None:
        provenance = {
            "url": url,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "content_hash": content_hash,
            "via": "crawl",
        }
        for record in records:
            record.pop("_index", None)
            writer.write({**record, **provenance})

    if item.depth < spec.scope.max_depth:
        _queue_links(spec, frontier, counters, url, item.depth + 1, dom, text)
    if counters.pages_ok % 10 == 0:
        frontier.checkpoint()
        _emit(
            on_event,
            {"event": "progress", **counters.as_dict(), "pending": frontier.stats().queued},
        )


def _polite_sleep(seconds: float, cancel: threading.Event) -> None:
    """Wait out a host's politeness delay without ignoring Ctrl-C: the sleep
    wakes every 100 ms to check the cancel flag."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not cancel.is_set():
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def _queue_links(
    spec: CrawlSpec,
    frontier: Frontier,
    counters: Counters,
    base_url: str,
    depth: int,
    dom: HtmlDom | None,
    text: str,
) -> None:
    """Normalize + scope-filter the page's links; push the survivors."""
    if dom is None:
        return
    seed_hosts = spec.seed_hosts()
    queued = 0
    seen: set[str] = set()
    for node in dom.root.iter():
        if node.tag != "a":
            continue
        href = node.attrs.get("href")
        if not href or href.startswith(("mailto:", "javascript:", "tel:", "#")):
            continue
        try:
            target = normalize_url(href, base=base_url)
        except ValueError:
            continue
        if target in seen:
            continue
        seen.add(target)
        counters.links_found += 1
        verdict = spec.scope.admits(target, depth=depth, seed_hosts=seed_hosts)
        if not verdict.ok:
            continue
        if frontier.push(target, host=_host_of(target) or "", depth=depth, priority=5):
            counters.links_queued += 1
            queued += 1
        if queued >= spec.limits.max_links_per_page:
            break


def _seed_sitemaps(
    cfg: BridgeConfig,
    spec: CrawlSpec,
    frontier: Frontier,
    robots: RobotsCache | None,
    counters: Counters,
    cancel: threading.Event,
) -> None:
    """Discover URLs from each seed origin's sitemap.xml (one index level)."""
    for origin in sorted(_seed_origins(spec)):
        if cancel.is_set():
            return
        sitemap_url = f"{origin}/sitemap.xml"
        outcome = crawl_fetch(
            cfg, sitemap_url, limits=spec.limits, robots=robots, extra_headers=spec.extra_headers
        )
        counters.by_kind[outcome.kind] = counters.by_kind.get(outcome.kind, 0) + 1
        if not outcome.ok or outcome.not_modified:
            _log(cfg, {"event": "sitemap_missing", "origin": origin, "kind": outcome.kind})
            continue
        try:
            root = parse_xml(outcome.body or b"")
        except ValueError as exc:
            _log(cfg, {"event": "sitemap_unsafe", "origin": origin, "error": str(exc)})
            continue
        tag = root.tag.rsplit("}", 1)[-1]
        locs = [el.text or "" for el in root.iter() if el.tag.rsplit("}", 1)[-1] == "loc"]
        if tag == "sitemapindex":
            child_urls = [loc for loc in locs if loc][:5]
            for child in child_urls:
                if cancel.is_set():
                    return
                sub = crawl_fetch(
                    cfg, child, limits=spec.limits, robots=robots, extra_headers=spec.extra_headers
                )
                if not sub.ok or sub.not_modified:
                    continue
                try:
                    sub_root = parse_xml(sub.body or b"")
                except ValueError:
                    continue
                _push_sitemap_urls(spec, frontier, counters, sub_root, depth=0)
        else:
            _push_sitemap_urls(spec, frontier, counters, root, depth=0)


def _push_sitemap_urls(
    spec: CrawlSpec,
    frontier: Frontier,
    counters: Counters,
    root: Any,
    depth: int,
) -> None:
    seed_hosts = spec.seed_hosts()
    added = 0
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] != "loc" or not el.text:
            continue
        try:
            target = normalize_url(el.text.strip())
        except ValueError:
            continue
        verdict = spec.scope.admits(target, depth=depth, seed_hosts=seed_hosts)
        if not verdict.ok:
            continue
        if frontier.push(target, host=_host_of(target) or "", depth=depth, priority=3):
            counters.sitemap_urls += 1
            added += 1
        if added >= spec.limits.max_sitemap_urls:
            break


def _default_record(dom: HtmlDom | None, payload: Any, url: str) -> dict[str, Any]:
    """The no-schema record: url, title, capped readable text."""
    cap = 20_000
    if dom is not None:
        titles = [n for n in dom.root.iter() if n.tag == "title"]
        title = titles[0].text.strip() if titles else ""
        body = re.sub(r"\s+", " ", dom.root.text).strip()
        return {"url": url, "title": title, "text": body[:cap]}
    if payload is not None:
        try:
            body = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError):
            body = str(payload)
        return {"url": url, "title": "", "text": body[:cap]}
    return {"url": url, "title": "", "text": ""}


def _store_artifact(
    spec: CrawlSpec,
    url: str,
    outcome: FetchOutcome,
    content_hash: str,
    counters: Counters,
) -> None:
    """Content-address the body under the artifacts dir + one manifest line."""
    if not spec.artifacts_dir or not outcome.body:
        return
    directory = os.path.join(spec.artifacts_dir, content_hash[:2])
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{content_hash}.body")
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(outcome.body)
        counters.artifacts_stored += 1
    manifest = os.path.join(spec.artifacts_dir, "manifest.jsonl")
    line = json.dumps(
        {
            "url": url,
            "hash": content_hash,
            "status": outcome.status,
            "content_type": outcome.content_type,
            "bytes": len(outcome.body),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        ensure_ascii=False,
    )
    with open(manifest, "a", encoding="utf-8") as fh:
        fh.write(redact_secrets(line) + "\n")


def _log(cfg: BridgeConfig, payload: dict[str, Any]) -> None:
    """Structured log line to stderr; redacted like everything else."""
    if not getattr(cfg, "log_requests", True):
        return
    line = json.dumps(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **{k: redact_secrets(str(v)) if isinstance(v, str) else v for k, v in payload.items()},
        },
        ensure_ascii=False,
    )
    print(line, file=sys.stderr)


def _emit(
    on_event: Callable[[dict[str, Any]], None] | None,
    payload: dict[str, Any],
) -> None:
    if on_event is not None:
        on_event(payload)


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
