# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| 2.9.x | Yes |
| < 2.9 | No |

## Reporting a Vulnerability

Report privately, not as a public issue:

- **GitHub private vulnerability reporting** (preferred):
  <https://github.com/plokmklo/freegem/security/advisories/new>
- Or email the maintainer at the address on the GitHub profile.

Include: affected version, the command you ran, what you expected, and what
happened. Do **not** include your `gemini-auth.json`, any cookie value, an
XSRF token, or a `SAPISID` value. Describe the symptom instead.

Expect an acknowledgement within 72 hours.

## The Actual Threat Model

Read this before filing anything, because most reports here are not what they
look like.

**This project does not authenticate anyone.** There is deliberately no API-key
layer. The access control is that `freegem serve` binds to `127.0.0.1` by
default. Anyone who can reach the port can use it. That is a documented design
decision, not a vulnerability — there is nothing in the code worth protecting
*except* the Google session it forwards.

If you expose the bridge beyond loopback, you are exposing your **Google
account session**. Put it behind a reverse proxy that does authentication. The
`serve` command prints a warning when bound to a non-loopback address for this
reason.

Reports of the form "anyone on my LAN can call the API" are expected behaviour
under an open bind and will be closed as such.

## In Scope

- Credential leakage: cookies, `SAPISID`, XSRF tokens, or the `at` form field
  appearing in logs, error messages, tracebacks, or HTTP responses.
- Request smuggling or header injection against `gemini.google.com`.
- Server-side request forgery through the `proxy` config, image-URL fetching, or
  the page-token scrape.
- A way to escape loopback binding without the explicit warning firing.
- Dependency confusion or a malicious transitive dependency in the pinned set.

## Out of Scope

- The absence of API-key authentication (see above — intentional).
- Google rate-limiting or banning an account. This drives an unofficial
  endpoint; throttling and account action are inherent risks, not defects.
  Google's Terms of Service generally prohibit automated access to the web
  frontend.
- Breakage caused by Google rotating `gemini_bl` or the `StreamGenerate`
  protocol. Expected and routine; fix the build tag and re-export the session.
- `usage` token counts being inaccurate. They are character estimates and are
  documented as such; the web endpoint returns no usage data.
- Local privilege escalation. This is a user-space CLI talking to Google.

## Hardening Guidance

- Run `freegem doctor` before publishing anything derived from this repo. It
  reports whether the cookie file parsed, whether SAPISID is present, and
  whether the XSRF token is set — without spending a request.
- Treat `gemini-auth.json` as a live password. It is in `.gitignore`; keep it
  that way. If it is ever committed, rotate the Google session (sign out
  everywhere) rather than deleting the file.
- Prefer `"temporary_chats": true` so requests do not land in your Gemini
  account history.
- Review Google's active sessions on your account periodically.

## Browser access: CORS and Host validation

The server binds to loopback, but loopback binding is a network-position
control, not an access control against a **browser** — pages reach
`127.0.0.1` from any origin. Two rules close that gap:

1. **`Host` header validation.** Requests whose `Host` is not
   `localhost`, `127.0.0.1`, `::1`, or the configured bind address are
   rejected with 403. This defeats DNS rebinding, where a browser believes
   it is talking to an attacker-controlled name while packets land here.
2. **Origin-scoped CORS.** `Access-Control-Allow-Origin` is only ever sent
   when the request's `Origin` is loopback, and it echoes that origin —
   never `*`. Non-browser clients (OpenAI SDK, Codex, Gemini CLI, curl)
   send no `Origin` and need no CORS headers at all.
3. **POST gates (CSRF).** Every POST must carry `Content-Type:
   application/json` (browsers cannot attach it to a preflight-free
   "simple" request), a loopback `Origin` when an `Origin` is present at
   all, and — for browsers that send Fetch Metadata — `Sec-Fetch-Site`
   values other than `cross-site`. A `cross-site` POST from a loopback
   `Origin` is allowed: a local UI on another port of this machine
   (`localhost:3000` -> this server) is cross-site by origin rules and is
   the intended case. `cross-site` with a foreign, `null`, lookalike, or
   missing `Origin` is a 403.

Under load the server sheds instead of dying: in-flight work is capped
(default 32, configurable per server); beyond the cap a 503 with
`Retry-After` is written before any thread is spawned. The kernel accept
backlog is 128 so a burst of real clients is absorbed at connect time and
then answered by the cap's shed rather than reset by the operating system.

Request bodies are capped at 32 MB (413 beyond), malformed or negative
lengths are rejected (400) with the connection closed, and client-supplied
image URLs are resolved first and refused unless every resolved address is
public (SSRF guard).
