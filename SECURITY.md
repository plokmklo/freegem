# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| 2.0.x | Yes |
| < 2.0 | No |

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
