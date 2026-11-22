"use client";

import { useCallback, useEffect, useState } from "react";
import { Check, Loader2, RotateCw, ThumbsDown, ThumbsUp, X } from "lucide-react";

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
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  api,
  ApiError,
  EVAL_METRICS,
  type EvalMetric,
  type EvalResultDetail,
  type EvalResultItem,
  type EvalScores,
  type EvalSummary,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

const LABELS: Record<EvalMetric, { title: string; help: string }> = {
  faithfulness: {
    title: "Faithfulness",
    help: "Claims in the answer that the retrieved sources support",
  },
  answer_relevance: {
    title: "Answer relevance",
    help: "How directly the answer addresses the question",
  },
  context_precision: {
    title: "Context precision",
    help: "Relevant sources ranked ahead of irrelevant ones",
  },
  citation_accuracy: {
    title: "Citation accuracy",
    help: "Cited sentences that their sources actually support",
  },
};

const ALL = "all";

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? "–" : `${Math.round(value * 100)}%`;
}

/** Daily means on a fixed 0–100% scale; hover a day for its value. */
function Trend({
  points,
  metric,
}: {
  points: EvalSummary["trend"];
  metric: EvalMetric;
}) {
  if (points.length < 2) return null;
  return (
    <div className="flex h-10 items-end gap-0.5" aria-hidden>
      {points.map((p) => {
        const value = p[metric];
        return (
          <div
            key={p.day}
            className="group flex h-full flex-1 items-end"
            title={`${p.day}: ${pct(value)} (${p.count} answers)`}
          >
            <div
              className="w-full rounded-t-[2px] bg-foreground/55 group-hover:bg-foreground"
              style={{ height: value === null ? 0 : `${Math.max(value * 100, 4)}%` }}
            />
          </div>
        );
      })}
    </div>
  );
}

function ScoreBadges({ scores }: { scores: EvalScores }) {
  return (
    <div className="flex flex-wrap gap-1">
      {EVAL_METRICS.map((m) =>
        scores[m] === null ? null : (
          <Badge key={m} variant="outline" className="font-normal">
            {LABELS[m].title.split(" ")[0]} {pct(scores[m])}
          </Badge>
        ),
      )}
    </div>
  );
}

function Verdict({ ok }: { ok: boolean }) {
  return ok ? (
    <Check className="mt-0.5 size-3.5 shrink-0" aria-label="supported" />
  ) : (
    <X className="mt-0.5 size-3.5 shrink-0 text-destructive" aria-label="unsupported" />
  );
}

export default function EvalsPage() {
  const auth = useAuth();
  const token = auth.status === "authenticated" ? auth.token : null;

  const [days, setDays] = useState("30");
  const [metric, setMetric] = useState<string>(ALL);
  const [model, setModel] = useState<string>(ALL);
  const [summary, setSummary] = useState<EvalSummary | null>(null);
  const [results, setResults] = useState<EvalResultItem[]>([]);
  const [total, setTotal] = useState(0);
  const [detail, setDetail] = useState<EvalResultDetail | null>(null);
  const [rerunning, setRerunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fail = (err: unknown, fallback: string) =>
    setError(
      err instanceof ApiError
        ? err.status === 403
          ? "Evaluation results are visible to workspace admins only."
          : `${err.message} (${err.code})`
        : fallback,
    );

  const load = useCallback(async () => {
    if (!token) return;
    setLoading(true);
    setError(null);
    try {
      const [s, page] = await Promise.all([
        api.evalSummary(token, Number(days)),
        api.evalResults(token, {
          order: "worst",
          metric: metric === ALL ? null : (metric as EvalMetric),
          model: model === ALL ? null : model,
        }),
      ]);
      setSummary(s);
      setResults(page.items);
      setTotal(page.total);
    } catch (err) {
      fail(err, "Failed to load evaluations");
    } finally {
      setLoading(false);
    }
  }, [token, days, metric, model]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data fetch on filter change
    void load();
  }, [load]);

  async function openDetail(id: string) {
    if (!token) return;
    try {
      setDetail(await api.evalResult(token, id));
    } catch (err) {
      fail(err, "Failed to load the evaluation");
    }
  }

  async function rerun() {
    if (!token || !detail) return;
    setRerunning(true);
    try {
      setDetail(await api.evaluateMessage(token, detail.message_id));
      void load();
    } catch (err) {
      fail(err, "Re-evaluation failed");
    } finally {
      setRerunning(false);
    }
  }

  const models = (summary?.by_model ?? []).filter((m) => m.count > 0 && m.deployment);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Evaluations</h1>
          <p className="text-sm text-muted-foreground">
            A sample of live answers is scored by an LLM judge from a different
            model family. Offline runs (<code>make eval</code>) appear here too.
          </p>
        </div>
        <Select value={days} onValueChange={setDays}>
          <SelectTrigger className="w-36">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="7">Last 7 days</SelectItem>
            <SelectItem value="30">Last 30 days</SelectItem>
            <SelectItem value="90">Last 90 days</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      {summary && (
        <>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {EVAL_METRICS.map((m) => (
              <Card key={m} className="gap-3">
                <CardHeader>
                  <CardDescription>{LABELS[m].title}</CardDescription>
                  <CardTitle className="text-3xl tabular-nums">
                    {pct(summary.metrics[m].mean)}
                  </CardTitle>
                </CardHeader>
                <CardContent className="flex flex-col gap-2">
                  <Trend points={summary.trend} metric={m} />
                  <p className="text-xs text-muted-foreground">
                    {LABELS[m].help} · {summary.metrics[m].count} answers
                  </p>
                </CardContent>
              </Card>
            ))}
          </div>

          <div className="flex flex-wrap gap-6 text-sm">
            <span>
              <span className="text-muted-foreground">Evaluated answers </span>
              <span className="font-medium tabular-nums">{summary.count}</span>
            </span>
            <span>
              <span className="text-muted-foreground">Abstained (&ldquo;I don&rsquo;t know&rdquo;) </span>
              <span className="font-medium tabular-nums">{pct(summary.abstention_rate)}</span>
            </span>
            <span className="flex items-center gap-1">
              <ThumbsUp className="size-3.5 text-muted-foreground" />
              <span className="font-medium tabular-nums">{summary.feedback.up}</span>
              <ThumbsDown className="ml-2 size-3.5 text-muted-foreground" />
              <span className="font-medium tabular-nums">{summary.feedback.down}</span>
              <span className="text-muted-foreground"> user feedback</span>
            </span>
          </div>

          <Card>
            <CardHeader>
              <CardTitle>By answering model</CardTitle>
              <CardDescription>
                Mean scores per gateway deployment that wrote the answer.
              </CardDescription>
            </CardHeader>
            <CardContent className="overflow-x-auto">
              {summary.by_model.length === 0 ? (
                <p className="text-sm text-muted-foreground">No evaluations yet.</p>
              ) : (
                <table className="w-full text-sm">
                  <thead className="text-left text-xs text-muted-foreground">
                    <tr>
                      <th className="py-2 pr-4 font-medium">Deployment</th>
                      <th className="py-2 pr-4 text-right font-medium">Answers</th>
                      {EVAL_METRICS.map((m) => (
                        <th key={m} className="py-2 pr-4 text-right font-medium">
                          {LABELS[m].title}
                        </th>
                      ))}
                      <th className="py-2 text-right font-medium">Feedback</th>
                    </tr>
                  </thead>
                  <tbody className="tabular-nums">
                    {summary.by_model.map((row) => (
                      <tr key={row.deployment ?? "unknown"} className="border-t">
                        <td className="py-2 pr-4">
                          {row.deployment ?? "unknown"}
                          {row.provider && (
                            <span className="text-muted-foreground"> · {row.provider}</span>
                          )}
                        </td>
                        <td className="py-2 pr-4 text-right">{row.count}</td>
                        {EVAL_METRICS.map((m) => (
                          <td key={m} className="py-2 pr-4 text-right">
                            {pct(row[m])}
                          </td>
                        ))}
                        <td className="py-2 text-right">
                          +{row.feedback_up} / −{row.feedback_down}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </CardContent>
          </Card>
        </>
      )}

      <Card>
        <CardHeader className="flex flex-row flex-wrap items-start justify-between gap-3">
          <div className="flex flex-col gap-1.5">
            <CardTitle>Lowest-scoring answers</CardTitle>
            <CardDescription>
              {total} evaluated answer{total === 1 ? "" : "s"}, worst first. Open one to
              see the judge&rsquo;s verdicts.
            </CardDescription>
          </div>
          <div className="flex gap-2">
            <Select value={metric} onValueChange={setMetric}>
              <SelectTrigger className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Any metric</SelectItem>
                {EVAL_METRICS.map((m) => (
                  <SelectItem key={m} value={m}>
                    {LABELS[m].title}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select value={model} onValueChange={setModel}>
              <SelectTrigger className="w-48">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All models</SelectItem>
                {models.map((m) => (
                  <SelectItem key={m.deployment} value={m.deployment as string}>
                    {m.deployment}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col divide-y">
          {loading && results.length === 0 && (
            <p className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" /> Loading…
            </p>
          )}
          {!loading && results.length === 0 && (
            <p className="text-sm text-muted-foreground">
              Nothing evaluated yet. Chat with your documents (a share of answers is
              sampled) or run <code>make eval</code>.
            </p>
          )}
          {results.map((r) => (
            <button
              key={r.id}
              type="button"
              onClick={() => openDetail(r.id)}
              className="flex flex-col gap-1.5 py-3 text-left hover:bg-muted/50"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-sm font-medium">
                  {r.question ?? "(question unavailable)"}
                </span>
                <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
                  {r.feedback !== null && r.feedback < 0 && <ThumbsDown className="size-3" />}
                  {r.feedback !== null && r.feedback > 0 && <ThumbsUp className="size-3" />}
                  <Badge variant="secondary" className="font-normal">
                    {r.source}
                  </Badge>
                  {new Date(r.created_at).toLocaleDateString()}
                </span>
              </div>
              <p className="line-clamp-2 text-xs text-muted-foreground">{r.answer}</p>
              <ScoreBadges scores={r} />
            </button>
          ))}
        </CardContent>
      </Card>

      <Sheet open={detail !== null} onOpenChange={(o) => !o && setDetail(null)}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
          {detail && (
            <>
              <SheetHeader>
                <SheetTitle>{detail.question ?? "Evaluated answer"}</SheetTitle>
                <SheetDescription>
                  {detail.answer_deployment ?? "unknown model"} · judged by{" "}
                  {detail.judge_deployment ?? "unknown"} · {detail.source}
                  {detail.run_id ? ` (${detail.run_id})` : ""}
                </SheetDescription>
              </SheetHeader>
              <div className="flex flex-col gap-5 px-4 pb-6 text-sm">
                <div className="grid grid-cols-2 gap-3">
                  {EVAL_METRICS.map((m) => (
                    <div key={m} className="rounded-lg border p-3">
                      <div className="text-xs text-muted-foreground">{LABELS[m].title}</div>
                      <div className="text-xl font-semibold tabular-nums">{pct(detail[m])}</div>
                      {detail.details[m]?.error && (
                        <div className="text-xs text-destructive">{detail.details[m]?.error}</div>
                      )}
                    </div>
                  ))}
                </div>

                <section className="flex flex-col gap-1">
                  <h3 className="font-medium">Answer</h3>
                  <p className="whitespace-pre-wrap rounded-lg bg-muted p-3">{detail.answer}</p>
                  {detail.details.answer_relevance?.reason && (
                    <p className="text-xs text-muted-foreground">
                      Relevance: {detail.details.answer_relevance.reason}
                    </p>
                  )}
                </section>

                {!!detail.details.faithfulness?.claims?.length && (
                  <section className="flex flex-col gap-1.5">
                    <h3 className="font-medium">Claims</h3>
                    {detail.details.faithfulness.claims.map((c, i) => (
                      <div key={i} className="flex gap-2">
                        <Verdict ok={c.supported} />
                        <span>{c.claim}</span>
                      </div>
                    ))}
                  </section>
                )}

                {!!detail.details.citation_accuracy?.statements?.length && (
                  <section className="flex flex-col gap-1.5">
                    <h3 className="font-medium">Cited sentences</h3>
                    {detail.details.citation_accuracy.statements.map((s, i) => (
                      <div key={i} className="flex gap-2">
                        <Verdict ok={s.supported} />
                        <span>{s.text}</span>
                      </div>
                    ))}
                  </section>
                )}

                <section className="flex flex-col gap-2">
                  <h3 className="font-medium">Sources the model saw</h3>
                  {detail.sources.length === 0 && (
                    <p className="text-muted-foreground">No sources were retrieved.</p>
                  )}
                  {detail.sources.map((s) => {
                    const relevant = detail.details.context_precision?.relevant?.includes(s.n);
                    return (
                      <div
                        key={s.n}
                        className={cn("rounded-lg border p-3", s.missing && "opacity-60")}
                      >
                        <div className="mb-1 flex items-center gap-2 text-xs text-muted-foreground">
                          <span className="font-medium text-foreground">[{s.n}]</span>
                          {s.doc_title || "Source"}
                          {relevant !== undefined && (
                            <Badge variant="outline" className="ml-auto font-normal">
                              {relevant ? "relevant" : "not relevant"}
                            </Badge>
                          )}
                        </div>
                        <p className="line-clamp-6 whitespace-pre-wrap text-xs">{s.text}</p>
                      </div>
                    );
                  })}
                </section>

                {detail.comments.length > 0 && (
                  <section className="flex flex-col gap-1.5">
                    <h3 className="font-medium">User feedback</h3>
                    {detail.comments.map((c, i) => (
                      <div key={i} className="flex gap-2">
                        {c.rating > 0 ? (
                          <ThumbsUp className="mt-0.5 size-3.5 shrink-0" />
                        ) : (
                          <ThumbsDown className="mt-0.5 size-3.5 shrink-0" />
                        )}
                        <span className={cn(!c.comment && "text-muted-foreground")}>
                          {c.comment ?? "(no comment)"}
                        </span>
                      </div>
                    ))}
                  </section>
                )}

                <Button variant="outline" onClick={rerun} disabled={rerunning}>
                  {rerunning ? <Loader2 className="animate-spin" /> : <RotateCw />}{" "}
                  Re-evaluate now
                </Button>
              </div>
            </>
          )}
        </SheetContent>
      </Sheet>
    </div>
  );
}
