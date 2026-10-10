# Architecture

How the layers fit together, and the decisions behind them.

## Provenance

Derived from [`Sophomoresty/gemini-web2api`](https://github.com/Sophomoresty/gemini-web2api)
(MIT). The protocol details documented below — wire-format field positions,
response parsing, auth derivation, the model registry — were reverse-engineered
upstream and carried over. Where this project departs from upstream, this
document says so and gives the reason.

The single-file original was ~1,100 lines with a module-global config dict. This
is the same behaviour, layered so that protocol knowledge is confined to one
file and translation is testable without a credential.

## Layer map

```
┌──────────────────────────────────────────────────────────────┐
│  cli.py            argparse: serve | mcp | ask | scrape | models | doctor
├──────────────────────────────────────────────────────────────┤
│  server.py         OpenAI + Google-native HTTP faces         │
│  mcp_tools.py      MCP tool definitions and dispatch         │
├──────────────────────────────────────────────────────────────┤
│  tools.py          protocol translation ── PURE              │
├──────────────────────────────────────────────────────────────┤
│  gemini.py         Gemini StreamGenerate client              │
│  multimodal.py     Scotty resumable image upload             │
│  web_scraper.py    untrusted-page fetcher (robots, caps)     │
├──────────────────────────────────────────────────────────────┤
│  ssrf.py           shared guard: resolve, check, pin         │
├──────────────────────────────────────────────────────────────┤
│  config.py         frozen config, env overrides              │
│  models.py         model registry, @think parsing            │
└──────────────────────────────────────────────────────────────┘
```

Dependencies point downward only.

## Why each boundary exists

### `config.py` is a frozen dataclass

The original single-file implementation used a module-global `CONFIG` dict that
every module mutated in place:

```python
# original — any module, any thread
CONFIG["cookie_file"] = path
```

Under `ThreadingMixIn` that is a data race. Request A sets a per-request value,
request B overwrites it, and A now uses B's credential. This is not
hypothetical: a Cloudflare Workers port of this same project documented exactly
this failure — concurrent requests sharing a warm `Isolate`, one request's
cookie observed by another — and shipped a fix creating a per-request config
copy.

`BridgeConfig` is `@dataclass(frozen=True)`, so the only mutation path is
`.replace()`, which returns a new instance. A handler holding a reference
physically cannot see another request's change.

`redacted()` exists so log lines can dump the whole config without leaking
`xsrf_token` or the cookie path.

### `tools.py` is pure

Protocol translation — OpenAI messages, Google `contents`, tool schemas,
fenced call blocks — contains no network and no configuration. Consequences:

- Every translation is a table-driven unit test with no fixtures or mocking.
- Bugs in prompt assembly are reproducible without a Google session.
- The same functions serve both the HTTP and MCP faces, so a fix lands once.

Keep it pure. If you find yourself wanting config in `tools.py`, the logic
probably belongs in the caller.

### `gemini.py` is the only module that talks to Google (plus image upload)

All Google-specific knowledge lives here: cookie parsing, `SAPISIDHASH`
derivation, the wire format, response parsing, retries, build-tag scraping.
`multimodal.py` is the single exception, because the image upload endpoint is
separate from `StreamGenerate`.

If Google changes the protocol, the blast radius is one file.

### `ssrf.py` guards every fetch of an UNTRUSTED URL

Two surfaces accept URLs from outside (client-supplied image URLs in
`multimodal.py`, user/agent-supplied pages in `web_scraper.py`), and both go
through the same guard. The original approach — resolve the host, check that
every address is public, then let urllib connect — has a window: the connect
re-resolves the host, and an attacker-controlled DNS server can answer
differently the second time (DNS rebinding).

`ssrf.py` closes it by resolving once, checking every resolved address, and
pinning the dial to exactly that checked set — while TLS keeps using the
original hostname for SNI and certificate verification. Redirect hops are
re-validated because urllib redispatches each one through the same handlers.
If you add another place that fetches a URL a user or an agent provided, it
goes through `build_guarded_opener` too.

### `server.py` and `mcp_tools.py` are adapters

They map protocol shapes onto `generate()` / `generate_stream()` and back. They
contain no protocol *behaviour*. If you are writing parsing logic in `server.py`,
it belongs in `tools.py`.

## The wire format

Gemini's `StreamGenerate` body is a positional array. The positions are not
semantic — they were read out of the frontend bundle. Notable fields:

| Index | Meaning |
|---|---|
| `[0]` | prompt and attached file refs |
| `[17]` | thinking depth (`[[0]]` deepest … `[[4]]` auto) |
| `[41]` | chat persistence: `[1]` temporary, `[2]` persistent |
| `[45]` | set alongside `[41] = [1]` for temporary chats |
| `[59]` | per-request UUID |
| `[79]` | **model** selection (`MODE_CATEGORY`) |

`build_payload()` writes these positions explicitly rather than via a sparse
dict, so adding a field is a visible one-line change with a test.

## Response shape

Gemini streams **progressively longer snapshots of the same answer**, not true
deltas. Each `wrb.fr` line contains a JSON array whose `[4]` holds candidate
texts; the longest string in a line is the current best version.

The streaming algorithm:

```
emitted = ""                       # text already sent to the client
for each line:
    for each candidate text:
        if text == emitted or emitted.startswith(text):
            continue               # not new information
        if not text.startswith(emitted):
            abort                  # Gemini rewrote prior output
        yield clean(text[len(emitted):])
        emitted = text
```

The `startswith` guard is the important one. If a snapshot stops extending what
was already sent, continuing would either duplicate or rewrite content the
client has already rendered — so the stream raises instead. Silently emitting a
rewrite is the failure mode that produces garbled answers.

## Tool calling without tools

Gemini Web has no structured function-calling channel. The bridge:

1. Writes tool schemas into the prompt with a fenced-block calling convention.
2. Parses the reply back out of code fences.
3. Re-emits as real OpenAI `tool_calls` or Google `functionCall` parts.

Lossy by nature. Plain text streams normally; **streaming + tools is
emulated** — the full answer is generated, parsed, then replayed as one chunk,
because OpenAI clients expect complete tool calls and a partial fence is not
parseable.

`tools.py` handles three shapes a model may emit for a call: a proper fence,
the fence keyword without backticks, and bare JSON when the whole reply is the
call. The third exists because models occasionally ignore the fence entirely.

## HTTP framing

`server.py` sets `protocol_version = "HTTP/1.1"` (keep-alive) and sends
`Content-Length` on every non-SSE response.

SSE has no `Content-Length`, so the only end-of-body signal is connection close.
`_start_sse()` therefore sets `close_connection = True`. Without it, a client
blocks forever after the final `data: [DONE]` waiting for bytes that will never
come — this was observed in testing with `curl -N` hanging on the
Google-native stream.

## The build tag

`gemini_bl` identifies Gemini's frontend build. When Google rotates it,
requests fail. Two formats appear in the wild:

```
boq_assistant-bard-web-server_20260716.08_p0
boq_gemini-web-uiserver_20261001.12_p0
```

The original matched only the first and **silently gave up** otherwise — failure
to find was indistinguishable from success, so the auto-update appeared to work
while doing nothing. `fetch_build_tag()` returns `Optional[str]`, matches both,
and logs explicitly when nothing is found. `refresh_build_tag()` only overwrites
on a genuine find.

## Cookies and sessions

`load_cookie()` caches by mtime and accepts two shapes:

```json
{"cookie": "SID=...; SAPISID=...", "sapisid": "..."}
```

or a bare cookie string, from which `SAPISID` is split out.

On parse failure it returns the **last good value** rather than `""`. A
half-written file (the extension writes with `JSON.stringify` then triggers a
download, but a sync tool can truncate mid-write) should not silently downgrade
the session to anonymous and start tripping rate limits.

Auth headers when a SAPISID is present:

```
Authorization: SAPISIDHASH <unix_ts>_<sha1(f"{ts} {sapisid} https://gemini.google.com")>
```

## Test strategy

| Layer | Approach |
|---|---|
| `config`, `models` | Table-driven, no mocks |
| `tools` | Pure function assertions, no mocks |
| `gemini` | Mock `urlopen` / `httpx.Client`; assert payload bytes and retry counts |
| `server` | Real `ThreadedServer` on port 0, real HTTP over the socket |
| `cli` | `main(argv)` returns exit codes; capture stderr |
| `mcp_tools` | `run_tool()` dispatch with `generate` patched |

No test touches the network. That is a hard rule — it is what makes CI safe on
a public fork.
