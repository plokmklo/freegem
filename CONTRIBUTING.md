# Contributing

Thanks for helping out. This document covers the rules that keep the codebase
honest; the code itself is the reference for style.

## Ground rules

**The layering is strict.** One direction of dependency:

```
cli.py ──► mcp_tools.py ──┐
                          ├──► tools.py ──► (pure, no imports from the package)
server.py ────────────────┤
                          └──► gemini.py ──► multimodal.py
                                   │
                                config.py ──► models.py
```

- `config.py` and `models.py` import nothing from the package.
- `tools.py` is **pure**: no network, no config, no I/O. Protocol translation
  should be testable without a credential. Keep it that way.
- `gemini.py` is the **only** module that talks to Google. `multimodal.py` is
  the only other module that makes requests, and only for image upload.
- `server.py` and `mcp_tools.py` are protocol adapters. They translate, they do
  not implement protocol behaviour.

**No global mutable state.** `BridgeConfig` is a frozen dataclass; derive
variants with `.replace()`. This is not stylistic purity — the mutable-dict
version had a real concurrency bug, and the Workers port of this project hit
cookie cross-contamination in production because of it.

**Never log a secret.** `config.redacted()` exists for this. Cookies, the XSRF
token, and SAPISID must not appear in logs, errors, or tracebacks. If you add a
credential-bearing field, add it to `redacted()` in the same commit.

## No API keys

If you are considering adding authentication: don't. There is nothing in this
project worth protecting except the user's Google session, and a bearer token
in front of that provides false comfort. The access control is the loopback
bind. Point people at a reverse proxy instead.

## Testing

Everything is mocked. **No test may hit the network or need a credential** —
that is what makes CI safe to run on a fork and safe to publish.

```bash
pytest                          # all tests
pytest --cov=freegem      # with coverage
pytest freegem/tests/test_gemini.py -k stream   # one test
```

When adding a test, prefer a real endpoint over a unit of a private function.
`StreamingEndpointTests` starts an actual `ThreadedServer` on port 0 and speaks
HTTP to it; that catches framing and keep-alive bugs that mocking the handler
would not.

## Adding a model

Edit `MODELS` in `freegem/models.py`:

```python
"gemini-4.0-flash": ModelSpec(
    mode=1,
    think=4,
    desc="Description shown in /v1/models and tool listings",
),
```

`mode` is Gemini's frontend `MODE_CATEGORY` enum:
`1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=FAST_DYNAMIC_THINKING, 6=FLASH_LITE`.
Set `requires_cookie=True` for anything that needs a session for real routing,
and `extra={...}` for experimental payload flags (see `gemini-3.1-pro-enhanced`).

Add it to the README table too.

## Adding an endpoint

1. Add a branch in `GeminiHandler.do_POST` (or `do_GET`).
2. Translate the request in `tools.py` — pure function, unit-test it directly.
3. Wire the response in `server.py`; keep protocol quirks out of `gemini.py`.

If the new endpoint is another OpenAI dialect, make it fall back through the
existing `messages_to_prompt` rather than forking the prompt logic.

## Style

```bash
ruff check .
ruff format --check .
mypy freegem
```

Type hints are required on public functions (`disallow_untyped_defs`). Docstrings
explain *why*, not *what* — if a comment restates the code, delete it; if it
records a decision or a trap, keep it.

## Commits

Conventional prefixes, imperative mood, ~72 char subject:

```
fix(gemini): match both build-tag formats

The auto-refresh only matched boq_assistant-bard-web-server_* and silently
gave up when Google switched to boq_gemini-web-uiserver_*, so a stale tag
looked like a working one. Match both and report an empty result.
```

## What not to send

- Authentication or API-key support (see above).
- New third-party dependencies for the core. `httpx` and `mcp` are optional
  extras precisely so the bridge installs and runs with zero dependencies.
- Vendor-specific hacks that only work for one client version.
- Anything that weakens the loopback default.

## Reporting

**Do not file an issue containing your `gemini-auth.json` or any cookie value.**
It is a live Google credential. Describe the symptom, the output of
`freegem doctor`, and the error text — never the credential.

When reporting a failure, include:

```
freegem --version
freegem doctor
```

plus the exact error message and whether it reproduces anonymously.
