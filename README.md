# FreeGem

**A local bridge for Gemini Web, presented through the interfaces you already use.**

FreeGem provides three compatible ways to work with Gemini:

| Face | Protocol | Command |
| --- | --- | --- |
| HTTP API | OpenAI-compatible + Google-native | `freegem serve` |
| Agent tools | MCP (Model Context Protocol) | `freegem mcp` |
| One-shot | plain CLI | `freegem ask "..."` |

Under the hood, FreeGem communicates with the Gemini Web session used by your
browser. On the client side, it exposes familiar protocols so existing tools
can usually be connected by changing a single base URL. It requires no
separate Gemini API key and introduces no additional per-request API charge,
although Gemini Web sessions remain subject to Google's availability,
rate-limits, policies, and account restrictions.

> **Project origin and attribution:** FreeGem is a maintained rebuild and
extension of [`Sophomoresty/gemini-web2api`](https://github.com/Sophomoresty/gemini-web2api),
released under the MIT License. The upstream project's protocol research and
implementation knowledge remain fundamental to this project. See
[Credits](#credits) for detailed attribution.

---

## Contents

- [Quick start](#quick-start)

- [The three faces](#the-three-faces)

- [Web scraping](#web-scraping)

- [Web research](#web-research)

- [Claim verification](#claim-verification)

- [Crawler](#crawler)

- [Dynamic pages and job API](#dynamic-pages-and-job-api)

- [Adapters and pipelines](#adapters-and-pipelines)

- [Models](#models)

- [Authentication](#authentication)

- [Configuration](#configuration)

- [Docker](#docker)

- [How it works](#how-it-works)

- [Limitations](#limitations)

- [Development](#development)

- [Credits](#credits)

- [License](#license)

- [Disclaimer](#disclaimer)

---

## Quick start

```bash
pip install freegem        # or: pip install "freegem[full]"
freegem serve
```

The server starts on `http://127.0.0.1:8081` — loopback only. Point any
OpenAI-compatible client at it:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8081/v1", api_key="not-needed")
resp = client.chat.completions.create(
    model="gemini-3.6-flash",
    messages=[{"role": "user", "content": "Explain quantum computing"}],
)
print(resp.choices[0].message.content)
```

Cherry Studio, ChatBox, Open WebUI, LangChain, Cline, anything that speaks
OpenAI — set the base URL and go.

**Try it before installing anything:**

```bash
freegem ask "what is the time in Tokyo?"
freegem ask --search "what changed in Python 3.14?"
freegem doctor
```

---

## The three faces

### 1. HTTP API — `freegem serve`

| Endpoint | For |
| --- | --- |
| `POST /v1/chat/completions` | OpenAI clients |
| `POST /v1/responses` | OpenAI Codex CLI |
| `GET /v1/models` | OpenAI model discovery |
| `POST /v1beta/models/{model}:generateContent` | Gemini CLI, non-streaming |
| `POST /v1beta/models/{model}:streamGenerateContent` | Gemini CLI, streaming |
| `GET /v1beta/models` | Google model discovery |
| `GET /health` | Liveness and session status |
| `GET /metrics` | Latency percentiles, status counts, retries, memory |

The server is loopback-only and assumes no auth, so it hardens its own
surface: POST bodies must be `Content-Type: application/json`, any request
carrying a non-loopback `Origin` header is refused with 403 (blocks
cross-site "simple" POSTs that skip CORS preflight), concurrent requests are
capped (default 32; extra connections get 503 instead of an unbounded thread
count), and a stalled client cannot hold a thread forever (65 s read timeout,
reported as 408).

Codex CLI:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8081/v1
export OPENAI_API_KEY=not-needed
```

Gemini CLI:

```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8081
gemini
```

### 2. MCP tools — `freegem mcp`

Exposes Gemini as tools an agent can call:

| Tool | Use for |
| --- | --- |
| `gemini_search` | Current facts; must cite sources |
| `gemini_ask` | Fast direct answers |
| `gemini_deep_think` | Long-form reasoning on hard problems |
| `gemini_advisor` | Structured second opinion on a decision |
| `gemini_chat` | Raw multi-turn, full control |
| `gemini_list_models` | Registry introspection |
| `web_scrape` | Read a web page (SSRF-guarded, capped) |
| `web_research` | Answer one question from several pages, cited [S1]..[Sn] |
| `web_verify` | Verify claims against sources: per-claim status + located evidence |

```bash
pip install "freegem[mcp]"

# stdio: your client launches it
freegem mcp --transport stdio

# sse: it stays up and clients connect over HTTP
freegem mcp --transport sse --port 8768
```

Client config (`.mcp.example.json`):

```json
{
  "mcpServers": {
    "freegem": {
      "type": "stdio",
      "command": "freegem",
      "args": ["mcp", "--transport", "stdio"]
    }
  }
}
```

### 3. One-shot CLI

```bash
freegem ask "summarise the plot of Dune"        # direct answer
freegem ask --search "latest Python release"    # web-grounded + sources
freegem scrape https://example.com --links      # read a web page
freegem verify "claim one" "claim two" --urls URL1 URL2   # per-claim verdicts
freegem crawl plan --seeds https://example.com --max-pages 20   # dry run
freegem models                                  # registry + session state
freegem doctor                                  # diagnostics, no generation
```

---

## Web scraping

`freegem scrape <url>` fetches a page and prints readable text — title, body,
no scripts, styles, or nav junk. The same capability is exposed to agents as
the `web_scrape` MCP tool.

```bash
freegem scrape https://example.com                     # text + title
freegem scrape https://example.com --links             # + resolved links
freegem scrape https://example.com --max-chars 5000    # cap the output
freegem scrape https://example.com --ask "what is this site?"
freegem scrape https://example.com --respect-robots    # opt into legacy robots honoring
```

**Security model** — this fetches *untrusted* URLs, so it reuses and hardens
the project's SSRF defenses (`freegem/ssrf.py`):

- Every URL and every redirect hop is **resolved, checked, and pinned**: the
connection dials only the verified-public IPs the check inspected, closing
the resolve→check→reconnect window (DNS rebinding) the image fetcher
previously had. TLS keeps verifying the original hostname (SNI + certs).

- Every redirect hop also re-checks scheme, host, and embedded credentials —
a redirect must not reintroduce what the guard refused on the first URL.

- **Proxy-mode carve-out**: with `proxy` configured, DNS belongs to the
proxy, so the connection cannot be pinned client-side (the proxy resolves
the hostname and dials what IT resolved). The client-side public-IP check
still runs on the first URL and on every redirect hop, and CGNAT
(`100.64.0.0/10`) plus deprecated IPv6 site-local (`fec0::/10`) are refused
everywhere. A configured proxy is trusted infrastructure by definition —
it can already see and route all traffic.

- Private ranges, loopback, link-local (including `169.254.169.254`),
reserved, and multicast targets are refused before any socket exists, as
are `file://`, `ftp://`, and URLs with embedded credentials.

- The session cookie is **never** sent to scraped sites.

- Responses are capped on the wire *and* after decompression, so gzip
bombs die at the cap instead of in RAM. Only HTML and text are accepted.

- `robots.txt` is **never fetched and never consulted**, by design. A
robots.txt is attacker-controlled text from the same origin as the pages
being read — a documented prompt-injection/policy-poisoning vector against
scrapers and AI agents — and a published page is published to be read.
`--respect-robots` opts back into the legacy honoring.

- Output is wrapped in `[UNTRUSTED WEB CONTENT]` markers, and `--ask` mode
instructs Gemini to treat everything between them as data, never as
instructions. Page text, titles, and link labels are defused first: a page
cannot carry the literal marker strings inside its own content to close
the wrapper early and speak outside it. A page can still contain text
aimed at your agent; the markers limit, but do not eliminate, that
injection surface.

**Limitations**: no JavaScript rendering (React/SPA sites come back mostly
empty — that needs a headless browser, deliberately not a dependency), and
authentication, paywalls, and legal blocks (401/402/451) are never bypassed.
Origins that refuse honest requests after the escalation ladder are reported
as such rather than forced.

---

## Web research

`freegem research` is the layer above `scrape`: provide a question and the
specific URLs you want compared. It does not discover or search for URLs on
your behalf. FreeGem fetches the supplied pages with the same guarded,
capped machinery, then returns one answer with inline `[S1]`,
`[S2]` citations, key points, and a disagreements-and-gaps section. Agents get
the same capability through the `web_research` MCP tool.

```bash
freegem research \
  --query "Which option fits a small Python API?" \
  https://docs.example.com/option-a \
  https://docs.example.com/option-b \
  --format markdown
```

What it does beyond N scrapes:

- **Local fetch cache** — successful fetches are stored under
`~/.cache/freegem/` (30-minute TTL, `$FREEGEM_CACHE_DIR` to relocate), so
repeated agent calls do not re-download pages. `--no-cache` / `--refresh`
control it. The SSRF guard runs on every real fetch; robots.txt is never
consulted (see [Web scraping](#web-scraping)).

- **Structured source adapters** — Wikipedia and other Wikimedia wikis are
fetched through their own Action API (`explaintext`) instead of scraping
HTML, so article text arrives clean, without navigation chrome. The adapter
runs behind the same SSRF guard; per-source metadata records it as
`via: mediawiki-api`.

- **Concurrent fetching** — pages from different sites are fetched in
parallel (bounded pool, 3 by default, `--fetch-concurrency` / the
`research_concurrency` config to change; fetches to the same host always
serialize). Budget decisions stay strictly in request order.

- **Extraction-quality indicators** — every source is labeled `ok`,
`nav_only`, or `empty` in the JSON output, so a caller can tell a real
article from a navigation stub without reading it.

- **Verified evidence quotes** — with evidence mode on (default; config
`research_evidence`, CLI `--evidence/--no-evidence`, MCP `evidence`), the
answer is followed by verbatim quotes for every cited source, each checked
against the text FreeGem actually fetched and flagged verified or
`UNVERIFIED`. A quote the model fabricated stays visible but is marked, so
fabrication is distinguishable from a verifier miss. A source that failed to
fetch is reported as missing evidence — never as proof a claim is false.

- **Source timestamps** — every fetched source records when it was fetched
(`fetched_at`, ISO-8601 UTC; cache hits keep the original fetch time) and the
origin's `Last-Modified` header when it sent one, so a reader can judge how
stale the evidence is.

- **Hard budgets** — at most 10 pages per call, a per-page character cap
(`--max-chars-per-page`, default 12 000), and a total fetched-character
budget (`--total-budget`, default 80 000). URLs that would push the run
over budget are reported as skipped, not fetched.

- **Bounded prompts** — small material goes to Gemini in one pass; larger
material takes a map/reduce path (capped per-page notes, then a reduce
pass), so prompt size does not grow with raw page size.

- **Authoritative citations** — the `[S#] -> URL` table is rendered from
freegem's own fetch metadata, never from model text, and an answer citing
sources that do not exist is rejected into the raw-extract fallback.

- **Never fails on Gemini** — with no session or on rate limits, you get the
labeled raw extracts plus fetch metadata instead of an error; a dead source
is recorded per-URL and the rest of the run continues.

- **Untrusted end to end** — every page stays inside
`[UNTRUSTED WEB CONTENT]` markers in every prompt. Folding several
attacker-writable pages into one prompt widens the injection surface; the
markers limit, but do not eliminate, it.

- **Formats** — `--format text|markdown|json`; the JSON schema documents
per-source status (`ok`, `error`, `truncated`, `from_cache`, `skipped`,
`chars`, `fetched_at`, `last_modified`), the verified evidence `citations`,
and run metadata (cache hits, failed pages, evidence offered vs verified,
timing, version).

Not a crawler: hand it the specific pages to compare. No JavaScript rendering
(same posture as the scraper), and cache entries store extracted text only —
never cookies or credentials.

---

## Claim verification

`freegem verify` is the layer above research: instead of one answer to one
question, you hand it a list of claims (1–8) and it judges each against the
fetched sources. Agents get the same capability through the `web_verify` MCP
tool. Each claim comes back with exactly one status:

| Status | Meaning |
| --- | --- |
| `supported` | The sources contain clear verbatim support for the whole claim |
| `partially_supported` | Verbatim support exists, but covers only part of the claim |
| `contradicted` | A source statement conflicts with the claim |
| `not_found` | The sources do not settle the claim either way |
| `source_unavailable` | The sources that would settle it failed to load |

The last two are the point: **"the source contradicts the claim" stays
distinct from "the source did not contain enough evidence"**, and a failed
fetch is never evidence of falsity.

```bash
freegem verify \
  "Alexander became king of Macedon in 336 BC." \
  "Alexander died in 330 BC." \
  --urls https://en.wikipedia.org/wiki/Alexander_the_Great
```

Output follows the documented shape, per claim:

```text
Claim: Alexander became king of Macedon in 336 BCE
Status: Supported

Evidence:
"Alexander succeeded his father Philip II in 336 BCE." (verified)

Source: [S1]
Location: Introduction
Retrieved: 2026-10-11
```

What the machine enforces, not just asks for:

- **Verbatim quote verification** — every status quote is checked against the
text FreeGem actually fetched (whitespace/case-insensitive). A quote the model
fabricated stays visible but is marked `UNVERIFIED` — fabrication stays
distinguishable from a verifier miss.

- **Deterministic Location** — each verified quote gets the section of the
source it came from ("Introduction", "§ History", "para 7"), computed by
finding the quote inside the sectioned text. The model never asserts where
evidence lives. MediaWiki extracts split on their own `== Heading ==`
markup; extracted HTML splits into labeled paragraphs.

- **Status schema enforcement** — unknown statuses are repaired to
`not_found`, a "supported" verdict without quotable evidence is downgraded
with an explanatory note, missing verdicts are reported as `not_found`, and
evidence citing unloaded sources is dropped. Every repair is counted
(`schema_repairs`) and shown. If every source is dead, no model call happens
at all: every claim is `source_unavailable` by construction. If Gemini is
unavailable, verdicts are left unjudged (`Note: gemini unavailable…`) with
the raw material attached — never fabricated.

- **Retrieved dates** — every evidence block carries the date its source was
fetched, from fetch metadata (cache hits keep the original fetch time), so
staleness is visible per claim.

**Optional URL discovery** — with `--discover` (MCP `discover: true`) and no
URLs, candidate pages come from a supported search API instead of you: the
Wikimedia API portal for Wikimedia wikis, or the site's own OpenSearch API
for other MediaWiki hosts (`discover_wiki` config, `--wiki` to override,
`--discovery-query` for a custom search). Discovery is one bounded API
call, never a crawl, and — like every fetch in freegem — robots.txt is not
consulted (it is attacker-controlled text; see [Web scraping](#web-scraping)).

```bash
freegem verify "Alexander tutored by Aristotle?" --discover \
  --discovery-query "Alexander the Great education"
```

**Run metrics** — verify (and research) attach a metrics block to JSON
output: a request id, per-stage timings (fetch / generate / verify),
counters (pages ok/failed/skipped, cache hits, evidence offered vs
verified), and process RSS. Source accounting reconciles explicitly:
`pages_ok + pages_failed + pages_skipped == urls_requested`, with
`pages_skipped` counting URLs reported as budget-skipped rather than
fetched-and-failed. The HTTP face exposes process
aggregates at `GET /metrics` — p50/p95/p99 latency over a bounded window,
response-status counts, queue refusals, upstream retry totals, memory. POST
responses carry an `X-Request-Id` header for correlating logs.

```bash
curl -s http://127.0.0.1:8081/metrics
```

Not a crawler either: discovery returns a handful of article URLs which then
go through the same guarded fetch pipeline; no link extraction, no following
results-of-results, no browser rendering.

---

## Crawler

`freegem crawl` is a bounded, resumable local crawler — the scraper
generalized from one URL to a budgeted set of them, with a plan step, a
persistent frontier, and typed output. It is deliberately NOT a recursive
spider by default: the safe shape is "these seeds, this scope, this page
budget, nothing else". robots.txt is never fetched or consulted anywhere in
the crawler (see [Web scraping](#web-scraping) for why); politeness is
per-host pacing.

```bash
# dry run: normalize seeds, show scope verdicts and budgets — no network
freegem crawl plan --seeds https://example.com/docs/ \
  --allow-path /docs --max-depth 2 --max-pages 50

# run it: results stream to JSONL, raw bodies land in artifacts/
freegem crawl run --seeds https://example.com/docs/ \
  --allow-path /docs --max-depth 2 --max-pages 50 \
  --state crawl.db --out results.jsonl --artifacts artifacts/ \
  --schema fields.json --sitemap --delay-ms 250 --concurrency 2

# inspect / continue
freegem crawl status --state crawl.db
freegem crawl run --seeds https://example.com/docs/ --state crawl.db   # resume
```

**Scope** — `--allow-domain` / `--deny-domain` (suffix-matched at a dot
boundary), `--allow-path` / `--deny-path` (prefix), `--max-depth`,
`--max-pages`, and same-host-only by default (`--any-host` to leave the
seed hosts). Deny always wins. Seeds are URLs, `.txt` files (one URL per
line), or `.jsonl` files (`{"url": ..., "category": ...}`).

**Frontier** — SQLite (`--state`), so a killed crawl resumes exactly where
it stopped: every state change commits, `crawl status` reports counts by
status plus the noisiest hosts, and an interrupted run leaves no in-flight
hole (`inflight` rows are returned to `pending` on the next start). URLs
dedupe by normalized spelling — tracking parameters (`utm_*`, `fbclid`,
`gclid`, ...) are stripped, fragments dropped, IDN hosts punycoded, default
ports and `%XX` casing normalized.

**Extraction** — pass `--schema fields.json` to cut typed records instead
of whole pages:

```json
{
  "item_selector": "article",
  "fields": [
    {"name": "title", "selector": "h2 a", "required": true,
     "normalize": ["strip", "collapse"]},
    {"name": "url", "selector": "h2 a", "attr": "href", "type": "url"},
    {"name": "score", "selector": "span.score", "type": "int"},
    {"name": "all_links", "selector": "a", "attr": "href", "type": "url",
     "multi": true}
  ]
}
```

Selectors are a compiled CSS subset (tag/class/id/attribute operators,
descendant and child combinators, comma groups), an ElementTree-subset
XPath (`kind: "xpath"`), or JSON paths (`kind: "json"`); types are
`str|int|float|bool|url` with `required`, `default`, `pattern` validation,
and per-field provenance in every output row (`url`, `fetched_at`,
`content_hash`, `via`). Without a schema you get one record per page:
url, title, readable text. Formats: `--format jsonl` (default), `json`,
`csv`.

**Hardening** — every response body is capped on the wire AND after
decompression (default 100 MiB, `--max-bytes`), with bodies over 16 MiB
spilled to disk instead of held in RAM; the DOM is capped in nodes
and depth; XML (sitemaps, feeds) is parsed with DTD/ENTITY refused outright;
every log line and output row passes a secret-redaction pass (API-key and
token shapes become `[REDACTED:kind]`) before touching disk; per-host
politeness delay (`--delay-ms`), exponential backoff with jitter on
retries, and `Retry-After` honored verbatim on 429/503. `Ctrl-C` is
graceful: the in-flight page finishes, the frontier checkpoints, the
results file is closed cleanly.

**Guard stack unchanged** — the crawler fetches through the same SSRF
guard as the scraper (DNS resolution pinned to checked public IPs, private
and link-local targets refused at dial time), sends the honest crawler
User-Agent, and never sends your session cookie. Every page goes through
the **escalation ladder** (`CrawlSpec.resilient`): browser-grade header
profiles on 403/406 walls, 429 waits, timeout growth, origin-root fallback
for dead paths, https↔http and www↔apex swaps, and one TLS-relaxed retry
flagged `tls_unverified` for broken certificate chains. 401/402/451 are
never bypassed. Oversized pages (≥ 2 MB) can be distilled to their
important content by Gemini (`CrawlSpec.distill`) with an extractive
fallback.

---

## Dynamic pages and job API

Some pages are not HTML until a browser runs: empty `<app-root>` shells,
"enable JavaScript" walls, and dashboards. The HTTP ladder stays first —
it is 100× cheaper — and a headless browser is the fallback, not the
default.

**Playwright rendering** (`pip install playwright && playwright install
chromium`, optional): `freegem.playwright_fetcher` renders with an isolated
browser context per fetch (no shared cookies, storage, or service
workers), selector/network waits, optional screenshots, PDFs (chromium),
and traces, with `finally`-guaranteed cleanup. `fetch_http_first()` runs
the guarded HTTP fetch and falls back to the browser when the body is a
JS shell.

**Session profiles** (`freegem.session_profiles`): named storage-state
profiles (cookies + origins, Playwright-compatible) under
`~/.config/freegem/profiles/`, with `expires_at` enforced on load,
playwright export, and `http.cookiejar` export for the HTTP face.
Credentials live where you put them — the directory is user-owned, and
`export`/`delete` are explicit.

**Job API** (REST, optional bearer auth): submit work, poll, download
artifacts:

```bash
# start the server with an API key for the job surface
FREEGEM_API_KEY=sk-local-1 freegem serve

# submit a dynamic render job
curl -s -X POST http://127.0.0.1:8081/v1/jobs \
  -H "Authorization: Bearer sk-local-1" -H "Content-Type: application/json" \
  -d '{"type": "playwright", "params": {"url": "https://spa.example.org",
       "wait_selector": "#main", "capture_screenshot": true}}'

# poll and download
curl -s -H "Authorization: Bearer sk-local-1" http://127.0.0.1:8081/v1/jobs/<id>/results
curl -s -H "Authorization: Bearer sk-local-1" -o shot.png http://127.0.0.1:8081/v1/artifacts/<artifact_id>
```

Job types: `scrape`, `research`, `playwright`, `document` (PDF/DOCX
adapters), `extract` (schema-constrained LLM extraction, below),
`selenium` (Grid adapter, below). Every job carries request ids, retry
traces, and lands in the audit log; `GET /v1/jobs` lists recent work,
`GET /health` reports liveness, `GET /metrics` the counters. The same
operations are exposed to MCP clients through four narrow, auth-gated
tools (`freegem_submit_job`, `freegem_get_job`, `freegem_get_artifact`,
`freegem_manage_profile`); the api key may also come from the `api_key`
config field (see [Authentication](#authentication)).

## Adapters and pipelines

Everything below is stdlib-first: the optional heavy dependencies
(`playwright`, `selenium`, `redis`, `pika`, `psycopg`, `boto3`) import
lazily and raise a named error with the pip line, never an ImportError
traceback. Nothing here phones home by itself.

**Text pipeline** (`freegem.text_pipeline`) — paragraph-aware chunking
with overlap, idf-weighted relevance selection to spend prompt budget on
the right 5% of a page, a deterministic extractive summarizer (real
sentences, no generation), and `CostQualityTelemetry` (chars in/out,
chunks made/selected, token estimates, reduction ratio) so "what did this
page cost" is a dict, not a guess.

**Schema-constrained extraction** (`freegem.llm_extract`) — declare typed
fields (`string|integer|number|boolean|array|enum`), get JSON Schema for
APIs and an instruction block for models; answers are validated and
REPAIRED (stringified numbers coerced, unknown keys dropped, enum
violations reported) with one complaint-driven retry before a required
field is allowed to fail. Providers are neutral: the bridge's own
`generate()` (default), any OpenAI-compatible endpoint via stdlib urllib,
or a `NullProvider` for pipeline tests. Reachable as the `extract` job
type.

**Queues** (`freegem.queues`) — one `WorkQueue` face over `memory://`,
`sqlite:///path` (durable, crash-safe, `recover()` requeues unacked work
after a restart), `redis://` (optional `redis`), and `amqp://` (optional
`pika`). `open_queue(url)` dispatches on scheme; reserve/ack/nack is the
whole contract.

**Document and blob stores** (`freegem.stores`) — documents:
`sqlite:///path` (default) or PostgreSQL (optional `psycopg` v2/v3,
JSONB metadata, server-side containment queries). Blobs: content-addressed
files (`ab/<sha256[:2]>/<sha256>`, the same layout the crawl runner uses)
or S3 (optional `boto3`, prefix-partitioned). `open_document_store` /
`open_blob_store` dispatch on scheme.

**Near-duplicate detection** (`freegem.dedup`) — 64-bit simhash over word
5-shingles with a jaccard verification pass, so mirrors and boilerplate
reprints collapse while genuinely different pages survive a fingerprint
collision. `add()` returns False and names the original for duplicates.

**Search sinks** (`freegem.search_sinks`) — `SqliteFtsSink` (FTS5 with a
LIKE fallback on FTS-less builds), `JsonlSink` (append-only log +
tf-idf ranking), and `NullSink`. Re-upserting a doc id replaces — a
recrawl updates, it does not duplicate.

**Adaptive recrawling** (`freegem.crawler.scheduling`) — per-URL change
history in the frontier's SQLite file; unchanged pages double their
interval toward the max, frequently-changing pages shrink toward the min;
recrawl probes send `If-None-Match` / `If-Modified-Since` so unchanged
pages cost a 304. Plans are inspectable (`recrawl_candidates`) before
they touch `next_attempt_at`.

**Source adapters** (`freegem.crawler.sources`) — MediaWiki
(`action=query`), arXiv (Atom API), RSS/Atom feeds (stdlib XML), and
documentation sites (sitemap.xml) turn a site's structured front door
into a seed list; `detect_source()` picks the adapter from the URL.
Adapters return `SourceItem` records — they never fetch page bodies, so
every guard stays in the runner.

**Discovery and prioritization** (`freegem.crawler.discovery`) —
`discover_js_endpoints()` lifts `fetch()`/axios/XHR API endpoints and
links out of the page's own markup and scripts (no execution); `score_url`
+ `BestFirstPrioritizer` rank the frontier: docs/api/guide paths and
same-origin near-root pages first, login walls, media files, and mobile
duplicates last. Deterministic scores, floored at zero, explainable by
construction.

**Selenium/Grid adapter** (`freegem.selenium_fetcher`) — the Playwright
alternative for deployments that already run a Grid: fresh session per
fetch on a Grid node (`grid_url`) or local chromium, waits, screenshots,
CDP `Page.printToPDF`, storage-state seeding shared with session
profiles. Reachable as the `selenium` job type.

---

## Models

| Model | Mode | Notes |
| --- | --- | --- |
| `gemini-3.7-flash` | FAST | Latest all-around |
| `gemini-3.6-flash` | FAST | All-around |
| `gemini-3.5-flash` | FAST | Alias for 3.6 Flash |
| `gemini-3.5-flash-thinking` | THINKING | Longest output (~20k chars) |
| `gemini-3.5-flash-thinking-lite` | DYNAMIC | Adaptive thinking depth |
| `gemini-3.1-pro` | PRO | Needs a session cookie for real routing |
| `gemini-3.1-pro-enhanced` | PRO | Experimental, extra output flags |
| `gemini-auto` | AUTO | Let Gemini pick |
| `gemini-flash-lite` | LITE | Fastest, lightweight |

**Thinking depth** — append `@think=N` to any model id:

```
gemini-3.5-flash-thinking@think=0   # deepest
gemini-3.5-flash-thinking@think=2   # medium
gemini-3.5-flash-thinking@think=4   # shallowest / auto
```

---

## Authentication

**Anonymous by default.** No key, no header, no setup.

**Optional session cookie** (`cookie_file`) improves things: anonymous requests
are rate-limited more aggressively, and `gemini-3.1-pro` silently routes to
Flash without a cookie. A cookie gives you the real model.

Get one with the bundled extension:

1. Load `gemini-cookie-sync-extension/` from `chrome://extensions` (Developer mode)

1. Sign in at [https://gemini.google.com/app](https://gemini.google.com/app) and reload

1. **Inspect session** → **Export gemini-auth.json**

Then either drop `gemini-auth.json` in the working directory (it is found
automatically) or point at it:

```bash
freegem serve --cookie-file ~/Downloads/gemini-auth.json
```

Check it without spending a request:

```bash
freegem doctor
```

> **There is no API-key authentication in this project, by design.** There is
nothing here worth protecting *except your Google session*. If you must
expose the bridge beyond loopback, put it behind a reverse proxy that does
the auth. Do not add keys to this code.

---

## Configuration

Optional. Copy the template and edit:

```bash
cp config.example.json config.json
```

```json
{
  "host": "127.0.0.1",
  "port": 8081,
  "default_model": "gemini-3.6-flash",
  "cookie_file": null,
  "temporary_chats": true
}
```

| Field | Default | Meaning |
| --- | --- | --- |
| `host` | `127.0.0.1` | Bind address. Non-loopback exposes your session |
| `port` | `8081` | Listen port |
| `default_model` | `gemini-3.6-flash` | Used when a request omits a model |
| `cookie_file` | `null` | Path to `gemini-auth.json` |
| `auth_user` | `null` | Account index for the `/u/N/` path |
| `xsrf_token` | `null` | `SNlM0e`; required when authenticated |
| `gemini_bl` | built-in | Frontend build tag; auto-refreshed on `serve` |
| `temporary_chats` | `false` | Keep requests out of your account history |
| `retry_attempts` | `3` | Retries on transient failure |
| `retry_delay_sec` | `2` | Base delay between retries; grows exponentially (2 s, 4 s, 8 s … capped at 30 s), and a server-sent `Retry-After` header wins when it asks for longer |
| `request_timeout_sec` | `180` | Per-request timeout |
| `proxy` | `null` | HTTP proxy if `gemini.google.com` is blocked; proxy-side DNS means local IP pinning cannot be guaranteed |
| `log_requests` | `true` | Request log to stderr |
| `research_concurrency` | `3` | Parallel page fetches per research call (same-host fetches serialize; capped at 8) |
| `robots_cache_ttl_sec` | `600` | legacy: TTL for the opt-in robots cache (`scrape_page(..., ignore_robots=False)`); freegem does not consult robots.txt by default |
| `request_queue_wait_sec` | `0.0` | Seconds a burst may wait for a free slot before getting 503 + `Retry-After`; `0` sheds immediately |
| `research_evidence` | `true` | Quote and verify verbatim evidence per cited source in research answers |
| `discover_wiki` | `en.wikipedia.org` | Wiki claim-verification's optional URL discovery searches (Wikimedia API portal, or the site's own OpenSearch for non-Wikimedia MediaWiki hosts) |

Environment variables override the file: `GEMINI_BL`, `GEMINI_AUTH_USER`,
`GEMINI_XSRF_TOKEN`, `GEMINI_PROXY`, `GEMINI_COOKIE_FILE`, `FREEGEM_CONFIG`.

Config is discovered in this order: `--config` flag → `$FREEGEM_CONFIG` →
`./config.json` → `./gemini-auth.json` → `~/.config/freegem/config.json`.

---

## Docker

The image is credential-free; mount the session at runtime.

```bash
docker compose up -d
```

```yaml
services:
  freegem:
    build: .
    ports: ["127.0.0.1:8081:8081"]
    environment:
      GEMINI_COOKIE_FILE: /secrets/gemini-auth.json
    volumes:
      - ./gemini-auth.json:/secrets/gemini-auth.json:ro
```

Or the MCP face:

```bash
docker compose --profile mcp up -d
```

---

## How it works

```
OpenAI client ─┐
Gemini CLI   ──┼─► freegem ─► Gemini Web StreamGenerate ─► Google
MCP agent    ──┘        │
                        └─ prompt translation (roles, tools, images → flat prompt)
                           Gemini wire format (positional array, field 79 = model)
                           response parsing (progressive snapshots → final text)
```

Requests go to the same `StreamGenerate` endpoint the Gemini web app uses. The
body is a positional array where `[79]` selects the model and `[17]` the
thinking depth — positions read out of Gemini's frontend bundle. Responses are
progressively longer snapshots of the same answer; the bridge yields the
difference between consecutive snapshots as the SSE delta.

**Notable implementation choices**, all covered by tests:

- **Immutable config.** `BridgeConfig` is a frozen dataclass. The original
single-file version used a module-global mutable dict, which under a threaded
server is a data race: one request's override could be observed by another
mid-flight. A Cloudflare Workers port of this same project hit exactly that
bug in production — cookie cross-contamination between concurrent requests —
and fixed it the same way.

- **Both build-tag formats matched.** The original only matched
`boq_assistant-bard-web-server_*` and *silently* gave up when Google switched
to `boq_gemini-web-uiserver_*`. The auto-update looked like it worked because
failure was indistinguishable from success. Both patterns are matched now, and
an empty result is reported.

- **Stream corruption is detected, not hidden.** If a snapshot stops extending
the already-emitted text, the stream aborts rather than silently rewriting
what the client has accumulated.

- **Session is never silently dropped.** A mid-write cookie file falls back to
the last good parse instead of degrading to anonymous.

---

## Limitations

Read this before you build on it.

- **Unofficial and unsupported.** This drives a private web endpoint. Google's
Terms of Service generally prohibit automated access to the web frontend.
Use for personal projects, learning, and prototyping — not as production
infrastructure, and not for anything where an account ban would matter.

- **Stability is not yours.** When Google changes the endpoint, rotates the
build tag, or tightens detection, this breaks until someone updates it. The
official API has an SLA; this has maintainers.

- **"Pro" is a label without the right cookie.** Without a Gemini Advanced
subscription cookie, `gemini-3.1-pro` routes to Flash. You get the name, not
the model.

- **No real multi-turn state.** Every request is independent. History is
concatenated into one prompt and re-sent each time, so costs and latency grow
with conversation length, and Gemini never actually remembers the session.

- **Token counts are estimates.** The web endpoint returns no usage data, so
`usage` is a character-count approximation (`len / 4`). Present so clients
that read the field do not crash; not accurate.

- **Tool calling is prose, not protocol.** Tool schemas are written into the
prompt and calls are scraped back out of markdown fences. It works, but it is
prompt-engineering, not a real function-calling channel, and can be less
reliable than a native API.

- **Images need an upload round-trip.** Multimodal input goes through Google's
resumable upload endpoint and is more likely to need a cookie.

- **Rate limits are real.** Anonymous requests are throttled harder. Sustained
heavy use can get the session blocked.

- **Streaming with tool calls is emulated.** OpenAI clients expect complete
tool calls, so the full answer is generated, parsed, then replayed as one
chunk. Plain text streams token-by-token.

- **The scraper fetches raw HTML.** `web_scrape` / `freegem scrape` render no
JavaScript: SPA sites return whatever the server sends before hydration,
which is often almost empty. That needs a headless browser and is
deliberately out of scope.

---

## Development

```bash
git clone https://github.com/plokmklo/freegem
cd freegem
pip install -e ".[dev]"

ruff check .            # lint
ruff format --check .   # format
mypy freegem            # types
pytest                  # tests
```

The test suite runs entirely offline: every Gemini call is mocked, so CI needs
no credentials and no network. The current suite contains 651 tests (+34
subtests, 685 cases as pytest counts them). See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the layering rules and
[`CONTRIBUTING.md`](CONTRIBUTING.md) for how to add a model or an endpoint.

### Layout

```
freegem/
  config.py       immutable config, env overrides
  models.py       model registry + @think parsing
  gemini.py       Gemini protocol client
  ssrf.py         shared SSRF guard: resolve, check, pin (DNS-rebinding safe)
  web_scraper.py  page fetching: SSRF guard, adapters, HTML -> text (no robots.txt, by design)
  web_research.py multi-source research: cache, budgets, [S#] citations
  claim_verify.py claim verification: statuses, evidence, discovery
  metrics.py      run + server metrics: timing, percentiles, counters
  multimodal.py   image upload
  tools.py        pure protocol translation (OpenAI/Google -> prompt)
  server.py       HTTP faces
  mcp_tools.py    MCP tools (dispatch is pure; build_server needs `mcp`)
  cli.py          argparse entry point
  tests/
gemini-cookie-sync-extension/   Chrome extension for exporting a session
docs/
```

---

## Credits

FreeGem is derived from and acknowledges the work in
[`Sophomoresty/gemini-web2api`](https://github.com/Sophomoresty/gemini-web2api),
licensed under the MIT License (© 2026 Sophomoresty). FreeGem is an
independent rebuild and extension of that Python project, not an official
Google product or an official upstream release.

The following foundational work came from upstream unchanged or with limited
adaptation:

- **The Gemini Web protocol implementation** — the `StreamGenerate`
positional-array wire format, `[79]` as the model selector, `[17]` as the
thinking depth, `SAPISIDHASH` authentication, and the `wrb.fr`
progressive-snapshot response shape.

- **The model registry** — the `MODE_CATEGORY` mapping
(1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=DYNAMIC_THINKING, 6=FLASH_LITE) and the
`gemini-*` model ids.

- **The multimodal path** — Gemini's Scotty resumable-upload protocol and the
page-token scraping it depends on.

- **The cookie-sync extension** — `gemini-cookie-sync-extension/` is a lightly
adapted copy of upstream's, with the export filename and project name
updated.

- **The Cloudflare Workers port** — upstream's `cloudflare/worker.js` informed
the request-scoped-config design, which was itself the fix for a
cookie-cross-contamination bug under concurrent warm-isolate requests.

FreeGem adds and maintains the following project-specific work:

- A layered package layout (`config` / `models` / `gemini` / `multimodal` /
`tools` / `server` / `mcp_tools` / `cli`) in place of the single-file
original.

- Immutable, request-safe configuration instead of a module-global mutable dict.

- A unified CLI running the HTTP, MCP, and one-shot faces.

- `gemini_advisor` and the `gemini_deep_think` output contract.

- A 416-test suite that runs offline with no credentials.

Upstream's own README credits its development agent capability to
[GenericAgent](https://github.com/lsdefine/GenericAgent), originally shared via
the [linux.do](https://linux.do) community — credit passed along here.

Also worth knowing: an unrelated Go implementation publishes under the same
name ([`ikhsan3adi/gemini-web2api`](https://github.com/ikhsan3adi/gemini-web2api)).
It is a separate project; this one derives only from the Python original.

## License

MIT — see [LICENSE](LICENSE).

Upstream's MIT copyright notice is retained in the spirit required by that
licence, and the files derived from it remain under the same terms.

## Disclaimer

Not affiliated with or endorsed by Google. "Gemini" is a trademark of Google
LLC. Use at your own risk and in accordance with Google's Terms of Service.
