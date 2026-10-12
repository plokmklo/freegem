/**
 * FreeGem TypeScript SDK — scaffold (2.9.0, P3 #1).
 *
 * STATUS: scaffold. The core HTTP contracts it binds (OpenAI-compatible
 * /v1/chat/completions, /v1/models, /health, /metrics) have been stable
 * across 2.5–2.9, but per the P3 plan additional-language SDKs widen only
 * "after the core contracts stabilize" — this client is the shape of that
 * stabilization, not a published npm package yet.
 *
 * Boundaries: this SDK speaks HTTP to a running freegem server. It embeds
 * no browser automation, no cookies, no Gemini session handling — all of
 * that stays server-side by design.
 */

export interface FreeGemClientOptions {
  baseUrl?: string;
  apiKey?: string | null;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

export interface ChatMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

export interface ChatCompletionRequest {
  model?: string;
  messages: ChatMessage[];
  temperature?: number;
  max_tokens?: number;
}

export interface ChatCompletionResponse {
  id: string;
  object: "chat.completion";
  model: string;
  choices: Array<{ index: number; message: ChatMessage; finish_reason: string }>;
  usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
}

export interface ModelInfo {
  id: string;
  object: "model";
  owned_by: string;
  description?: string;
}

export interface HealthInfo {
  status: string;
  version: string;
  models: string[];
  session: string;
}

export class FreeGemError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly body?: unknown,
  ) {
    super(message);
    this.name = "FreeGemError";
  }
}

const DEFAULT_BASE = "http://127.0.0.1:8081";

export class FreeGem {
  private readonly base: string;
  private readonly apiKey: string | null;
  private readonly fetchImpl: typeof fetch;
  private readonly timeoutMs: number;

  constructor(opts: FreeGemClientOptions = {}) {
    this.base = (opts.baseUrl ?? DEFAULT_BASE).replace(/\/+$/, "");
    this.apiKey = opts.apiKey ?? null;
    this.fetchImpl = opts.fetchImpl ?? globalThis.fetch;
    this.timeoutMs = opts.timeoutMs ?? 120_000;
    if (typeof this.fetchImpl !== "function") {
      throw new FreeGemError("no fetch implementation available (provide opts.fetchImpl)");
    }
  }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      ...(init?.headers as Record<string, string> | undefined),
    };
    if (this.apiKey) headers["Authorization"] = `Bearer ${this.apiKey}`;
    const response = await this.fetchImpl(`${this.base}${path}`, {
      ...init,
      headers,
      signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!response.ok) {
      const body = await response.text().catch(() => "");
      throw new FreeGemError(
        `freegem ${path} failed: HTTP ${response.status}${body ? ` — ${body.slice(0, 300)}` : ""}`,
        response.status,
        body,
      );
    }
    return (await response.json()) as T;
  }

  health(): Promise<HealthInfo> {
    return this.request<HealthInfo>("/health");
  }

  listModels(): Promise<{ object: "list"; data: ModelInfo[] }> {
    return this.request("/v1/models");
  }

  async chat(
    messages: ChatMessage[],
    opts: Omit<ChatCompletionRequest, "messages"> = {},
  ): Promise<ChatCompletionResponse> {
    return this.request("/v1/chat/completions", {
      method: "POST",
      body: JSON.stringify({ model: opts.model, messages, ...opts }),
    });
  }

  metrics(): Promise<Record<string, unknown>> {
    return this.request("/metrics");
  }
}
