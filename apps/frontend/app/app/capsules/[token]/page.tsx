"use client";

import { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { GitFork, Loader2 } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { api, ApiError, type CapsuleView, type ModelInfo } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const DEFAULT_ALIAS = "__default__";
const LABELS: Record<string, string> = {
  entities: "Entities",
  user_preferences: "Preferences",
  decisions: "Decisions",
  open_questions: "Open questions",
  referenced_docs: "Referenced documents",
};

export default function CapsulePage() {
  const auth = useAuth();
  const token = auth.status === "authenticated" ? auth.token : null;
  const router = useRouter();
  const params = useParams<{ token: string }>();
  const shareToken = decodeURIComponent(params.token);

  const [capsule, setCapsule] = useState<CapsuleView | null>(null);
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [alias, setAlias] = useState(DEFAULT_ALIAS);
  const [forking, setForking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    api
      .getCapsule(token, shareToken)
      .then(setCapsule)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Capsule not found"),
      );
    api
      .models(token)
      .then((m) =>
        setModels(m.filter((x) => x.kind === "chat" && x.id !== "auto")),
      )
      .catch(() => setModels([]));
  }, [token, shareToken]);

  async function fork() {
    if (!token || !capsule) return;
    setForking(true);
    try {
      const conversation = await api.forkCapsule(
        token,
        capsule.id,
        alias === DEFAULT_ALIAS ? null : alias,
      );
      router.push(`/app/chat?c=${conversation.id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Fork failed");
      setForking(false);
    }
  }

  if (error) {
    return (
      <Alert variant="destructive" className="max-w-xl">
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    );
  }
  if (!capsule) {
    return <Loader2 className="size-5 animate-spin text-muted-foreground" />;
  }

  const content = capsule.content;
  const facts = Object.entries(content.memory ?? {}).filter(
    ([, list]) => list.length > 0,
  );

  return (
    <div className="flex max-w-3xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-2xl font-semibold">{capsule.title}</h1>
          <Badge variant="secondary">{capsule.token_count} tokens</Badge>
          {capsule.source_model && (
            <Badge variant="outline">from {capsule.source_model}</Badge>
          )}
        </div>
        <p className="text-sm text-muted-foreground">
          Read-only context capsule · {content.message_count} messages condensed
          · {new Date(capsule.created_at).toLocaleString()}
          {capsule.expires_at &&
            ` · expires ${new Date(capsule.expires_at).toLocaleDateString()}`}
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Continue this conversation</CardTitle>
          <CardDescription>
            Fork starts a new chat that already knows this capsule — on any
            model.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-wrap items-center gap-2">
          <Select value={alias} onValueChange={setAlias}>
            <SelectTrigger className="w-56">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={DEFAULT_ALIAS}>Default model</SelectItem>
              {models.map((m) => (
                <SelectItem key={m.id} value={m.id}>
                  {m.id}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button onClick={fork} disabled={forking}>
            {forking ? <Loader2 className="animate-spin" /> : <GitFork />} Fork
          </Button>
        </CardContent>
      </Card>

      {content.summary && (
        <Card>
          <CardHeader>
            <CardTitle>Summary</CardTitle>
          </CardHeader>
          <CardContent className="text-sm whitespace-pre-wrap">
            {content.summary}
          </CardContent>
        </Card>
      )}

      {facts.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Facts</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-3 text-sm">
            {facts.map(([category, list]) => (
              <div key={category}>
                <div className="mb-1 text-xs font-medium text-muted-foreground">
                  {LABELS[category] ?? category}
                </div>
                <ul className="list-disc pl-5">
                  {list.map((f) => (
                    <li key={f.text}>{f.text}</li>
                  ))}
                </ul>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {content.key_exchanges.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Key exchanges</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4 text-sm">
            {content.key_exchanges.map((x, i) => (
              <div key={i} className="flex flex-col gap-1">
                <div className="font-medium">Q: {x.question}</div>
                <div className="whitespace-pre-wrap text-muted-foreground">
                  A: {x.answer}
                </div>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {capsule.resolved_citations.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Cited sources</CardTitle>
            <CardDescription>
              Passages visible to you in this workspace
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-2 text-sm">
            {capsule.resolved_citations.map((c) => (
              <div key={c.chunk_id} className="rounded-md border p-2">
                {c.missing ? (
                  <span className="text-muted-foreground">
                    Source no longer available
                  </span>
                ) : (
                  <>
                    <div className="text-xs font-medium">
                      {c.doc_title}
                      {c.page ? `, p. ${c.page}` : ""}
                    </div>
                    <div className="text-muted-foreground">{c.snippet}</div>
                  </>
                )}
              </div>
            ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
