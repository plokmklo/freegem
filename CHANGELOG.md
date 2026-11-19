## [2.14.0] — 2026-11-19

The resume round: the campaign's crash-proof SQLite state finally gets the
contract it was missing — a digest-bound checkpoint manifest that makes an
interrupted run resumable *safely*, not just possible (roadmap item 2,
issue #93).

### Added

- **Campaign checkpoint manifest** (`freegem/crawler/checkpoint.py`):
  `campaign run --checkpoint PATH` writes a versioned
  `freegem-campaign-checkpoint/1.0` manifest at the end of every run —
  normal completion AND SIGINT — atomically (tmp + `os.replace`, so a
  crash mid-write leaves the previous manifest intact). The manifest binds
  the campaign identity: a sha-256 over the canonical seeds set
  (order-independent), a sha-256 over the frontier (`queue` as
  `seeds[:limit]` — a changed `--limit` is a different campaign), and the
  per-URL outcome rows `[kind, status]` with a digest self-check, plus the
  run parameters and the live `CampaignResult` counters.
- **`campaign run --resume MANIFEST`**: verifies the manifest FIRST —
  format, seeds digest, queue digest, self-integrity, and every recorded
  outcome against the live state — before any write, so a refused resume
  leaves the state file byte-identical. On success it fetches exactly the
  seeds without an `ok` outcome: attempted-and-failed AND never-attempted.
  The resume query is a queue LEFT JOIN on results, not a results-only
  scan: an interrupted seed carries no results row, and a results-only
  query would silently lose it. Resume implies unfinished-only, whatever
  `--retry-failed` says; the rewritten manifest records `resumed_from`
  (the inherited counters and interrupt state) and the report carries a
  `checkpoint` summary.
- **Lower-bound verification semantics**: outcomes ADDED to the state after
  the manifest was written are accepted — they are the progress a killed
  resume made without a manifest rewrite, and accepting them is what makes
  an interrupt *during* resume itself resumable. Outcomes recorded in the
  manifest that are missing, altered, or disagreeing in kind/status are
  hard refusals (`CheckpointError`), as are unknown manifest versions,
  corrupt files, and seeds/queue digest mismatches.
- **CLI refusal contract**: a refused resume prints one
  `{"error": "checkpoint_refused", "detail": ...}` JSON line and exits 2 —
  an operator error, not a traceback.

### Tests

- `freegem/tests/test_campaign_checkpoint.py` — 24 contract tests: manifest
  write contract (format, digests recomputed against the db, params,
  atomicity with no temp leftovers), SIGINT interrupt producing a
  recoverable manifest with the interrupted seed still queued, resume
  re-fetching exactly the failed+missing seeds and never the completed
  ones, the killed-resume progress (lower-bound) case, the refusal matrix
  (different seeds, tampered state, tampered queue, corrupt file, unknown
  format, missing manifest, fresh state, hand-edited rows), state-untouched
  on refusal, digest determinism/order-independence, and the CLI
  checkpoint→resume round trip with exit 2 on refusal.

## [2.13.0] — 2026-11-11

The second evidence round: the crawl's WARC proof becomes a standard,
replayable, self-verifying collection, and discovery gets an honest
scale lever. WACZ is what turns "we hold the bytes we quote" into
something anyone can open in ReplayWeb.page or Browsertrix and check
without trusting us — the digest chain and the CDX offsets are checked
against the bytes, by the bytes.

### Added

- **`crawl run --wacz PATH`** (`freegem/crawler/wacz.py`): when the crawl
  finishes, the WARC evidence is packaged as a WACZ 1.2.0 collection —
  `archive/data.warc` byte-identical (kept uncompressed so the CDX
  `offset`/`length` fields point at exact positions; the zip provides the
  compression), `index/index.cdx` with a CDXJ line for EVERY response
  record (wins and walls alike — a 403 is real evidence of what the
  server answered), `pages/pages.jsonl` with the spec's `json-pages-1.0`
  header plus one entry per successful (2xx/3xx) response, and the
  Frictionless `datapackage.json` + `datapackage-digest.json` sha-256
  chain binding it all. Requires `--warc`. `verify_wacz()` re-derives the
  whole thing without trusting the writer: digest chain, every resource
  hash and byte count, then a seek into the archive at each CDX offset to
  recompute the payload digest — tampering fails loudly.
- **`crawl run --bloom-seen`** (`freegem/crawler/bloom.py`): opt-in Bloom
  pre-filter in front of discovery inserts (links + sitemaps), the same
  trade Heritrix's `BloomUriUniqFilter` made — probably-seen URLs skip the
  frontier write at a documented false-positive rate instead of paying a
  SQLite round trip per repeated discovery. Stdlib-only blake2b
  double-hashing, design parameters reported in the crawl events
  (`bloom_enabled`/`bloom_stats`), default OFF so the exact crawl stays
  exact; the frontier on disk remains the authority.

### Fixed

- **MCP `web_research` accepts `query` as an alias for `question`** (#70,
  #71): the other research-style tools name the prompt parameter `query`,
  and a caller following that convention got a `ValueError` instead of
  research. Both names now work through the MCP schema and the `run_tool`
  dispatch, and a missing prompt names both accepted parameters in the
  error. The generated schema requires only `urls`.
- **`web_research`'s advertised description leads with the requirement**
  (#73): the description sent over `tools/list` now opens with "Research
  pages YOU provide — pass `urls` (2-10) and a `question` (alias: `query`);
  this tool does not search the web, use gemini_search to find pages
  first" instead of burying `urls` twelve lines into the docstring.
- **MediaWiki fallback honesty** (#67): the `prop=extracts` fallback keeps
  its deliberate budget-free request, now documented and regression-tested:
  the TextExtracts extension truncates an extract only when
  `exchars`/`exsentences` is requested and hard-caps `exchars` at 1200, so
  an explicit budget would CUT a long article's extract instead of bounding
  a bigger one. Upstream returns the complete plain text, freegem's own
  budgets cut after the full extraction, and `truncated=True` on a
  `mediawiki-api` result can only be freegem's own caller cut.

### Tests

- `test_wacz_round.py`: 28 contract tests — SURT vectors, the exact zip
  layout, the digest chain, pages-vs-index honesty (walls indexed, not
  paged), byte-identical archive copy, and the tamper matrix (stale
  resource hash, forged datapackage digest, shifted CDX offset, missing
  member) plus end-to-end WACZ through the real crawl loop.
- `test_version_consistency.py` (#65): runtime version, pyproject, and
  installed metadata must agree with the changelog, or CI fails.
- `test_web_research_contract.py` and `test_mediawiki_budget.py`: the
  issue-round contract tests — hermetic, every seam mocked.

## [2.12.0] — 2026-10-20

Two research waves landed under one version. The **research wave** turned a
documented sweep (r/webscraping threads, the FlareSolverr / curl_cffi /
camoufox / nodriver codebases, a 566-paper arXiv literature sweep —
docs/ANTIBOT_RESEARCH.md + docs/antibot_papers.md) into opt-in rungs for the
walls that survived 2.11.0. The **evidence wave** — grounded in a separate
450-project (123 scrapers / 122 fetchers / 150+ crawlers) + 59-paper survey —
made the crawl able to PROVE what it did: archive the bytes, cite the
offsets, hand over a politeness receipt. WARC from the archivists, tables
and chunks from the LLM-scrapers, receipts from nobody — that one is
freegem's own.

### Added

- **`tls-browser` rung** (optional `curl_cffi` dependency): the same URL
  fetched with Chrome's exact TLS/HTTP2 fingerprint — the client
  presenting browser headers IS the client whose hello the site
  fingerprints. No header overrides (a mismatched header/TLS pair is
  itself a detection signal), one cookie warm-up retry carrying the
  wall's own Set-Cookie, session discarded afterwards, `cfg.proxy`
  honored. OFF by default (`smart_fetch_tls_browser`); labeled
  `via="tls-browser"`; no CAPTCHA solving, no identity rotation.
- **`flaresolverr` rung**: an adapter for a FlareSolverr instance the
  USER runs — the community's standard self-hosted answer to managed
  challenge walls. FreeGem ships no solver and never starts one; when
  `smart_fetch_flaresolverr` holds an endpoint, the rung talks the v1
  API and labels every result `via="flaresolverr"`. Empty string (the
  default) = the rung does not exist.
- **Ladder order** is now browser-headers → tls-browser → reader →
  browser-render → flaresolverr → wayback → ai-search, with the three
  opt-in rungs dropped from the order entirely while their gates are
  closed; per-host strategy memory fronts a remembered rung only while
  its gate stays open.
- **Research documentation**: docs/ANTIBOT_RESEARCH.md (how sites stop
  agents, what passes, the ethics line, and what was shipped) and
  docs/antibot_papers.md (566-paper arXiv sweep appendix, generated by
  scripts/antibot_paper_sweep.py).
- **WARC 1.1 evidence capture** (`crawler/warc.py`, `crawl run --warc`):
  every fetch — wins, redirects, 304 revalidations, and 4xx/5xx walls —
  becomes request/response record pairs with sha-256 block+payload digests;
  fetches that never got an HTTP answer (DNS death, timeout, SSRF refusal)
  become metadata records carrying the outcome taxonomy; wall classes
  (`auth_wall`/`bot_check`/`rate_limited`), ladder fallbacks, and unverified
  TLS flags ride along as warc-fields. `warcinfo` discloses the honest
  robots stance inside the archive. `iter_records`/`verify`/`summary`
  re-check structure and digests on any producer's files. Stdlib-only.
- **GFM table extraction** (`tables.py` + `html_to_markdown`): `<table>`
  renders as a real markdown table (pipes escaped, ragged rows padded) and
  `extract_tables()` returns rectangular frames with `to_markdown()` /
  `to_csv()`. Spans are counted, never expanded — fabricated cells stay out.
- **`freegem map`** — one guarded fetch, then pure discovery: `<a href>`
  links + the page's own fetch()/XHR endpoints, ranked by the best-first
  scorer with `sources` provenance (`link` / `js_endpoint`). Firecrawl's
  `map` proved agents want URL discovery without content; freegem's is one
  fetch and a plan, not a mini-crawl.
- **`scrape --chunks N`** (with `--chunk-overlap`): RAG-ready chunk records
  (jsonl: `index`/`start_char`/`end_char`/`text`/`total`) so vector stores
  can cite `url#chunk-3` and verify quotes against the page's own offsets.
  Deterministic transform of the extracted text — no AI, no cache writes.
- **Politeness receipts** (`crawler/receipts.py`, `crawl report`): per-host
  evidence straight from the frontier — status histograms, wall counts,
  retry volume, observed inter-fetch intervals, delay-consistency verdicts
  (with tolerance), the exact UA, the robots stance, and a sha-256 digest
  over the canonical JSON. `frontier.mark_error` now records `http_status`
  so walls are data, not error-string guesses (legacy strings still parse).
- **Fetch hardening**: `Retry-After` honored in the HTTP-date form (RFC
  7231) as well as delay-seconds; optional `freegem[brotli]`/`[zstd]`
  extras add brotli/zstd decode, and `Accept-Encoding` advertises exactly
  what the cap-enforced streaming reader can decode — never more. An
  encoding we did not advertise is an honest failure, not raw bytes
  stored as text.

### Changed

- **NON_GOALS entry 2 reworded** to state the accurate boundary: no
  CAPTCHA solving, no identity rotation, no stealth *by default*; the
  two new rungs are explicit user choices. Same update applied to
  README "Limitations", docs/NON_GOALS.md, and the /health +
  /v1/about API surfaces.
- Live evidence from the campaign's hardest leftovers: TLS
  impersonation alone moved patreon.com from a bare 403 to a
  challenge page (solver territory) and investing.com (Akamai-class)
  stays 403 — both facts recorded in the research doc so expectations
  stay calibrated.

### Tests

- `test_evidence_round.py`: 37 contract tests — WARC round-trip, digest
  tamper detection, truncated-tail refusal, field-injection refusal,
  warcinfo propagation; table GFM/CSV rendering, span counting, markdown
  pipeline integration; receipts totals/walls/digest determinism/pacing
  verdicts; Retry-After both forms; codec honesty under faked and missing
  decoders; map scoring/dedup/sources; chunk CLI offsets; and two full
  crawler-through-HTTP end-to-end runs producing verifiable WARC + JSON
  receipts over a loopback site with a 403 wall.

## [2.11.0] — 2026-10-20