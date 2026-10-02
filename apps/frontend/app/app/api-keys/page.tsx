"use client";

import { useCallback, useEffect, useState } from "react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { api, ApiError, type ApiKey } from "@/lib/api";
import { useAuth } from "@/lib/auth";

function formatDate(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}

export default function ApiKeysPage() {
  const auth = useAuth();
  const token = auth.status === "authenticated" ? auth.token : null;

  const [keys, setKeys] = useState<ApiKey[] | null>(null);
  const [name, setName] = useState("");
  const [newKey, setNewKey] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!token) return;
    try {
      setKeys(await api.listApiKeys(token));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load API keys");
    }
  }, [token]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial data fetch
    void refresh();
  }, [refresh]);

  async function onCreate(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!token) return;
    setError(null);
    try {
      const created = await api.createApiKey(token, name);
      setNewKey(created.key);
      setName("");
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to create API key");
    }
  }

  async function onRevoke(id: string) {
    if (!token) return;
    try {
      await api.revokeApiKey(token, id);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to revoke API key");
    }
  }

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">API keys</h1>
        <p className="text-sm text-muted-foreground">
          Use with <code className="font-mono">X-API-Key</code> or{" "}
          <code className="font-mono">Authorization: Bearer</code>.
        </p>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {newKey && (
        <Alert>
          <AlertTitle>Copy your new key now — it won&apos;t be shown again</AlertTitle>
          <AlertDescription className="flex items-center gap-2">
            <code className="flex-1 truncate rounded bg-muted px-2 py-1 font-mono text-xs">
              {newKey}
            </code>
            <Button size="sm" variant="outline" onClick={() => navigator.clipboard.writeText(newKey)}>
              Copy
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setNewKey(null)}>
              Done
            </Button>
          </AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Create key</CardTitle>
          <CardDescription>Keys act on your behalf within your tenant.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onCreate} className="flex gap-2">
            <Input
              placeholder="Key name, e.g. local-scripts"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              maxLength={100}
            />
            <Button type="submit">Create</Button>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Your keys</CardTitle>
        </CardHeader>
        <CardContent>
          {keys === null && <p className="text-sm text-muted-foreground">Loading…</p>}
          {keys?.length === 0 && <p className="text-sm text-muted-foreground">No keys yet.</p>}
          {keys && keys.length > 0 && (
            <table className="w-full text-sm">
              <thead className="text-left text-muted-foreground">
                <tr>
                  <th className="pb-2 font-medium">Name</th>
                  <th className="pb-2 font-medium">Key</th>
                  <th className="pb-2 font-medium">Last used</th>
                  <th className="pb-2 font-medium">Status</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {keys.map((key) => (
                  <tr key={key.id} className="border-t">
                    <td className="py-2">{key.name}</td>
                    <td className="py-2 font-mono text-xs">{key.key_prefix}…</td>
                    <td className="py-2">{formatDate(key.last_used_at)}</td>
                    <td className="py-2">
                      {key.revoked_at ? (
                        <Badge variant="outline">Revoked</Badge>
                      ) : (
                        <Badge variant="secondary">Active</Badge>
                      )}
                    </td>
                    <td className="py-2 text-right">
                      {!key.revoked_at && (
                        <Button size="sm" variant="destructive" onClick={() => onRevoke(key.id)}>
                          Revoke
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
