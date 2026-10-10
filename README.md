# gemini-bridge

**Free Gemini, shaped as the APIs you already use.**

One bridge, three faces, no API key, no bill:

| Face | Protocol | Command |
|---|---|---|
| HTTP API | OpenAI-compatible + Google-native | `gemini-bridge serve` |
| Agent tools | MCP (Model Context Protocol) | `gemini-bridge mcp` |
| One-shot | plain CLI | `gemini-bridge ask "..."` |

Under the hood it talks to the same free Gemini web session your browser uses.
Outwardly it looks like OpenAI, so existing clients and code work by changing
one base URL.

---

## Contents

- [Quick start](#quick-start)
- [The three faces](#the-three-faces)
- [Models](#models)
- [Authentication](#authentication)
- [Configuration](#configuration)
- [Docker](#docker)
- [How it works](#how-it-works)
- [Limitations](#limitations)
- [Development](#development)

---

## Quick start

```bash
pip install gemini-bridge        # or: pip install "gemini-bridge[full]"
gemini-bridge serve
```

The server starts on `http://127.0.0.1:8081` — loopback only. Point any
OpenAI-compatible client at it:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8081/v1", api_key="not-needed")
resp = client.chat.completions.create(
    model="gemini-3.6-flash",
    messages=[{"role": "user", "content": "Explain quantum computing"}],
)
print(resp.choices[0].message.content)
```

Cherry Studio, ChatBox, Open WebUI, LangChain, Cline, anything that speaks
OpenAI — set the base URL and go.

**Try it before installing anything:**

```bash
gemini-bridge ask "what is the time in Tokyo?"
gemini-bridge ask --search "what changed in Python 3.14?"
gemini-bridge doctor
```

---

## The three faces

### 1. HTTP API — `gemini-bridge serve`

| Endpoint | For |
|---|---|
| `POST /v1/chat/completions` | OpenAI clients |
| `POST /v1/responses` | OpenAI Codex CLI |
| `GET  /v1/models` | OpenAI model discovery |
| `POST /v1beta/models/{model}:generateContent` | Gemini CLI, non-streaming |
| `POST /v1beta/models/{model}:streamGenerateContent` | Gemini CLI, streaming |
| `GET  /v1beta/models` | Google model discovery |
| `GET  /health` | Liveness and session status |

Codex CLI:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8081/v1
export OPENAI_API_KEY=not-needed
```

Gemini CLI:

```bash
export GEMINI_API_KEY=none
export GOOGLE_GEMINI_BASE_URL=http://127.0.0.1:8081
gemini
```

### 2. MCP tools — `gemini-bridge mcp`

Exposes Gemini as tools an agent can call:

| Tool | Use for |
|---|---|
| `gemini_search` | Current facts; must cite sources |
| `gemini_ask` | Fast direct answers |
| `gemini_deep_think` | Long-form reasoning on hard problems |
| `gemini_advisor` | Structured second opinion on a decision |
| `gemini_chat` | Raw multi-turn, full control |
| `gemini_list_models` | Registry introspection |

```bash
pip install "gemini-bridge[mcp]"

# stdio: your client launches it
gemini-bridge mcp --transport stdio

# sse: it stays up and clients connect over HTTP
gemini-bridge mcp --transport sse --port 8768
```

Client config (`.mcp.example.json`):

```json
{
  "mcpServers": {
    "gemini-bridge": {
      "type": "stdio",
      "command": "gemini-bridge",
      "args": ["mcp", "--transport", "stdio"]
    }
  }
}
```

### 3. One-shot CLI

```bash
gemini-bridge ask "summarise the plot of Dune"        # direct answer
gemini-bridge ask --search "latest Python release"    # web-grounded + sources
gemini-bridge models                                  # registry + session state
gemini-bridge doctor                                  # diagnostics, no generation
```

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
gemini-bridge serve --cookie-file ~/Downloads/gemini-auth.json
```

Check it without spending a request:

```bash
gemini-bridge doctor
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
| `retry_delay_sec` | `2` | Delay between retries |
| `request_timeout_sec` | `180` | Per-request timeout |
| `proxy` | `null` | HTTP proxy if `gemini.google.com` is blocked |
| `log_requests` | `true` | Request log to stderr |

Environment variables override the file: `GEMINI_BL`, `GEMINI_AUTH_USER`,
`GEMINI_XSRF_TOKEN`, `GEMINI_PROXY`, `GEMINI_COOKIE_FILE`, `GEMINI_BRIDGE_CONFIG`.

Config is discovered in this order: `--config` flag → `$GEMINI_BRIDGE_CONFIG` →
`./config.json` → `./gemini-auth.json` → `~/.config/gemini-bridge/config.json`.

---

## Docker

The image is credential-free; mount the session at runtime.

```bash
docker compose up -d
```

```yaml
services:
  gemini-bridge:
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
Gemini CLI   ──┼─► gemini-bridge ─► Gemini Web StreamGenerate ─► Google
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

---

## Development

```bash
git clone https://github.com/plokmklo/gemini-bridge
cd gemini-bridge
pip install -e ".[dev]"

ruff check .            # lint
ruff format --check .   # format
mypy gemini_bridge      # types
pytest                  # tests
```

The test suite runs entirely offline: every Gemini call is mocked, so CI needs
no credentials and no network. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the layering rules and
[`CONTRIBUTING.md`](CONTRIBUTING.md) for how to add a model or an endpoint.

### Layout

```
gemini_bridge/
  config.py       immutable config, env overrides
  models.py       model registry + @think parsing
  gemini.py       Gemini protocol client (the only module that does network)
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

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Not affiliated with or endorsed by Google. "Gemini" is a trademark of Google
LLC. Use at your own risk and in accordance with Google's Terms of Service.
