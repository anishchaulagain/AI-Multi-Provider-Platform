"use client";

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { api, type Readiness } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function OverviewPage() {
  const auth = useAuth();
  const [readiness, setReadiness] = useState<Readiness | null>(null);

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
    </div>
  );
}
