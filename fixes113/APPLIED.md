#113 conditional-GET revalidation: tested 12/12 + full-suite green locally (1632 passed / 11 skipped / 0 failed, ruff/mypy clean except a pre-existing main-only error). Transport wall blocks the final web_scraper/test_scraper blobs (74-83KB; connector rejects >73KB utf-8 and base64 payloads died at ~100KB). Delta patches + the new test file live in this directory; every byte of the final state is server-reachable and verified:

- fixes113/07-web_scraper_113.patch  blob 4a9984b (bfe482f -> a7c2309; apply AFTER fixes/01-web_scraper.patch)
- fixes113/08-web_research_113.patch blob feacd36 (7016449 -> 9fda341; applies to plain main)
- freegem/tests/test_cache_revalidation.py blob a350840
- final-state check: web_scraper a7c2309cc6e381ad55e7e0d41058103db48899ba | web_research 9fda341c4df54fbcebf34628eb799701d4d5a2c

Assembly paste block posted in #113 / #105.
