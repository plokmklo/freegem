# Non-goals and non-guarantees

FreeGem reads the live web on a best-effort basis and tells you the truth
about what it got. This page states, in one place, the things it does **not**
promise. These are deliberate scope boundaries and design stances — not bugs,
not roadmap gaps waiting for a volunteer.

The machine-readable versions of this list live in the service itself:

- `GET /health` — short labels under `non_guarantees`.
- `GET /v1/about` — full label / assumption / reality detail.
- Source of truth: `freegem/non_goals.py` (`NON_GOALS`).

---

## 1. No guaranteed scraping SLA

**The assumption:** every URL succeeds, with complete, fresh, accurate
content, on any site, every time.

**The reality:** scraping success, availability, completeness, freshness,
and accuracy on arbitrary sites are best-effort. Sites go down, move
content, truncate responses, serve broken markup, and change without
notice. FreeGem's jobs and tools report what they actually received —
including partial content and hard failures with their status codes — so
your code can react to reality instead of an assumption. But nothing about
an arbitrary third-party site is guaranteed, by FreeGem or by anyone.

## 2. No CAPTCHA solving, identity rotation, or stealth by default

**The assumption:** a crawler that slips past CAPTCHAs, bot walls, and
bot-detection fingerprints out of the box.

**The reality:** FreeGem fetches politely by default — honest user agent,
per-host rate limits, no CAPTCHA solving, no IP rotation, no credential
bypass. Two opt-in read paths go further, but only when you switch them
on: a **tls-browser** rung (Chrome's TLS fingerprint through curl_cffi,
config `smart_fetch_tls_browser`) and a **flaresolverr** adapter that
talks to a solver service you run yourself (config
`smart_fetch_flaresolverr`). Neither is enabled by default, neither
answers CAPTCHAs, and a site that still refuses is reported as refused —
honestly, with the full ladder taxonomy.

## 3. No free proxy pool or managed browser fleet

**The assumption:** bundled rotating proxies or a hosted browser cluster
that spreads requests across many addresses.

**The reality:** requests originate from your machine and your IPs. There
is no proxy pool, no managed browser fleet, no bundled egress
infrastructure. If your use case needs proxies, you choose, configure, and
pay for them yourself.

## 4. No proprietary web index or built-in SERP

**The assumption:** a built-in search engine over the crawled web.

**The reality:** FreeGem reads pages; it does not ship a web index or a
SERP service. It ships the building blocks instead — the crawler, simhash
deduplication, and search-index sinks (SQLite FTS5 with a LIKE fallback,
JSONL tf-idf) — so you can implement and own your own index without
starting from zero.

## 5. No unlimited crawling or unlimited autoscaling

**The assumption:** crawl forever, at any concurrency, scaling without
bounds.

**The reality:** concurrency limits, queue depth, request/response body
caps, and per-host politeness delays are deliberate guardrails. They
protect your machine from OOM and the target sites from unintentional
denial of service. They are configurable, not removable. Scaling out means
running more workers on infrastructure you provide.

## 6. No universal JavaScript, login, WebSocket, CAPTCHA, PDF, DOCX, or media support

**The assumption:** one tool that renders, logs into, and parses
everything.

**The reality:** adapters cover common cases, not all of them. Playwright
and Selenium render JavaScript on a best-effort basis; session profiles
carry cookies and storage state that you supply and control; PDF/DOCX
adapters parse common documents with stdlib fallbacks; source adapters
handle MediaWiki, arXiv, feeds, and documentation sites. WebSockets,
CAPTCHA flows, interactive logins, and exotic media formats are out of
scope.

## 7. No deterministic or always-correct LLM extraction

**The assumption:** AI extraction that is right every time.

**The reality:** LLM extraction and summarization are probabilistic.
Schema-constrained prompts, validation with repair, and complaint-driven
retries improve quality; they do not make the output deterministic or
always correct. Spot-check results before your pipeline depends on them.

## 8. No hosted queues, databases, dashboards, proxies, or billing-free infrastructure

**The assumption:** a managed service with free hosting included.

**The reality:** FreeGem is a local tool. Redis/RabbitMQ queues,
PostgreSQL document stores, S3/object storage, dashboards, and proxies are
adapters you point at infrastructure you run and pay for. Nothing hosted,
nothing billed, nothing bundled.
