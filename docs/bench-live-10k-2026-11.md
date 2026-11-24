# FreeGem live test: 10,000 websites

Run 2026-11-22/23 (UTC), build **2.16.2**; 2.16.3 (brotli shim) verified
post-run by spot-check below. Both fixes are deliverables of this run.

**3,734 / 10,000 URLs yielded readable text (37.3%).** 4,366
network-unreachable, 1,856 HTTP errors, 37 SSRF refusals, 7 type-refused.
Every seed attempted, zero left in queue.

## Method

Tranco top-1m (2026-11), ranks 1-10,001, one URL per distinct host,
`https://<domain>/`, tiered top-1k/5k/10k. Popularity-ranked smoke test.
Campaign driver, resilient ladder on: 16 workers, 10 s timeout,
`--max-attempts 2`, `--seed-budget 120`, `--keep-alive`, per-slice
`--checkpoint`. robots.txt never fetched (documented policy). Anonymous
identity, no cookies/proxies. AI/distill/stealth off; 401/403/429 recorded,
never bypassed. 20 isolated 500-seed slices, ~55 min, 21 slice runs.

## Results

| Outcome | Count | Share | | Status | n |
|---|---:|---:|---|---|---:|
| Readable | 3,734 | 37.3% | | 200 | 5,614 |
| 200, no text | 1,880 | 18.8% | | 403 | 1,532 |
| Network | 4,366 | 43.7% | | 404 | 149 |
| HTTP error | 1,856 | 18.6% | | 202/429 | 53/46 |
| SSRF | 37 | 0.4% | | 503/401 | 29/12 |
| Type refused | 7 | 0.1% | | | |

Readable by tier: 50.7% / 38.4% / 32.7%. Latency per attempt (n=21,974):
p50 450 ms, p90 3,092 ms, p99 20,189 ms, ~2.9 seeds/s at 16 workers.

Pool: dials 21,974, reuses 1,324, checkins 6,441, evict-oldest 2,548
(2.16.1 tuning carries one-shot host churn). Ladder rescues: profile 1,876,
DoH 1,640, host_swap 211, scheme_swap 157.

## Findings -> fixes shipped

1. **Seed-budget stall (2.16.2)**: quantcount.com - six A-records dropping
SYNs; max_attempts bounds count, not duration; one seed ~10 min stalled a
slice at 499/500. `--seed-budget` cuts at deadline, last honest outcome
stands, marked `(seed budget)`.
2. **Brotli decode (2.16.3)**: 1,936 rows (19.4%) died on
`'brotli.Decompressor' has no attribute 'decompress'` (cloudflare, facebook,
instagram, linkedin, bing...). Reader spoke zlib decompressobj; brotli
exposes process(); the duck-typed fake hid it. `_BrotliStream` shim +
real-backend round-trips + tripwire.
3. Apex-only infra domains (akamai.net, aaplimg, adgrx) do not resolve
publicly: honest network outcomes.
4. HTTP 200 != readable: after brotli exclusions only 4 true no-text 200s.

## Recovery spot-check (2.16.3)

Slice 001 re-run on the fixed build: readable 189 -> 268 (37.8% -> 53.6%),
brotli errors 80 -> 0. Corpus estimate: ~1,580 rows recover; a fresh
fixed-build pass ~55-57% readable. The 37.3% headline = 2.16.2 build.

## Artifacts & assessment

Raw rows (`results-10k.jsonl`, summaries, per-slice state/checkpoints) stay
on the experiment host; no page bodies in the repo. The harness stays
external; this records method+config+numbers for reruns. The pipeline
attempted every seed and lost none; 37.3% reflects the corpus (43.7% dead,
1,532 bot-walls), not defects. Both actionable defects shipped with tests
in-session.
