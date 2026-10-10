# Community guidelines

Short version: the code is the reference for style, and the tests are the
reference for behaviour. Everything below exists to keep review cheap.

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
  should be testable without a credential.
- `gemini.py` is the **only** module that talks to Google. `multimodal.py` is
  the single exception, for the separate image-upload endpoint.
- `server.py` and `mcp_tools.py` are adapters. They translate, they do not
  implement protocol behaviour.

**No global mutable state.** `BridgeConfig` is a frozen dataclass; derive
variants with `.replace()`. This is not stylistic purity — the mutable-dict
version had a real concurrency bug, and a Cloudflare Workers port of this same
project shipped cookie cross-contamination in production because of it.

**Never log a secret.** `config.redacted()` exists for this. Cookies, the XSRF
token, and `SAPISID` must not appear in logs, errors, or tracebacks. Add a
credential-bearing field to `redacted()` in the same commit.

**No API keys.** Requests to add authentication will be closed. Nothing here is
worth protecting except the user's Google session, and a bearer token in front
of that is false comfort. The access control is the loopback bind.

## Testing

Every Google call is mocked. **No test may touch the network** — that is what
makes CI safe to run on a fork and safe to publish.

```bash
pytest                          # all tests
pytest --cov=freegem            # with coverage
pytest freegem/tests/test_gemini.py -k stream   # one test
```

Prefer a real endpoint over a unit of a private function.
`StreamingEndpointTests` starts an actual `ThreadedServer` on port 0 and speaks
HTTP to it; that catches framing and keep-alive bugs that mocking the handler
would not. Two such bugs shipped in the upstream code: an SSE response that
never signalled completion, and a hang on the Google-native stream.

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
Set `requires_cookie=True` for anything needing a session for real routing, and
`extra={...}` for experimental payload flags (see `gemini-3.1-pro-enhanced`).

Also update the README model table and `CHANGELOG.md`.

## Adding an endpoint

1. Add a branch in `GeminiHandler.do_POST` (or `do_GET`).
2. Translate the request in `tools.py` — a pure function, unit-tested directly.
3. Wire the response in `server.py`; keep protocol quirks out of `gemini.py`.

If the new endpoint is another OpenAI dialect, route it through the existing
`messages_to_prompt` rather than forking the prompt logic.

## Style

```bash
ruff check .
ruff format --check .
mypy freegem --ignore-missing-imports
```

Type hints are required on public functions (`disallow_untyped_defs`). Docstrings
explain *why*, not *what* — if a comment restates the code, delete it; if it
records a decision or a trap, keep it.

## Commits

Conventional prefixes, imperative mood, ~72 character subject:

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
- A change that makes a test hit the network.

## Reporting

**Do not file an issue containing your `gemini-auth.json` or any cookie value.**
It is a live Google credential. Describe the symptom, paste the output of
`freegem doctor` (it redacts secrets), and the exact error — never the
credential.

Security issues go to `SECURITY.md`, not a public issue.
