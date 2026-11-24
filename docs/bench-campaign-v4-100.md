# FreeGem campaign v4: 100 websites, all features (freegem 2.9.0-era)

Two-stage live validation of the full feature surface against 100 distinct
hosts plus an end-to-end job-API pass against a real server process.

## Stage 1 — per-feature results (100 distinct hosts)

| Feature | Passed | Ran | Rate |
|---|---:|---:|---:|
| fetch | 88 | 100 | 88.0% |
| markdown | 86 | 88 | 97.7% |
| metadata | 87 | 88 | 98.9% |
| article | 87 | 88 | 98.9% |
| links | 84 | 88 | 95.5% |
| text_pipeline | 88 | 88 | 100% |
| dedup | 88 | 88 | 100% |
| search_index | 88 | 88 | 100% |
| serp (DuckDuckGo live) | pass | - | 5 hits |
| search_query (FTS5) | pass | - | 88 indexed, probe 5/5 |
| job API (REST e2e) | 16 | 16 | stage 2 below |

The 12 fetch failures are the honest taxonomy (no evasion, per NON_GOALS):
arstechnica 405, stackoverflow 403, akamai/bloomberg/economist/ietf/
japantimes/nih/oracle/ox 403, reuters 401, state.gov 407 — bot-walls and
proxy/auth classes recorded as refusals, never bypassed.

## Stage 2 — job API end-to-end (16/16)

health (+ non_guarantees), v1/about detail, metrics jobs stats; scrape job
202 -> completed with extracted text (len=156); retry job honors the cap
(attempts==2 -> failed); document job parses; metrics pending bounded;
slow job -> cancel accepted (200, cooperative=True) -> terminal cancelled;
durable SQLite: 4 job rows; reopen loads history
{completed, failed, cancelled}.

## Bugs this campaign found and fixed (committed 2.9.0-era)

1. POST /v1/jobs was never routed (fell into the /v1/ chat catch-all -> 400).
2. GET /v1/jobs, /v1/jobs/{id}, /v1/jobs/{id}/results, GET /v1/artifacts/{id}
   were never wired into do_GET.
3. MCP freegem_submit_job created jobs but never executed them.
4. Windows: runtime cap boundary (max_runtime_sec=0.0 let pages through).
5. Windows: SQLite handle leaks in tests (WinError 32).
6. Windows: SqliteJobStore connection state -> per-path singleton + graceful
   degradation.

## Reproduce

Two scripts from the era (ai_deep_phase.py style drivers under scripts/
history) ran stage 1; stage 2 is the REST e2e contract now pinned by
test_jobs / test_server suites in CI (hermetic seams, issue #74).

Context: this is the 100-host all-feature sample; the 1,000-site and
10,000-URL corpus runs are recorded in docs/bench-live-10k-2026-11.md and
the campaign changelog entries (2.7.0 - 2.9.0).
