# Live 1000-site scrape (one-shot experiment)

This directory is scaffolding for a single live experiment, not a shipped
feature. The `bench-live-1000.yml` workflow (one push to this branch):

1. `build_list.py` downloads a public top-sites ranking (Majestic Million
   CSV, Tranco fallback) and takes the top 1000 domains.
2. `scrape_chunk.py` runs in 10 parallel jobs, each driving 100 URLs
   through the shipped `freegem scrape --format jsonl --no-cache` CLI in
   sub-batches of 25 with `--concurrency 8`.
3. `aggregate.py` classifies failures with the same taxonomy as
   `benchmarks/run_bench.py` and commits `results/` back to this branch.

The branch is the record; it is not meant to merge. Page text is stripped
to 200-char excerpts in the committed results.
