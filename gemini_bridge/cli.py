"""Unified command-line entry point.

One command runs either face of the bridge:

    gemini-bridge serve   # OpenAI + Google-native HTTP API
    gemini-bridge mcp     # MCP server for agents

Plus one-shot utilities for checking a setup without starting a server:
    gemini-bridge ask "what is the time in Tokyo?"
    gemini-bridge models
    gemini-bridge doctor
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from . import __version__
from .config import BridgeConfig, config_search_paths, find_config, load_config
from .gemini import BridgeError, log, refresh_build_tag
from .models import MODELS, resolve_model
from .mcp_tools import build_server, run_tool


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gemini-bridge",
        description="Free Gemini, shaped as the APIs you already use.",
    )
    parser.add_argument("--version", action="version", version=f"gemini-bridge {__version__}")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="config file (default: search ./config.json, ./gemini-auth.json, "
        "~/.config/gemini-bridge/config.json, $GEMINI_BRIDGE_CONFIG)",
    )
    parser.add_argument("--model", metavar="ID", help="default model id")
    parser.add_argument("--cookie-file", metavar="PATH", help="session cookie file")
    parser.add_argument("--proxy", metavar="URL", help="HTTP proxy, e.g. http://127.0.0.1:7890")
    parser.add_argument("--quiet", action="store_true", help="suppress request logging")

    sub = parser.add_subparsers(dest="command", metavar="{serve,mcp,ask,models,doctor}")

    serve = sub.add_parser("serve", help="run the HTTP API server")
    serve.add_argument("--port", type=int, help="port to listen on (default 8081)")
    serve.add_argument("--host", metavar="ADDR", help="bind address (default 127.0.0.1)")
    serve.add_argument(
        "--no-refresh",
        action="store_true",
        help="skip the startup build-tag refresh",
    )

    mcp = sub.add_parser("mcp", help="run the MCP server")
    mcp.add_argument(
        "--transport", choices=["stdio", "sse"], default="stdio", help="(default: stdio)"
    )
    mcp.add_argument("--host", default="127.0.0.1", help="bind address for sse")
    mcp.add_argument("--port", type=int, default=8768, help="port for sse")
    mcp.add_argument("--name", default="gemini-bridge", help="server name shown to clients")

    ask = sub.add_parser("ask", help="ask one question and print the answer")
    ask.add_argument("prompt", nargs="+", help="the question")
    ask.add_argument("--search", action="store_true", help="ground in the web and cite sources")

    sub.add_parser("models", help="list available models")

    sub.add_parser("doctor", help="check session, build tag, and network reachability")

    return parser


def _apply_overrides(cfg: BridgeConfig, args: argparse.Namespace) -> BridgeConfig:
    if args.model:
        cfg = cfg.replace(default_model=args.model)
    if args.cookie_file:
        cfg = cfg.replace(cookie_file=os.path.abspath(args.cookie_file))
    if args.proxy:
        cfg = cfg.replace(proxy=args.proxy)
    if args.quiet:
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

    print(f"gemini-bridge v{__version__}")
    print(f"  OpenAI:    http://{cfg.host}:{cfg.port}/v1/chat/completions")
    print(f"  Google:    http://{cfg.host}:{cfg.port}/v1beta/models/<model>:generateContent")
    print(f"  Health:    http://{cfg.host}:{cfg.port}/health")
    print(f"  Models:    {', '.join(MODELS)}")
    print(f"  Session:   {'cookie configured' if cfg.has_session_cookie() else 'anonymous'}")
    print(f"  Proxy:     {cfg.proxy or 'system env'}")
    print(f"  Streaming: {'token-by-token' if _has_httpx() else 'whole reply (install httpx)'}")
    print(f"  Temporary: {'yes' if cfg.temporary_chats else 'no'}")
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        warning = (
            "\n  WARNING: bound to a non-loopback address ({host}).\n"
            "  This exposes your Google session to the network.\n"
            "  Anyone who can reach this port can spend your Gemini quota.\n"
        ).format(host=cfg.host)
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


def cmd_ask(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    question = " ".join(args.prompt).strip()
    if not question:
        print("nothing to ask", file=sys.stderr)
        return 2
    try:
        if args.search:
            answer = run_tool(cfg, "gemini_search", {"query": question})
        else:
            answer = run_tool(cfg, "gemini_ask", {"question": question})
    except BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(answer)
    return 0


def cmd_models(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    from .mcp_tools import model_help

    print(model_help(cfg))
    return 0


def cmd_doctor(cfg: BridgeConfig, args: argparse.Namespace) -> int:
    """Read-only diagnostics. Never sends a generation request."""
    from .gemini import account_prefix, load_cookie

    print(f"gemini-bridge v{__version__}\n")
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
            print(f"  sapisid              {'present' if sapisid else 'absent (auth will be anonymous)'}")
            names = sorted({p.split('=', 1)[0].strip() for p in cookie.split(';') if '=' in p})
            print(f"  cookie names ({len(names):<3})    {', '.join(names[:8])}{' ...' if len(names) > 8 else ''}")
        print(f"  account path prefix  {account_prefix(cfg) or '/'}")
        print(f"  xsrf token           {'set' if cfg.xsrf_token else 'NOT SET (authenticated requests may 400)'}")
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
    except Exception as exc:  # noqa: BLE001
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    config_path = args.config or find_config()
    cfg = load_config(config_path)
    if config_path:
        log(cfg, f"config loaded: {config_path}")
    cfg = _apply_overrides(cfg, args)

    command = args.command or "serve"
    handlers = {
        "serve": cmd_serve,
        "mcp": cmd_mcp,
        "ask": cmd_ask,
        "models": cmd_models,
        "doctor": cmd_doctor,
    }
    return handlers[command](cfg, args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
