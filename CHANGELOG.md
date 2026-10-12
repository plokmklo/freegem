# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.9.0] — 2026-10-12

Release gate, browser/proxy security, and bounded background jobs.

### Release gate

- Fixed Ruff E741 (ambiguous `l`) in `test_grid_pool.py`; the package is
  now fully `ruff format`-clean (41 files reformatted) so CI's format
  gate passes.
- Fixed the Windows-only runtime-cap bug: `max_runtime_sec=0.0` let pages
  through on coarse monotonic ticks (`<=` → `<`).

### Security first (browser + proxy)

- **`freegem/browser_guard.py`** — per-request SSRF policy for real
  browsers. Playwright: a context-wide route guard validates EVERY
  request (redirects, JS navigation, all subresources); blocked
  navigations fail the fetch, blocked subresources are dropped. Selenium:
  a CDP `Network.setBlockedURLs` blocklist (special-use IPv4/IPv6 ranges,
  localhost/metadata names, http/https/ws/wss), `--host-resolver-rules`
  killing metadata/special-name resolution at the browser's resolver, and
  final-URL re-validation BEFORE content is returned.
- **`ssrf.py`**: strict `host_public_strict` (browsers) vs lenient-
  where-required `host_public_or_unresolvable` (proxy split-horizon);
  metadata/special-use hostname policy (localhost, `.internal`, `.local`,
  `.localhost`, `.home.arpa`, cloud metadata names); IPv4-mapped IPv6 and
  CGNAT already covered. **`PreflightedProxyHandler`** adds proxy-mode
  initial-URL validation at the single choke point all guarded callers
  share; redirects stay strict (`SafeRedirect`).
- Accurate three-way security model documented in the README (direct /
  proxy / browser), residuals included.

### Bounded background jobs (required before agent)

- **`freegem/jobs.py` rewritten**: the unbounded thread-per-job model is
  replaced by a `JobRunner` — fixed worker pool, bounded queue
  (saturation → HTTP 429 `queue_full`), per-job timeouts with a watchdog
  (`timed_out`, results never delivered), cooperative cancellation
  (`DELETE /v1/jobs/{id}`), capped retries with exponential backoff and
  traces, retention eviction, and optional external-queue execution
  (`memory://`, `sqlite://`, `redis://`, `amqp://`).
- **Durable SQLite status** (`SqliteJobStore`, on by default at
  `~/.config/freegem/jobs.sqlite3`; config `job_db`): restart recovery
  relabels queued/processing jobs `interrupted` — nothing silently lost.
- The MCP `freegem_submit_job` tool now actually EXECUTES jobs (they were
  created and never run before); job bodies are shared between the REST
  and MCP faces (`execute_job`).
- Config: `job_max_workers`, `job_max_pending`, `job_timeout_sec`,
  `job_retention`, `job_db`, `job_queue_url`.

### P3 — ecosystem and advanced features

- **Article/news adapter** (`freegem/article.py`): headline, authors,
  published/modified time, section, description, image, publisher,
  canonical, language and body text from news-shaped HTML; each field
  resolves across four signal families (JSON-LD > OpenGraph > meta > DOM)
  with per-field provenance in `result.signals` (news tags lie; callers
  can audit). Offline-only.
- **SERP providers** (`freegem/serp.py`): `duckduckgo` (HTML endpoint, no
  key) and `brave` (official API — refuses half-configured with
  `ProviderNotConfigured`); `get_provider` is the single opt-in point;
  config gains `brave_api_key`. Boundary: search returns candidates and
  never fetches them.
- **Grid capacity management** (`freegem/grid_pool.py`): least-loaded
  leasing over Selenium Grid nodes with honest session budgets,
  backpressure (`GridPoolTimeout`), drain/maintenance, crash-safe release,
  stats; `fetch_with_grid` composes lease → render → release. Pure
  scheduling — no selenium import.
- **Recurring scheduler** (`freegem/scheduler.py`): `run_once` ticks over
  the crawl state's adaptive recrawl plan (due = `next_attempt_at` passed),
  `SchedulerLoop` for long-lived deployments, and a cron/systemd-friendly
  CLI. The scheduler never fetches; a failed probe still moves the due
  time (no hot loop).
- **Optional distributed control-plane** (`freegem/control_plane.py`):
  worker registry + expiring leases + supervision (reap returns expired
  leases; dead workers flagged) over any `WorkQueue` (SQLite locally,
  Redis multi-host) — nothing activates unless a deployment constructs it.
- **TypeScript SDK scaffold** (`sdk/typescript/`): typed client over the
  four stable HTTP surfaces; widens only after the core contracts freeze.
- **Scope document** (`docs/P3_ECOSYSTEM.md`): what shipped per P3 item and
  where the product boundaries sit (dashboards/hosted offerings are a
  separate product, no code in-repo by design).
- **P3 live campaigns** (sandbox network, 2026-10-12): 1003 distinct
  websites swept — coverage complete, 819/1003 readable (81.6%), failures
  itemized in the state DB (bot walls, dead hosts, sandbox egress); and a
  114-host AI-unscrappable hard-target sweep — 59/114 readable (51.8%),
  the ladder's profile swaps, cookie jar and wayback last resort doing the
  rescues. Politeness: per-host pacing, honest identity, no wall-cracking.

## [2.8.1] — 2026-10-12

Honest scope boundaries: the service now states what it does NOT promise.

- **`GET /v1/about`** (new): returns the full non-goals list — label,
  common assumption, and reality for each of the eight boundaries — plus
  the project stance (best-effort, polite, honest failures).
- **`GET /health`** now carries `non_guarantees`: the short labels, so
  every client that probes the server also learns the scope boundaries.
- **`freegem/non_goals.py`**: canonical `NON_GOALS` data — no scraping SLA,
  no CAPTCHA solving / anti-bot bypass / stealth / fingerprint evasion,
  no free proxy pool or managed browser fleet, no proprietary web index or
  built-in SERP, no unlimited crawling or autoscaling, no universal
  JS/login/WebSocket/PDF/DOCX/media support, no deterministic LLM
  extraction, no hosted queues/databases/dashboards/proxies/infrastructure.
- **`docs/NON_GOALS.md`** (new): long-form writeup of each boundary.
- **README**: new "What the scraper does not guarantee" subsection under
  Limitations; the plain-scraper bullet no longer claims JS rendering is
  "deliberately out of scope" — it points at the dynamic-pages fetchers.

## [2.8.0] — 2026-10-12

Practical dynamic pages and a service boundary (P1), production-scale
adapters and optional intelligence (P2). 691 → 800+ tests.

### P1 — dynamic pages, service boundary

- **Optional Playwright rendering** (`playwright_fetcher.py`): HTTP-first
  with browser fallback for JS shells, isolated browser context per fetch,
  selector/network waits, screenshots, PDFs, traces, guaranteed cleanup.
- **Session/storage-state profiles** (`session_profiles.py`): named
  profiles with expiry, Playwright and `cookiejar` export, user-owned
  storage directory.
- **Markdown/main-content extraction and richer metadata**
  (`crawler/extract.py`): `html_to_markdown`, OpenGraph/Twitter-card/
  JSON-LD metadata.
- **PDF/DOCX adapters** (`adapters.py`): text + Markdown + metadata, with
  stdlib fallbacks when pypdf/python-docx are missing.
- **Authenticated REST job API** (`server.py`, `jobs.py`): `POST /v1/jobs`,
  `GET /v1/jobs/{id}[/results]`, `GET /v1/artifacts/{id}`, optional bearer
  auth via `api_key` config / `FREEGEM_API_KEY`, background workers with
  audit events and retry traces.
- **Narrow authenticated MCP adapter** (`mcp_tools.py`): submit/poll jobs,
  fetch artifacts, manage profiles — each gated by `_check_mcp_auth`.
- **Metrics hardening** (`metrics.py`): structured audit log, request ids
  wired through job endpoints, health/metrics surfaces documented.
- **Bounded parallel fetching**: per-host politeness + concurrency caps on
  multi-source research (existing admission queue).

### P2 — production scale, optional intelligence

- **Selenium/Grid adapter** (`selenium_fetcher.py`): fresh session per
  fetch (local chromium or `grid_url`), waits, screenshots, CDP
  `Page.printToPDF`, shared storage-state seeding; reachable as the
  `selenium` job type.
- **Queue adapters** (`queues.py`): `memory://`, durable `sqlite://`
  (crash-safe, `recover()`), optional `redis://` and `amqp://` (pika),
  one reserve/ack/nack contract, scheme-dispatch factory.
- **Document and blob stores** (`stores.py`): SQLite default and
  PostgreSQL (optional psycopg v2/v3, JSONB containment queries) for
  documents; content-addressed files (same layout as crawl artifacts) and
  S3 (optional boto3) for blobs.
- **Schema-constrained, provider-neutral extraction** (`llm_extract.py`):
  typed fields -> JSON Schema + prompt block; validation with repair
  (coercion, unknown-key drop, enum enforcement) and one complaint-driven
  retry; Bridge / OpenAI-compatible (stdlib urllib) / Null providers;
  reachable as the `extract` job type.
- **Text pipeline** (`text_pipeline.py`): paragraph-aware chunking with
  overlap, idf-weighted relevance selection, deterministic extractive
  summary, cost/quality telemetry.
- **Content-change recrawling** (`crawler/scheduling.py`): per-URL change
  history in the frontier DB, exponential-backoff-when-stable intervals,
  conditional-validation (If-None-Match/If-Modified-Since) recrawl plans.
- **Similarity dedup** (`dedup.py`): 64-bit simhash over word shingles
  with jaccard verification; **search sinks** (`search_sinks.py`): FTS5
  (LIKE fallback), JSONL+tf-idf, and null sinks with replace-on-recrawl.
- **Source adapters** (`crawler/sources.py`): MediaWiki API, arXiv Atom,
  RSS/Atom feeds, docs sitemaps — seed-list discovery only, all fetch
  guards remain in the runner.
- **JS/XHR discovery + best-first scoring** (`crawler/discovery.py`):
  fetch/axios/XHR endpoint extraction from page markup/scripts, and
  deterministic, floored URL scoring with a prioritizer that maps scores
  to frontier priorities.

## [2.7.1] — 2026-10-12

Security and correctness pass over the research/verify/scrape/server faces,
from the audit findings. 651 → 690 tests.

### Security

- **Untrusted-marker escape closed** (`web_scraper.py`, `web_research.py`):
  page text, titles, and link labels are defused before wrapping — a page
  carrying the literal `[END UNTRUSTED WEB CONTENT]` string could close the
  trust wrapper early and inject the rest of its content as prompt-level
  instruction. Markers inside untrusted payloads now read
  `[untrusted-content marker removed]`.
- **Image fetch capped** (`multimodal.py`): `fetch_image_bytes` streams in
  1 MiB blocks under a 25 MB hard cap and refuses an over-large declared
  Content-Length up front; the old unbounded `response.read()` let any
  `image_url` in a request body exhaust RAM (unauthenticated).
- **CGNAT and site-local refused** (`ssrf.py`, `multimodal.py`): the guard
  now blocks `100.64.0.0/10` (RFC 6598) and `fec0::/10` explicitly — stdlib
  `ipaddress` classifies neither as private on CPython 3.12. One shared
  `ip_is_nonpublic()` is now the single source of truth for the pinned
  handlers and the image pre-check.
- **Redirect hops check credentials** (`ssrf.py`): `SafeRedirect` now
  refuses redirect targets with embedded `user:pass@` — the first URL was
  checked, the redirect was the loophole.
- **Chunked bodies capped cumulatively** (`server.py`): a stream of
  under-cap chunks accumulated unbounded RAM before the post-join check;
  the total is now refused the moment it would exceed the limit.
- **Server counters under a lock** (`metrics.py`): `requests_total`,
  `by_status`, and the upstream-retry counter are read-modify-write from
  worker threads — `+=` is not atomic under the GIL, so lock-free counting
  silently lost updates.

### Fixed

- **Evidence verification at map/reduce scale** (`web_research.py`): the
  reduce pass only ever saw paraphrased notes, so verbatim-quote
  verification silently degraded to 0 verified exactly when material
  crossed the 40k single-pass budget. The map pass now nominates verbatim
  quote candidates from the raw chunks, they are verified deterministically
  against the chunk they came from, and the reduce pass quotes the
  pre-verified pool (any pool quote re-verifies page-level; anything else
  still fails honestly).
- **Skipped-page accounting** (`web_research.py`, `claim_verify.py`):
  budget-skipped pages (ok=False, skipped="max_pages") fell into neither
  `pages_ok` nor `pages_failed`, so the totals did not reconcile. Both JSON
  faces now carry `pages_skipped`, and
  `pages_ok + pages_failed + pages_skipped == urls_requested`; the text
  renderers show `N skipped` too.
- **Windows cp1252 crash** (`cli.py`): `main()` forces stdout/stderr to
  UTF-8 with replacement, so the markdown renderers' check/cross marks (or
  any non-Latin Gemini answer) can no longer kill a documented output
  format after all the network work completed.
- **Corrupt config no longer bricks the CLI** (`config.py`): an unparsable
  or unreadable config.json is reported on stderr and defaults apply —
  previously every command, doctor included, died at startup.
- **Config values type-coerced** (`config.py`): `"port": "9000"` now binds
  (was a string that broke the bind) and `"temporary_chats": "false"` now
  disables (was a truthy non-empty string silently enabling the feature).
  Unsalvageable values fall back to documented defaults.
- **`DEFAULT_CONFIG` is actually immutable** (`config.py`): wrapped in
  `MappingProxyType` instead of a dict annotated `Mapping`.
- **Dead guard removed** (`tools.py`): `MAX_INLINE_IMAGE_B64` implied a
  limit that did not exist; deleted (the real cap is the 32 MB request-body
  limit and the new image-fetch cap).
- **README honesty**: documents the proxy-mode carve-out (DNS and dialing
  belong to the proxy; client-side checks still run on the first URL and
  every redirect hop) and marker defusing.

### Tests

- New `tests/test_audit_fixes.py` (39 tests): marker defusing, image-fetch
  cap, skipped accounting, map/reduce quote pool verifying end-to-end,
  chunked total cap, CGNAT/site-local refusal, redirect-credential refusal,
  config corruption/coercion, metrics lock under contention, UTF-8 stdio.

## [2.7.0] — 2026-10-11

### Changed — v3 escalation pass (campaign 806 → 812/1000 readable; more from an unrestricted network)

- **Shared cookie jar** (`ssrf.py`, `resilient.py`, `campaign.py`): the guard's
  opener accepts a `cookie_jar` and the ladder keeps origin-granted cookies
  across attempts (one jar per campaign). A 302 that sets an anonymous
  entitlement token and loops back — tagesanzeiger.ch's consent gate — now
  resolves the way a browser resolves it. Fixes an empty-CookieJar falsy bug
  (`if jar` → `if jar is not None`): an empty `CookieJar` is falsy.
- **DNS-over-HTTPS fallback** (`resilient.py`): when the local resolver
  cannot answer for a name, the ladder resolves via DoH (Google, then
  Cloudflare, A + AAAA) and hands the addresses to the SAME public-only
  check (`_checked_addresses`) before any dial — the anti-rebinding contract
  is unchanged. Dead domains (NXDOMAIN at DoH too) still fail, honestly.
- **Wayback last resort** (`resilient.py`): a page the live origin still
  refuses (hard bot walls, dead domains, legal blocks, persistent 429s) ends
  at the Internet Archive's raw-capture snapshot
  (`web.archive.org/web/2id_/<url>`) — reading a published page's public
  copy attacks no one. Successful reads are flagged `via="wayback"` and
  `fallback="wayback"` so reports count them as copies, not origin reads.
  From networks that can reach the archive this covers most of the
  remaining 403/401/402/451/dead-host bucket.
- **5xx → origin-root fallback**: a path-specific origin error (sohu.com
  `/about` 502s while `/` serves) now falls back to the origin root after
  one backoff, same as dead 404 paths.
- **Third browser profile** (Safari, minimal header set) and a deeper
  attempt budget (9 → 16); 429s wait out three `Retry-After` windows before
  the archive decides.
- **Injection points** (`ssrf.py`): `build_guarded_opener(proxy, context,
  resolver, cookie_jar)` — `resolver` swaps the resolve-and-check step
  (must raise `SSRFRefused` for anything it cannot vouch for); both pinned
  handlers accept it. `crawl_fetch` threads both through.
- Campaign v3 pass over the 1000-page corpus: 812/1000 readable live
  (81.2%). Recoveries this pass: wordpress.org (429 window reset),
  standaard.be ×2 (Chrome profile), tagesanzeiger.ch ×2 (cookie jar),
  sohu.com/about (5xx → root). The remaining 188: 143 hard CDN walls
  (datacenter-IP reputation), 23 timeouts (incl. archive-unreachable
  probes), 7 dead paths whose root also fails, 5 paywall/legal, 3
  persistent 429s, plus a tail of dead DNS/TLS/5xx — each documented in
  `report_v3_final.json`.

### Changed — fetch-for-readability policy (no robots.txt, by design)

- **robots.txt is never fetched and never consulted anywhere in freegem.**
  A robots.txt is attacker-controlled text served from the same origin as
  the pages being read; parsing it (or, worse, feeding it to a model) is a
  documented prompt-injection and policy-poisoning vector — other scrapers
  and AI agents have been steered by lines planted in robots.txt. The
  crawler, the campaign, `scrape_page`, the MediaWiki adapter, and URL
  discovery all dropped the gate. The legacy behavior stays available as an
  explicit opt-in (`scrape_page(..., ignore_robots=False)`, CLI
  `freegem scrape --respect-robots`). Politeness is per-host pacing; attacks
  (auth bypass, wall cracking, hammering) remain out of scope.
- **Escalation ladder** (`crawler/resilient.py`, new): every campaign/crawl
  fetch goes through `fetch_resilient` — honest freegem identity first, then
  browser-grade header profiles (Chrome, then Firefox) for 403/406 walls,
  `Retry-After`-honoring 429 waits (bailing out early on IP-keyed rate
  limits — later `--retry-failed` passes catch the window reset), timeout
  doubling for slow origins, origin-root fallback for dead paths (404/410),
  https↔http and www↔apex swaps for broken transports, and — ONLY for broken
  certificate chains — one TLS-relaxed retry flagged `tls_unverified`.
  401/402/451 are never bypassed. A `HostPacer` spaces same-host requests
  across threads and extends the gap on 429.
- **100 MB page reads** (`crawler/fetch.py`, `hardening.py`): the body cap
  went from 2–5 MB to 100 MB (wire AND decompressed, bomb-safe), with
  streaming reads that SPILL to disk past 16 MB (`spill_bytes`,
  `FetchOutcome.body_path`) so a huge page costs disk, not RAM; spill files
  decode incrementally with the same charset precedence as in-memory bodies.
  cnn.com, big Wikipedia pages, and chosun.com front pages now read fully.
- **Important-only distillation** (`crawler/distill.py`, new): pages ≥ 2 MB
  are distilled by Gemini to their load-bearing content (bounded output,
  original language), with a deterministic extractive fallback (headings +
  densest paragraphs, boilerplate dropped) when no session is available —
  the mode used is recorded on every record (`distill_mode`). Campaign
  `--distill` stores per-URL summaries in a `distilled` table; the runner
  adds `distilled`/`distill_mode` to no-schema records with
  `CrawlSpec.distill`.
- **Campaign v2** (`crawler/campaign.py`): resilient fetch by default, no
  robots step, 100 MB cap, per-host pacing, `--retry-failed` (re-attempt
  exactly the rows whose last outcome was not ok — the upgrade path between
  passes), `--distill`, `--host-delay`, v1→v2 state migration (adds
  `via`/`fallback`/`attempts` columns), and a report with `readable`,
  `readable_pct`, and per-fallback counters.
- **Runner**: `CrawlSpec.robots` is gone (there is deliberately NO switch);
  `CrawlSpec.resilient` (default on) routes pages through the ladder;
  `CrawlSpec.distill` and `spill_dir` added; the `robots_denied` counter and
  the `robots` outcome kind no longer exist; artifacts store spilled bodies
  too (the spill is consumed into the content-addressed store).
- CLI: `freegem scrape` gained `--respect-robots` (robots are ignored by
  default now); the crawl subcommand lost its `--ignore-robots` flag — the
  crawler never consults robots.txt. MCP tool descriptions updated to match.

### Fixed

- DNS-resolution failures are classified `network`, never `ssrf` — the
  first campaign had recorded a transient resolver failure as a guard
  refusal; regression-tested against the exact message.
- `_default_record` pages and artifacts handle spilled (multi-MB) bodies;
  content hashes are computed streaming from the spill file.

### Tests

- Suite now counts **651 tests + 34 subtests** (research-era count was
  366 + 20; the crawler commits grew it). New coverage: ladder decisions
  over fabricated outcomes, browser-profile flip on a live 403 wall,
  429 Retry-After + pacer penalties, 404→origin-root, gzip bomb cap during
  decompression, spill-to-disk + incremental decoding, distill fallback and
  Gemini mode, campaign retry-failed semantics, and "robots.txt is never
  fetched" assertions on both the crawl and scrape paths.

### Campaign v2 — same 1000 URLs, upgraded fetcher

- **806/1000 readable (80.6%)**, up from 559 (55.9%) on the v1 stack, 316 MB
  read. The ladder rescued 206 pages v1 had failed: `origin_root` fallback
  169 (the dead `/about` seed paths), browser-profile flip 30, host/scheme
  swaps 7; plus 26 robots refusals released by the no-robots policy, 18
  former `too_large` pages now read whole under the 100 MB cap (cnn.com,
  big Wikipedia pages, chosun.com), and backoff retries.
- The remaining 194 are honest limits, itemized in
  `report_v2_final.json`: 144 host-wide CDN bot walls across 101 hosts
  (Akamai/DataDome-class; passing them needs TLS-fingerprint evasion, which
  is out of scope by design — on a residential IP many of these pass),
  18 network failures (sandbox egress blocks for gnu.org/archive.org/tcl.tk
  et al. plus dead DNS), 8 dead paths, 6 slow-origin timeouts, 6
  auth/paywall/legal refusals (401/402/451 — never bypassed), 4 IP-keyed
  429s (wordpress.org's window is hours, not seconds), and 12 misc
  (consent redirect loops, WAF 405/406, dead origins).

## [2.6.0] — 2026-10-11

### Added — the crawler subsystem (`freegem crawl`)

- **URL normalization & scope** (`crawler/urls.py`): scheme/host lowercasing,
  IDN punycode, default-port stripping, fragment dropping, percent-encoding
  normalization, and a fixed tracking-parameter removal list (`utm_*`,
  `fbclid`, `gclid`, ...) that preserves query order and never touches signed
  parameters. `Scope` admits/refuses URLs with stable reasons (`ok`,
  `out_of_scope_host`, `deny_path`, `too_deep`, ...): allow/deny domains
  (dot-boundary suffix match), allow/deny path prefixes, depth and page
  budgets, same-host-only by default.
- **Persistent frontier** (`crawler/frontier.py`): SQLite work list with
  priorities, per-host politeness timestamps, exponential backoff with
  jitter, and `Retry-After` honored verbatim; every state change commits, so
  the state file is ALWAYS a valid checkpoint — resume is reopening the
  file. In-flight rows are recovered on restart; a `crawl_meta` table keeps
  run provenance.
- **Guarded fetch** (`crawler/fetch.py`): one GET through the SSRF-pinned
  opener with an honest outcome taxonomy (`ok`, `http_error`, `blocked`,
  `robots`, `ssrf`, `network`, `timeout`, `too_large`, `type_refused`),
  conditional requests (ETag / If-Modified-Since → 304 revalidation), gzip
  decompression caps, and DNS-resolution failures classified as `network`
  (not `ssrf`) so campaign reports don't inflate the guard's catch.
- **Extraction** (`crawler/extract.py`): size-capped HTML DOM; a compiled
  CSS subset (tag/#id/.class/[attr ops], descendant + child combinators,
  comma groups, compound sequences like `div.x#y[a^=b]`); ElementTree-subset
  XPath; dotted/bracketed JSON paths; typed fields (`str|int|float|bool|url`)
  with `required`/`default`/`pattern` validation, normalizers, `multi`
  list fields, and per-field provenance (url, fetched_at, content_hash,
  selector kind) in every record.
- **Output** (`crawler/output.py`): JSONL (streaming), JSON (document), CSV
  (schema-ordered columns + provenance); all writes pass the redaction pass
  and land via temp-file rename, so a killed run never leaves a half-written
  results file.
- **Hardening** (`crawler/hardening.py`): secret redaction for common token
  shapes (AWS, GitHub, OpenAI-style, Google, Slack, JWT, private-key blocks,
  bearer/query secrets) as `[REDACTED:kind]`; safe XML defaults — DOCTYPE,
  ENTITY, ELEMENT, ATTLIST, and xml-stylesheet prologs are refused before
  parsing; body decoding chain (header charset → BOM → HTML `<meta charset>`
  sniff → UTF-8 replace) verified against GBK/Shift-JIS/legacy hosts;
  resource limits (per-page bytes, DOM nodes/depth, links/items per page,
  sitemap URL cap, runtime) enforced during construction, not after.
- **Runner & CLI** (`crawler/runner.py`, `freegem crawl`): `plan` (fully
  offline: seed normalization + scope verdicts + budget math), `run`,
  `status`, and resume; sitemap discovery per seed origin (one index level,
  safe-XML parsed); structured JSON logs and progress events; content-
  addressed artifacts (`sha256` bodies + manifest); graceful Ctrl-C that
  finishes the in-flight page and checkpoints.
- **Fetch campaign** (`crawler/campaign.py` + `scripts/gen_campaign_seeds.py`):
  resumable bulk fetcher with a per-category outcome report. The 1000-URL
  campaign across old/new sites, 15+ languages/encodings, PHP/Java/JS/Python
  ecosystems, gov/edu, and bot-walled heavyweights drove three fetch
  upgrades: DNS-failure reclassification, `Retry-After` honoring, and the
  encoding chain above. Report: 559/1000 ok, 181×403 (bot walls, kept
  blocked by the honest-UA posture), 26 robots-denied by our own compliance,
  167×404, 18 over the byte cap, 5 timeouts.
- **Server**: `Sec-Fetch-Site: cross-site` POST refusal — a third, independent
  CSRF gate (Fetch Metadata cannot be forged or withheld by page scripts),
  alongside the existing Origin and Content-Type layers.

### Fixed

- Stale test counts in the changelog: the research-era suite was 366 tests
  plus 20 subtests, not "386 tests" (measured at the commit that introduced
  the claim).

### Tests

- 144 new tests (638 tests + 20 subtests): scope/normalization, frontier
  durability and contention, CSS/XPath/JSON selectors, typed extraction,
  output writers, hardening (redaction/safe-XML/DOM caps/decoding), Sec-
  Fetch-Site gates, and end-to-end crawl runs against a loopback fake site
  that re-verifies the SSRF pinning invariant on every fetch.

## [Unreleased]

Nothing yet.

## [2.5.0] — 2026-10-11

### Added

- **Claim verification (`freegem verify` CLI, `web_verify` MCP tool)** —
  judge a list of claims (1–8) against fetched sources and get, per claim,
  exactly one machine-checked status: `supported`, `partially_supported`,
  `contradicted`, `not_found`, or `source_unavailable`. "The source
  contradicts the claim" is finally distinct from "the sources did not
  contain enough evidence", and a failed fetch is never evidence of falsity.
  Sources use the same guarded, robots-aware, budget-capped fetch machinery
  as research, and fetches share the same local cache.
- **Evidence passages with Source / Location / Retrieved** — every status
  quote is checked verbatim against the text actually fetched (flagged
  verified or `UNVERIFIED`, never silently deleted), each verified quote
  gets a deterministic Location computed by finding it inside the source's
  sectioned text ("Introduction", "§ History", "para 7" — the model never
  asserts where evidence lives), and a Retrieved date from the fetch
  metadata. Text output follows the documented Claim / Status / Evidence /
  Source / Location / Retrieved shape; markdown and JSON render the same
  data.
- **Status schema enforcement** — the model's verdicts are validated
  against the closed vocabulary: unknown statuses become `not_found`, a
  "supported" verdict without quotable evidence is downgraded with a note,
  missing verdicts are reported as `not_found` ("no verdict was returned"),
  evidence citing unloaded sources is dropped, and every repair is counted
  and surfaced (`schema_repairs` in the metrics). When all sources are
  dead, no model call happens at all: every claim is `source_unavailable`
  by construction. When Gemini is unavailable, verdicts are left unjudged
  with the raw material attached — never fabricated.
- **Optional URL discovery** — with `discover=True` and no URLs, candidate
  pages come from a supported search API instead of the user: the
  Wikimedia API portal (api.wikimedia.org) for Wikimedia wikis and the
  site's own OpenSearch API for other MediaWiki hosts (config
  `discover_wiki`). Every discovery request is robots-checked like any
  other fetch — Wikimedia's robots.txt disallows its `/w/` search APIs to
  generic agents, so the portal host is used rather than working around a
  rule — and discovery is one bounded API call, never a crawl. If robots
  refuses, discovery refuses with an explicit message.
- **Metrics layer (`freegem/metrics.py`)** — per-run request IDs, stage
  timings (discover / fetch / generate / parse), and counters (fetches,
  cache hits, evidence offered/verified, schema repairs) attached to
  verify output and surfaced in JSON `meta.metrics`; process-wide
  `ServerMetrics` for the HTTP face with a bounded latency window and
  p50/p95/p99 (nearest-rank), response-status counts, queue-refusal
  counts, upstream retry counters, and RSS memory — rendered by the new
  **`GET /metrics`** endpoint (aggregates only). POST responses carry an
  `X-Request-Id` header.
- **Hardening fixture corpus (`tests/fixtures/verify/`)** — malformed-HTML
  pages (void-element jam, nested unclosed markup) run through the real
  extractor and must degrade honestly; a prompt-injection page that
  fabricates verdicts ("Status: Supported") must never move the
  deterministic status machinery; changed-Gemini-response fixtures pin the
  claims-block parser against unknown statuses, missing evidence,
  duplicate/out-of-range entries, and a second fence; and
  `claim_set.json` is an executable benchmark contract pairing claims,
  source text, a model answer, and the expected statuses, quote
  verification, and Locations.

### Fixed

- A `supported`/`partially_supported`/`contradicted` verdict whose every
  quote was dropped (evidence cited an unloaded source) is now downgraded
  to `not_found` with a note — the evidence rule holds after drops, not
  only at parse time.

## [2.4.0] — 2026-10-11

### Added

- **Verified evidence quotes for research** — with `research_evidence` on
  (default; CLI `--evidence/--no-evidence`, MCP `evidence`), the answer is
  followed by a per-source evidence appendix. The model is asked to quote the
  passages that back its claims; every quote is checked against the text
  actually fetched (whitespace/case-insensitive) and flagged verified or
  `UNVERIFIED` — fabricated quotes stay visible and marked, never silently
  dropped. The JSON output carries `citations` plus `evidence_offered` /
  `evidence_verified` counters; markdown renders an Evidence section.
- **Source timestamps** — each fetched source records `fetched_at` (ISO-8601
  UTC; cache hits keep the original fetch time) and the origin's
  `Last-Modified` header when present. The research cache round-trips both,
  including entries written by older versions.
- **Failed-vs-contradicted prompt contract** — research prompts now state
  explicitly that a source which failed to fetch or extracted to a navigation
  stub means evidence is unavailable, never proof a claim is false, and that
  contradictions must say which source says what.
- **Tool-call authorization** — emitted tool calls are validated against the
  tools the request actually declared on all three faces (OpenAI chat
  completions, OpenAI Responses, Google `generateContent`). A fenced call for
  an undeclared tool is dropped instead of forwarded; a named `tool_choice`
  keeps only calls to that name; the Google face honors
  `allowedFunctionNames`. Argument contents are deliberately not
  second-guessed.

### Tests

- 20 new tests (416 total): evidence parsing/verification/rendering, cache
  round-trips, config/MCP flag plumbing, and tool-call validation on both
  wire shapes.

## [2.3.0] — 2026-10-11

### Performance

- **Connection reuse for Gemini calls** — non-streaming requests now go
  through the same pooled `httpx.Client` as streaming (when httpx is
  installed), saving a fresh TLS handshake per request. Clients are keyed by
  (proxy, timeout) so a config change can never silently ride an old client.
- **Concurrent research fetching** — pages from different sites are fetched
  in parallel by a bounded worker pool (`research_concurrency`, default 3,
  CLI `--fetch-concurrency`, MCP `fetch_concurrency`; hard cap 8). Fetches to
  the same host serialize via per-host locks; budget decisions stay strictly
  in request order and total-budget accounting is unchanged.
- **robots.txt cache** — per-origin cache with a TTL (default 600 s,
  `robots_cache_ttl_sec`) and conditional revalidation via ETag /
  Last-Modified, so research over N pages of one site pays for robots.txt
  once instead of N times. Refusal semantics unchanged; a network failure
  during revalidation serves the last known rules.
- **Time-to-first-token logging** — both streaming API faces log `ttft` per
  request, so buffering or upstream slowness shows up in the log.

### Added

- **MediaWiki source adapter** — Wikipedia/Wikimedia article URLs are fetched
  through the site's own Action API (`prop=extracts&explaintext`) instead of
  scraping HTML, producing clean article text with no navigation chrome.
  Same SSRF guard, robots.txt honored for the API URL (never bypassed), same
  size caps; per-source metadata records `via: mediawiki-api`.
- **Extraction-quality indicators** — every research source is labeled
  `ok` / `nav_only` / `empty` in the JSON output (and flagged in the text
  tables), so a caller can tell a real article from a navigation stub.
- **Optional bounded request queue** — `request_queue_wait_sec > 0` lets a
  burst wait briefly for a free slot instead of getting an immediate 503;
  the in-flight bound (active + queued) stays hard. Default `0` keeps the
  refuse-fast behavior. Shed responses now carry a `Retry-After: 2` header.

### Changed

- README rewritten: positioning, per-face tables, structured-adapter and
  quality documentation; GitHub About description and repository topics
  updated separately from the README.
- Config template documents the three new fields.
- 29 new offline tests (395 total): robots cache, MediaWiki adapter,
  quality labels, concurrent collection (order / budgets / per-host
  serialization), admission queue, pooled-client keying.

## [2.2.0] — 2026-10-11

### Added

- **`freegem research` CLI command and `web_research` MCP tool** — multi-source
  cited research built on the scraper. Give it a question and 2-10 URLs; it
  fetches each page with the same SSRF/robots/5 MB machinery as
  `freegem scrape`, then returns one answer with inline `[S1]`, `[S2]`
  citations, key points, and a Disagreements-and-gaps section.

  - **Local fetch cache** (`~/.cache/freegem/research_cache.json`, 30-minute
    TTL, `$FREEGEM_CACHE_DIR` to relocate): repeated agent calls over the same
    URLs do not re-download pages. Successful fetches only; robots.txt and the
    SSRF guard still run on every real fetch. `--no-cache` and `--refresh`
    control it from the CLI.
  - **Hard budgets**: at most 10 pages per call, a per-page character cap
    (default 12 000, max 50 000), and a total fetched-character budget
    (default 80 000). A URL that would push the run over the budget is not
    fetched at all and is reported as skipped in the metadata.
  - **Bounded prompts**: material that fits goes to Gemini in one pass;
    larger material takes a map/reduce path (per-page chunked notes, capped,
    then a reduce pass), so neither prompt grows with the raw page sizes.
  - **Authoritative source table**: the `[S#] -> URL` table is rendered by
    freegem from its own fetch metadata — never from model output — and a
    model answer citing `[Sn]` beyond the real source count is rejected into
    the deterministic fallback rather than shipped with a fabricated table.
  - **Deterministic fallback**: when Gemini is unavailable (no session, rate
    limit), the tool returns the labeled raw extracts instead of failing the
    whole operation; failed sources are recorded per-URL and the run
    continues with the rest.
  - **Untrusted markers end to end**: every page stays wrapped in
    `[UNTRUSTED WEB CONTENT]` in the prompts, with explicit "data, not
    instructions" rules — multi-page aggregation widens the injection surface
    and the markers limit, but do not eliminate, it.
  - **Boilerplate dedup**: lines shared across most pages (cookie banners,
    nav) are kept only once in the prompt.
  - **Formats**: `--format text|markdown|json` (documented JSON schema with
    per-source fetch status, cache hits, truncation, and timing).
- MCP: `web_research(urls, question, max_pages=5, max_chars_per_page=12000)`,
  text output with the source table; no robots bypass, same as `web_scrape`.
- 36 new tests (366 tests + 20 subtests): cache roundtrip/TTL/corruption/eviction, budget
  skipping, max-pages capping, citation preservation and out-of-range
  rejection, map/reduce staging, dead-chunk survival, boilerplate dedup,
  deterministic fallback, JSON schema, MCP dispatch and validation, CLI
  parsing and exit codes.

## [2.1.1] — 2026-10-10

### Security

- **Server: cross-site POSTs are refused before any work happens.** A browser
  can send a "simple" cross-origin POST (`Content-Type: text/plain`) without a
  CORS preflight, and such requests previously ran to completion — including
  the Gemini call on the user's session. Two independent layers now gate every
  POST: (1) any `Origin` header that is not this machine's own loopback gets a
  403 (`Origin: null` included; a missing Origin — curl, SDKs — still works),
  and (2) the body must be `Content-Type: application/json` (415 otherwise),
  which browsers cannot attach without a preflight. Charset suffixes
  (`application/json; charset=utf-8`) are accepted.

### Fixed

- **Server: hard cap on concurrent request threads** (`ThreadedServer`,
  default 32, configurable via `max_concurrent_requests=`). Every in-flight
  request holds a worker for up to the 65 s handler timeout; beyond the cap,
  new connections get an immediate 503 written on the raw socket before any
  thread is spawned, instead of growing the thread count without bound.
- **Retries now back off exponentially and honor `Retry-After`.** The fixed
  `retry_delay_sec` sleep became `delay * 2^(attempt-1)`, capped at 30 s; when
  the upstream sends `Retry-After` (delay-seconds or HTTP-date), the longer of
  the two wins, capped the same way. Applies to both `generate()` and
  `generate_stream()`.
- **One shared SSLContext everywhere.** `build_guarded_opener()` accepts a
  caller-supplied context, and every guarded fetch (scraper, robots.txt,
  image fetcher) now passes the project's cached `gemini._ssl_ctx()` instead
  of building a fresh context — and re-parsing the CA bundle — per request.
- **CI: the `mcp` extra is actually tested.** A new `smoke-mcp` job installs
  `.[mcp]`, builds the FastMCP server, and runs the tool-dispatch tests with
  the real `mcp` package present, so a broken MCP face can no longer hide
  behind "extra missing, skipped".

### Added

- **Daily breakage canary** (`.github/workflows/canary.yml`, cron 07:23 UTC).
  This project talks to an unversioned web endpoint and live sites, so the
  likely failure is an upstream change, not a commit. The canary runs without
  credentials: `freegem doctor`, an end-to-end scrape of a stable page, a
  robots.txt refusal, and two SSRF-refusal probes (loopback and the cloud
  metadata address). Red canary = something upstream moved.
- **26 new tests** (330 total): cross-origin/Content-Type refusals assert the
  Gemini call is never reached, thread-cap shed-load behavior over a real
  socket, exponential backoff curves and Retry-After parsing (both RFC forms),
  and the shared-SSLContext invariant on the guarded opener.

## [2.1.0] — 2026-10-10

### Added

- **`freegem scrape <url>`** CLI command and **`web_scrape`** MCP tool: fetch
  a web page and return readable text (title, body, no scripts/styles/nav
  junk), optionally with resolved links, or have Gemini answer a question from
  the page. Stdlib only — no new dependencies, no JavaScript rendering (raw
  HTML; SPA sites stay a documented limitation).
- **`freegem/ssrf.py`, a shared SSRF guard that closes the DNS-rebinding
  window.** The old resolve→check→reconnect sequence let an attacker-controlled
  DNS server answer differently on the second lookup. The guard now resolves
  once, verifies that every resolved address is public, and pins the dial to
  exactly that checked set — per redirect hop — while TLS keeps using the
  original hostname for SNI and certificate verification. `fetch_image_bytes`
  was migrated onto it, closing the same gap on the image path.
- **Scrape hardening:** `robots.txt` honored by default (the MCP tool cannot
  bypass it; `--ignore-robots` is CLI-only), 5 MB caps on the wire AND after
  decompression (gzip bombs die at the cap), HTML/text-only content types,
  honest User-Agent, no session cookie ever sent to scraped sites, and output
  wrapped in `[UNTRUSTED WEB CONTENT]` markers with explicit anti-injection
  instructions in `--ask` mode.
- **60 new tests** (304 total): every SSRF refusal is tested with the guard
  unstubbed — literal private/loopback/metadata IPs resolve locally, so the
  production code path runs end to end without network — plus robots
  allow/disallow, wire and decompression caps, gzip, redirect-hop rejection,
  pinning dial-order checks, MCP dispatch, and the CLI surface.

## [2.0.0] — 2026-10-10

Rebuild of the upstream `gemini-web2api` design into a maintainable package.
See [Credits](README.md#credits): the Gemini Web protocol knowledge, the model
registry, the multimodal upload flow, and the cookie-sync extension all come
from that project.

### Added

- **Three faces, one command.** `freegem serve` (OpenAI + Google-native HTTP
  API), `freegem mcp` (agent tools), and a one-shot CLI (`ask`, `models`,
  `doctor`) all run through one entry point.
- **`gemini_advisor`** MCP tool: a structured second opinion returned as
  Recommendation / Options / Tradeoffs / Risks / What would change my mind.
- **`doctor`** command: reports session state, cookie parse result, XSRF
  presence, connectivity, and model resolution — without spending a request.
- **177 tests** covering config, model resolution, the protocol client, prompt
  translation, HTTP endpoints, the CLI, and MCP dispatch. No test touches the
  network, so CI needs no credentials.
- Docker image and `docker-compose.yml` with an optional MCP profile. The image
  carries no credentials; the session is mounted read-only at runtime.
- `docs/ARCHITECTURE.md` recording the layering rules and the reasoning behind
  each boundary.
- `CONTRIBUTING.md`, `SECURITY.md`, and GitHub issue/PR templates.
- Editable install with full type hints (`py.typed`) and strict `mypy`.

### Changed

- **Configuration is immutable.** `BridgeConfig` is a frozen dataclass. The
  upstream single-file version used a module-global mutable dict, which under a
  threaded server is a data race: one request's override can be observed by
  another mid-flight. A Cloudflare Workers port of the same project shipped
  that exact bug as cookie cross-contamination between concurrent requests.
- **Build-tag refresh actually works.** Upstream matched only
  `boq_assistant-bard-web-server_*` and *silently* gave up when Google moved to
  `boq_gemini-web-uiserver_*`, so the auto-update appeared functional while
  doing nothing. Both patterns are matched, newer format preferred, and an
  empty result is logged instead of passing for success.
- **Default bind is loopback** (`127.0.0.1`), not `0.0.0.0`. Since this project
  has no authentication layer, an open bind would expose the Google session to
  the whole network.
- **Authentication removed entirely.** There is no `api_keys` field and no
  bearer-token path. Access control is the loopback bind; a test asserts the
  field does not come back.
- **SSE terminates correctly.** The OpenAI `[DONE]` sentinel is emitted as a
  bare `data: [DONE]` line (upstream wrapped it in JSON, so no OpenAI client
  detected end-of-stream), and the socket is closed, since an SSE response has
  no `Content-Length` and clients otherwise block waiting for bytes that never
  arrive.
- **Function-call parsing handles nested arguments.** Upstream's non-greedy
  `[^`]*?}` regex stopped at the first `}`, truncating
  `{"city":"Lima","opts":{...}}` and dropping the call. Brace balancing now
  respects string literals, and two consecutive calls are both parsed instead
  of the second leaking into the reply text.
- **Stream corruption aborts instead of hiding.** If a snapshot stops extending
  already-emitted text, the stream raises rather than silently rewriting what
  the client has rendered.
- **A half-written cookie file no longer drops the session.** On parse failure
  the last good value is kept rather than degrading to anonymous.
- `gemini-web2api` → `freegem` throughout: package, command, imports, config
  paths, and the `GEMINI_BRIDGE_CONFIG` → `FREEGEM_CONFIG` environment variable.

### Removed

- Dead `GEMINI_BRIDGE_HOST` / `GEMINI_BRIDGE_PORT` environment variables, which
  were set by the Docker files and never read. Host comes from `--host` or
  `config.json` only.

### Fixed

- SSE hang on the Google-native stream (`curl -N` blocked indefinitely after
  the final chunk).
- Tool-call response missing `tool_calls` when the model emitted a fence
  without surrounding prose.
- Unknown model names falling back silently without any log line, making a
  typo indistinguishable from a routing decision.

### Fixed

- **Malformed client input returned HTTP 500 with a traceback instead of a
  protocol error.** Fuzzing the published package found six client-triggerable
  500s: `messages[].content` as an int or dict raised inside `" ".join`,
  `systemInstruction` as a string raised on `.get`, `toolConfig` and
  `toolCall.function` as non-dicts raised AttributeError, and
  `functionDeclarations` as a string silently dropped every tool while still
  returning 200. All now coerce or skip and return a valid response. A
  malformed request is the client's problem and should not read as a server
  fault.
- **A client that declared `Content-Length: 99999` and sent 36 bytes hung a
  worker thread forever.** No socket timeout was set, so `rfile.read()`
  blocked indefinitely; under `ThreadingMixIn` each such connection
  permanently consumed a thread. Requests now time out after 65s and return
  HTTP 408 with a JSON body. Detected live: the connection used to block
  indefinitely with no response at all.
- **`"name": null` in a model-emitted tool call was forwarded as a null name.**
  OpenAI's schema requires a string, so a strict client rejected the entire
  completion over one malformed call. Blocks without a string name are now
  dropped, and `"args": null` is coerced to `{}` so the emitted
  `functionCall` part is always schema-valid.
- **`tools` sent as an unwrapped dict dropped every declaration.** Google's
  schema says an array; a dict-shaped `tools` meant the client sent one group
  unwrapped, and it was silently discarded.

### Changed

- `resolve_model`'s contract check is a `RuntimeError` rather than an `assert`,
  which `python -O` strips — a contract break would otherwise surface as
  `int(None) TypeError` in production and nowhere in development.
- SHA1 in `make_sapisidhash` is marked `usedforsecurity=False`. It is mandated
  by Google's `SAPISIDHASH` scheme; the marker records that the hash choice is
  a protocol constant so a future reader does not "fix" it into a broken login.

### Security

- **The image upload no longer sends the session cookie to an arbitrary host.**
  Step two of Gemini's resumable upload posts the image bytes plus the `Cookie`
  and `SAPISIDHASH` headers to a URL taken from a response header. With
  `proxy` configured — a supported setup for exactly this project — a hostile or
  compromised proxy could answer that header with any host and harvest the
  session. The upload URL is now constrained to `content-push.googleapis.com`
  and `upload.googleapis.com` over HTTPS. Covered by regression tests including
  subdomain and userinfo spoofing.

### Fixed

- **A failed page-token scrape no longer re-fetches on every image.** The cache
  guard was `if fresh and tokens`, so an empty result stayed falsy and each
  subsequent upload triggered another authenticated page load — an extra request
  per image precisely when the network is already failing. The cache now records
  whether a fetch happened, separately from whether it succeeded.

- No credentials are read, logged, or included in error output. `redacted()`
  exists so a full config dump is safe to print.
- The full git history was scanned for live cookie, SAPISID, XSRF, and auth-file
  values before publication. None were ever committed.

[Unreleased]: https://github.com/plokmklo/freegem/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/plokmklo/freegem/releases/tag/v2.0.0
