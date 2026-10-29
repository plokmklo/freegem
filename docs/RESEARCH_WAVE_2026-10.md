# Research Wave — October 2026: antibot walls, ecosystem shipments, and what FreeGem should do next

**Date:** 2026-10-20 · **Scope:** internet survey by FreeGem's research division.
Companion to [`ANTIBOT_RESEARCH.md`](ANTIBOT_RESEARCH.md) (which froze the
seven-layer model and the 566-paper literature sweep) and
[`P3_ECOSYSTEM.md`](P3_ECOSYSTEM.md). This wave covers what changed on the
wall and in the open-source scraper ecosystem between roughly March and
October 2026, and what of that is newly actionable inside FreeGem's existing
line (honest client first; opt-in rungs only; no CAPTCHA solving; no
stealth). Every factual claim below carries an inline source; the full list
with URLs sits in [Sources](#sources). Claims marked *(search-summary)* were
obtained through a web-search answer that listed the cited page as its
source; the page itself was not fetched verbatim, because in two cases it
served us a bot-challenge instead of content — see [Wall Landscape §6](#6-fetching-the-antibot-literature-is-now-itself-a-battleground).

---

## Wall Landscape

### 1. Cloudflare Precursor: the challenge moved from a checkpoint to the whole session

On **July 13, 2026**, Cloudflare launched **Precursor**, a client-side,
session-based verification system that continuously injects JavaScript into
HTML responses and collects behavioral signals as the visitor interacts with
the application — then feeds those signals into bot protection in real time
([Cloudflare Blog](https://blog.cloudflare.com/introducing-precursor/)).
Turnstile itself now "runs nearly 3 billion times per day" and "has evolved
from a CAPTCHA replacement to a risk-based managed challenge"; Precursor
closes the gap *between* challenges by scoring the entire user journey
(same source). The launch post is explicit about the threat model it
targets: modern automation "can execute JavaScript, use real browser
environments, and pass individual CAPTCHAs without raising suspicion. What
remains difficult to replicate is consistent human behavior over time." It
then names the tells — Gaussian-noise mouse paths and mathematically ideal
Bézier curves read as *bot-like* because humans pivot from the wrist, show
cognitive-load delay between seeing and clicking, and have physiological
hand tremor (same source).

The operational consequence for scrapers is sharp: **the "solve the
challenge once, harvest `cf_clearance`, replay it from a plain HTTP client"
playbook — the play behind FlareSolverr and behind FreeGem's opt-in
`flaresolverr` rung — has a shrinking validity window.** Precursor scores
behavior *after* the challenge is passed, so a clearance cookie obtained
without a full human session can be scored down mid-session. Precursor is
currently an Enterprise Bot Management add-on (same source), so it is not
yet on every Cloudflare site — but it is on the class of sites that matter
commercially.

### 2. Cloudflare's September 15 AI-agent default: a policy wall, not a fingerprint wall

**Effective September 15, 2026**, Cloudflare's default bot configuration
blocks bots classified as **Training** or **Agent** on pages that display
ads, while **Search** stays allowed. New customers get the defaults from day
one; new domains added to existing accounts get them; Free-tier accounts
that never touched bot settings were switched over automatically; paid
customers with pre-existing domains keep their prior configuration
([Anakin, Oct 7, 2026](https://anakin.io/blog/cloudflare-ai-agent-blocking-default-2026);
also [Hosting.com's summary](https://hosting.com/learn/cloudflares-new-ai-crawler-defaults-and-what-they-mean-for-site-owners/) *(search-summary)*).

The classification is the point: Cloudflare splits automated traffic into
**Search** (indexes content, sends traffic back — allowed), **Agent**
(fetches a page in real time to answer one user request — RAG pipelines,
browser-use chat tools, i.e. *exactly FreeGem's `ai-search`/research
profile*), and **Training** (harvesting for model training) (same Anakin
source). The stated rationale is economic: Imperva's 2026 Bad Bot Report
puts automated traffic at **>53% of all web traffic** (up from 51%), and
Fastly's network data shows AI requests grew **~30% between January and May
2026 — about 6.5× the growth rate of human traffic — with >51% of AI
requests requiring an origin trip versus <9% for human requests** (same
Anakin source, citing Imperva and Fastly). An agent fetch that can't be
cached, multiplied across that curve, is an infrastructure cost problem,
and the default is aimed at that pattern.

Practically: **a domain that worked for a research agent last month can
return an empty/blocked response this month with identical code** — the
block page is a deliberate policy response, not a TLS or JS challenge. The
ladder must learn to tell those apart (see Recommendations R1).

### 3. Turnstile and hCaptcha: invisible, adaptive, session-aware

Turnstile's trajectory (risk-based managed challenge → Precursor's
continuous session scoring, §1) is documented in the Cloudflare launch post
itself. On the hCaptcha side, their 2026 positioning leans on behavioral
machine learning and adaptive invisible challenges: canvas, WebGL, audio
context and micro-timing probing happen without user friction, and the
vendor argues that improved vision-language models have not closed the gap
because micro-behavioral and human-perception differences still expose
automated agents ([hCaptcha blog, 2026](https://www.hcaptcha.com/blog/hcaptcha-captchas-are-highly-effective-against-bots-and-agents-in-2026)
*(search-summary)* — fetched directly, the page returned a bot-challenge
page rather than the article; the irony was productive). Community-side
guides written this year still rank managed challenges (Cloudflare,
Akamai, DataDome-class) as the layer that plain TLS impersonation can
*reach but not pass*: you get the interstitial, not the content
([Apify 2026 guide](https://use-apify.com/blog/how-to-bypass-cloudflare-web-scraping),
[ScrapeBadger 2026 guide](https://scrapebadger.com/blog/how-to-bypass-cloudflare-anti-bot-protection-a-complete-technical-guide-2026),
[RoundProxies 2026](https://roundproxies.com/blog/bypass-bot-detection/)
*(search-summary)*).

### 4. TLS-fingerprint detection: still the baseline; the arms race is now about *which Chrome*

TLS/HTTP2 fingerprinting (JA3/JA4 plus HTTP/2 settings frames) remains the
cheapest high-coverage wall: Python's `ssl` stack never matches any real
browser, and headers cannot patch a hello
([ScrapeWise on curl_cffi](https://scrapewise.ai/blogs/curl-cffi-tls-fingerprint-web-scraping-2026) *(search-summary)*;
[2026 community consensus via RoundProxies](https://roundproxies.com/blog/bypass-bot-detection/) *(search-summary)*).
What changed in 2026 is the *resolution* of the arms race: defenders score
specific browser versions, so impersonators must ship new targets on
Chrome's release cadence. **curl_cffi v0.16.3 (September 2, 2026) added
support for the TLS `trust_anchors` extension as shipped by Chrome 152**,
usable via the `chrome152` target, and fixed `tls_signed_cert_timestamps`
not being applied ([curl_cffi releases](https://github.com/lexiforest/curl_cffi/releases)).
The follow-up beta v0.16.4b1 landed September 20, 2026 (same source). A
FreeGem still saying `impersonate="chrome"` with an old pinned wheel is
visibly a *past version* of Chrome to any origin that tracks current
distributions.

A second transport development: **HTTP/3 (QUIC) fingerprinting is now
shipped in production impersonation stacks.** Apify's `impit` — the Rust
browser-impersonation library built on reqwest/rustls/tokio that now backs
Crawlee's `impit-client` — supports HTTP/1.1, HTTP/2 and HTTP/3, exposes
~20 fingerprint profiles, and publishes Python and Node bindings
([impit repo](https://github.com/apify/impit)). Its own October 1, 2026
benchmark measures **3,047 req/s for impit vs 2,450 for curl_cffi** on a
warm-connection HTTP/2 loop, in a 5.2 MB wheel versus 13.5 MB (same source;
reproduce on your own hardware before believing any leaderboard).

### 5. Headless-browser detection: `navigator.webdriver` is dead; the binary and the GPU are the tells

The 2026 detector stack is layered, and the old flags are at the bottom of
it:

- `navigator.webdriver` and obvious API gaps (empty plugin arrays, missing
  `window.chrome`) are **trivial legacy signals** — nearly every automation
  framework and stealth plugin patches them, and detectors that rely on them
  alone "catch less than 5% of modern traffic"
  ([Maskbreak, 2026](https://maskbreak.com/blog/headless-browser-detection-2026) *(search-summary)*).
- **The binary matters.** Playwright's default headless path uses
  `chrome-headless-shell`, a lighter build with different rendering paths;
  detectors compare font rasterization, subpixel text positioning and canvas
  output against full consumer Chrome, and software renderers (SwiftShader)
  expose themselves through `WEBGL_debug_renderer_info` and calibration-scene
  deltas ([cside](https://cside.com/blog/headless-browser-detection) *(search-summary)*,
  [Maskbreak](https://maskbreak.com/blog/headless-browser-detection-2026) *(search-summary)*).
- **CDP leaves timing traces**: `performance.now()` and
  `requestAnimationFrame` cadences driven without a real hardware compositor
  produce statistical footprints ([cside](https://cside.com/blog/headless-browser-detection) *(search-summary)*,
  [AlterLab on Playwright 2026](https://alterlab.io/blog/playwright-bot-detection-bypass-what-actually-works-in-2026) *(search-summary)*).
- **Behavioral entropy** — input-event jitter, typing dwell times, scroll
  distributions, dwell-before-action — is the most robust layer, and the
  one Precursor (§1) industrializes ([Maskbreak](https://maskbreak.com/blog/headless-browser-detection-2026) *(search-summary)*).

For FreeGem this cuts both ways: our `browser-render` rung's
`chromium.launch(headless=True)` (playwright_fetcher.py) is exactly the
headless-shell profile the detectors profile, which explains renders that
"work locally, get 403 in production". The fix on this layer is **not
stealth** — it is running the full browser binary, a configuration choice
any human's Playwright script makes (Recommendation R4).

### 6. Fetching the antibot literature is now itself a battleground

A small but honest datapoint from this survey: `hcaptcha.com`'s 2026 blog
post about resisting bots and agents **served our scraper a bot-challenge
page** on every read path (direct, browser-headers, reader), and
`archive.org` rate-limited us (HTTP 429) while trying to rescue it. The
antibot industry now defends its own marketing pages with the same stack it
sells. Search-summary sources had to substitute for the primary page for
that claim.

### 7. The standards layer: preference signals, bot auth, and HTTP 402

While CDNs enforce, standards bodies are writing the *consent and
compensation* layer. The IETF chartered an **AIPREF (AI Preferences) working
group** to standardize machine-readable AI-use preferences beyond binary
robots.txt; Cloudflare and others ship **`Content-Signal`-style headers**
granularizing use into `ai-train`, `search`, and levels like *immediate*,
*reference*, *full*; and **Web Bot Auth**, `terms.txt`, and HTTP 402
pay-per-crawl negotiation are advancing to make robots.txt-class signals
cryptographically enforceable
([AICrawlerCheck robots.txt guide](https://aicrawlercheck.com/blog/robots-txt-best-practices-ai-seo) *(search-summary)*,
[Content-Signal explainer](https://changegamer.ai/resources/content-signals-explained) *(search-summary)*,
[AI crawler list 2026](https://alexturik.com/blog/ai-crawler-list-2026) *(search-summary)*,
and the research paper [*A Consent and Compensation Protocol for Agentic
Web Access*](https://arxiv.org/html/2609.11152v1)). Cloudflare's own Pay
Per Crawl program sits at the same intersection
([Anakin, Oct 2026](https://anakin.io/blog/cloudflare-ai-agent-blocking-default-2026)).
FreeGem's declared position — robots not consulted by default, published
pages only, no credential bypass — survives this layer intact, but an
honest client should at least *see* the signals and treat a paid/402
response as the hard stop it is (Recommendation R6).

---

## What The Ecosystem Shipped

Release data below was read from each project's GitHub releases page (or
repository front page) on 2026-10-20; dates are as published there.

### Crawlee (Apify) — v4.0.0-rc.1, Oct 6, 2026

Crawlee is the most active tracker of Cloudflare challenge *markup drift*.
v3.18.1 (Aug 12, 2026) shipped "playwright: update
`handleCloudflareChallenge` for new Cloudflare challenge markup (#4019)";
v4.0.0-rc.1 (Oct 6, 2026) includes another "Cloudflare challenge detection"
fix (#4170) plus "a robots.txt 404 now means allow-all (#4171)",
OpenTelemetry instrumentation via `@crawlee/otel`, a pluggable-parser
DomCrawler, and restructured internals (crawler code moved from
`@crawlee/core` to `@crawlee/basic`); v3.18.0 (Aug 4) wired session-cookie
parity into the `impit-client` redirect handler (#3938)
([releases](https://github.com/apify/crawlee/releases)). **Two challenge
markup fixes in one quarter is the empirical proof that marker-based
challenge detection decays** — the same decay risk applies to FreeGem's
`_CHALLENGE_RE`.

### curl_cffi (lexiforest) — v0.16.3, Sep 2, 2026

Chrome 152 `trust_anchors` support; `tls_signed_cert_timestamps` fix;
`chrome152` target (preset "to be added later"); v0.16.2 (Aug 25) added
Android wheels and CPython 3.14; v0.16.4b1 followed Sep 20
([releases](https://github.com/lexiforest/curl_cffi/releases)).
FreeGem's `tls-browser` rung rides this library
(`tls_fetcher.py`, `impersonate="chrome"`), so our TLS ceiling moves only
when our dependency floor moves (Recommendation R3).

### Camoufox — v158.0-beta.40, Oct 19, 2026

The anti-detect Firefox build moved to a twice-monthly release train and a
browser/python/typescript monorepo. Notable shipping items: `headless =
"virtual-gpu"` (render on a named GPU with no monitor — the direct answer
to SwiftShader tells), hiding the Juggler CDP extension from pages while
presenting as an official Firefox build, GeoIP AIO as default source,
IPv4/IPv6 geo-consistency at launch, vendored Cursory 2.1.0 humanization,
first-launch paired-browser install, and a published npm package
([releases](https://github.com/daijro/camoufox/releases)). Camoufox is a
stealth product; FreeGem's NON_GOALS bar it from the default path. We log
it as a watch item (R4's "not this" note), because *virtual-GPU headless
rendering* is the one idea here that is a correctness fix, not evasion.

### nodriver — no tagged releases

`ultrafunkamsterdam/nodriver` has **no GitHub releases at all** ("There
aren't any releases here" — [releases page](https://github.com/ultrafunkamsterdam/nodriver/releases),
fetched 2026-10-20); it advances by commits only. That is a supply-chain
posture worth noting: undetectable-Chrome tooling with no release tags is
harder to pin, audit, or vendor responsibly — a reason FreeGem keeps its
browser layer on Playwright/Selenium, which have real releases.

### Firecrawl — v2.11.0, Jun 19, 2026

Headline items for our class of tool: **`deterministicJson`** — structured
JSON without an LLM per request; Firecrawl "generates a reusable extractor
for your schema and caches it per site, so repeat scrapes are cheaper and
return consistent results"; **`cdpUrl`** — a CDP WebSocket URL attached to
browser-session responses so callers can drive Firecrawl's live browser
with Playwright/Puppeteer; a Research Index over 3M arXiv papers plus the
GitHub code behind them (issues, merged PRs, READMEs, daily refresh) with
claimed +18% arXivQA recall; `redactPII`; keyless access for official
clients on core endpoints; monitor improvements (field-level JSON diffs,
render-settled checks, LLM "goal" alert triage)
([releases](https://github.com/firecrawl/firecrawl/releases)).
Firecrawl remains the benchmark competitor in FreeGem's own `bench1000_*`
scripts; its extractor-caching and CDP-attach patterns are the two ideas
worth stealing (R2, R7).

### Browser Use — 0.13.13, Oct 15, 2026

The open-source agent-browser project is shipping hard on *integration*
rather than stealth: 0.13.11 (Oct 7) added Claude toolsets
(`browser_use.integrations.toolsets_for_claude`, pending an Anthropic SDK
with `anthropic.tools.browser`); 0.13.12/0.13.13 (Oct 15) added an OpenAI
browser-tool driver with 18 default browser tools via the "Browser Use
Actor", supporting local browsers, Browser Use Cloud, and *borrowed*
browser sessions (an existing connected `BrowserSession` you don't own)
([releases](https://github.com/browser-use/browser-use/releases)).
Two takeaways: (a) LLM vendors are standardizing *browser tool* surfaces,
which grows the traffic Cloudflare now classifies as "Agent" (§2) — a
rising tide against our own research use case; (b) the "borrowed session"
concept validates FreeGem's existing `session_profiles.py` storage-state
model and suggests a CDP-attach variant (R7).

### spider (spider-rs) — v2.52.2, Mar 31, 2026

The Rust crawler's tag changelog since v1.7.5 includes: **`wreq`**
TLS-impersonation plumbing (network interface binding included),
**AdaptiveSemaphore** runtime concurrency hooks, a **RemoteFetcher** hook
for delegated per-URL fetch, `chrome_intercept` policies, markdown-by-
default CLI scraping, embedded Python/JS scripting, bounded MCP sessions,
and robots-block callbacks (`on_link_blocked_callback`)
([releases](https://github.com/spider-rs/spider/releases)).
The RemoteFetcher hook is the same architectural move as FreeGem's ladder:
keep the crawler, delegate the hard fetch.

### impit (Apify) — active, 616★, 556 commits

Rust browser-impersonation HTTP client on reqwest/rustls/tokio with
HTTP/1.1/2/**3**, a fingerprint database (e.g. `firefox_144`), cookie jar,
proxies, and Python + Node bindings. Benchmarks dated 2026-10-01 place it
at 3,047 req/s (Python API) versus curl_cffi's 2,450, with far smaller
wheels ([repo](https://github.com/apify/impit)). It is already inside our
competitor's stack via Crawlee's `impit-client` (#3938 above), and it is
the first production-grade HTTP/3-impersonation option we can point a
Python process at without writing QUIC ourselves (R8).

---

## Techniques Newly Viable For FreeGem

"Viable" here means three things at once: the technique is shipping in a
maintained open-source project as of October 2026; it does not cross
FreeGem's NON_GOALS line (no CAPTCHA solving, no stealth-by-default, no
identity rotation, honest labeling); and it maps onto a subsystem FreeGem
already has, rather than requiring a new product surface.

1. **Challenge-vs-policy classification.** The wall now has two distinct
   faces: a *technical* interstitial (Turnstile/managed challenge) and a
   *policy* refusal (Cloudflare's Agent/Training block on ad pages, §2).
   Crawlee's two challenge-markup fixes this year prove marker sets rot;
   Cloudflare's block pages are a different, stabler species. Classifying
   the second species early lets the ladder skip rungs that cannot succeed
   (TLS and browser rungs do not open a policy block) and jump to
   `wayback`/`ai-search` — which already exist and already label
   themselves. Viable now; purely a detection + routing change in
   `smart_fetch.py`.

2. **Per-site cached extractors (deterministicJson pattern).** Firecrawl
   proves the demand: generate the schema→JSON extractor once per site,
   cache it, skip the LLM on repeats. FreeGem's `llm_extract.py` pays a
   full model call per URL today. A host-keyed extractor cache is a pure
   economics win for `research`/crawl over large same-domain sets — no
   wall implications at all.

3. **Full-binary browser rendering.** Playwright's default headless path
   (headless-shell + software GL) is a detectable configuration; launching
   the full Chromium (or the `headless=new` full-mode path) is what a human
   running Playwright does anyway. This is a *correctness* fix for
   `browser-render`, not evasion, and sits comfortably under the existing
   no-stealth line.

4. **Named, current TLS targets with a pinned floor.** `impersonate="chrome"`
   floats; origin-side scoring is version-aware; curl_cffi ≥0.16.3 knows
   Chrome 152's `trust_anchors`. Pinning a named target (e.g.
   `chrome136`/`chrome152` as available) and asserting a minimum curl_cffi
   version makes our opt-in `tls-browser` rung legible in logs and stops
   silent staleness.

5. **Strategy-memory TTLs.** Precursor (§1) means a clearance/cookie that
   won yesterday's fetch may be scored down today, mid-session. FreeGem's
   `StrategyMemory` already re-ranks the ladder per host; giving remembered
   wins an expiry (and treating a re-challenge as invalidation) converts
   Precursor from a silent failure into a measured re-climb.

6. **Content-Signal / 402 awareness in the crawler path.** Parsing
   `Content-Signal`-style headers and treating HTTP 402 as a hard stop
   (rather than a generic 5xx retry) is standards-convergent (§7) and
   requires no new network behavior — only recognition and honest
   reporting in receipts/metrics.

7. **CDP-attach to a user-owned browser.** Firecrawl's `cdpUrl` and Browser
   Use's "borrowed sessions" both productize connecting to a real browser
   the user already runs. FreeGem already persists cookies/storage state
   via `session_profiles.py`; an attach-CDP mode is the natural extension
   for authenticated reads *without* driving a fresh automation profile —
   the user's own session, their own consent, our SSRF guard still applied
   to what we extract.

8. **HTTP/3 impersonation (watch item).** impit ships H3 fingerprints
   today; curl_cffi does not yet foreground H3 presets. The protocol
   difference (QUIC/UDP, different anti-replay and ECH surface) may or may
   not move pass rates on any given host — but it is measurable with our
   own `bench1000_*` harness before any code decision.

Deliberately **not** on this list: camoufox fingerprint injection, nodriver
CDP-free stealth, behavioral humanization (Cursory), and CAPTCHA-solving
services. They work in their own lanes; they cross FreeGem's stated line;
and the 2026 wall (Precursor-style session scoring) means half-measures buy
little while costing the honesty the product is built on. If the line ever
moves, camoufox's `virtual-gpu` headless and impit's speed are the two
shipments worth revisiting first.

---

## Concrete Recommendations

Prioritized P1 → P4. Each maps to a named FreeGem subsystem and file.
Effort is a coarse engineering estimate; no estimate implies a deadline.

### R1 (P1) — Classify policy blocks separately from challenges
**Subsystem:** `smart_fetch.py` (`_looks_like_challenge`,
`classify_failure`, `_strategy_order`).
Extend `_CHALLENGE_RE` *or* add a sibling classifier for Cloudflare
AI-agent/policy block pages (e.g. "sorry, you have been blocked", "you
have been blocked", "unable to ... Cloudflare" block layouts), grounded in
the response shapes documented by Cloudflare-adjacent write-ups
([Anakin](https://anakin.io/blog/cloudflare-ai-agent-blocking-default-2026)).
When a host's failure classifies as a policy block, **down-rank rungs that
cannot succeed** (`tls-browser`, `browser-render` — a real browser with a
real fingerprint is still Agent traffic) and promote `wayback` and
`ai-search`, which are labeled rescues rather than smuggled passes. Record
the class in the fetch taxonomy so bench output shows *why* a rung was
skipped. Grounding: Crawlee's repeated challenge-detection fixes
([#4019, #4170](https://github.com/apify/crawlee/releases)) show marker
sets rot; the policy class is new as of Sep 15, 2026 and currently
indistinguishable from a CAPTCHA wall in our taxonomy.

### R2 (P2) — Per-site cached extractors in `llm_extract`
**Subsystem:** `llm_extract.py` (+ `stores.py` if a cache home is needed).
Adopt Firecrawl's `deterministicJson` economics: after a successful
schema-constrained extraction for host *H*, cache the *procedure* (prompt
pattern / validated output schema, not the content) keyed by host+schema
hash; on repeats, try the cached procedure first and fall back to a full
model call on parse failure. Firecrawl's claim — "repeat scrapes are
cheaper and return consistent results"
([releases](https://github.com/firecrawl/firecrawl/releases)) — is directly
testable with our `bench1000_*` harness before/after.

### R3 (P1) — Pin and floor the TLS impersonation target
**Subsystem:** `tls_fetcher.py`.
Change `impersonate="chrome"` (line 102) to a named, current target and
fail soft to the previous one; document the minimum curl_cffi version
(`>=0.16.3` for Chrome 152 `trust_anchors`, per
[curl_cffi releases](https://github.com/lexiforest/curl_cffi/releases)) in
the optional-extra docs. Keep the existing invariant — **no header
overrides**; a Chrome hello with Python-flavoured headers is itself a
signal. This is the cheapest way to stop our opt-in rung from aging into a
detectable "old Chrome".

### R4 (P2) — Launch full Chromium on the browser-render rung
**Subsystem:** `playwright_fetcher.py` (`chromium.launch(headless=True)`,
line 118).
Move to the full-browser headless mode (Playwright's modern headless /
non-shell path) and, where cheap, prefer the installed full Chromium
channel. This addresses font/canvas/WebGL divergence from
`chrome-headless-shell` documented in 2026 detector write-ups
([cside](https://cside.com/blog/headless-browser-detection),
[Maskbreak](https://maskbreak.com/blog/headless-browser-detection-2026) *(both search-summary)*).
Explicit non-goal reminder in the docstring: this is *not* camoufox-style
fingerprint injection and must not become one.

### R5 (P1) — TTL + invalidation for StrategyMemory
**Subsystem:** `smart_fetch.py` (`StrategyMemory`, `fallback_scrape`).
Give remembered per-host wins a TTL (start at 30 minutes — the order of
magnitude of `cf_clearance` lifetimes) and immediately invalidate a host's
memory when a previously-winning rung now returns a challenge or policy
block. Grounding: Precursor scores behavior *through* the session
([Cloudflare](https://blog.cloudflare.com/introducing-precursor/)), so
cookie-class wins are no longer sticky; without a TTL the ladder silently
repeats a failing strategy.

### R6 (P3) — See Content-Signal; stop hard on 402
**Subsystem:** `web_scraper.py` / `crawler/receipts.py` (reporting only);
`classify_failure` in `smart_fetch.py` for the 402 class.
Parse `Content-Signal`-style response headers and surface them in scrape
metadata; treat HTTP 402 as a deliberate commercial refusal (hard stop,
never a retry-the-ladder trigger). Grounding: standards convergence on
AIPREF / Content-Signal / pay-per-crawl
([arXiv:2609.11152](https://arxiv.org/html/2609.11152v1),
[Anakin](https://anakin.io/blog/cloudflare-ai-agent-blocking-default-2026)).
Note: FreeGem's robots-not-consulted-by-default stance is unchanged; this
is about *response headers and status codes on fetches we already make*,
and about honesty in receipts.

### R7 (P3) — CDP-attach mode for user-owned browsers
**Subsystem:** `session_profiles.py` + `playwright_fetcher.py`.
Add an optional attach path: given a running browser's CDP endpoint (the
`cdpUrl` pattern Firecrawl ships,
[releases](https://github.com/firecrawl/firecrawl/releases); the "borrowed
session" concept in Browser Use 0.13.13,
[releases](https://github.com/browser-use/browser-use/releases)), open a
page in the *user's* existing profile and read with the existing SSRF
guard (`browser_guard.py`). Consent and identity stay with the user; no
new stealth surface; authenticated-site reads stop requiring cookie-file
plumbing for users who already have a browser open.

### R8 (P4) — Benchmark HTTP/3 / impit before any backend swap
**Subsystem:** `tls_fetcher.py` / `parallel_scraper.py` (measurement only
at this stage).
Add an opt-in bench mode comparing current curl_cffi targets against
`impit` (Python bindings, H3 on/off) on the existing `bench1000_*` seeds.
Grounding: impit's published numbers ([repo](https://github.com/apify/impit))
are on their hardware; Crawlee already runs impit-client in production
([releases](https://github.com/apify/crawlee/releases)). Ship a swap only
if our own numbers show a pass-rate or latency win; the stdlib guard and
no-header-override invariants must hold for whichever backend wins.

### What we are still not doing (and why)
- **No CAPTCHA/Challenge solvers** — non-goals; the 2026 literature's
  attacker-side breakthroughs ([the CAPTCHA cluster summarized in
  ANTIBOT_RESEARCH.md](ANTIBOT_RESEARCH.md)) are exactly the capability we
  refuse to ship.
- **No camoufox/nodriver stealth backends** — they cross the line; the
  actionable subset of their value (full-binary rendering, virtual-GPU
  correctness) is captured by R4.
- **No IP/identity rotation** — residential proxies remain a user-supplied
  `cfg.proxy` pass-through, unchanged.

---

## Sources

Primary pages fetched 2026-10-20 unless marked *(search-summary)*, in which
case a web-search answer cited the page and we could not fetch it verbatim.

1. Cloudflare Blog — *Introducing Precursor: detecting agentic behavior
   with continuous client-side signals* (Jul 13, 2026).
   https://blog.cloudflare.com/introducing-precursor/
2. Anakin.io — *Cloudflare Blocks AI Agents by Default: What Builders Need
   to Know* (Oct 7, 2026).
   https://anakin.io/blog/cloudflare-ai-agent-blocking-default-2026
3. Hosting.com — *Cloudflare's New AI Crawler Defaults and What They Mean
   for Site Owners* *(search-summary)*.
   https://hosting.com/learn/cloudflares-new-ai-crawler-defaults-and-what-they-mean-for-site-owners/
4. hCaptcha Blog — *hCaptcha CAPTCHAs: Highly Effective Against Bots and
   Agents in 2026* *(search-summary; direct fetch hit a bot-challenge page)*.
   https://www.hcaptcha.com/blog/hcaptcha-captchas-are-highly-effective-against-bots-and-agents-in-2026
5. GitHub — apify/crawlee releases (v4.0.0-rc.1, v3.18.2, v3.18.1,
   v3.18.0). https://github.com/apify/crawlee/releases
6. GitHub — lexiforest/curl_cffi releases (v0.16.3, v0.16.2, v0.16.4b1).
   https://github.com/lexiforest/curl_cffi/releases
7. GitHub — daijro/camoufox releases (v158.0-beta.40).
   https://github.com/daijro/camoufox/releases
8. GitHub — ultrafunkamsterdam/nodriver releases (empty: no tagged
   releases). https://github.com/ultrafunkamsterdam/nodriver/releases
9. GitHub — firecrawl/firecrawl releases (v2.11.0).
   https://github.com/firecrawl/firecrawl/releases
10. GitHub — browser-use/browser-use releases (0.13.11–0.13.13).
    https://github.com/browser-use/browser-use/releases
11. GitHub — spider-rs/spider releases (v2.52.2).
    https://github.com/spider-rs/spider/releases
12. GitHub — apify/impit (browser impersonation, HTTP/3, benchmarks
    dated 2026-10-01). https://github.com/apify/impit
13. Maskbreak — *Headless Browser Detection in 2026: What Still Works*
    *(search-summary)*. https://maskbreak.com/blog/headless-browser-detection-2026
14. cside — *Headless Browser Detection: Signals, Methods, and What Works
    in 2026* *(search-summary)*. https://cside.com/blog/headless-browser-detection
15. AlterLab — *Playwright Bot Detection Bypass: What Works in 2026*
    *(search-summary)*.
    https://alterlab.io/blog/playwright-bot-detection-bypass-what-actually-works-in-2026
16. RoundProxies — *How to bypass bot detection in 2026*
    *(search-summary)*. https://roundproxies.com/blog/bypass-bot-detection/
17. ScrapeBadger — *How to Bypass Cloudflare Anti-Bot Protection (2026
    Guide)* *(search-summary)*.
    https://scrapebadger.com/blog/how-to-bypass-cloudflare-anti-bot-protection-a-complete-technical-guide-2026
18. Apify — *How to Bypass Cloudflare When Web Scraping (2026)*
    *(search-summary)*. https://use-apify.com/blog/how-to-bypass-cloudflare-web-scraping
19. ScrapeWise — *curl_cffi and TLS Fingerprint Impersonation*
    *(search-summary)*.
    https://scrapewise.ai/blogs/curl-cffi-tls-fingerprint-web-scraping-2026
20. AICrawlerCheck — *Robots.txt Best Practices for AI SEO in 2026*
    *(search-summary)*. https://aicrawlercheck.com/blog/robots-txt-best-practices-ai-seo
21. ChangeGamer — *Content Signals Explained* *(search-summary)*.
    https://changegamer.ai/resources/content-signals-explained
22. Alex Turik — *AI Crawler List 2026* *(search-summary)*.
    https://alexturik.com/blog/ai-crawler-list-2026
23. arXiv:2609.11152 — *A Consent and Compensation Protocol for Agentic
    Web Access* *(search-summary of abstract)*.
    https://arxiv.org/html/2609.11152v1

Internal context consulted (not modified): `docs/ANTIBOT_RESEARCH.md`,
`docs/ARCHITECTURE.md`, `docs/P3_ECOSYSTEM.md`, `freegem/smart_fetch.py`,
`freegem/tls_fetcher.py`, `freegem/playwright_fetcher.py`,
`freegem/session_profiles.py`, `freegem/web_scraper.py`,
`freegem/browser_guard.py`, `freegem/llm_extract.py`.

