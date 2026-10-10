# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [2.0.0] — 2026-10-10

Rebuild of the upstream `gemini-web2api` design into a maintainable package.
See [Credits](README.md#credits): the Gemini Web protocol knowledge, the model
registry, the multimodal upload flow, and the cookie-sync extension all come
from that project.

### Added

- **Three faces, one command.** `freegem serve` (OpenAI + Google-native HTTP
  API), `freegem mcp` (agent tools), and a one-shot CLI (`ask`, `models`,
  `doctor`) all run through one entry point.
- **`gemini_advisor`** MCP tool: a structured second opinion returned as
  Recommendation / Options / Tradeoffs / Risks / What would change my mind.
- **`doctor`** command: reports session state, cookie parse result, XSRF
  presence, connectivity, and model resolution — without spending a request.
- **177 tests** covering config, model resolution, the protocol client, prompt
  translation, HTTP endpoints, the CLI, and MCP dispatch. No test touches the
  network, so CI needs no credentials.
- Docker image and `docker-compose.yml` with an optional MCP profile. The image
  carries no credentials; the session is mounted read-only at runtime.
- `docs/ARCHITECTURE.md` recording the layering rules and the reasoning behind
  each boundary.
- `CONTRIBUTING.md`, `SECURITY.md`, and GitHub issue/PR templates.
- Editable install with full type hints (`py.typed`) and strict `mypy`.

### Changed

- **Configuration is immutable.** `BridgeConfig` is a frozen dataclass. The
  upstream single-file version used a module-global mutable dict, which under a
  threaded server is a data race: one request's override can be observed by
  another mid-flight. A Cloudflare Workers port of the same project shipped
  that exact bug as cookie cross-contamination between concurrent requests.
- **Build-tag refresh actually works.** Upstream matched only
  `boq_assistant-bard-web-server_*` and *silently* gave up when Google moved to
  `boq_gemini-web-uiserver_*`, so the auto-update appeared functional while
  doing nothing. Both patterns are matched, newer format preferred, and an
  empty result is logged instead of passing for success.
- **Default bind is loopback** (`127.0.0.1`), not `0.0.0.0`. Since this project
  has no authentication layer, an open bind would expose the Google session to
  the whole network.
- **Authentication removed entirely.** There is no `api_keys` field and no
  bearer-token path. Access control is the loopback bind; a test asserts the
  field does not come back.
- **SSE terminates correctly.** The OpenAI `[DONE]` sentinel is emitted as a
  bare `data: [DONE]` line (upstream wrapped it in JSON, so no OpenAI client
  detected end-of-stream), and the socket is closed, since an SSE response has
  no `Content-Length` and clients otherwise block waiting for bytes that never
  arrive.
- **Function-call parsing handles nested arguments.** Upstream's non-greedy
  `[^`]*?}` regex stopped at the first `}`, truncating
  `{"city":"Lima","opts":{...}}` and dropping the call. Brace balancing now
  respects string literals, and two consecutive calls are both parsed instead
  of the second leaking into the reply text.
- **Stream corruption aborts instead of hiding.** If a snapshot stops extending
  already-emitted text, the stream raises rather than silently rewriting what
  the client has rendered.
- **A half-written cookie file no longer drops the session.** On parse failure
  the last good value is kept rather than degrading to anonymous.
- `gemini-web2api` → `freegem` throughout: package, command, imports, config
  paths, and the `GEMINI_BRIDGE_CONFIG` → `FREEGEM_CONFIG` environment variable.

### Removed

- Dead `GEMINI_BRIDGE_HOST` / `GEMINI_BRIDGE_PORT` environment variables, which
  were set by the Docker files and never read. Host comes from `--host` or
  `config.json` only.

### Fixed

- SSE hang on the Google-native stream (`curl -N` blocked indefinitely after
  the final chunk).
- Tool-call response missing `tool_calls` when the model emitted a fence
  without surrounding prose.
- Unknown model names falling back silently without any log line, making a
  typo indistinguishable from a routing decision.

### Security

- **The image upload no longer sends the session cookie to an arbitrary host.**
  Step two of Gemini's resumable upload posts the image bytes plus the `Cookie`
  and `SAPISIDHASH` headers to a URL taken from a response header. With
  `proxy` configured — a supported setup for exactly this project — a hostile or
  compromised proxy could answer that header with any host and harvest the
  session. The upload URL is now constrained to `content-push.googleapis.com`
  and `upload.googleapis.com` over HTTPS. Covered by regression tests including
  subdomain and userinfo spoofing.

### Fixed

- **A failed page-token scrape no longer re-fetches on every image.** The cache
  guard was `if fresh and tokens`, so an empty result stayed falsy and each
  subsequent upload triggered another authenticated page load — an extra request
  per image precisely when the network is already failing. The cache now records
  whether a fetch happened, separately from whether it succeeded.

- No credentials are read, logged, or included in error output. `redacted()`
  exists so a full config dump is safe to print.
- The full git history was scanned for live cookie, SAPISID, XSRF, and auth-file
  values before publication. None were ever committed.

[Unreleased]: https://github.com/plokmklo/freegem/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/plokmklo/freegem/releases/tag/v2.0.0
