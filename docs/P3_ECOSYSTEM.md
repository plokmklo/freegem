# P3 — Ecosystem and Advanced Features: scope and boundaries

Version 2.9.0 implements the P3 plan with an explicit line between what
lives in this repository and what is a separate product. Each item below is
the plan's own wording, then what actually shipped and where the boundary
sits.

## 1. Additional language SDKs "after the core contracts stabilize"

Shipped: `sdk/typescript/` — a scaffold client binding the four stable
surfaces (`/health`, `/v1/models`, `/v1/chat/completions`, `/metrics`),
optional bearer auth, typed errors that distinguish auth (401/403),
capacity (503) and upstream (502) failures.

Boundary: a scaffold, not an npm release. It widens only when the
OpenAI-compatible surface freezes (no new optional fields for one minor
line). Go/Python-client SDKs follow the same rule. The SDK never embeds
sessions, cookies, or browser automation — those are server-side.

## 2. Specialized article/news adapters

Shipped: `freegem/article.py` — `parse_article` extracts headline, authors,
published/modified time, section, description, image, publisher, canonical
URL, language and body text from news-shaped HTML, resolving each field
across four signal families (JSON-LD > OpenGraph > meta > DOM heuristics)
with per-field provenance recorded in `result.signals`.

Boundary: offline HTML interpretation only. No fetch, no paywall
interaction, no login. Provenance exists because news tags lie; callers
audit, we never silently prefer one lying source over another.

## 3. External search/SERP integrations with clear service boundaries

Shipped: `freegem/serp.py` — `duckduckgo` (unofficial HTML endpoint, no key)
and `brave` (official API, refuses to run without a subscription token:
`ProviderNotConfigured`, never a silent empty page). `get_provider(name,
cfg)` is the single opt-in point; config carries `brave_api_key`.

Boundary: search returns `SearchHit` candidates and NEVER fetches them —
turning hits into pages is the crawler's job, and the two never happen in
one call. No Google/Bing adapters: keying and ToS terms that freegem cannot
honestly accept; the registry deliberately rejects unknown names.

## 4. Advanced browser capability and Grid capacity management

Shipped: `freegem/grid_pool.py` — a fleet dispatcher over Selenium Grid
nodes with honest session budgets, least-loaded leasing, backpressure
(`GridPoolTimeout` instead of an unbounded queue), drain/maintenance
(`set_active`), crash-safe release, and stats; `fetch_with_grid` composes
lease → render → release with the existing renderer.

Boundary: pure scheduling, no selenium import — capacity logic is testable
without a browser. The pool manages CAPACITY, not capabilities: it does not
crack bot walls, solve challenges, or rotate identities.

## 5. Recurring scheduler integrations

Shipped: `freegem/scheduler.py` — `run_once` (one tick over the crawl
state's adaptive recrawl plan), `SchedulerLoop` (background thread with
honest stop), and a CLI entry (`freegem-schedule run --state ... [--loop
--every-sec N]`) so crontab lines and systemd timers need zero glue.

Boundary: the scheduler never fetches — a runner does that. A failed probe
still moves the due time forward (no hot loop). Workflow tools (n8n,
Zapier, Airflow) integrate by calling the same CLI; no vendor plugins
in-repo.

## 6. Distributed control-plane features as OPTIONAL deployment

Shipped: `freegem/control_plane.py` — worker registry (join/heartbeat/
leave/expiry), expiring leases with renewal, and supervision (`reap()`
returns expired leases to the queue and flags dead workers), over any
`freegem.queues.WorkQueue` (SQLite locally, Redis for multi-host) with a
SQLite bookkeeping store.

Boundary: OPTIONAL by construction — nothing activates unless a deployment
constructs a `ControlPlane` itself. Single-node freegem remains fully
functional without it, and this module never imports `redis`. Multi-tenant
orchestration is out of scope here (item 7).

## 7. Multi-tenant dashboards and hosted offerings

Shipped: this document. There is deliberately NO code: a multi-tenant
control panel is a separate product with its own auth model, billing, data
isolation and threat surface — bolting it onto a loopback-first tool would
break the project's security posture (bind-to-loopback is the access
control). If such a product is built, it consumes freegem's HTTP/MCP
surfaces exactly like the SDK does, and lives in its own repository.

## Campaign verification (this sandbox, 2026-10-12)

Two live measurements back the P3 crawler claims — see
`CHANGELOG.md` [2.9.0] for the numbers: a 1003-distinct-website sweep and a
114-host AI-unscrappable-hard-target sweep, both run through the honest
identity ladder with per-host pacing. State DBs keep every outcome row;
failure taxonomy and fallback attribution come from the same tables.
