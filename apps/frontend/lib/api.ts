// Typed client for the Platform API. Mirrors the backend error schema:
// { "error": { "code", "message", "request_id", "details"? } }

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
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

async function request<T>(url: string, { token, json, headers, ...init }: RequestOptions = {}) {
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
    throw new ApiError(0, "network_error", "Cannot reach the API. Is the backend running?");
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

export type TokenResponse = { access_token: string; token_type: "bearer"; expires_in: number };

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

export type Readiness = { status: "ready" | "not_ready"; checks: Record<string, string> };

// ---------- Endpoints ----------
export const api = {
  login: (email: string, password: string) =>
    request<TokenResponse>(`${API_V1}/auth/login`, {
      method: "POST",
      json: { email, password },
    }),

  me: (token: string) => request<User>(`${API_V1}/auth/me`, { token }),

  listApiKeys: (token: string) => request<ApiKey[]>(`${API_V1}/api-keys`, { token }),

  createApiKey: (token: string, name: string, expiresInDays?: number) =>
    request<ApiKeyCreated>(`${API_V1}/api-keys`, {
      method: "POST",
      token,
      json: { name, expires_in_days: expiresInDays ?? null },
    }),

  revokeApiKey: (token: string, id: string) =>
    request<void>(`${API_V1}/api-keys/${id}`, { method: "DELETE", token }),

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
