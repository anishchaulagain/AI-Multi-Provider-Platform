"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

import { api, ApiError, type User } from "@/lib/api";

// The access token lives in localStorage so the browser can call the API directly
// (including SSE streaming later). Swap for an httpOnly cookie + BFF if needed.
const TOKEN_KEY = "aip.access_token";

type AuthState =
  | { status: "loading"; user: null; token: null }
  | { status: "anonymous"; user: null; token: null }
  | { status: "authenticated"; user: User; token: string };

type AuthContextValue = AuthState & {
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

function readToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function writeToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage unavailable (private mode); session simply won't persist.
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading", user: null, token: null });

  const logout = useCallback(() => {
    writeToken(null);
    setState({ status: "anonymous", user: null, token: null });
  }, []);

  const loadUser = useCallback(
    async (token: string) => {
      try {
        const user = await api.me(token);
        setState({ status: "authenticated", user, token });
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) logout();
        else setState({ status: "anonymous", user: null, token: null });
      }
    },
    [logout],
  );

  useEffect(() => {
    const token = readToken();
    // eslint-disable-next-line react-hooks/set-state-in-effect -- syncing from localStorage on mount
    if (token) void loadUser(token);
    else setState({ status: "anonymous", user: null, token: null });
  }, [loadUser]);

  const login = useCallback(
    async (email: string, password: string) => {
      const { access_token } = await api.login(email, password);
      writeToken(access_token);
      await loadUser(access_token);
    },
    [loadUser],
  );

  const value = useMemo(() => ({ ...state, login, logout }), [state, login, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside <AuthProvider>");
  return ctx;
}
