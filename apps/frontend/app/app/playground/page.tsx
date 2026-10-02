"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Send, Square } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  ApiError,
  type ChatMessage,
  type GatewayMeta,
  type ModelInfo,
  type ProviderStatus,
  type Usage,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";

type RunResult = { meta: GatewayMeta; usage: Usage | null; latencyMs: number };

const BREAKER_VARIANT = {
  closed: "secondary",
  half_open: "outline",
  open: "destructive",
  unknown: "outline",
} as const;

export default function PlaygroundPage() {
  const auth = useAuth();
  const token = auth.status === "authenticated" ? auth.token : null;
  const isAdmin = auth.status === "authenticated" && auth.user.role === "admin";

  const [models, setModels] = useState<ModelInfo[]>([]);
  const [model, setModel] = useState("auto");
  const [system, setSystem] = useState("You are a concise, helpful assistant.");
  const [prompt, setPrompt] = useState("");
  const [temperature, setTemperature] = useState(0.7);
  const [stream, setStream] = useState(true);
  const [noCache, setNoCache] = useState(false);

  const [output, setOutput] = useState("");
  const [result, setResult] = useState<RunResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [providers, setProviders] = useState<ProviderStatus[] | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!token) return;
    api
      .models(token)
      .then((m) => setModels(m.filter((x) => x.kind === "chat")))
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load models"));
  }, [token]);

  const loadProviders = useCallback(async () => {
    if (!token || !isAdmin) return;
    try {
      setProviders(await api.providers(token));
    } catch {
      setProviders(null);
    }
  }, [token, isAdmin]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial data fetch
    void loadProviders();
  }, [loadProviders]);

  async function run() {
    if (!token || !prompt.trim()) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setRunning(true);
    setError(null);
    setOutput("");
    setResult(null);

    const messages: ChatMessage[] = [
      ...(system.trim() ? [{ role: "system" as const, content: system }] : []),
      { role: "user", content: prompt },
    ];
    const body = { model, messages, temperature };
    const started = performance.now();

    try {
      if (stream) {
        const { meta, usage } = await api.streamChat(token, body, {
          noCache,
          signal: controller.signal,
          onDelta: (text) => setOutput((prev) => prev + text),
        });
        setResult({ meta, usage, latencyMs: Math.round(performance.now() - started) });
      } else {
        const { completion, meta } = await api.chat(token, body, {
          noCache,
          signal: controller.signal,
        });
        setOutput(completion.choices[0]?.message.content ?? "");
        setResult({
          meta,
          usage: completion.usage ?? null,
          latencyMs: Math.round(performance.now() - started),
        });
      }
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        setError(err instanceof ApiError ? `${err.message} (${err.code})` : "Request failed");
      }
    } finally {
      setRunning(false);
      abortRef.current = null;
      void loadProviders();
    }
  }

  return (
    <div className="flex max-w-6xl flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Playground</h1>
        <p className="text-sm text-muted-foreground">
          Send prompts through the AI Gateway and see which free provider served them.
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
        <div className="flex flex-col gap-4">
          <Card>
            <CardContent className="flex flex-col gap-4">
              <div className="flex flex-col gap-2">
                <Label htmlFor="system">System prompt</Label>
                <Textarea
                  id="system"
                  rows={2}
                  value={system}
                  onChange={(e) => setSystem(e.target.value)}
                />
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor="prompt">Prompt</Label>
                <Textarea
                  id="prompt"
                  rows={5}
                  placeholder="Ask anything… (Ctrl+Enter to send)"
                  value={prompt}
                  onChange={(e) => setPrompt(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void run();
                  }}
                />
              </div>
              <div className="flex justify-end gap-2">
                {running && (
                  <Button variant="outline" onClick={() => abortRef.current?.abort()}>
                    <Square /> Stop
                  </Button>
                )}
                <Button onClick={run} disabled={running || !prompt.trim()}>
                  {running ? <Loader2 className="animate-spin" /> : <Send />}
                  Send
                </Button>
              </div>
            </CardContent>
          </Card>

          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}

          <Card>
            <CardHeader>
              <CardTitle>Response</CardTitle>
              {result && (
                <div className="flex flex-wrap gap-1.5 pt-1">
                  <Badge variant="secondary">alias: {result.meta.alias}</Badge>
                  {result.meta.deployment && (
                    <Badge variant="secondary">deployment: {result.meta.deployment}</Badge>
                  )}
                  {result.meta.provider && (
                    <Badge variant="secondary">provider: {result.meta.provider}</Badge>
                  )}
                  <Badge variant={result.meta.cache?.match(/exact|semantic/) ? "default" : "outline"}>
                    cache: {result.meta.cache}
                  </Badge>
                  <Badge variant={result.meta.fallbacks ? "destructive" : "outline"}>
                    fallbacks: {result.meta.fallbacks}
                  </Badge>
                  {result.meta.guardrail && (
                    <Badge variant="destructive">guardrail: {result.meta.guardrail}</Badge>
                  )}
                  <Badge variant="outline">{result.latencyMs} ms</Badge>
                  {result.usage && (
                    <Badge variant="outline">
                      {result.usage.prompt_tokens} in / {result.usage.completion_tokens} out tokens
                    </Badge>
                  )}
                </div>
              )}
            </CardHeader>
            <CardContent>
              <div className="min-h-32 text-sm whitespace-pre-wrap">
                {output || (
                  <span className="text-muted-foreground">
                    {running ? "Waiting for the first token…" : "No response yet."}
                  </span>
                )}
              </div>
            </CardContent>
          </Card>
        </div>

        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle>Settings</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-col gap-5">
              <div className="flex flex-col gap-2">
                <Label>Model alias</Label>
                <Select value={model} onValueChange={setModel}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {(models.length ? models : [{ id: "auto", description: "" } as ModelInfo]).map(
                      (m) => (
                        <SelectItem key={m.id} value={m.id}>
                          {m.id}
                        </SelectItem>
                      ),
                    )}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  {models.find((m) => m.id === model)?.description}
                </p>
              </div>
              <div className="flex flex-col gap-3">
                <div className="flex justify-between">
                  <Label>Temperature</Label>
                  <span className="text-sm text-muted-foreground">{temperature.toFixed(1)}</span>
                </div>
                <Slider
                  min={0}
                  max={2}
                  step={0.1}
                  value={[temperature]}
                  onValueChange={([v]) => setTemperature(v)}
                />
              </div>
              <div className="flex items-center justify-between">
                <Label htmlFor="stream">Stream</Label>
                <Switch id="stream" checked={stream} onCheckedChange={setStream} />
              </div>
              <div className="flex items-center justify-between">
                <Label htmlFor="nocache">Bypass cache</Label>
                <Switch id="nocache" checked={noCache} onCheckedChange={setNoCache} />
              </div>
            </CardContent>
          </Card>

          {isAdmin && (
            <Card>
              <CardHeader>
                <CardTitle>Providers</CardTitle>
                <CardDescription>Circuit breaker state per provider</CardDescription>
              </CardHeader>
              <CardContent className="flex flex-col gap-2 text-sm">
                {providers === null && (
                  <span className="text-muted-foreground">Gateway unavailable</span>
                )}
                {providers?.map((p) => (
                  <div key={p.provider} className="flex items-center justify-between gap-2">
                    <span className={p.configured ? "" : "text-muted-foreground"}>
                      {p.provider}
                      {!p.configured && " (no key)"}
                    </span>
                    <Badge variant={BREAKER_VARIANT[p.breaker.state]}>
                      {p.breaker.state.replace("_", "-")}
                    </Badge>
                  </div>
                ))}
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
