# freegem — TypeScript SDK (scaffold)

Status: **scaffold**. The Python server's HTTP contracts are the source of
truth; this client tracks them. Not yet published to npm — per the P3 plan,
language SDKs widen after the core contracts stabilize.

## Boundaries

- Speaks plain HTTP to a running freegem server (`--base-url`).
- No browser automation, no cookie handling, no Gemini session logic:
  everything like that stays server-side on purpose.
- Auth is optional: if the server was started with `api_key`, pass it here.

## Usage

```ts
import { FreeGem } from "./src/index.ts";

const gem = new FreeGem({ baseUrl: "http://127.0.0.1:8081" });

console.log(await gem.health());
console.log((await gem.listModels()).data.map((m) => m.id));

const answer = await gem.chat([{ role: "user", content: "hello in one word" }]);
console.log(answer.choices[0].message.content);
```

Error model: non-2xx raises `FreeGemError` with `status` and the response
body prefix — callers can distinguish auth (401/403), capacity (503) and
upstream (502) failures.
