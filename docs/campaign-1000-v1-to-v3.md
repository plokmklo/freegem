e# FreeGem fetch campaign: 1,000 sites (v1 -> v3)

Era builds: 2.6.x (v1/v2 baseline) -> 2.7.0 v3 ladder. This record predates
the 2.14+ checkpoint work and explains how the campaign driver got its
shape: escalation ladder, cookie jar, DoH, wayback, honest taxonomy.

## Headline

| pass | readable | notes |
|---|---|---|
| v1 (2.6.0) | 559 / 1000 | robots honored-era default profile |
| v2 (fetch-for-readability) | 806 / 1000 | ladder + retry-after + origin-root, no robots gate |
| v3 (cookie/doH/wayback/safari, budget 9->16) | 812 / 1000 | +6 vs v2; conversions: tagesanzeiger x2 (cookie loop), sohu 5xx->root, standaard.be x2 + wordpress.org (profiles/429 windows); DoH/wayback cost visible as honest network rows under sandbox egress |

## v3 taxonomy (188 failures)

- hard_cdn_wall_403: 143 — IP-reputation walls; three browser profiles do not move them; recorded, never bypassed
- timeout_slow_origin: 23 (includes unreachable-archive probes)
- dead_path_404: 7 — path AND origin root dead
- auth_paywall_legal: 5 — 401/402/451 buckets
- rate_limited_429: 3 — window never opened for this IP
- network_egress_or_dead: 3, other (522/406/405, dead DNS, TLS): 7

## What the ladder earned (policy, unchanged since)

- No robots.txt fetch/consult (attacker-controlled text; prompt-injection vector). Opt-in only.
- No CAPTCHA solving, no identity rotation, no wall cracking, no paywall bypass. 401/403/429/451 are recorded refusals.
- Per-origin budget: cookies ride only from origin-granted responses; keep-alive pool bounded; seed-budget caps pathological walks (2.16.2).

## Follow-ons this record drove

- Windows CI reliability arc: idle-TTL clock-tick fix (>=), pooled-https MRO pass-through, evict-oldest tuning -> folded into the 2.14-2.16.1 segment (main, da2b6b6).
- 10k corpus campaigns: docs/bench-live-10k-2026-11.md, docs/bench-repass-2163-2026-11-24.md.
- Staging kits for #86/#89/#102: branch live-ship-trigger.
