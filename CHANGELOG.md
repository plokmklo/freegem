# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- 36 new tests (386 total): cache roundtrip/TTL/corruption/eviction, budget
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
