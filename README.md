# FreeGem

**A local bridge for Gemini Web, presented through the interfaces you already use.**

FreeGem provides three compatible ways to work with Gemini:

| Face | Protocol | Command |
|---|---|---|
| HTTP API | OpenAI-compatible + Google-native | `freegem serve` |
| Agent tools | MCP (Model Context Protocol) | `freegem mcp` |
| One-shot | plain CLI | `freegem ask "..."` |

Under the hood, FreeGem communicates with the Gemini Web session used by your
browser. On the client side, it exposes familiar protocols so existing tools
can usually be connected by changing a single base URL. It requires no
separate Gemini API key and introduces no additional per-request API charge,
although Gemini Web sessions remain subject to Google's availability,
rate-limits, policies, and account restrictions.

> **Project origin and attribution:** FreeGem is a maintained rebuild and
> extension of [`Sophomoresty/gemini-web2api`](https://github.com/Sophomoresty/gemini-web2api),
> released under the MIT License. The upstream project's protocol research and
> implementation knowledge remain fundamental to this project. See
> [Credits](#credits) for detailed attribution.

---

## Contents

- [Quick start](#quick-start)
- [The three faces](#the-three-faces)
- [Web scraping](#web-scraping)
- [Models](#models)
- [Authentication](#authentication)
- [Configuration](#configuration)
- [Docker](#docker)
- [How it works](#how-it-works)
- [Limitations](#limitations)
- [Development](#development)
- [Credits](#credits)
- [License](#license)
- [Disclaimer](#disclaimer)

---

## Quick start

```bash
pip install freegem        # or: pip install "freegem[full]"
freegem serve
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
freegem ask "what is the time in Tokyo?"
freegem ask --search "what changed in Python 3.14?"
freegem doctor
```

---

## The three faces

### 1. HTTP API — `freegem serve`

| Endpoint | For |
|---|---|
| `POST /v1/chat/completions` | OpenAI clients |
| `POST /v1/responses` | OpenAI Codex CLI |
| `GET  /v1/models` | OpenAI model discovery |
| `POST /v1beta/models/{model}:generateContent` | Gemini CLI, non-streaming |
| `POST /v1beta/models/{model}:streamGenerateContent` | Gemini CLI, streaming |
| `GET  /v1beta/models` | Google model discovery |
| `GET  /health` | Liveness and session status |

The server is loopback-only and assumes no auth, so it hardens its own
surface: POST bodies must be `Content-Type: application/json`, any request
carrying a non-loopback `Origin` header is refused with 403 (blocks
cross-site "simple" POSTs that skip CORS preflight), concurrent requests are
capped (default 32; extra connections get 503 instead of an unbounded thread
count), and a stalled client cannot hold a thread forever (65 s read timeout,
reported as 408).

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

### 2. MCP tools — `freegem mcp`

Exposes Gemini as tools an agent can call:

| Tool | Use for |
|---|---|
| `gemini_search` | Current facts; must cite sources |
| `gemini_ask` | Fast direct answers |
| `gemini_deep_think` | Long-form reasoning on hard problems |
| `gemini_advisor` | Structured second opinion on a decision |
| `gemini_chat` | Raw multi-turn, full control |
| `gemini_list_models` | Registry introspection |
| `web_scrape` | Read a web page (SSRF-guarded, robots-aware) |
| `web_research` | Answer one question from several pages, cited [S1]..[Sn] |

```bash
pip install "freegem[mcp]"

# stdio: your client launches it
freegem mcp --transport stdio

# sse: it stays up and clients connect over HTTP
freegem mcp --transport sse --port 8768
```

Client config (`.mcp.example.json`):

```json
{
  "mcpServers": {
    "freegem": {
      "type": "stdio",
      "command": "freegem",
      "args": ["mcp", "--transport", "stdio"]
    }
  }
}
```

### 3. One-shot CLI

```bash
freegem ask "summarise the plot of Dune"        # direct answer
freegem ask --search "latest Python release"    # web-grounded + sources
freegem scrape https://example.com --links      # read a web page
freegem models                                  # registry + session state
freegem doctor                                  # diagnostics, no generation
```

---

## Web scraping

`freegem scrape <url>` fetches a page and prints readable text — title, body,
no scripts, styles, or nav junk. The same capability is exposed to agents as
the `web_scrape` MCP tool.

```bash
freegem scrape https://example.com                     # text + title
freegem scrape https://example.com --links             # + resolved links
freegem scrape https://example.com --max-chars 5000    # cap the output
freegem scrape https://example.com --ask "what is this site?"
freegem scrape https://example.com --ignore-robots     # CLI only; MCP cannot
```

**Security model** — this fetches *untrusted* URLs, so it reuses and hardens
the project's SSRF defenses (`freegem/ssrf.py`):

- Every URL and every redirect hop is **resolved, checked, and pinned**: the
  connection dials only the verified-public IPs the check inspected, closing
  the resolve→check→reconnect window (DNS rebinding) the image fetcher
  previously had. TLS keeps verifying the original hostname (SNI + certs).
- Private ranges, loopback, link-local (including `169.254.169.254`),
  reserved, and multicast targets are refused before any socket exists, as
  are `file://`, `ftp://`, and URLs with embedded credentials.
- The session cookie is **never** sent to scraped sites.
- Responses are capped at 5 MB on the wire *and* after decompression, so gzip
  bombs die at the cap instead of in RAM. Only HTML and text are accepted.
- `robots.txt` is honored by default. The MCP tool cannot bypass it —
  `--ignore-robots` exists only on the interactive CLI.
- Output is wrapped in `[UNTRUSTED WEB CONTENT]` markers, and `--ask` mode
  instructs Gemini to treat everything between them as data, never as
  instructions. A page can still contain text aimed at your agent; the
  markers limit, but do not eliminate, that injection surface.

**Limitations**: no JavaScript rendering (React/SPA sites come back mostly
empty — that needs a headless browser, deliberately not a dependency), sites
that block non-browser agents stay blocked (the UA is honest), and scraping
some sites violates their terms even when `robots.txt` allows it.

## Web research

`freegem research` is the layer above `scrape`: provide a question and the
specific URLs you want compared. It does not discover or search for URLs on
your behalf. FreeGem fetches the supplied pages with the same guarded,
robots-aware, capped machinery, then returns one answer with inline `[S1]`,
`[S2]` citations, key points, and a disagreements-and-gaps section. Agents get
the same capability through the `web_research` MCP tool.

```bash
freegem research \
  --query "Which option fits a small Python API?" \
  https://docs.example.com/option-a \
  https://docs.example.com/option-b \
  --format markdown
```

What it does beyond N scrapes:

- **Local fetch cache** — successful fetches are stored under
  `~/.cache/freegem/` (30-minute TTL, `$FREEGEM_CACHE_DIR` to relocate), so
  repeated agent calls do not re-download pages. `--no-cache` / `--refresh`
  control it. robots.txt and the SSRF guard still run on every real fetch.
- **Hard budgets** — at most 10 pages per call, a per-page character cap
  (`--max-chars-per-page`, default 12 000), and a total fetched-character
  budget (`--total-budget`, default 80 000). URLs that would push the run
  over budget are reported as skipped, not fetched.
- **Bounded prompts** — small material goes to Gemini in one pass; larger
  material takes a map/reduce path (capped per-page notes, then a reduce
  pass), so prompt size does not grow with raw page size.
- **Authoritative citations** — the `[S#] -> URL` table is rendered from
  freegem's own fetch metadata, never from model text, and an answer citing
  sources that do not exist is rejected into the raw-extract fallback.
- **Never fails on Gemini** — with no session or on rate limits, you get the
  labeled raw extracts plus fetch metadata instead of an error; a dead source
  is recorded per-URL and the rest of the run continues.
- **Untrusted end to end** — every page stays inside
  `[UNTRUSTED WEB CONTENT]` markers in every prompt. Folding several
  attacker-writable pages into one prompt widens the injection surface; the
  markers limit, but do not eliminate, it.
- **Formats** — `--format text|markdown|json`; the JSON schema documents
  per-source status (`ok`, `error`, `truncated`, `from_cache`, `skipped`,
  `chars`) and run metadata (cache hits, failed pages, timing, version).

Not a crawler: hand it the specific pages to compare. No JavaScript rendering
(same posture as the scraper), and cache entries store extracted text only —
never cookies or credentials.

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
| `proxy` | `null` | HTTP proxy if `gemini.google.com` is blocked; proxy-side DNS means local IP pinning cannot be guaranteed |
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
no credentials and no network. The current suite contains 366 tests. See
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

FreeGem is derived from and acknowledges the work in
[`Sophomoresty/gemini-web2api`](https://github.com/Sophomoresty/gemini-web2api),
licensed under the MIT License (© 2026 Sophomoresty). FreeGem is an
independent rebuild and extension of that Python project, not an official
Google product or an official upstream release.

The following foundational work came from upstream unchanged or with limited
adaptation:

- **The Gemini Web protocol implementation** — the `StreamGenerate`
  positional-array wire format, `[79]` as the model selector, `[17]` as the
  thinking depth, `SAPISIDHASH` authentication, and the `wrb.fr`
  progressive-snapshot response shape.
- **The model registry** — the `MODE_CATEGORY` mapping
  (1=FAST, 2=THINKING, 3=PRO, 4=AUTO, 5=DYNAMIC_THINKING, 6=FLASH_LITE) and the
  `gemini-*` model ids.
- **The multimodal path** — Gemini's Scotty resumable-upload protocol and the
  page-token scraping it depends on.
- **The cookie-sync extension** — `gemini-cookie-sync-extension/` is a lightly
  adapted copy of upstream's, with the export filename and project name
  updated.
- **The Cloudflare Workers port** — upstream's `cloudflare/worker.js` informed
  the request-scoped-config design, which was itself the fix for a
  cookie-cross-contamination bug under concurrent warm-isolate requests.

FreeGem adds and maintains the following project-specific work:

- A layered package layout (`config` / `models` / `gemini` / `multimodal` /
  `tools` / `server` / `mcp_tools` / `cli`) in place of the single-file
  original.
- Immutable, request-safe configuration instead of a module-global mutable dict.
- A unified CLI running the HTTP, MCP, and one-shot faces.
- `gemini_advisor` and the `gemini_deep_think` output contract.
- A 366-test suite that runs offline with no credentials.

Upstream's own README credits its development agent capability to
[GenericAgent](https://github.com/lsdefine/GenericAgent), originally shared via
the [linux.do](https://linux.do) community — credit passed along here.

Also worth knowing: an unrelated Go implementation publishes under the same
name ([`ikhsan3adi/gemini-web2api`](https://github.com/ikhsan3adi/gemini-web2api)).
It is a separate project; this one derives only from the Python original.

## License

MIT — see [LICENSE](LICENSE).

Upstream's MIT copyright notice is retained in the spirit required by that
licence, and the files derived from it remain under the same terms.

## Disclaimer

Not affiliated with or endorsed by Google. "Gemini" is a trademark of Google
LLC. Use at your own risk and in accordance with Google's Terms of Service.
