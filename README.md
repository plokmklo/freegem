# FreeGem

## A local bridge to Gemini Web for APIs, developer tools, and AI agents

FreeGem is an unofficial, local-first bridge that exposes Gemini Web through the interfaces developers already use:

- **OpenAI-compatible HTTP API** for existing SDKs and applications
- **Google-native HTTP API** for Gemini-compatible clients
- **MCP server** for AI agents and developer tools
- **Command-line interface** for direct questions, web extraction, and research

FreeGem communicates with the Gemini Web session used by your browser. This allows compatible clients to connect through a familiar API surface without requiring a separate Gemini API key or an additional per-request API provider.

```text
OpenAI-compatible clients ─┐
Google-native clients     ─┼─► FreeGem ─► Gemini Web
MCP agents                ─┤
CLI users                 ─┘
```

## What FreeGem provides

### Compatible API access

Connect OpenAI-compatible tools and SDKs by changing the API base URL:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8081/v1",
    api_key="not-needed",
)

response = client.chat.completions.create(
    model="gemini-3.6-flash",
    messages=[{"role": "user", "content": "Explain quantum computing"}],
)

print(response.choices[0].message.content)
```

### Agent integration

Run FreeGem as an MCP server and expose Gemini capabilities to compatible AI agents:

```bash
pip install "freegem[mcp]"
freegem mcp --transport stdio
```

Available capabilities include:

- Current-information search with sources
- Direct questions and explanations
- Extended reasoning
- Structured decision advice
- Multi-turn conversations
- Web-page extraction
- Multi-source research with citations
- Model discovery

### Web extraction and research

FreeGem includes a guarded web extraction layer for reading public pages:

```bash
freegem scrape https://example.com
```

It also supports multi-source research from URLs supplied by the user:

```bash
freegem research \
  --query "Compare these two options and recommend one" \
  https://example.com/option-a \
  https://example.com/option-b \
  --format markdown
```

Research results can include:

- Inline source citations such as `[S1]` and `[S2]`
- Source metadata
- Local fetch caching
- Per-page and total-content budgets
- Markdown, text, and JSON output
- Deterministic fallback when Gemini is unavailable

`freegem research` compares the URLs provided by the user. It does not independently discover or search for URLs.

## Important project status

FreeGem is an unofficial integration with Gemini Web. It is intended for personal projects, development, experimentation, and learning.

Gemini Web sessions, availability, rate limits, account restrictions, and Google's policies still apply. This project is not affiliated with, sponsored by, or endorsed by Google.

## Project origin

FreeGem is an independent rebuild and extension of the Python project [`Sophomoresty/gemini-web2api`](https://github.com/Sophomoresty/gemini-web2api), released under the MIT License.

The upstream project provided important protocol research and implementation foundations, including the Gemini Web request format, model selection fields, response parsing, multimodal upload flow, and session-cookie tooling.

FreeGem preserves that attribution and adds a layered package architecture, multiple API faces, MCP integration, CLI workflows, security hardening, guarded web extraction, and cited multi-source research.

---

## Models

| Model | Mode | Notes |
|---|---|---|
| `gemini-3.7-flash` | FAST | Latest all-around |
| `gemini-3.6-flash` | FAST | All-around |
| `gemini-3.5-flash` | FAST | Alias for 3.6 Flash |
| `gemini-3.5-flash-thinking` | THINKING | Longest output (~20k chars) |
| `gemini-3.5-flash-thinking-lite` | DYNAMIC | Adaptive thinking depth |
| `gemini-3.1-pro` | PRO | Needs a session cookie for real routing |
| `gemini-3.1-pro-enhanced` | PRO | Experimental, extra output flags |
| `gemini-auto` | AUTO | Let Gemini pick |
| `gemini-flash-lite` | LITE | Fastest, lightweight |

**Thinking depth** — append `@think=N` to any model id:

```
gemini-3.5-flash-thinking@think=0   # deepest
gemini-3.5-flash-thinking@think=2   # medium
gemini-3.5-flash-thinking@think=4   # shallowest / auto
```

---

## Authentication

**Anonymous by default.** No key, no header, no setup.

**Optional session cookie** (`cookie_file`) improves things: anonymous requests
are rate-limited more aggressively, and `gemini-3.1-pro` silently routes to
Flash without a cookie. A cookie gives you the real model.

Get one with the bundled extension:

1. Load `gemini-cookie-sync-extension/` from `chrome://extensions` (Developer mode)
2. Sign in at <https://gemini.google.com/app> and reload
3. **Inspect session** → **Export gemini-auth.json**

Then either drop `gemini-auth.json` in the working directory (it is found
automatically) or point at it:

```bash
freegem serve --cookie-file ~/Downloads/gemini-auth.json
```

Check it without spending a request:

```bash
freegem doctor
```

> **There is no API-key authentication in this project, by design.** There is
> nothing here worth protecting *except your Google session*. If you must
> expose the bridge beyond loopback, put it behind a reverse proxy that does
> the auth. Do not add keys to this code.

---

## Configuration

Optional. Copy the template and edit:

```bash
cp config.example.json config.json
```

```json
{
  "host": "127.0.0.1",
  "port": 8081,
  "default_model": "gemini-3.6-flash",
  "cookie_file": null,
  "temporary_chats": true
}
```

| Field | Default | Meaning |
|---|---|---|
| `host` | `127.0.0.1` | Bind address. Non-loopback exposes your session |
| `port` | `8081` | Listen port |
| `default_model` | `gemini-3.6-flash` | Used when a request omits a model |
| `cookie_file` | `null` | Path to `gemini-auth.json` |
| `auth_user` | `null` | Account index for the `/u/N/` path |
| `xsrf_token` | `null` | `SNlM0e`; required when authenticated |
| `gemini_bl` | built-in | Frontend build tag; auto-refreshed on `serve` |
| `temporary_chats` | `false` | Keep requests out of your account history |
| `retry_attempts` | `3` | Retries on transient failure |
| `retry_delay_sec` | `2` | Base delay between retries; grows exponentially (2 s, 4 s, 8 s … capped at 30 s), and a server-sent `Retry-After` header wins when it asks for longer |
| `request_timeout_sec` | `180` | Per-request timeout |
| `proxy` | `null` | HTTP proxy if `gemini.google.com` is blocked |
| `log_requests` | `true` | Request log to stderr |

Environment variables override the file: `GEMINI_BL`, `GEMINI_AUTH_USER`,
`GEMINI_XSRF_TOKEN`, `GEMINI_PROXY`, `GEMINI_COOKIE_FILE`, `FREEGEM_CONFIG`.

Config is discovered in this order: `--config` flag → `$FREEGEM_CONFIG` →
`./config.json` → `./gemini-auth.json` → `~/.config/freegem/config.json`.

---

## Docker

The image is credential-free; mount the session at runtime.

```bash
docker compose up -d
```

```yaml
services:
  freegem:
    build: .
    ports: ["127.0.0.1:8081:8081"]
    environment:
      GEMINI_COOKIE_FILE: /secrets/gemini-auth.json
    volumes:
      - ./gemini-auth.json:/secrets/gemini-auth.json:ro
```

Or the MCP face:

```bash
docker compose --profile mcp up -d
```

---

## How it works

```
OpenAI client ─┐
Gemini CLI   ──┼─► freegem ─► Gemini Web StreamGenerate ─► Google
MCP agent    ──┘        │
                        └─ prompt translation (roles, tools, images → flat prompt)
                           Gemini wire format (positional array, field 79 = model)
                           response parsing (progressive snapshots → final text)
```

Requests go to the same `StreamGenerate` endpoint the Gemini web app uses. The
body is a positional array where `[79]` selects the model and `[17]` the
thinking depth — positions read out of Gemini's frontend bundle. Responses are
progressively longer snapshots of the same answer; the bridge yields the
difference between consecutive snapshots as the SSE delta.

**Notable implementation choices**, all covered by tests:

- **Immutable config.** `BridgeConfig` is a frozen dataclass. The original
  single-file version used a module-global mutable dict, which under a threaded
  server is a data race: one request's override could be observed by another
  mid-flight. A Cloudflare Workers port of this same project hit exactly that
  bug in production — cookie cross-contamination between concurrent requests —
  and fixed it the same way.
- **Both build-tag formats matched.** The original only matched
  `boq_assistant-bard-web-server_*` and *silently* gave up when Google switched
  to `boq_gemini-web-uiserver_*`. The auto-update looked like it worked because
  failure was indistinguishable from success. Both patterns are matched now, and
  an empty result is reported.
- **Stream corruption is detected, not hidden.** If a snapshot stops extending
  the already-emitted text, the stream aborts rather than silently rewriting
  what the client has accumulated.
- **Session is never silently dropped.** A mid-write cookie file falls back to
  the last good parse instead of degrading to anonymous.

---

## Limitations

Read this before you build on it.

- **Unofficial and unsupported.** This drives a private web endpoint. Google's
  Terms of Service generally prohibit automated access to the web frontend.
  Use for personal projects, learning, and prototyping — not as production
  infrastructure, and not for anything where an account ban would matter.
- **Stability is not yours.** When Google changes the endpoint, rotates the
  build tag, or tightens detection, this breaks until someone updates it. The
  official API has an SLA; this has maintainers.
- **"Pro" is a label without the right cookie.** Without a Gemini Advanced
  subscription cookie, `gemini-3.1-pro` routes to Flash. You get the name, not
  the model.
- **No real multi-turn state.** Every request is independent. History is
  concatenated into one prompt and re-sent each time, so costs and latency grow
  with conversation length, and Gemini never actually remembers the session.
- **Token counts are estimates.** The web endpoint returns no usage data, so
  `usage` is a character-count approximation (`len / 4`). Present so clients
  that read the field do not crash; not accurate.
- **Tool calling is prose, not protocol.** Tool schemas are written into the
  prompt and calls are scraped back out of markdown fences. It works, but it is
  prompt-engineering, not a real function-calling channel, and can be less
  reliable than a native API.
- **Images need an upload round-trip.** Multimodal input goes through Google's
  resumable upload endpoint and is more likely to need a cookie.
- **Rate limits are real.** Anonymous requests are throttled harder. Sustained
  heavy use can get the session blocked.
- **Streaming with tool calls is emulated.** OpenAI clients expect complete
  tool calls, so the full answer is generated, parsed, then replayed as one
  chunk. Plain text streams token-by-token.
- **The scraper fetches raw HTML.** `web_scrape` / `freegem scrape` render no
  JavaScript: SPA sites return whatever the server sends before hydration,
  which is often almost empty. That needs a headless browser and is
  deliberately out of scope.

---

## Development

```bash
git clone https://github.com/plokmklo/freegem
cd freegem
pip install -e ".[dev]"

ruff check .            # lint
ruff format --check .   # format
mypy freegem      # types
pytest                  # tests
```

The test suite runs entirely offline: every Gemini call is mocked, so CI needs
no credentials and no network. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the layering rules and
[`CONTRIBUTING.md`](CONTRIBUTING.md) for how to add a model or an endpoint.

### Layout

```
freegem/
  config.py       immutable config, env overrides
  models.py       model registry + @think parsing
  gemini.py       Gemini protocol client
  ssrf.py         shared SSRF guard: resolve, check, pin (DNS-rebinding safe)
  web_scraper.py  page fetching: robots, caps, HTML -> text
  web_research.py multi-source research: cache, budgets, [S#] citations
  multimodal.py   image upload
  tools.py        pure protocol translation (OpenAI/Google -> prompt)
  server.py       HTTP faces
  mcp_tools.py    MCP tools (dispatch is pure; build_server needs `mcp`)
  cli.py          argparse entry point
  tests/
gemini-cookie-sync-extension/   Chrome extension for exporting a session
docs/
```

---

## Credits

FreeGem is derived from and acknowledges the work in:

[`Sophomoresty/gemini-web2api`](https://github.com/Sophomoresty/gemini-web2api)

The upstream project is licensed under the MIT License and is Copyright © 2026 Sophomoresty.

The upstream project provided foundational research and implementation work for:

- Gemini Web's `StreamGenerate` request format
- Model selection and thinking-depth fields
- `wrb.fr` progressive response parsing
- `SAPISIDHASH` session authentication
- Gemini Web model routing
- Multimodal upload behavior
- Session-cookie synchronization
- The related Cloudflare Workers implementation

FreeGem is an independent rebuild and extension. Project-specific work includes:

- Layered Python package architecture
- OpenAI-compatible HTTP endpoints
- Google-native HTTP endpoints
- MCP server integration
- CLI commands
- Secure SSRF-guarded web extraction
- Robots-aware page fetching
- Multi-source research with citations
- Local research caching and content budgets
- Request validation and CSRF protections
- Thread limiting and retry backoff
- Cross-platform test coverage and CI workflows

FreeGem is not an official Google product and is not affiliated with or endorsed by Google.

## License

MIT — see [LICENSE](LICENSE).

Upstream's MIT copyright notice is retained in the spirit required by that
licence, and the files derived from it remain under the same terms.

## Disclaimer

Not affiliated with or endorsed by Google. "Gemini" is a trademark of Google
LLC. Use at your own risk and in accordance with Google's Terms of Service.
