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
        metavar="{serve,mcp,ask,models,scrape,research,verify,crawl,watch,doctor}",
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

    status_p = crawl_sub.add_parser("status", help="show frontier stats for a state file")
    status_p.add_argument("--state", metavar="PATH", required=True, help="frontier SQLite file")

    watch = sub.add_parser(
        "watch",
        help="watch a URL and report what changed (simhash + line diff, local state)",
        parents=[common],
        epilog="exit codes: 0 baseline/no change (or clean Ctrl-C), 3 changed (--once), "
        "1 fetch/error, 2 usage",
    )
    watch.add_argument("url", metavar="URL", help="the page to watch (http/https)")
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


def cmd_serve(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    from .server import ThreadedServer

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
    answer, just printed whole. Any stream failure AFTER bytes were printed
    cannot be undone, so it surfaces as an error note instead of a rewrite.
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
        if printed:
            print(f"\n[stream interrupted: {exc}]", file=sys.stderr)
            raise
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
    """
    from .web_research import ResearchCache
    from .web_scraper import scrape_cli, scrape_many

    max_chars = args.max_chars
    if max_chars is None:
        # Question mode is the signal for "give me more content".
        max_chars = 200_000 if args.ask else 50_000
    smart = not args.no_smart and getattr(cfg, "smart_fetch", True)
    cache = None if args.no_cache else ResearchCache(ResearchCache.default_path(), args.cache_ttl)

    try:
        if len(args.url) == 1 and args.format == "text":
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
            )
            print(output)
            return 0

        if args.ask:
            print("error: --ask works with a single URL only", file=sys.stderr)
            return 2

        if len(args.url) == 1 and args.format == "markdown":
            # Single-URL markdown: render the extracted page as a document
            # (H1 title, body, optional link list) instead of the plain-text
            # wrapper. Same fetch path, guards, cache, and ladder as text.
            from .web_scraper import scrape_page

            page = scrape_page(
                cfg,
                args.url[0],
                max_chars=max_chars,
                ignore_robots=not args.respect_robots,
                smart=smart,
                cache=cache,
                refresh=args.refresh,
            )
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


def cmd_research(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    """Fetch several pages (guarded, cached, budgeted) and print a cited answer."""
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
        )
    except (BridgeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


def cmd_verify(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    """Verify claims against fetched sources with per-claim statuses."""
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
    )


def cmd_crawl(cfg: BridgeConfig, args: argparse.Namespace) -> int:
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
    the command loops until Ctrl-C and exits 0 on the interrupt.
    """
    from .watch import parse_interval, render_text, watch_poll

    interval_sec: float | None = None
    if args.interval is not None:
        try:
            interval_sec = parse_interval(args.interval)
        except ValueError as exc:
            print(f"error: --interval {args.interval!r}: {exc}", file=sys.stderr)
            return 2
    single_check = args.once or interval_sec is None
    last = None
    try:
        for result in watch_poll(
            args.url,
            cfg,
            interval_sec=interval_sec if interval_sec is not None else 0.0,
            max_polls=1 if single_check else None,
        ):
            last = result
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
        "watch": cmd_watch,
        "doctor": cmd_doctor,
    }
    return handlers[command](cfg, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
