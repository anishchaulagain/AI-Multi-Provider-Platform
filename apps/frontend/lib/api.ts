// Typed client for the Platform API. Mirrors the backend error schema:
// { "error": { "code", "message", "request_id", "details"? } }

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const API_V1 = `${API_URL}/api/v1`;

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public requestId?: string,
    public details?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type RequestOptions = Omit<RequestInit, "body"> & {
  token?: string | null;
  json?: unknown;
};

async function request<T>(
  url: string,
  { token, json, headers, ...init }: RequestOptions = {},
) {
  const finalHeaders = new Headers(headers);
  if (json !== undefined) finalHeaders.set("Content-Type", "application/json");
  if (token) finalHeaders.set("Authorization", `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers: finalHeaders,
      body: json !== undefined ? JSON.stringify(json) : undefined,
    });
  } catch {
    throw new ApiError(
      0,
      "network_error",
      "Cannot reach the API. Is the backend running?",
    );
  }

  if (response.status === 204) return undefined as T;

  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const err = body?.error;
    throw new ApiError(
      response.status,
      err?.code ?? "unknown_error",
      err?.message ?? response.statusText,
      err?.request_id,
      err?.details,
    );
  }
  return body as T;
}

// ---------- Types ----------
export type User = {
  id: string;
  tenant_id: string;
  email: string;
  full_name: string | null;
  role: "admin" | "member";
  created_at: string;
};

export type TokenResponse = {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
};

export type ApiKey = {
  id: string;
  name: string;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
};

export type ApiKeyCreated = ApiKey & { key: string };

export type Readiness = {
  status: "ready" | "not_ready";
  checks: Record<string, string>;
};

export type ModelInfo = {
  id: string;
  kind: "chat" | "embedding";
  description: string;
  deployments: string[];
};

export type ChatMessage = {
  role: "system" | "user" | "assistant";
  content: string;
};

export type ChatRequest = {
  model: string;
  messages: ChatMessage[];
  temperature?: number;
  max_tokens?: number;
};

export type Usage = {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
};

export type GatewayMeta = {
  alias: string | null;
  deployment: string | null;
  provider: string | null;
  cache: string | null;
  fallbacks: number;
  guardrail: string | null;
};

export type ChatCompletion = {
  id: string;
  model: string;
  choices: {
    index: number;
    message: ChatMessage;
    finish_reason: string | null;
  }[];
  usage?: Usage;
};

export type AliasUsage = {
  alias: string;
  requests: number;
  total_tokens: number;
  cache_hits: number;
  errors: number;
  avg_latency_ms: number;
};

export type UsageSummary = {
  hours: number;
  requests: number;
  total_tokens: number;
  cache_hits: number;
  errors: number;
  by_alias: AliasUsage[];
  tokens_used_today: number | null;
  daily_token_limit: number | null;
};

export type ProviderStatus = {
  provider: string;
  configured: boolean;
  breaker: {
    state: "closed" | "open" | "half_open" | "unknown";
    recent_failures: number | null;
    reopens_in_s: number | null;
  };
  deployments: {
    name: string;
    kind: string;
    configured: boolean;
    rpm: number | null;
  }[];
};

function readMeta(headers: Headers): GatewayMeta {
  return {
    alias: headers.get("x-gateway-alias"),
    deployment: headers.get("x-gateway-deployment"),
    provider: headers.get("x-gateway-provider"),
    cache: headers.get("x-gateway-cache"),
    fallbacks: Number(headers.get("x-gateway-fallbacks") ?? 0),
    guardrail: headers.get("x-gateway-guardrail"),
  };
}

async function errorFrom(response: Response): Promise<ApiError> {
  const body = await response.json().catch(() => null);
  const err = body?.error;
  return new ApiError(
    response.status,
    err?.code ?? "unknown_error",
    err?.message ?? response.statusText,
    err?.request_id,
    err?.details,
  );
}

function llmHeaders(token: string, noCache: boolean): Headers {
  const headers = new Headers({
    "Content-Type": "application/json",
    Authorization: `Bearer ${token}`,
  });
  if (noCache) headers.set("Cache-Control", "no-cache");
  return headers;
}

/** Non-streaming chat completion, returning the gateway routing metadata too. */
async function chat(
  token: string,
  body: ChatRequest,
  opts: { noCache?: boolean; signal?: AbortSignal } = {},
) {
  let response: Response;
  try {
    response = await fetch(`${API_V1}/llm/chat/completions`, {
      method: "POST",
      headers: llmHeaders(token, opts.noCache ?? false),
      body: JSON.stringify({ ...body, stream: false }),
      signal: opts.signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError(
      0,
      "network_error",
      "Cannot reach the API. Is the backend running?",
    );
  }
  if (!response.ok) throw await errorFrom(response);
  return {
    completion: (await response.json()) as ChatCompletion,
    meta: readMeta(response.headers),
  };
}

/** Streaming chat completion over SSE. Calls onDelta for each text fragment. */
async function streamChat(
  token: string,
  body: ChatRequest,
  opts: {
    onDelta: (text: string) => void;
    noCache?: boolean;
    signal?: AbortSignal;
  },
): Promise<{ meta: GatewayMeta; usage: Usage | null }> {
  let response: Response;
  try {
    response = await fetch(`${API_V1}/llm/chat/completions`, {
      method: "POST",
      headers: llmHeaders(token, opts.noCache ?? false),
      body: JSON.stringify({ ...body, stream: true }),
      signal: opts.signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError(
      0,
      "network_error",
      "Cannot reach the API. Is the backend running?",
    );
  }
  if (!response.ok || !response.body) throw await errorFrom(response);

  const meta = readMeta(response.headers);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let usage: Usage | null = null;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) {
      if (!line.startsWith("data:")) continue;
      const payload = line.slice(5).trim();
      if (payload === "[DONE]") return { meta, usage };
      const data = JSON.parse(payload);
      if (data.error)
        throw new ApiError(
          502,
          data.error.code ?? "stream_error",
          data.error.message,
        );
      for (const choice of data.choices ?? []) {
        if (choice.delta?.content) opts.onDelta(choice.delta.content);
      }
      if (data.usage) usage = data.usage;
    }
  }
  return { meta, usage };
}

// ---------- Documents ----------
export type DocumentStatus =
  "queued" | "parsing" | "chunking" | "embedding" | "ready" | "failed";

export type DocumentInfo = {
  id: string;
  filename: string;
  content_type: string;
  size_bytes: number;
  content_hash: string;
  status: DocumentStatus;
  error: string | null;
  attempts: number;
  title: string | null;
  page_count: number | null;
  chunk_count: number;
  details: { format?: string; summary?: string; keywords?: string[] } & Record<
    string,
    unknown
  >;
  created_at: string;
  updated_at: string;
  ingested_at: string | null;
};

export type DocumentEvent = {
  doc_id: string;
  status: DocumentStatus | "deleted";
  error: string | null;
  chunk_count: number | null;
  attempts: number | null;
  deleted: boolean;
};

export type ChunkInfo = {
  id: string;
  chunk_index: number;
  text: string;
  page: number | null;
  section: string | null;
};

export type ChunkPage = {
  items: ChunkInfo[];
  offset: number;
  limit: number;
  total: number;
};

export const ACCEPTED_UPLOADS = ".pdf,.docx,.md,.markdown,.html,.htm,.txt";

/** Upload one file. Resolves with the document and whether it already existed. */
async function uploadDocument(token: string, file: File) {
  const form = new FormData();
  form.append("file", file);
  let response: Response;
  try {
    response = await fetch(`${API_V1}/documents`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    });
  } catch {
    throw new ApiError(
      0,
      "network_error",
      "Cannot reach the API. Is the backend running?",
    );
  }
  if (!response.ok) throw await errorFrom(response);
  return {
    document: (await response.json()) as DocumentInfo,
    duplicate: response.headers.get("x-duplicate") === "true",
  };
}

/**
 * Follow the tenant-wide document status stream (SSE over fetch, so the auth
 * header can be sent). Reconnects with backoff until `signal` is aborted.
 */
async function watchDocuments(
  token: string,
  onEvent: (event: DocumentEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  let delay = 1000;
  while (!signal.aborted) {
    try {
      const response = await fetch(`${API_V1}/documents/events`, {
        headers: { Authorization: `Bearer ${token}` },
        signal,
      });
      if (!response.ok || !response.body) throw await errorFrom(response);
      delay = 1000;
      const reader = response.body
        .pipeThrough(new TextDecoderStream())
        .getReader();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += value;
        const blocks = buffer.split("\n\n");
        buffer = blocks.pop() ?? "";
        for (const block of blocks) {
          const data = block
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trim())
            .join("");
          if (data) onEvent(JSON.parse(data) as DocumentEvent);
        }
      }
    } catch (err) {
      if (signal.aborted) return;
      if (err instanceof ApiError && (err.status === 401 || err.status === 403))
        return;
    }
    await new Promise((resolve) => setTimeout(resolve, delay));
    delay = Math.min(delay * 2, 15000);
  }
}

// ---------- Endpoints ----------
export const api = {
  login: (email: string, password: string) =>
    request<TokenResponse>(`${API_V1}/auth/login`, {
      method: "POST",
      json: { email, password },
    }),

  me: (token: string) => request<User>(`${API_V1}/auth/me`, { token }),

  listApiKeys: (token: string) =>
    request<ApiKey[]>(`${API_V1}/api-keys`, { token }),

  createApiKey: (token: string, name: string, expiresInDays?: number) =>
    request<ApiKeyCreated>(`${API_V1}/api-keys`, {
      method: "POST",
      token,
      json: { name, expires_in_days: expiresInDays ?? null },
    }),

  revokeApiKey: (token: string, id: string) =>
    request<void>(`${API_V1}/api-keys/${id}`, { method: "DELETE", token }),

  models: (token: string) =>
    request<{ data: ModelInfo[] }>(`${API_V1}/llm/models`, { token }).then(
      (r) => r.data,
    ),

  chat,
  streamChat,

  usageSummary: (token: string, hours = 24) =>
    request<UsageSummary>(`${API_V1}/usage/summary?hours=${hours}`, { token }),

  providers: (token: string) =>
    request<{ providers: ProviderStatus[] }>(`${API_V1}/admin/providers`, {
      token,
    }).then((r) => r.providers),

  listDocuments: (token: string, limit = 100) =>
    request<{ items: DocumentInfo[]; total: number }>(
      `${API_V1}/documents?limit=${limit}`,
      {
        token,
      },
    ),

  uploadDocument,
  watchDocuments,

  deleteDocument: (token: string, id: string) =>
    request<void>(`${API_V1}/documents/${id}`, { method: "DELETE", token }),

  reingestDocument: (token: string, id: string) =>
    request<DocumentInfo>(`${API_V1}/documents/${id}/reingest`, {
      method: "POST",
      token,
    }),

  documentChunks: (token: string, id: string, offset = 0, limit = 20) =>
    request<ChunkPage>(
      `${API_V1}/documents/${id}/chunks?offset=${offset}&limit=${limit}`,
      {
        token,
      },
    ),

  // /readyz returns 503 with a body when not ready; surface the body either way.
  readiness: async (): Promise<Readiness> => {
    try {
      const response = await fetch(`${API_URL}/readyz`, { cache: "no-store" });
      return (await response.json()) as Readiness;
    } catch {
      return { status: "not_ready", checks: { api: "error" } };
    }
  },
};
