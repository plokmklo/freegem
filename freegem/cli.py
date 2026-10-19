"""Unified command-line entry point.

One command runs either face of the bridge:

    freegem serve   # OpenAI + Google-native HTTP API
    freegem mcp     # MCP server for agents

Plus one-shot utilities for checking a setup without starting a server:
    freegem ask "what is the time in Tokyo?"
    freegem scrape https://example.com --links --ask "what is this site?"
    freegem scrape URL1 URL2 URL3 --format jsonl
    freegem research URL1 URL2 -q "question"
    freegem verify "claim one" "claim two" --urls URL1 URL2
    freegem ai clean URL                 # AI-distill page to main content
    freegem ai summarize URL             # AI summary with key points
    freegem ai extract URL --schema {...} # structured field extraction
    freegem ai research URL1 URL2 -q "..." # multi-page AI research
    freegem ai crawl SEED -q "topic"     # AI-powered crawl with scoring
    freegem watch https://example.com/status --interval 30m
    freegem models
    freegem doctor
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import sys
from typing import Any

from . import __version__
from .config import BridgeConfig, config_search_paths, find_config, load_config, redact_proxy
from .gemini import BridgeError, log, refresh_build_tag
from .mcp_tools import build_server, run_tool
from .models import MODELS, resolve_model

# Subprocess children inherit this environment; without it a Python script
# on Windows that pipes freegem output dies on cp1252 decode errors.
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("PYTHONUTF8", "1")


def _build_parser() -> argparse.ArgumentParser:
    # Global flags live on a shared parent parser so they are accepted BOTH
    # before and after the subcommand: `freegem --quiet serve` and
    # `freegem serve --quiet` must behave identically.
    #
    # default=SUPPRESS is load-bearing, not decoration: argparse gives a
    # subparser's own defaults precedence over values the main parser already
    # parsed, so a plain store_true default here would silently clobber
    # `--quiet` placed before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="config file (default: search ./config.json, ./gemini-auth.json, "
        "~/.config/freegem/config.json, $FREEGEM_CONFIG)",
    )
    common.add_argument("--model", metavar="ID", default=argparse.SUPPRESS, help="default model id")
    common.add_argument(
        "--cookie-file", metavar="PATH", default=argparse.SUPPRESS, help="session cookie file"
    )
    common.add_argument(
        "--proxy",
        metavar="URL",
        default=argparse.SUPPRESS,
        help="HTTP proxy, e.g. http://127.0.0.1:7890",
    )
    common.add_argument(
        "--quiet", action="store_true", default=argparse.SUPPRESS, help="suppress request logging"
    )

    parser = argparse.ArgumentParser(
        prog="freegem",
        description="Free Gemini, shaped as the APIs you already use.",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"freegem {__version__}")

    sub = parser.add_subparsers(
        dest="command",
        metavar="{serve,mcp,ask,models,scrape,ai,research,verify,crawl,watch,doctor}",
    )

    serve = sub.add_parser("serve", help="run the HTTP API server", parents=[common])
    serve.add_argument("--port", type=int, help="port to listen on (default 8081)")
    serve.add_argument("--host", metavar="ADDR", help="bind address (default 127.0.0.1)")
    serve.add_argument(
        "--no-refresh",
        action="store_true",
        help="skip the startup build-tag refresh",
    )

    mcp = sub.add_parser("mcp", help="run the MCP server", parents=[common])
    mcp.add_argument(
        "--transport", choices=["stdio", "sse"], default="stdio", help="(default: stdio)"
    )
    mcp.add_argument("--host", default="127.0.0.1", help="bind address for sse")
    mcp.add_argument("--port", type=int, default=8768, help="port for sse")
    mcp.add_argument("--name", default="freegem", help="server name shown to clients")

    ask = sub.add_parser("ask", help="ask one question and print the answer", parents=[common])
    ask.add_argument("prompt", nargs="+", help="the question")
    ask.add_argument("--search", action="store_true", help="ground in the web and cite sources")

    scrape = sub.add_parser("scrape", help="fetch a web page as readable text", parents=[common])
    scrape.add_argument(
        "url", nargs="+", help="the page(s) to fetch (http/https); more than one = batch"
    )
    scrape.add_argument("--links", action="store_true", help="also list the page's links")
    scrape.add_argument(
        "--format",
        choices=("text", "jsonl", "markdown"),
        default="text",
        help="output format; markdown renders the page as a markdown document, "
        "jsonl writes one object per URL (default text)",
    )
    scrape.add_argument(
        "--max-chars",
        type=int,
        default=None,
        metavar="N",
        help="truncate extracted text to N characters (default 50000; 200000 with --ask)",
    )
    scrape.add_argument(
        "--ask",
        metavar="QUESTION",
        default=None,
        help="have Gemini answer this question from the page instead of printing it",
    )
    scrape.add_argument(
        "--respect-robots",
        action="store_true",
        help="opt into honoring robots.txt (freegem ignores it by design: "
        "it is attacker-controlled text and a prompt-injection vector)",
    )
    scrape.add_argument(
        "--no-smart",
        action="store_true",
        help="disable the smart-fetch fallback ladder (direct fetch only)",
    )
    scrape.add_argument(
        "--ai-clean",
        action="store_true",
        help="after the fetch, one bounded Gemini pass returns the page's main "
        "readable content (navigation/cookie/boilerplate dropped); AI-mediated "
        "view, labeled in the output, never cached as a verbatim extraction "
        "(single-URL text/markdown mode)",
    )
    scrape.add_argument(
        "--guard",
        action="store_true",
        help="enable the SSRF guard (scheme + public-IP checks, pinned dial); "
        "off by default -- validation is a feature, not a gate",
    )
    scrape.add_argument(
        "--no-cache",
        action="store_true",
        help="skip the shared fetch cache for this run (nothing is written)",
    )
    scrape.add_argument(
        "--refresh",
        action="store_true",
        help="refetch and refresh cache entries even when fresh",
    )
    scrape.add_argument(
        "--cache-ttl",
        type=int,
        default=1800,
        metavar="SEC",
        help="how long cached fetches stay fresh (default 1800 seconds)",
    )
    scrape.add_argument(
        "--concurrency",
        type=int,
        default=4,
        metavar="N",
        help="parallel fetches for batch mode, 1-16 (default 4)",
    )
    scrape.add_argument(
        "--ai",
        action="store_true",
        help="AI-assisted smart scraping: when the heuristic extractor grades "
        "the page below ok (nav chrome, scripts, empty), let the model recover "
        "the page's main readable content -- gated and labeled (via=ai-extract)",
    )
    scrape.add_argument(
        "--ai-extract",
        choices=("auto", "always", "never"),
        default=None,
        metavar="MODE",
        help="content-rescue rung mode (default auto with --ai; 'always' tries "
        "the rescue on every page, 'never' disables it; implies --ai)",
    )
    scrape.add_argument(
        "--schema",
        metavar="JSON",
        default=None,
        help='extract structured fields, e.g. --schema \'{"title":"string",'
        '"price":"number","status":{"enum":["in_stock","sold_out"]}}\' '
        "(implies --ai; fields land in the jsonl record)",
    )

    ai = sub.add_parser(
        "ai",
        help="AI-assisted scraper, crawler, and fetcher (clean, summarize, extract, "
        "research, crawl)",
        parents=[common],
    )
    ai_sub = ai.add_subparsers(dest="ai_cmd", metavar="SUBCMD", required=False)

    ai_common = argparse.ArgumentParser(add_help=False, parents=[common])
    ai_common.add_argument(
        "--budget",
        type=int,
        default=None,
        metavar="N",
        help="per-call character budget for AI passes (default: 100000)",
    )
    ai_common.add_argument(
        "--max-chars",
        type=int,
        default=None,
        metavar="N",
        help="truncate fetched text to N characters before AI (default: 50000)",
    )
    ai_common.add_argument(
        "--no-cache", action="store_true", help="skip the fetch cache (nothing is written)"
    )
    ai_common.add_argument(
        "--refresh", action="store_true", help="refetch and refresh cache entries"
    )
    ai_common.add_argument(
        "--no-smart", action="store_true", help="disable smart-fetch fallback ladder"
    )
    ai_common.add_argument(
        "--concurrency",
        type=int,
        default=3,
        metavar="N",
        help="parallel fetches (default 3)",
    )
    ai_common.add_argument(
        "--no-limit",
        action="store_true",
        help="bypass the batch concurrency cap (use with care; 100+ workers can "
        " overwhelm sites and exhaust file descriptors)",
    )

    ai_clean = ai_sub.add_parser(
        "clean",
        help="fetch a page and distill it to main content (drops nav/cookie boilerplate)",
        parents=[ai_common],
    )
    ai_clean.add_argument("url", metavar="URL", help="the page to clean")

    ai_summarize = ai_sub.add_parser(
        "summarize",
        help="fetch a page and produce a summary with key points",
        parents=[ai_common],
    )
    ai_summarize.add_argument("url", metavar="URL", help="the page to summarize")

    ai_extract = ai_sub.add_parser(
        "extract",
        help="fetch one or more pages and extract structured fields matching a schema",
        parents=[ai_common],
    )
    ai_extract.add_argument("urls", nargs="+", metavar="URL", help="page(s) to extract from")
    ai_extract.add_argument(
        "--schema",
        metavar="JSON",
        default=None,
        required=True,
        help='JSON field schema, e.g. \'{"title":"string","author":"string"}\'',
    )
    ai_extract.add_argument(
        "--query",
        "--question",
        "-q",
        dest="query",
        default=None,
        help="the question to answer from the pages",
    )

    ai_entities = ai_sub.add_parser(
        "entities",
        help="fetch a page and extract named entities, topics, and terms",
        parents=[ai_common],
    )
    ai_entities.add_argument("url", metavar="URL", help="the page to extract entities from")

    ai_questions = ai_sub.add_parser(
        "questions",
        help="fetch a page and generate interview-style questions about its content",
        parents=[ai_common],
    )
    ai_questions.add_argument("url", metavar="URL", help="the page to question")

    ai_research = ai_sub.add_parser(
        "research",
        help="fetch several pages, answer one question, and cite sources",
        parents=[ai_common],
    )
    ai_research.add_argument("urls", nargs="+", metavar="URL", help="pages to research")
    ai_research.add_argument(
        "--query", "--question", "-q", dest="query", required=True, help="the question to answer"
    )

    ai_crawl = ai_sub.add_parser(
        "crawl",
        help="AI-powered crawl: score pages against a query and summarize findings",
        parents=[ai_common],
    )
    ai_crawl.add_argument("seeds", nargs="+", metavar="URL", help="seed URL(s) to crawl from")
    ai_crawl.add_argument(
        "--query", metavar="TEXT", required=True, help="what to score pages against"
    )
    ai_crawl.add_argument("--max-pages", type=int, default=50, metavar="N", help="(default 50)")
    ai_crawl.add_argument("--max-depth", type=int, default=2, metavar="N", help="(default 2)")

    research = sub.add_parser(
        "research",
        help="fetch several pages and answer one question with [S#] citations",
        parents=[common],
    )
    research.add_argument("urls", nargs="+", metavar="URL", help="pages to research (http/https)")
    research.add_argument(
        "--query",
        "--question",
        "-q",
        dest="query",
        required=True,
        help="the question to answer from the pages",
    )
    research.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="output format (default text)",
    )
    research.add_argument(
        "--max-pages",
        type=int,
        default=5,
        metavar="N",
        help="fetch at most N pages (default 5, hard cap 10)",
    )
    research.add_argument(
        "--max-chars-per-page",
        type=int,
        default=12000,
        metavar="N",
        help="truncate each page's text to N characters (default 12000)",
    )
    research.add_argument(
        "--total-budget",
        type=int,
        default=80000,
        metavar="N",
        help="hard cap on total fetched characters kept across pages (default 80000)",
    )
    research.add_argument(
        "--no-cache",
        action="store_true",
        help="skip the local fetch cache for this run (still writes nothing)",
    )
    research.add_argument(
        "--refresh",
        action="store_true",
        help="refetch every URL this run and refresh the cache entries",
    )
    research.add_argument(
        "--cache-ttl",
        type=int,
        default=1800,
        metavar="SEC",
        help="how long cached fetches stay fresh (default 1800 seconds)",
    )
    research.add_argument(
        "--fetch-concurrency",
        type=int,
        default=None,
        metavar="N",
        help=(
            "fetch pages N at a time (default: research_concurrency config, 3); "
            "1 = sequential. Fetches to the same host always serialize"
        ),
    )
    research.add_argument(
        "--evidence",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "ask for, and verify, verbatim evidence quotes per cited source "
            "(default: research_evidence config, on)"
        ),
    )
    research.add_argument(
        "--ai",
        action="store_true",
        help=(
            "fetch pages with the AI-assisted smart scraper: gated content "
            "rescue for poor extractions plus the AI wall rung for pages whose "
            "fetch itself fails (via=ai-extract / ai-wall-rescue, honest notes; "
            "AI views are never cached)"
        ),
    )

    verify = sub.add_parser(
        "verify",
        help="verify claims against fetched sources with per-claim statuses",
        parents=[common],
    )
    verify.add_argument(
        "claims", nargs="+", metavar="CLAIM", help="claims to verify (1 to 8, quoted)"
    )
    verify.add_argument(
        "--urls",
        nargs="+",
        metavar="URL",
        default=None,
        help="pages to verify against (http/https); or use --discover",
    )
    verify.add_argument(
        "--discover",
        action="store_true",
        help=(
            "find candidate URLs through a supported search API (Wikimedia "
            "portal / site OpenSearch) instead of listing them"
        ),
    )
    verify.add_argument(
        "--discovery-query",
        metavar="QUERY",
        default=None,
        help="custom search query for --discover (default: the claims joined)",
    )
    verify.add_argument(
        "--discovery-limit",
        type=int,
        default=3,
        metavar="N",
        help="how many URLs discovery may return (default 3, cap 5)",
    )
    verify.add_argument(
        "--wiki",
        metavar="HOST",
        default=None,
        help="wiki host discovery searches (default: discover_wiki config, en.wikipedia.org)",
    )
    verify.add_argument(
        "--discover-backend",
        choices=("wiki", "serp"),
        default=None,
        help=(
            "discovery backend: 'wiki' searches a MediaWiki site, 'serp' uses the "
            "DuckDuckGo HTML endpoint for any topic (default: discover_backend config, wiki)"
        ),
    )
    verify.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="output format (default text)",
    )
    verify.add_argument(
        "--max-pages",
        type=int,
        default=5,
        metavar="N",
        help="fetch at most N pages (default 5, hard cap 10)",
    )
    verify.add_argument(
        "--max-chars-per-page",
        type=int,
        default=12000,
        metavar="N",
        help="truncate each page's text to N characters (default 12000)",
    )
    verify.add_argument(
        "--total-budget",
        type=int,
        default=80000,
        metavar="N",
        help="hard cap on total fetched characters kept (default 80000)",
    )
    verify.add_argument(
        "--no-cache",
        action="store_true",
        help="skip the local fetch cache for this run",
    )
    verify.add_argument(
        "--refresh",
        action="store_true",
        help="refetch every URL this run and refresh cache entries",
    )
    verify.add_argument(
        "--cache-ttl",
        type=int,
        default=1800,
        metavar="SEC",
        help="how long cached fetches stay fresh (default 1800 seconds)",
    )
    verify.add_argument(
        "--fetch-concurrency",
        type=int,
        default=None,
        metavar="N",
        help="fetch pages N at a time (default: research_concurrency config, 3)",
    )

    crawl = sub.add_parser(
        "crawl",
        help="bounded, resumable local crawler (robots.txt is never consulted)",
        parents=[common],
    )
    crawl_sub = crawl.add_subparsers(dest="crawl_cmd", required=True)

    # parents=[common]: the global flags must also parse AFTER the nested
    # subcommand (`freegem crawl run --model x`), not only before it --
    # the top-level crawl parser never sees args that follow "run".
    crawl_common = argparse.ArgumentParser(add_help=False, parents=[common])
    crawl_common.add_argument(
        "--seeds",
        nargs="+",
        metavar="SRC",
        default=None,
        help="seed URLs, or files (.txt: one URL per line, .jsonl: {url,category})",
    )
    crawl_common.add_argument("--state", metavar="PATH", default=None, help="frontier SQLite file")
    crawl_common.add_argument(
        "--allow-domain",
        action="append",
        metavar="D",
        default=None,
        help="also crawl this domain (repeatable; suffix-matched)",
    )
    crawl_common.add_argument(
        "--deny-domain",
        action="append",
        metavar="D",
        default=None,
        help="never crawl this domain (repeatable, wins over allow)",
    )
    crawl_common.add_argument(
        "--allow-path",
        action="append",
        metavar="P",
        default=None,
        help="only crawl paths with this prefix (repeatable)",
    )
    crawl_common.add_argument(
        "--deny-path",
        action="append",
        metavar="P",
        default=None,
        help="never crawl paths with this prefix (repeatable)",
    )
    crawl_common.add_argument("--max-depth", type=int, default=2, metavar="N", help="(default 2)")
    crawl_common.add_argument(
        "--max-pages", type=int, default=100, metavar="N", help="page budget (default 100)"
    )
    crawl_common.add_argument(
        "--any-host",
        action="store_true",
        help="follow links off the seed hosts (default: same-host only)",
    )
    crawl_common.add_argument(
        "--sitemap", action="store_true", help="discover URLs from each seed host's /sitemap.xml"
    )
    crawl_common.add_argument(
        "--delay-ms", type=int, default=250, metavar="MS", help="per-host politeness (default 250)"
    )
    crawl_common.add_argument(
        "--concurrency", type=int, default=2, metavar="N", help="parallel fetches 1-8 (default 2)"
    )
    crawl_common.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        metavar="SEC",
        help="per-request timeout (default 20)",
    )
    crawl_common.add_argument(
        "--max-bytes",
        type=int,
        default=5 * 1024 * 1024,
        metavar="N",
        help="per-page byte cap, wire and decompressed (default 5 MiB)",
    )
    crawl_common.add_argument(
        "--max-retries", type=int, default=2, metavar="N", help="retries per page (default 2)"
    )
    crawl_common.add_argument(
        "--max-runtime-sec",
        type=float,
        default=900.0,
        metavar="SEC",
        help="stop the crawl after this many seconds (default 900)",
    )

    crawl_sub.add_parser(
        "plan", help="dry run: normalize seeds, show scope and budget", parents=[crawl_common]
    )

    run_p = crawl_sub.add_parser(
        "run", help="run a crawl (fresh or resumed state)", parents=[crawl_common]
    )
    run_p.add_argument("--out", metavar="PATH", default="", help="results file (jsonl default)")
    run_p.add_argument(
        "--format", choices=("jsonl", "json", "csv"), default="jsonl", help="results format"
    )
    run_p.add_argument("--schema", metavar="PATH", default="", help="extraction schema JSON")
    run_p.add_argument("--artifacts", metavar="DIR", default="", help="store raw page bodies here")
    run_p.add_argument(
        "--json-logs", action="store_true", help="also print progress events as JSON to stdout"
    )
    run_p.add_argument(
        "--ai-rescue",
        action="store_true",
        help=(
            "when a page's default extraction grades below 'ok', attempt the "
            "gated AI content rescue (same gate as scrape --ai; the record "
            "carries ai_rescued/ai_rescue_note). Fetch failures stay the "
            "resilient fetcher's business -- no model calls on dead URLs"
        ),
    )

    status_p = crawl_sub.add_parser("status", help="show frontier stats for a state file")
    status_p.add_argument("--state", metavar="PATH", required=True, help="frontier SQLite file")

    watch = sub.add_parser(
        "watch",
        help="watch a URL and report what changed (simhash + line diff, local state)",
        parents=[common],
        epilog="exit codes: 0 baseline/no change (or clean Ctrl-C), 3 changed (--once), "
        "1 fetch/error, 2 usage",
    )
    watch.add_argument(
        "url", nargs="?", default=None, metavar="URL", help="the page to watch (http/https)"
    )
    watch.add_argument(
        "--interval",
        metavar="DUR",
        default=None,
        help="poll every DUR (seconds, or 30m/1h style) until Ctrl-C; "
        "omitted = one check and exit (default)",
    )
    watch.add_argument(
        "--once",
        action="store_true",
        help="force a single check even with --interval (cron-friendly)",
    )
    watch.add_argument(
        "--delta",
        action="store_true",
        help="print every diff block instead of the first few",
    )
    watch.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default text)",
    )
    watch.add_argument(
        "--list",
        action="store_true",
        help="list every stored watch (url, last check, state file) and exit",
    )
    watch.add_argument(
        "--forget",
        action="store_true",
        help="with a URL: drop that URL's baseline and exit",
    )
    watch.add_argument(
        "--forget-all",
        action="store_true",
        help="drop EVERY watch baseline (and stale tmp files) and exit",
    )
    watch.add_argument(
        "--max-age",
        metavar="DUR",
        default=None,
        help="before checking: drop watch entries not polled within DUR (e.g. 30d)",
    )
    watch.add_argument(
        "--max-entries",
        type=int,
        metavar="N",
        default=None,
        help="before checking: keep at most N watch entries, evicting the oldest-checked",
    )

    sub.add_parser("models", help="list available models", parents=[common])

    sub.add_parser(
        "doctor", help="check session, build tag, and network reachability", parents=[common]
    )

    return parser


def _apply_overrides(cfg: BridgeConfig, args: argparse.Namespace) -> BridgeConfig:
    # getattr, not attribute access: the global flags use default=SUPPRESS, so
    # an attribute only exists when the flag was actually given.
    if getattr(args, "model", None):
        cfg = cfg.replace(default_model=args.model)
    if getattr(args, "cookie_file", None):
        cfg = cfg.replace(cookie_file=os.path.abspath(args.cookie_file))
    if getattr(args, "proxy", None):
        cfg = cfg.replace(proxy=args.proxy)
    if getattr(args, "quiet", False):
        cfg = cfg.replace(log_requests=False)
    return cfg


# ── commands ──────────────────────────────────────────────────────────────────


def _strict_guard(cfg: BridgeConfig) -> BridgeConfig:
    """Force the SSRF guard ON for every surface except ``freegem scrape``.

    The config default is off so a plain scrape -- where the operator typed
    each target -- just fetches (validation is a feature, not a default).
    Every other surface fetches URLs nobody typed at the keyboard: search
    discovery (research, verify, ask --search), crawl frontiers, watch lists,
    server clients and MCP tools. Those stay strict, exactly like the
    server/MCP surfaces have always been.
    """
    return cfg.replace(guard=True)


def cmd_serve(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    from .server import ThreadedServer

    # Network-facing surface: strict regardless of the config default.
    cfg = _strict_guard(cfg)

    if args.port:
        cfg = cfg.replace(port=args.port)
    if args.host:
        cfg = cfg.replace(host=args.host)

    if not args.no_refresh:
        cfg, change = refresh_build_tag(cfg)
        if change:
            print(f"build tag refreshed: {change}", file=sys.stderr)

    server = ThreadedServer((cfg.host, cfg.port), cfg)

    print(f"freegem v{__version__}")
    print(f"  OpenAI:    http://{cfg.host}:{cfg.port}/v1/chat/completions")
    print(f"  Google:    http://{cfg.host}:{cfg.port}/v1beta/models/<model>:generateContent")
    print(f"  Health:    http://{cfg.host}:{cfg.port}/health")
    print(f"  Models:    {', '.join(MODELS)}")
    print(f"  Session:   {'cookie configured' if cfg.has_session_cookie() else 'anonymous'}")
    print(f"  Proxy:     {redact_proxy(cfg.proxy)}")
    print(f"  Streaming: {'token-by-token' if _has_httpx() else 'whole reply (install httpx)'}")
    print(f"  Temporary: {'yes' if cfg.temporary_chats else 'no'}")
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        warning = (
            f"\n  WARNING: bound to a non-loopback address ({cfg.host}).\n"
            "  This exposes your Google session to the network.\n"
            "  Anyone who can reach this port can spend your Gemini quota.\n"
        )
        # One write to one stream: a warning split between stdout and stderr
        # is easy to miss when someone pipes one of them away.
        print(warning, file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        server.shutdown()
    return 0


def cmd_mcp(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    # Network-facing surface: strict (see _strict_guard note). The MCP
    # web_scrape tool answers URLs an agent's prompt chose.
    cfg = _strict_guard(cfg)
    server = build_server(cfg, name=args.name)
    log(
        cfg,
        f"mcp transport={args.transport} model={cfg.default_model} "
        f"session={'cookie' if cfg.has_session_cookie() else 'anonymous'}",
    )
    if args.transport == "sse":
        server.settings.host = args.host
        server.settings.port = args.port
        log(cfg, f"mcp listening on http://{args.host}:{args.port}/sse")
    server.run(transport=args.transport)
    return 0


#: Gemini's chat-flavored refusal scripts: phrasings the upstream serves as a
#: "successful" answer when a request cannot be fulfilled. smart_fetch's
#: _looks_like_ai_nonanswer already covers the page-reading family ("could not
#: access ..."); these are the generic request refusals observed on
#: `ask --search` ("Sorry, I'm having a hard time fulfilling your request",
#: "something went wrong"). Like the detector there, the match is capped at
#: SHORT answers only: a long reply that happens to CONTAIN the phrase is
#: content, not a refusal.
_ASK_NONANSWER_RE = re.compile(r"having a hard time|something went wrong", re.IGNORECASE)
_ASK_NONANSWER_MAX_CHARS = 2000


def _looks_like_soft_failure(text: str) -> bool:
    """True when a 'successful' answer is actually the upstream's soft-failure."""
    # Lazy import: smart_fetch pulls in the scraper stack; the CLI should not
    # pay for it at startup, and the cycle stays broken at call time.
    from .smart_fetch import _looks_like_ai_nonanswer

    stripped = text.strip()
    if _looks_like_ai_nonanswer(stripped):
        return True
    if len(stripped) > _ASK_NONANSWER_MAX_CHARS:
        return False
    return bool(_ASK_NONANSWER_RE.search(stripped))


def cmd_ask(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    # --search fetches URLs a search engine chose; stay strict.
    cfg = _strict_guard(cfg)
    question = " ".join(args.prompt).strip()
    if not question:
        print("nothing to ask", file=sys.stderr)
        return 2
    try:
        if args.search:
            # Grounded search runs its own multi-step pipeline; it has no
            # streamable single request. Say something instead of silence.
            print("searching the web...", file=sys.stderr)
            answer = run_tool(cfg, "gemini_search", {"query": question})
            if _looks_like_soft_failure(answer):
                # The upstream served a refusal script as a "successful"
                # answer. stdout stays EMPTY so a pipeline never mistakes the
                # failure for an answer; the raw text goes to stderr for the
                # human, and the exit code says something went wrong.
                print(answer, file=sys.stderr)
                print(
                    "ask: upstream soft-failure (non-answer); retry or rephrase",
                    file=sys.stderr,
                )
                return 1
        else:
            _stream_answer(cfg, question)
            return 0
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(answer)
    return 0


def _stream_answer(cfg: BridgeConfig, question: str) -> None:
    """Print a plain answer as it is produced, token-by-token when possible.

    Falls back to one non-streaming request when httpx is missing -- same
    answer, just printed whole. If the stream is interrupted AFTER partial
    output reached the terminal, a non-streaming replay fills in the rest
    rather than leaving the user with a half-answer.
    """
    from .gemini import HAS_HTTPX, BridgeError, generate, generate_stream
    from .models import resolve_model

    _name, mode_id, think_mode, err, extra = resolve_model(cfg.default_model, cfg.default_model)
    if err:
        raise BridgeError(err)
    assert mode_id is not None and think_mode is not None, "resolve_model broke its contract"
    if not HAS_HTTPX:
        print(generate(cfg, question, int(mode_id), int(think_mode), None, extra))
        return
    printed = False
    try:
        for delta in generate_stream(cfg, question, int(mode_id), int(think_mode), None, extra):
            if not delta:
                continue
            sys.stdout.write(delta)
            sys.stdout.flush()
            printed = True
    except BridgeError as exc:
        if not printed:
            raise
        # Partial output reached the terminal. The non-streaming path gives
        # the user a complete answer instead of a half-streamed failure.
        # The already-printed text is a best-effort prefix; the full reply
        # follows after a separator line so nothing is silently lost.
        print(f"\n[stream interrupted: {exc}]", file=sys.stderr)
        answer = generate(cfg, question, int(mode_id), int(think_mode), None, extra)
        if answer:
            print(f"\n[complete reply follows]\n{answer}")
            return
        raise
    if not printed:
        raise BridgeError(
            "Gemini returned nothing. Likely a rate limit, a stale build tag, "
            "or an expired session cookie."
        )
    print()


def cmd_models(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    from .mcp_tools import model_help

    print(model_help(cfg))
    return 0


def _scrape_markdown(
    *,
    url: str,
    title: str,
    text: str,
    truncated: bool = False,
    links: list[tuple[str, str]] | None = None,
) -> str:
    """Render one scraped page as a minimal markdown document.

    Mirrors the markdown shapes the repo's other renderers emit: the title
    becomes the H1 (article.py), and links become a ``## Links`` section of
    real ``[label](href)`` links (research's linked source table). Page-
    controlled strings (title, text, link labels) go through the same
    marker defusing ``format_page`` applies, and multi-line titles are
    flattened so a page cannot open headings of its own.
    """
    from .web_scraper import _defuse_untrusted_markers

    heading = " ".join(_defuse_untrusted_markers(title or url).split()) or url
    lines = [f"# {heading}", ""]
    if text:
        lines.append(_defuse_untrusted_markers(text).rstrip())
    if truncated:
        lines += ["", "*[content truncated at the max_chars limit]*"]
    if links:
        lines += ["", "## Links", ""]
        for label, href in links:
            flat_href = " ".join(_defuse_untrusted_markers(href).split())
            lines.append(f"- [{_defuse_untrusted_markers(label or flat_href)}]({flat_href})")
    return "\n".join(lines)


def cmd_scrape(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    """Fetch page(s) (SSRF-guarded, no robots.txt) and print text, JSONL, or an answer.

    One URL keeps the classic behavior: readable text (or a ``--ask`` answer).
    Several URLs become a batch: bounded parallel pool, per-host politeness,
    one result line per URL, failures recorded per URL instead of killing
    the run. ``--ask`` implies the 200k character budget when no explicit
    ``--max-chars`` was given -- a question deserves the whole page.

    Targets may be URLs or LOCAL FILES (#50): an existing path (``./notes.html``),
    a Windows drive path (``C:\\...``), or a ``file:`` URI reads from disk with
    the same cap and extractor as the wire path -- the quota-free local-batch
    path. The operator typed the target here, so local reads are enabled for
    the whole command; MCP/server surfaces keep them off.

    ``--ai`` / ``--ai-extract`` / ``--schema`` switch to the AI-assisted
    path (:mod:`freegem.ai_scrape`): the deterministic pipeline runs first;
    when its grade is poor the model recovers the page's main content
    (gated, labeled ``via=ai-extract``), ``--ask`` answers from the shipped
    text, and ``--schema`` adds validated fields. Notes about every AI
    event go to stderr; ``--format jsonl`` carries them in the record.
    """
    from .web_research import ResearchCache

    if getattr(args, "guard", False):
        cfg = cfg.replace(guard=True)
    ai_extract = args.ai_extract
    if ai_extract is None:
        ai_extract = "auto" if (args.ai or args.schema) else "never"
    ai_mode = bool(args.ai or args.ai_extract or args.schema)
    schema = None
    if args.schema is not None:
        from .ai_scrape import schema_from_json

        try:
            schema = schema_from_json(args.schema)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    if not ai_mode:
        return _cmd_scrape_classic(cfg, args)

    from .ai_scrape import ai_scrape, ai_scrape_many

    if args.ask and len(args.url) > 1:
        print("error: --ask works with a single URL only", file=sys.stderr)
        return 2
    max_chars = args.max_chars
    if max_chars is None:
        max_chars = 200_000 if args.ask else 50_000
    smart = not args.no_smart and getattr(cfg, "smart_fetch", True)
    cache = None if args.no_cache else ResearchCache(ResearchCache.default_path(), args.cache_ttl)
    kwargs: dict[str, Any] = {
        "max_chars": max_chars,
        "ignore_robots": not args.respect_robots,
        "smart": smart,
        "cache": cache,
        "refresh": args.refresh,
        "ai_extract": ai_extract,
        "schema": schema,
        "allow_local": True,
    }
    if len(args.url) == 1:
        if args.ask:
            kwargs["query"] = args.ask
        result = ai_scrape(cfg, args.url[0], **kwargs)
        if args.format == "jsonl":
            print(json.dumps(result.as_dict(), ensure_ascii=False))
            return 0 if result.ok else 1
        if not result.ok:
            print(f"error: {result.error}", file=sys.stderr)
            return 1
        for note in result.notes:
            print(f"note: {note}", file=sys.stderr)
        if args.ask:
            print(result.answer or "")
            return 0
        if args.format == "markdown":
            print(
                _scrape_markdown(
                    url=result.url,
                    title=result.title,
                    text=result.text,
                    truncated=result.truncated,
                )
            )
        else:
            print(result.text)
        return 0

    results = ai_scrape_many(cfg, list(args.url), concurrency=args.concurrency, **kwargs)
    if args.format == "jsonl":
        for item in results:
            print(json.dumps(item.as_dict(), ensure_ascii=False))
    elif args.format == "markdown":
        for index, item in enumerate(results, 1):
            if index > 1:
                print()
                print("---")
                print()
            if not item.ok:
                print(f"# {item.url}\n\n**error:** {item.error}")
                continue
            print(
                _scrape_markdown(
                    url=item.url, title=item.title, text=item.text, truncated=item.truncated
                )
            )
    else:
        for index, item in enumerate(results, 1):
            if index > 1:
                print()
                print("=" * 72)
            header = f"[{index}/{len(results)}] {item.url}"
            if not item.ok:
                print(f"{header}\nerror: {item.error}")
                continue
            ai_tag = " +ai" if item.ai_used else ""
            print(
                f"{header}\ntitle: {item.title} ({len(item.text)} chars, via {item.via}{ai_tag})\n"
            )
            print(item.text)
    failed = sum(1 for item in results if not item.ok)
    if failed == len(results) and results:
        return 1
    return 0


def _cmd_scrape_classic(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    """The pre-AI scrape path, byte-identical when no AI flag is given."""
    from .web_research import ResearchCache
    from .web_scraper import scrape_cli, scrape_many

    if getattr(args, "guard", False):
        cfg = cfg.replace(guard=True)

    want_ai_clean = getattr(args, "ai_clean", False)
    if want_ai_clean:
        # The per-run flag IS the explicit choice the config gate wants:
        # without this, --ai-clean could never leave the "disabled" state
        # for users who did not also set the config key (smoke-matrix
        # finding). Same pattern as --guard above.
        cfg = cfg.replace(smart_fetch_ai_clean=True)
    if want_ai_clean and args.ask:
        print("error: --ai-clean and --ask are both AI transformations; pick one", file=sys.stderr)
        return 2
    if want_ai_clean and (len(args.url) > 1 or args.format == "jsonl"):
        print(
            "error: --ai-clean works with a single URL in text/markdown mode "
            "(the cleaned view is caller-specific and never cached as a verbatim extraction)",
            file=sys.stderr,
        )
        return 2

    max_chars = args.max_chars
    if max_chars is None:
        # Question mode is the signal for "give me more content".
        max_chars = 200_000 if args.ask else 50_000
    smart = not args.no_smart and getattr(cfg, "smart_fetch", True)
    cache = None if args.no_cache else ResearchCache(ResearchCache.default_path(), args.cache_ttl)

    try:
        if len(args.url) == 1 and args.format == "text" and not want_ai_clean:
            output = scrape_cli(
                cfg,
                url=args.url[0],
                include_links=args.links,
                max_chars=max_chars,
                question=args.ask,
                ignore_robots=not args.respect_robots,
                smart=smart,
                cache=cache,
                refresh=args.refresh,
                allow_local=True,
            )
            print(output)
            return 0

        if args.ask:
            print("error: --ask works with a single URL only", file=sys.stderr)
            return 2

        if len(args.url) == 1 and args.format in ("text", "markdown"):
            # Single-URL text/markdown: render the extracted page (plain or
            # as a document). --ai-clean adds one bounded Gemini pass over
            # the extracted text and prints the labeled AI-mediated view.
            from .web_scraper import scrape_page

            page = scrape_page(
                cfg,
                args.url[0],
                max_chars=max_chars,
                ignore_robots=not args.respect_robots,
                smart=smart,
                cache=cache,
                refresh=args.refresh,
                allow_local=True,
            )
            if want_ai_clean:
                from .smart_fetch import ai_clean

                # The cleaned view is caller-specific: computed per run,
                # NEVER written into the fetch cache (#21 discipline), and
                # labeled where no reader can miss it.
                cleaned = ai_clean(cfg, page.url, page.text)
                if args.format == "markdown":
                    print(
                        _scrape_markdown(
                            url=page.url,
                            title=page.title,
                            text=cleaned,
                            truncated=False,
                            links=page.links if args.links else None,
                        )
                    )
                    print()
                    print(
                        "<!-- ai-clean: AI-mediated cleanup of the extracted page text; not a verbatim fetch -->"
                    )
                else:
                    print(f"title: {page.title}  (ai-clean view of {page.url})")
                    print(
                        "note: AI-mediated cleanup -- navigation and boilerplate dropped by Gemini; not a verbatim fetch\n"
                    )
                    print(cleaned)
                return 0
            if args.format == "markdown":
                print(
                    _scrape_markdown(
                        url=page.url,
                        title=page.title,
                        text=page.text,
                        truncated=page.truncated,
                        links=page.links if args.links else None,
                    )
                )
                return 0

        items = scrape_many(
            cfg,
            list(args.url),
            max_chars=max_chars,
            ignore_robots=not args.respect_robots,
            smart=smart,
            cache=cache,
            refresh=args.refresh,
            concurrency=args.concurrency,
            allow_local=True,
        )
    except (BridgeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.format == "jsonl":
        for item in items:
            print(json.dumps(item.as_dict(), ensure_ascii=False))
    elif args.format == "markdown":
        for index, item in enumerate(items, 1):
            if index > 1:
                print()
                print("---")
                print()
            if not item.ok:
                # BatchItem carries no link list, so batch markdown is
                # title + body; failures stay honest, one block per URL.
                print(f"# {item.url}\n\n**error:** {item.error}")
                continue
            print(
                _scrape_markdown(
                    url=item.url, title=item.title, text=item.text, truncated=item.truncated
                )
            )
    else:
        for index, item in enumerate(items, 1):
            if index > 1:
                print()
                print("=" * 72)
            header = f"[{index}/{len(items)}] {item.url}"
            if not item.ok:
                print(f"{header}\nerror: {item.error}")
                continue
            print(f"{header}\ntitle: {item.title} ({item.chars} chars, via {item.via})\n")
            print(item.text)
    failed = sum(1 for item in items if not item.ok)
    if failed == len(items) and items:
        return 1  # nothing succeeded: say so through the exit code
    return 0


def _ai_common_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """Build the kwargs dict shared by all freegem ai subcommands."""
    kwargs: dict[str, Any] = {}
    if args.max_chars is not None:
        kwargs["max_chars"] = args.max_chars
    kwargs["smart"] = not getattr(args, "no_smart", False)
    kwargs["refresh"] = getattr(args, "refresh", False)
    kwargs["cache"] = None if getattr(args, "no_cache", False) else _research_cache()
    if getattr(args, "budget", None) is not None:
        kwargs["budget_chars"] = args.budget
    if getattr(args, "no_limit", False):
        kwargs["no_limit"] = True
        kwargs["concurrency"] = getattr(args, "concurrency", 3)
    elif getattr(args, "concurrency", None) is not None:
        kwargs["concurrency"] = args.concurrency
    return kwargs


def _research_cache() -> Any:
    from .web_research import ResearchCache

    return ResearchCache(ResearchCache.default_path(), 1800)


def cmd_ai(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    """Dispatch AI-assisted scraping/crawling subcommands to freegem_ai."""
    from freegem.web_scraper import scrape_page
    from freegem_ai import (
        ai_clean_page,
        ai_extract,
        ai_extract_entities,
        ai_questionnaire,
        ai_research,
        ai_summarize,
        scrape,
    )
    from freegem_ai.crawler import ai_crawl

    mode = args.ai_cmd
    common = _ai_common_kwargs(args)
    model = getattr(args, "model", None)
    if model == argparse.SUPPRESS:
        model = None

    max_chars = common.get("max_chars", 50_000)
    smart = common.get("smart", True)
    cache = common.get("cache")
    refresh = common.get("refresh", False)
    budget_chars = common.get("budget_chars", 100_000)
    concurrency = common.get("concurrency", 3)
    no_limit = common.get("no_limit", False)

    if mode == "clean":
        url = args.url
        clean_result = ai_clean_page(
            cfg,
            url,
            model=model,
            max_chars=max_chars,
            smart=smart,
            cache=cache,
            refresh=refresh,
        )
        if not clean_result.scrape.text:
            print(f"error: could not fetch {url}", file=sys.stderr)
            return 1
        print(clean_result.ai_clean.text if clean_result.ai_clean else clean_result.scrape.text)
        return 0

    if mode == "summarize":
        url = args.url
        page = scrape_page(cfg, url, max_chars=max_chars, smart=smart, cache=cache, refresh=refresh)
        if not page.text:
            print(f"error: could not fetch {url}", file=sys.stderr)
            return 1
        summary = ai_summarize(cfg, page.text, model=model, budget_chars=budget_chars)
        print(summary.text)
        return 0

    if mode == "extract":
        urls = args.urls
        schema: dict[str, str] | None = None
        if args.schema is not None:
            try:
                parsed = json.loads(args.schema)
                if not isinstance(parsed, dict):
                    raise ValueError("schema must be a JSON object")
                schema = {
                    str(k): str(v) if isinstance(v, str) else "string" for k, v in parsed.items()
                }
            except (ValueError, TypeError) as exc:
                print(f"error: --schema is not valid JSON: {exc}", file=sys.stderr)
                return 2
        query = getattr(args, "query", None)
        if query:
            ext_result = ai_extract(
                cfg,
                query,
                urls,
                schema=schema or {},
                model=model,
                max_chars_per_page=max_chars,
                smart=smart,
                cache=cache,
                refresh=refresh,
                budget_chars=budget_chars,
            )
            print(ext_result.text)
            for src in ext_result.sources:
                print(f"  source: {src}", file=sys.stderr)
        else:
            results = scrape(
                cfg,
                urls,
                schema=schema,
                model=model,
                max_chars=max_chars,
                smart=smart,
                cache=cache,
                refresh=refresh,
                budget_chars=budget_chars,
                concurrency=concurrency,
                no_limit=no_limit,
            )
            for r in results:
                if r.ai_extract:
                    print(r.ai_extract.text)
                else:
                    print(json.dumps({"url": r.page.scrape.url, "text": r.page.scrape.text[:200]}))
        return 0

    if mode == "entities":
        url = args.url
        page = scrape_page(cfg, url, max_chars=max_chars, smart=smart, cache=cache, refresh=refresh)
        if not page.text:
            print(f"error: could not fetch {url}", file=sys.stderr)
            return 1
        ents = ai_extract_entities(cfg, page.text, model=model, budget_chars=budget_chars)
        print(ents.text)
        return 0

    if mode == "questions":
        url = args.url
        page = scrape_page(cfg, url, max_chars=max_chars, smart=smart, cache=cache, refresh=refresh)
        if not page.text:
            print(f"error: could not fetch {url}", file=sys.stderr)
            return 1
        q_result = ai_questionnaire(cfg, page.text, model=model, budget_chars=budget_chars)
        print(q_result.text)
        return 0

    if mode == "research":
        urls = args.urls
        query = args.query
        research_result = ai_research(
            cfg,
            query,
            urls,
            schema=None,
            model=model,
            max_chars_per_page=max_chars,
            smart=smart,
            cache=cache,
            refresh=refresh,
            budget_chars=budget_chars,
        )
        print(research_result.text)
        for src in research_result.sources:
            print(f"  source: {src}", file=sys.stderr)
        return 0

    if mode == "crawl":
        seeds = args.seeds
        query = args.query
        crawl_result = ai_crawl(
            cfg,
            seeds,
            query,
            max_pages=getattr(args, "max_pages", 50),
            max_depth=getattr(args, "max_depth", 2),
            model=model,
            budget_chars=budget_chars,
            smart=smart,
            concurrency=concurrency,
        )
        for rec in crawl_result.records:
            score = rec.ai_score.text if rec.ai_score else "no score"
            print(f"url: {rec.url} score: {score}")
            if rec.ai_distill:
                print(rec.ai_distill.text)
            print("---")
        return 0

    print(f"error: unknown ai subcommand: {mode}", file=sys.stderr)
    return 2


def cmd_research(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    """Fetch several pages (guarded, cached, budgeted) and print a cited answer."""
    # Sources can come from discovery, not just the operator's argv.
    cfg = _strict_guard(cfg)
    from .web_research import research_cli

    try:
        output = research_cli(
            cfg,
            urls=args.urls,
            query=args.query,
            fmt=args.format,
            max_pages=args.max_pages,
            max_chars_per_page=args.max_chars_per_page,
            total_budget=args.total_budget,
            cache_ttl=args.cache_ttl,
            no_cache=args.no_cache,
            refresh=args.refresh,
            fetch_concurrency=args.fetch_concurrency,
            evidence=args.evidence,
            ai_extract="auto" if args.ai else "never",
        )
    except (BridgeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


def cmd_verify(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    """Verify claims against fetched sources with per-claim statuses."""
    # --discover fetches URLs a search engine chose; stay strict.
    cfg = _strict_guard(cfg)
    from .claim_verify import verify_cli

    try:
        output = verify_cli(
            cfg,
            claims=args.claims,
            urls=args.urls,
            discover=args.discover,
            discovery_query=args.discovery_query,
            discovery_limit=args.discovery_limit,
            wiki_host=args.wiki,
            discovery_backend=args.discover_backend,
            fmt=args.format,
            max_pages=args.max_pages,
            max_chars_per_page=args.max_chars_per_page,
            total_budget=args.total_budget,
            cache_ttl=args.cache_ttl,
            no_cache=args.no_cache,
            refresh=args.refresh,
            fetch_concurrency=args.fetch_concurrency,
            progress=True,
        )
    except (BridgeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


def cmd_doctor(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    """Read-only diagnostics. Never sends a generation request."""
    from .gemini import account_prefix, load_cookie

    print(f"freegem v{__version__}\n")
    print("Configuration")
    for key, value in cfg.redacted().items():
        print(f"  {key:<20} {value}")
    print(f"  searched paths       {', '.join(config_search_paths())}")

    print("\nSession")
    if cfg.cookie_file:
        exists = os.path.exists(cfg.cookie_file)
        print(f"  cookie file          {'found' if exists else 'MISSING'} ({cfg.cookie_file})")
        if exists:
            cookie, sapisid = load_cookie(cfg)
            print(f"  cookie loaded        {'yes' if cookie else 'no'}")
            print(
                f"  sapisid              {'present' if sapisid else 'absent (auth will be anonymous)'}"
            )
            names = sorted({p.split("=", 1)[0].strip() for p in cookie.split(";") if "=" in p})
            print(
                f"  cookie names ({len(names):<3})    {', '.join(names[:8])}{' ...' if len(names) > 8 else ''}"
            )
        print(f"  account path prefix  {account_prefix(cfg) or '/'}")
        print(
            f"  xsrf token           {'set' if cfg.xsrf_token else 'NOT SET (authenticated requests may 400)'}"
        )
    else:
        print("  no cookie file configured: anonymous mode")

    print("\nConnectivity")
    from urllib.error import URLError
    from urllib.request import urlopen

    try:
        with urlopen("https://gemini.google.com/app", timeout=10) as response:
            print(f"  gemini.google.com    reachable (HTTP {response.status})")
    except URLError as exc:
        print(f"  gemini.google.com    UNREACHABLE ({exc.reason})")
        print("                         a proxy or VPN is likely required")
    except Exception as exc:
        print(f"  gemini.google.com    error ({exc})")

    print("\nModel resolution")
    bad = [name for name in (cfg.default_model,) if resolve_model(name)[3]]
    if bad:
        print(f"  ERROR: default model is invalid: {', '.join(bad)}")
    else:
        name, mode, think, _, _ = resolve_model(cfg.default_model, cfg.default_model)
        print(f"  default resolves to  {name} (mode={mode}, think={think})")

    ok = True
    if cfg.cookie_file and not cfg.has_session_cookie():
        ok = False
    if bad:
        ok = False
    print("\n" + ("All checks passed." if ok else "Some checks failed; see above."))
    return 0 if ok else 1


def _has_httpx() -> bool:
    from .gemini import HAS_HTTPX

    return HAS_HTTPX


# ── crawl ─────────────────────────────────────────────────────────────────


def _crawl_seeds(sources: list[str]) -> list[str]:
    """Flatten --seeds: literal URLs plus .txt/.jsonl files."""
    seeds: list[str] = []
    for source in sources:
        if source.startswith(("http://", "https://")):
            seeds.append(source)
            continue
        if not os.path.isfile(source):
            print(f"crawl: seed source not found: {source}", file=sys.stderr)
            continue
        with open(source, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if source.endswith(".jsonl"):
                    try:
                        data = json.loads(line)
                    except ValueError:
                        continue
                    url = str(data.get("url", "")).strip()
                    if url:
                        seeds.append(url)
                else:
                    seeds.append(line)
    return seeds


def _crawl_spec(args: argparse.Namespace) -> Any:
    from .crawler import CrawlSpec, ResourceLimits, Scope

    seeds = _crawl_seeds(list(args.seeds or []))
    scope = Scope(
        allow_domains=tuple(args.allow_domain or ()),
        deny_domains=tuple(args.deny_domain or ()),
        allow_paths=tuple(args.allow_path or ()),
        deny_paths=tuple(args.deny_path or ()),
        max_depth=max(0, args.max_depth),
        max_pages=max(1, args.max_pages),
        same_host_only=not args.any_host,
    )
    limits = ResourceLimits(
        timeout_sec=args.timeout,
        max_bytes=args.max_bytes,
        max_retries=max(0, args.max_retries),
        max_runtime_sec=args.max_runtime_sec,
    )
    return CrawlSpec(
        seeds=tuple(seeds),
        scope=scope,
        limits=limits,
        delay_ms=max(0, args.delay_ms),
        concurrency=max(1, min(args.concurrency, 8)),
        sitemaps=args.sitemap,
        output_path=getattr(args, "out", "") or "",
        output_format=getattr(args, "format", "jsonl"),
        append_output=False,
        schema_path=getattr(args, "schema", "") or "",
        artifacts_dir=getattr(args, "artifacts", "") or "",
        state_path=args.state or "",
        ai_rescue=bool(getattr(args, "ai_rescue", False)),
    )


def cmd_crawl(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    # A crawl frontier is discovered, never typed; the crawler stays guarded.
    cfg = _strict_guard(cfg)
    import threading
    import time as _time

    from .crawler import Frontier, load_schema
    from .crawler import plan as plan_crawl
    from .crawler import run as run_crawl

    mode = args.crawl_cmd
    if mode == "status":
        if not args.state or not os.path.exists(args.state):
            print("crawl status needs --state pointing at an existing state file", file=sys.stderr)
            return 2
        frontier = Frontier(args.state)
        try:
            stats = frontier.stats()
            meta_started = frontier.get_meta("started_at")
            meta_finished = frontier.get_meta("finished_at")
            print(
                json.dumps(
                    {**stats.as_dict(), "started_at": meta_started, "finished_at": meta_finished},
                    indent=2,
                )
            )
        finally:
            frontier.close()
        return 0

    if not args.seeds and mode in ("plan", "run"):
        print("crawl needs --seeds (URLs or seed files)", file=sys.stderr)
        return 2

    spec = _crawl_spec(args)
    if mode == "plan":
        report = plan_crawl(spec)
        print(json.dumps(report.as_dict(), indent=2))
        return 0

    if not spec.state_path:
        print("crawl run needs --state PATH (the frontier is how resume works)", file=sys.stderr)
        return 2

    # Validate the schema BEFORE any state file or worker exists: a typo in
    # the schema must be a clean error, not a half-started crawl.
    try:
        load_schema(spec.schema_path)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"crawl: bad schema: {exc}", file=sys.stderr)
        return 2

    if mode == "run":
        resume = bool(args.state and os.path.exists(args.state))
        if resume:
            # A resume continues the SAME crawl: seeds come from the frontier
            # (push is INSERT OR IGNORE anyway) and results append.
            spec.append_output = bool(spec.output_path)
    frontier = Frontier(spec.state_path)
    cancel = threading.Event()

    def _stop(_signum: object, _frame: object) -> None:
        cancel.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            with contextlib.suppress(ValueError, OSError):  # non-main thread
                signal.signal(sig, _stop)

    on_event = None
    if getattr(args, "json_logs", False):

        def on_event(payload: dict) -> None:
            print(json.dumps(payload, ensure_ascii=False), flush=True)

    started = _time.time()
    print(
        f"crawl: {len(spec.seeds)} seed(s), budget {spec.scope.max_pages} pages, "
        f"depth {spec.scope.max_depth}, state {spec.state_path}",
        file=sys.stderr,
    )
    try:
        counters = run_crawl(cfg, spec, frontier, cancel=cancel, on_event=on_event)
    except KeyboardInterrupt:  # pragma: no cover - signal path above
        cancel.set()
        counters = run_crawl(cfg, spec, frontier, cancel=cancel, on_event=on_event)
    finally:
        frontier.close()
    summary = counters.as_dict()
    summary["wall_sec"] = round(_time.time() - started, 1)
    print(json.dumps(summary, indent=2))
    return 0


def cmd_watch(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    """Watch one URL: baseline on the first run, then simhash + diff reports.

    Default (no --interval, or --once) is a single check so cron can call it
    once per invocation: exit 0 (baseline/no change), 3 (changed), 1 (fetch
    failure -- the fetch path is the ordinary guarded scraper, and its
    ScrapeError comes through here as an honest error line). With --interval
    the command loops until Ctrl-C and exits 0 on the interrupt; (#52) one
    failed poll inside the loop is a recorded line and a retry, not a crash.

    (#55) State management: ``--list`` shows every stored watch, ``--forget
    URL`` drops one baseline, ``--forget-all`` clears the state dir, and
    ``--max-age`` / ``--max-entries`` sweep stale entries before a check.
    """
    # Watch targets come from a config file, not the keyboard; stay strict.
    cfg = _strict_guard(cfg)
    from .watch import (
        forget_all_watches,
        forget_watch,
        list_watches,
        parse_interval,
        prune_watches,
        render_text,
        watch_poll,
    )

    # ── state-management modes (no URL, no fetch) ──
    if args.list:
        entries = list_watches()
        if not entries:
            print("no watches stored")
            return 0
        import time as _time

        for entry in entries:
            if entry["corrupt"]:
                print(
                    f"CORRUPT  {entry['path']}  ({entry['bytes']} bytes) -- use --forget-all or delete the file"
                )
                continue
            when = _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime(entry["checked_at"]))
            changed = (
                _time.strftime("%Y-%m-%d %H:%M:%S", _time.gmtime(entry["last_changed_at"]))
                if entry["last_changed_at"]
                else "never"
            )
            print(
                f"{when}  last changed {changed}  {entry['url']}\n  state: {entry['path']} ({entry['bytes']} bytes)"
            )
        return 0
    if args.forget_all:
        removed = forget_all_watches()
        print(f"forgot {removed} watch state file(s)")
        return 0
    if args.forget:
        if not args.url:
            print("error: --forget needs a URL argument", file=sys.stderr)
            return 2
        if forget_watch(args.url):
            print(f"forgot: {args.url}")
            return 0
        print(f"no stored watch for {args.url}")
        return 1

    if not args.url:
        print("error: watch needs a URL (or --list / --forget-all)", file=sys.stderr)
        return 2

    # ── optional pre-check sweeps (#55) ──
    max_age_sec: float | None = None
    if args.max_age is not None:
        try:
            max_age_sec = parse_interval(args.max_age)
        except ValueError as exc:
            print(f"error: --max-age {args.max_age!r}: {exc}", file=sys.stderr)
            return 2
    if max_age_sec is not None or args.max_entries is not None:
        removed = prune_watches(max_age_sec=max_age_sec, max_entries=args.max_entries)
        if removed:
            print(f"pruned {removed} stale watch state file(s)", file=sys.stderr)

    interval_sec: float | None = None
    if args.interval is not None:
        try:
            interval_sec = parse_interval(args.interval)
        except ValueError as exc:
            print(f"error: --interval {args.interval!r}: {exc}", file=sys.stderr)
            return 2
    single_check = args.once or interval_sec is None
    last = None
    saw_error = False
    try:
        for result in watch_poll(
            args.url,
            cfg,
            interval_sec=interval_sec if interval_sec is not None else 0.0,
            max_polls=1 if single_check else None,
        ):
            last = result
            if result.error is not None:
                saw_error = True
            if args.format == "json":
                print(json.dumps(result.to_dict(), ensure_ascii=False), flush=True)
            else:
                print(render_text(result, delta=args.delta))
    except KeyboardInterrupt:
        print("\nstopped.", file=sys.stderr)
        return 0
    except (BridgeError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if single_check and saw_error:
        return 1  # machine-grep-able "poll failed" for --once scripts
    if last is not None and last.changed:
        return 3  # machine-grep-able "changed" for --once scripts
    return 0


def _force_utf8_stdio() -> None:
    """Make stdout/stderr UTF-8 regardless of the platform locale.

    On Windows, a redirected pipe or file defaults to the ANSI code page
    (cp1252): the markdown renderers' check/cross marks -- or any non-Latin
    Gemini answer -- raised UnicodeEncodeError AFTER all the network work
    had completed, killing a documented output format at the last step.
    Reconfiguring to UTF-8 with replacement keeps the bytes flowing; modern
    terminals expect UTF-8, and everything older still gets readable output
    instead of a traceback.

    Also pins the child-process environment (PYTHONIOENCODING, PYTHONUTF8):
    every subprocess this process spawns inherits them, so scripts that
    shell out to freegem no longer decode our output through cp1252 and die
    on the first typographic quote (``UnicodeDecodeError: 'charmap' codec``).
    """
    # Children inherit the environment: set these BEFORE any output so a
    # subprocess spawned mid-run already sees UTF-8.
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # replaced/embedded streams without the TextIOWrapper API
        # detached or exotic stream: better best-effort than a crash
        with contextlib.suppress(ValueError, OSError):
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    raw = list(sys.argv[1:] if argv is None else argv)
    # A bare `freegem` used to reach cmd_serve with no serve flags on the
    # namespace (AttributeError on args.port). When no subcommand is present,
    # parse as if `serve` had been written. --help/--version keep the top-level
    # parser so the full command list stays visible.
    _commands = {
        "serve",
        "mcp",
        "ask",
        "models",
        "scrape",
        "research",
        "verify",
        "crawl",
        "ai",
        "watch",
        "doctor",
    }
    if not any(a in _commands for a in raw) and not any(
        a in ("-h", "--help", "--version") for a in raw
    ):
        raw.insert(0, "serve")
    parser = _build_parser()
    args = parser.parse_args(raw)

    config_path = getattr(args, "config", None) or find_config()
    # A config planted in a directory the user cd's into can silently point the
    # session at a hostile proxy. Load it (users rely on this) but say so.
    if (
        config_path
        and os.path.isfile(config_path)
        and os.path.abspath(config_path)
        == os.path.abspath(os.path.join(os.getcwd(), "config.json"))
    ):
        print(
            "WARNING: using ./config.json from the current directory. If you did "
            "not put it there yourself, do not trust it: a config file can route "
            "your Google session through a hostile proxy.",
            file=sys.stderr,
        )
    cfg = load_config(config_path)
    if config_path:
        # load_config tolerates a missing file (defaults apply), so only claim
        # "loaded" when the file is actually there.
        state = (
            "config loaded" if os.path.exists(config_path) else "config not found, using defaults"
        )
        log(cfg, f"{state}: {config_path}")
    cfg = _apply_overrides(cfg, args)

    command = args.command or "serve"
    handlers = {
        "serve": cmd_serve,
        "mcp": cmd_mcp,
        "ask": cmd_ask,
        "models": cmd_models,
        "scrape": cmd_scrape,
        "research": cmd_research,
        "verify": cmd_verify,
        "crawl": cmd_crawl,
        "ai": cmd_ai,
        "watch": cmd_watch,
        "doctor": cmd_doctor,
    }
    return handlers[command](cfg, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
