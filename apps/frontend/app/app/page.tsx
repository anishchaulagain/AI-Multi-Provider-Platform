"use client";

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type Readiness, type UsageSummary } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function OverviewPage() {
  const auth = useAuth();
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [usage, setUsage] = useState<UsageSummary | null>(null);
  const token = auth.status === "authenticated" ? auth.token : null;

  useEffect(() => {
    if (!token) return;
    api
      .usageSummary(token)
      .then(setUsage)
      .catch(() => setUsage(null));
  }, [token]);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api.readiness().then((r) => {
        if (!cancelled) setReadiness(r);
      });
    void load();
    const timer = setInterval(load, 10_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  if (auth.status !== "authenticated") return null;

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Overview</h1>
        <p className="text-sm text-muted-foreground">Welcome back, {auth.user.email}</p>
      </div>

      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Account</CardTitle>
            <CardDescription>Your identity on this platform</CardDescription>
          </CardHeader>
          <CardContent className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
            <span className="text-muted-foreground">Email</span>
            <span>{auth.user.email}</span>
            <span className="text-muted-foreground">Role</span>
            <span className="capitalize">{auth.user.role}</span>
            <span className="text-muted-foreground">Tenant</span>
            <span className="truncate font-mono text-xs leading-5">{auth.user.tenant_id}</span>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              System status
              {readiness && (
                <Badge variant={readiness.status === "ready" ? "default" : "destructive"}>
                  {readiness.status === "ready" ? "Ready" : "Degraded"}
                </Badge>
              )}
            </CardTitle>
            <CardDescription>Backend dependency checks (/readyz)</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            {!readiness && <span className="text-muted-foreground">Checking…</span>}
            {readiness &&
              Object.entries(readiness.checks).map(([name, value]) => (
                <div key={name} className="flex items-center justify-between">
                  <span className="capitalize">{name}</span>
                  <Badge variant={value === "ok" ? "secondary" : "destructive"}>{value}</Badge>
                </div>
              ))}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>LLM usage (last 24h)</CardTitle>
          <CardDescription>
            {usage?.daily_token_limit
              ? `${usage.tokens_used_today?.toLocaleString() ?? "?"} / ${usage.daily_token_limit.toLocaleString()} tokens used today`
              : "Requests served through the AI Gateway for your tenant"}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4 text-sm">
          {!usage && <span className="text-muted-foreground">Loading…</span>}
          {usage && (
            <>
              <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
                <Stat label="Requests" value={usage.requests} />
                <Stat label="Tokens" value={usage.total_tokens} />
                <Stat label="Cache hits" value={usage.cache_hits} />
                <Stat label="Errors" value={usage.errors} />
              </div>
              {usage.by_alias.length > 0 && (
                <table className="w-full">
                  <thead className="text-left text-muted-foreground">
                    <tr>
                      <th className="pb-2 font-medium">Alias</th>
                      <th className="pb-2 text-right font-medium">Requests</th>
                      <th className="pb-2 text-right font-medium">Tokens</th>
                      <th className="pb-2 text-right font-medium">Cache hits</th>
                      <th className="pb-2 text-right font-medium">Avg latency</th>
                    </tr>
                  </thead>
                  <tbody>
                    {usage.by_alias.map((a) => (
                      <tr key={a.alias} className="border-t">
                        <td className="py-2 font-mono text-xs">{a.alias}</td>
                        <td className="py-2 text-right tabular-nums">{a.requests}</td>
                        <td className="py-2 text-right tabular-nums">{a.total_tokens.toLocaleString()}</td>
                        <td className="py-2 text-right tabular-nums">{a.cache_hits}</td>
                        <td className="py-2 text-right tabular-nums">{a.avg_latency_ms} ms</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-lg border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="text-xl font-semibold tabular-nums">{value.toLocaleString()}</div>
    </div>
  );
}
