# Ecosystem scan — October 2026

Strategy-desk note for FreeGem maintainers. Date of observation: 2026-10.
Method: live web checks of competitor homepages, pricing pages, docs, and
review aggregators (Google was rate-limited mid-research; DuckDuckGo HTML
and direct page loads were used instead), compared against FreeGem 2.9.x
as documented in `README.md` and the modules under `freegem/`. Pricing
figures were read on the dates shown in Sources; several come from
third-party pricing trackers and should be re-verified before any external
citation. No code in this repository was changed by this scan.

## Landscape overview

The 2026 market splits into four business shapes:

1. **Hosted context APIs** — Firecrawl sells "the context API for AI
   agents": search, scrape, map, crawl, interact, agent runs, and
   monitoring behind one API key and a credit meter, with an MCP server
   (`docs.firecrawl.dev/mcp-server/tools`) as the primary agent on-ramp.
2. **Free reader endpoints** — Jina Reader (`r.jina.ai`) converts a URL to
   LLM-friendly markdown for free (about 20 requests/minute anonymous,
   higher with a key), sitting inside Jina's broader token-credit lineup
   (embeddings, reranker, deepsearch). FreeGem already consumes this as
   the `reader` rung of the smart-fetch ladder.
3. **Open-source crawl frameworks** — Crawlee (Apache-2.0, from Apify,
   successor to the Apify SDK) and Crawl4AI (open-source Python crawler
   aimed at LLM-ready markdown, Apache-2.0-style with attribution) are
   free-as-in-library; both funnel users toward paid clouds (Apify
   platform, Crawl4AI Cloud) for hosting, proxies, and fleets.
4. **Anti-bot scraping APIs** — ScrapFly and ZenRows sell exactly what
   FreeGem's NON_GOALS refuse: managed proxies, browser fingerprints,
   challenge bypass, geotargeting, "pay only for successful requests".

Browser Use straddles shapes 3 and 4: an MIT-licensed Python library where
an LLM drives a real browser, plus a cloud selling remote browser hours
($0.02/hour per its pricing page, pay-as-you-go, credits never expire).

## Feature gap matrix

| Capability | FreeGem | Firecrawl | Jina Reader | Crawlee | Browser Use | Apify | Crawl4AI | ScrapFly / ZenRows |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Hosted, metered API | No (local, loopback-first) | Yes | Yes (free endpoint + tiers) | No (library) | Yes (cloud) | Yes | Yes (Cloud) | Yes |
| URL → LLM markdown | Yes (`web_scrape`, smart-fetch) | Yes | Yes (core product) | Yes (libs) | Yes | Via actors | Yes (core product) | Yes |
| Crawl with budget/resume | Yes (SQLite frontier, `crawl run`) | Yes (async batches) | No | Yes (queues, autoscale) | Weak | Yes (actors) | Yes (adaptive) | Weak / batch API |
| JS rendering | Optional rung (Playwright/Selenium jobs) | Built-in | Yes | Built-in | Core strength | Built-in | Built-in (Playwright) | Built-in |
| Anti-bot / stealth | Opt-in only (tls-browser, flaresolverr adapter); no CAPTCHA solving, no rotation | Yes (stealth proxies) | N/A | Rotation helpers | Real browser | Yes | Partial | Core strength |
| SERP / query-first | Discovery for `verify` only (DDG HTML, Brave); research takes URLs | `firecrawl_search` + Agent | s.jina.ai search | Bring your own | Bring your own | Marketplace actors | Bring your own | Add-on |
| Structured JSON extraction | Crawler `--schema` + `ai_extract`; not on single-URL scrape | Scrape JSON mode; Extract deprecated in MCP | JSON mode headers | User code | User code | Actors | Yes | Yes |
| Evidence / audit trail | WARC 1.1 + digests, politeness receipts, verbatim quote verification | Dashboards, webhooks | None | Datasets (hosted) | Traces/session recordings | Datasets, runs history | Logs | Logs |
| Honest failure taxonomy | Per-rung refusals, wall classification, labeled AI-mediated reads | Partial | No | Partial | No | Partial | Partial | No |
| Change monitoring | Local `watch` + simhash | Monitor (hosted) | No | Scheduler glue | No | Schedules | No | Alerts (hosted) |
| Injection defense | `[UNTRUSTED WEB CONTENT]` markers, defusal, caps | Prompt guidance | Minimal | N/A | N/A | N/A | Prompt templates | Minimal |
| MCP server included | Yes (stdio/SSE, no key by design) | Yes (official, hosted-backed) | Yes | Community | Community | Community | Yes | Community |
| Self-host everything | Yes (stdlib-first; heavy deps lazy) | Open-core self-host | Reader self-host | Yes | Library self-host | Platform self-host | Yes | No |
| Price to start | $0 (your machine; Gemini session optional) | $0 tier, then $19–$749/mo | $0 (rate-limited) | $0 | $0 credits grant, $0.02/hr browsers | $0 ($5 credit) | $0 + $10 Cloud credit | 1,000 free credits (ScrapFly) / free tier (ZenRows) |

Read the matrix as "what ships", not "what is possible": FreeGem can render
pages and follow redirects like anyone; it deliberately does not sell
bypass, rotation, or hosted scale.

## Pricing / business-model observations

- **Everyone meters except FreeGem.** Firecrawl: ~1 credit per basic
  scrape/crawl/map page; free 1,000 credits/month (plus 5 agent runs/day),
  then roughly $19 hobby, $99 standard (100k credits), $399 growth (500k),
  $749 scale (1M), enterprise custom. Apify: free plan carries $5/month of
  usage; Starter $29, Scale $199, Business $999, enterprise custom — and
  the real product is the 91,000+ actor marketplace. ScrapFly sells
  usage-based credits from 1,000 free on signup, "one key" for scraping +
  cloud browser + screenshot; ZenRows unifies all primitives on one shared
  credit balance and markets "pay only for successful requests" (its
  comparison pages attack credit multipliers directly). Jina Reader's free
  anonymous tier is a funnel into token-credit top-ups ($50 per 1B tokens
  per third-party trackers). Browser Use dropped flat subscriptions for
  pure credits: $0.02/browser-hour, top-ups that never expire.
- **The free tier is the sales funnel, not the product.** Every hosted
  player caps the free tier at hobby volume and monetizes agent workloads
  (Firecrawl agent runs, Apify serverless actor seconds, Browser Use
  browser-hours). Credit pricing aligns vendor revenue with agent
  invocation frequency — the 2026 growth loop is "your agent calls us".
- **Open source is the distribution channel for the cloud.** Crawlee and
  Crawl4AI are genuine free libraries, but both companies monetize the
  hosted run: Apify platform (storage, proxies, schedules), Crawl4AI
  Cloud ($10 free credit, packs from $10). Crawl4AI's license carries an
  attribution requirement — freer than proprietary, stricter than plain
  Apache-2.0.
- **FreeGem's model is orthogonal and should stay that way.** There is no
  meter because there is no service: cost is the user's machine, and the
  optional Gemini session is a personal credential, not a billing
  relationship. README explicitly refuses API-key auth and hosted
  multi-tenancy (`docs/P3_ECOSYSTEM.md` item 7). The strategic implication:
  FreeGem cannot win a price-per-page fight it is not in; it wins where
  "free, local, auditable, no-egress" is the requirement (compliance
  environments, air-gapped research, personal agent stacks, cost-sensitive
  high-volume crawls where per-page credits make unit economics hostile).

## Where FreeGem is distinctive

1. **Evidence-grade output.** No competitor in the scan pairs WARC 1.1
   capture (request/response pairs with sha-256 block and payload
   digests, metadata records for DNS death and SSRF refusals —
   `freegem/crawler/warc.py`), re-verification (`warc.verify`), and
   politeness receipts (`crawl report` with inter-fetch gap audits).
   Hosted platforms store *your* results on *their* disks; FreeGem holds
   the bytes it quotes, locally, replayable with warcio/pywb.
2. **The honest ladder.** `freegem/smart_fetch.py` escalates through
   browser-headers → r.jina.ai → optional Playwright → Wayback →
   Gemini search (plus opt-in tls-browser and FlareSolverr rungs), with
   per-host strategy memory, wall-class phrase-density detection, a global
   wall-clock bound, and a refusal taxonomy that names each rung's exact
   answer. Competitors either silently retry stealth tricks or return
   empty success. AI-mediated reads are labeled (`via="ai-search"`,
   `via="ai-wall-rescue"`) and never enter the cache.
3. **Verification as a product surface.** `web_verify` keeps
   "contradicted" distinct from "not_found" and from
   "source_unavailable"; every quote is checked verbatim against fetched
   text with fabricated quotes marked UNVERIFIED, locations computed
   deterministically, schema repairs counted. Firecrawl and the scraping
   APIs return bytes; they do not judge claims.
4. **Injection-aware untrusted-content handling.** End-to-end
   `[UNTRUSTED WEB CONTENT]` wrapping with marker defusal, wire and
   post-decompression caps, no cookie egress to scraped hosts, and
   robots.txt deliberately never consulted (documented as an
   attacker-controlled policy-poisoning vector in `docs/NON_GOALS.md`).
   Nobody in the landscape ships this posture.
5. **One process, three faces, zero accounts.** HTTP API (OpenAI-compatible),
   MCP server, and CLI over the same guarded fetch core; the 1,183-test
   offline suite and lazy optional deps make it maintainable in a way
   credit-metered SaaS comparisons cannot be audited.

## Top 5 feature opportunities (value / effort, with sketches)

1. **Query-first research: `research --from-search` (value: high, effort: low-medium).**
   Agents overwhelmingly start from a question, not URLs; today
   `freegem/web_research.py` requires URLs while `freegem/serp.py`
   deliberately never fetches (P3 boundary #3), and only `verify` has
   `--discover-backend serp`. Compose at the research layer: add a
   `--from-search` / MCP `search=` parameter that runs
   `serp.get_provider(...).search(query)`, takes the top-k hits (k clamped
   to the existing 10-page budget), dedupes by normalized host+path, and
   feeds them into the existing `_collect_sources` pipeline. Record
   `discovery: {backend, query, hits}` in the result metadata so answers
   never pretend the URLs were user-supplied. serp.py stays fetch-free;
   every page still goes through the guarded cache path.
2. **Structured JSON on the single-URL scrape face (value: high, effort: low-medium).**
   Crawl4AI and Firecrawl both market "any URL → typed JSON"; FreeGem has
   `freegem/llm_extract.py` (schema, validation, repair counters) and the
   `ai_extract` MCP tool, but `web_scrape` / `freegem scrape` return text
   only. Add `--schema fields.json` to scrape: run the normal guarded
   fetch and extraction, then one gated `llm_extract` pass over the
   extracted text, reusing crawler-style honesty (required-field misses
   reported, repairs counted, model failure degrades to raw text — never
   an empty "success").
3. **Evidence-bundle export: `crawl export-bundle` + `crawl replay` (value: medium-high, effort: medium).**
   The WARC writer and digests already exist; the missing product surface
   is a portable package. `export-bundle` zips `evidence.warc`,
   `results.jsonl`, the `crawl report` receipts JSON, and a manifest of
   sha-256 digests plus crawl parameters — one file an auditor can verify
   offline. `crawl replay --warc evidence.warc URL` re-extracts a page's
   text straight from the WARC (stdlib reader next to `warc.verify`) so
   quotes can be re-checked without any network. This is the feature
   Firecrawl Monitor and Apify datasets cannot ship: they host evidence;
   FreeGem would hand it over.
4. **Watch at fleet scale, locally (value: medium, effort: low-medium).**
   `freegem/watch.py` is per-URL with solid state hygiene, but each
   invocation handles one URL and always refetches. Add (a) `watch
   --urls-file` batch mode over the existing per-URL state dir with the
   same exit-code contract extended to "any changed = 3"; (b) conditional
   revalidation — store the origin's `Last-Modified`/`ETag` (the research
   fetch path already records `last_modified`) and send
   `If-Modified-Since`/`If-None-Match`, recording 304 as "no change
   without a body download"; (c) a `snapshot.json` written atomically per
   watch so agents can poll one JSON endpoint via a file watcher instead
   of spawning processes. This is the local answer to Firecrawl Monitor.
5. **Make the FTS crawl index a first-class face (value: medium, effort: medium).**
   `freegem/search_sinks.py` already ships `SqliteFtsSink` /
   `JsonlSink` implementing `upsert`/`search`, but nothing user-facing
   queries them. Add `freegem search "query" --index crawl.db` (CLI) and
   an MCP tool `crawl_search(index, query, limit)` that opens the sink and
   returns ranked hits with `url`, score, and snippet — turning any
   finished crawl into a self-hosted search index. Zero new fetch code;
   it completes the "crawls become queryable" story that the README's
   non-goals say is intentionally left to integrators, at the exact moment
   hosted players charge for dataset storage and query APIs.

Honorable mentions (not ranked): richer MediaWiki/arXiv adapters in
`crawler/sources.py`; a `--sink` flag if crawl output wiring is still
manual; embeddings for the existing `--chunks` RAG output (needs a model
policy decision first).

## Sources

- Firecrawl MCP tool surface (search/scrape/interact/crawl/map/agent/monitor) — https://docs.firecrawl.dev/mcp-server/tools and https://github.com/firecrawl/firecrawl-mcp-server (read 2026-10)
- Firecrawl pricing (plans $0/$19/$99/$399/$749 + enterprise; credit model) — Google/G2 pricing summary, https://www.firecrawl.dev (read 2026-10; third-party summary — re-verify before citing)
- Jina Reader product page — https://jina.ai/reader (read 2026-10)
- Jina Reader rate limits (~20 RPM anonymous; key tiers) — https://makerstack.co/reviews/jina-reader-review/ (2026-07-20; third-party)
- Jina AI token-credit pricing (10M free tokens, $50/1B top-ups) — https://www.usagepricing.com/blueprint/jina-ai (2026-08-27; third-party)
- Crawlee positioning and features (open-source, Apify SDK lineage, autoscaling, JS rendering) — https://crawlee.dev/blog/scrapy-vs-crawlee and https://github.com/apify/crawlee (read 2026-10)
- Apify pricing (free $5 credit; Starter $29; Scale $199; Business $999; 91k+ actors) — https://apify.com/pricing plus https://automationatlas.io/answers/apify-pricing-explained-2026/ (2026-07-23; third-party)
- Browser Use pricing (pay-as-you-go credits, $0.02/browser-hour, credits never expire, SOC 2) — https://browser-use.com/pricing (read 2026-10)
- Browser Use free-tier character (grant that runs out; plans to ~$999) — https://agentdeals.dev/vendor/browser-use (2026-10-04; third-party)
- Crawl4AI open-source + cloud ($10 free credit, packs from $10; Apache-2.0 with attribution) — https://github.com/unclecode/crawl4ai, https://crawl4ai.org, https://recatools.com/ai-directory/crawl4ai/ (2026-07-21; third-party)
- ScrapFly pricing (usage-based, 1,000 free credits, one key for scrape/browser/screenshot, 15B+ req/mo claim) — https://scrapfly.io/pricing (read 2026-10)
- ZenRows pricing (one plan, pay only for successful requests, shared credit balance) — https://www.zenrows.com/pricing and comparison pages (read 2026-10)
- FreeGem feature ground truth — this repository's `README.md`, `docs/NON_GOALS.md`, `docs/P3_ECOSYSTEM.md`, and modules `freegem/smart_fetch.py`, `freegem/web_research.py`, `freegem/claim_verify.py`, `freegem/serp.py`, `freegem/watch.py`, `freegem/llm_extract.py`, `freegem/search_sinks.py`, `freegem/crawler/{warc,sources,resilient,receipts}.py` (as of 2.9.x)
