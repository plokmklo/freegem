# Gemini Cookie Sync

A Chrome extension that exports your Gemini session to a file `freegem` can read.

It exists because authenticating by hand is the worst part of using this
project: you need cookies, an XSRF token, a build tag, and an account index,
all of which have to come from the live signed-in page. This extension collects
all four in one click.

> Adapted from the cookie-sync extension in
> [`Sophomoresty/gemini-web2api`](https://github.com/Sophomoresty/gemini-web2api)
> (MIT). Changes are limited to the export filename and project references.

## What it exports

Saved as `gemini-auth.json`:

| Field | Purpose in freegem |
|---|---|
| `cookie` | Session cookie string sent with every request |
| `sapisid` | Used to derive the `Authorization: SAPISIDHASH ...` header |
| `xsrf_token` | `SNlM0e`, sent as the `at` form field; required when authenticated |
| `gemini_bl` | Current build tag; stale values cause HTTP 400 |
| `auth_user` | Account index for the `/u/<index>/` path when you have multiple accounts |

## Install

1. Open `chrome://extensions`
2. Enable **Developer mode**
3. Click **Load unpacked**
4. Select this folder (`gemini-cookie-sync-extension`)
5. Open <https://gemini.google.com/app>, sign in, and reload the page
6. Click the extension icon, then **Inspect session**
7. When it reports ready, click **Export gemini-auth.json**

## Use it

Move the exported file next to your config, or point at it directly:

```bash
# either: let the CLI find it automatically
mv ~/Downloads/gemini-auth.json .
freegem serve

# or: point at it explicitly
freegem serve --cookie-file ~/Downloads/gemini-auth.json
```

Verify with:

```bash
freegem doctor
```

The doctor command reports whether the cookie file was found, whether the
SAPISID was parsed, how many cookies were loaded, and whether the XSRF token is
present — without sending any generation request.

## Refresh cadence

Google sessions and build tags expire. When requests start failing with HTTP
400 or an empty response:

1. Open the extension and click **Inspect session** again
2. Re-export `gemini-auth.json`
3. Restart the bridge

`freegem serve` also refreshes the build tag from the page automatically
at startup, so usually only the cookie needs re-exporting.

## Security

The exported file **is your Google session**. Anyone holding it can act as your
account on Gemini.

- Do not commit it. It is covered by `.gitignore`.
- Do not share it, paste it into an issue, or send it anywhere.
- Prefer a temporary-chat configuration if you do not want requests saved to
  your history (`"temporary_chats": true`).
- Periodically review your Google account's active sessions.

The extension itself makes no network requests of its own beyond reading
`gemini.google.com`; the export download is a local `blob:` URL.
