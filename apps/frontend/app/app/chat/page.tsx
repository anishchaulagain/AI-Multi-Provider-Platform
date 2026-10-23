"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import {
  FileText,
  Filter,
  Loader2,
  MessageSquarePlus,
  Send,
  Square,
  Trash2,
} from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Textarea } from "@/components/ui/textarea";
import {
  api,
  ApiError,
  type ChatUsage,
  type Citation,
  type Conversation,
  type DocumentInfo,
  type Message,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

type UiMessage = Message & {
  streaming?: boolean;
  invalid?: number[];
  error?: string;
};

const CITATION = /\[(\d{1,3}(?:\s*,\s*\d{1,3})*)\]/g;

/** Render answer text with [n] markers as buttons that open the cited source. */
function AnswerText({
  text,
  citations,
  onCite,
}: {
  text: string;
  citations: Citation[];
  onCite: (citation: Citation) => void;
}) {
  const byN = new Map(citations.map((c) => [c.n, c]));
  const parts: React.ReactNode[] = [];
  let last = 0;
  for (const match of text.matchAll(CITATION)) {
    parts.push(text.slice(last, match.index));
    const numbers = match[1].split(",").map((n) => Number(n.trim()));
    parts.push(
      <span key={match.index} className="whitespace-nowrap">
        {numbers.map((n) => {
          const citation = byN.get(n);
          return citation ? (
            <button
              key={n}
              type="button"
              onClick={() => onCite(citation)}
              className="mx-0.5 rounded bg-primary/10 px-1 text-xs font-medium text-primary hover:bg-primary/20"
              title={citation.doc_title ?? "Source"}
            >
              {n}
            </button>
          ) : (
            // Invalid or not-yet-resolved citation: shown, but not clickable.
            <span key={n} className="mx-0.5 px-1 text-xs text-muted-foreground">
              [{n}]
            </span>
          );
        })}
      </span>,
    );
    last = (match.index ?? 0) + match[0].length;
  }
  parts.push(text.slice(last));
  return <div className="whitespace-pre-wrap leading-relaxed">{parts}</div>;
}

function UsageLine({ usage }: { usage: ChatUsage }) {
  const gw = usage.gateway;
  return (
    <div className="flex flex-wrap gap-1.5 text-xs text-muted-foreground">
      {gw?.deployment && <Badge variant="outline">{gw.deployment}</Badge>}
      {gw?.provider && <Badge variant="outline">{gw.provider}</Badge>}
      <Badge variant="outline">
        {usage.prompt_tokens} in / {usage.completion_tokens} out
      </Badge>
      {usage.sources !== undefined && (
        <Badge variant="outline">{usage.sources} sources</Badge>
      )}
      {usage.retrieval_ms?.total !== undefined && (
        <Badge variant="outline">retrieval {usage.retrieval_ms.total} ms</Badge>
      )}
    </div>
  );
}

export default function ChatPage() {
  const auth = useAuth();
  const token = auth.status === "authenticated" ? auth.token : null;

  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [active, setActive] = useState<Conversation | null>(null);
  const [messages, setMessages] = useState<UiMessage[]>([]);
  const [documents, setDocuments] = useState<DocumentInfo[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<Citation | null>(null);
  const [filterOpen, setFilterOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const fail = (err: unknown, fallback: string) =>
    setError(
      err instanceof ApiError ? `${err.message} (${err.code})` : fallback,
    );

  const loadConversations = useCallback(async () => {
    if (!token) return;
    try {
      setConversations(await api.listConversations(token));
    } catch (err) {
      fail(err, "Failed to load conversations");
    }
  }, [token]);

  useEffect(() => {
    if (!token) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial data fetch
    void loadConversations();
    api
      .listDocuments(token, 200)
      .then((r) => setDocuments(r.items.filter((d) => d.status === "ready")))
      .catch(() => setDocuments([]));
  }, [token, loadConversations]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  async function open(conversation: Conversation) {
    if (!token) return;
    abortRef.current?.abort();
    setActive(conversation);
    setError(null);
    try {
      setMessages(await api.listMessages(token, conversation.id));
    } catch (err) {
      fail(err, "Failed to load messages");
    }
  }

  async function newConversation() {
    if (!token) return;
    try {
      const conversation = await api.createConversation(token);
      setConversations((c) => [conversation, ...c]);
      setActive(conversation);
      setMessages([]);
    } catch (err) {
      fail(err, "Failed to create a conversation");
    }
  }

  async function remove(conversation: Conversation) {
    if (!token || !confirm(`Delete "${conversation.title}"?`)) return;
    try {
      await api.deleteConversation(token, conversation.id);
      setConversations((c) => c.filter((x) => x.id !== conversation.id));
      if (active?.id === conversation.id) {
        setActive(null);
        setMessages([]);
      }
    } catch (err) {
      fail(err, "Failed to delete");
    }
  }

  async function setFilter(docIds: string[]) {
    if (!token || !active) return;
    try {
      const updated = await api.updateConversation(token, active.id, {
        doc_ids: docIds,
      });
      setActive(updated);
      setConversations((c) =>
        c.map((x) => (x.id === updated.id ? updated : x)),
      );
    } catch (err) {
      fail(err, "Failed to update the document filter");
    }
  }

  async function send() {
    const question = input.trim();
    if (!token || !question || sending) return;
    let conversation = active;
    if (!conversation) {
      try {
        conversation = await api.createConversation(token);
        setConversations((c) => [conversation as Conversation, ...c]);
        setActive(conversation);
      } catch (err) {
        fail(err, "Failed to create a conversation");
        return;
      }
    }

    const controller = new AbortController();
    abortRef.current = controller;
    setSending(true);
    setError(null);
    setInput("");
    const now = new Date().toISOString();
    const userMsg: UiMessage = {
      id: `local-user-${now}`,
      role: "user",
      content: question,
      token_count: 0,
      citations: [],
      usage: null,
      created_at: now,
    };
    const draftId = `local-assistant-${now}`;
    setMessages((m) => [
      ...m,
      userMsg,
      {
        ...userMsg,
        id: draftId,
        role: "assistant",
        content: "",
        streaming: true,
      },
    ]);
    const patch = (update: (m: UiMessage) => UiMessage) =>
      setMessages((all) => all.map((m) => (m.id === draftId ? update(m) : m)));

    try {
      const { messageId } = await api.chatInConversation(
        token,
        conversation.id,
        question,
        {
          signal: controller.signal,
          onToken: (text) =>
            patch((m) => ({ ...m, content: m.content + text })),
          onCitations: (citations, invalid) =>
            patch((m) => ({ ...m, citations, invalid })),
          onUsage: (usage) => patch((m) => ({ ...m, usage })),
          onError: (_code, message) => patch((m) => ({ ...m, error: message })),
        },
      );
      patch((m) => ({ ...m, id: messageId ?? m.id, streaming: false }));
      void loadConversations(); // title and ordering change after the first answer
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        patch((m) => ({ ...m, streaming: false }));
      } else {
        setMessages((all) => all.filter((m) => m.id !== draftId));
        fail(err, "Chat request failed");
      }
    } finally {
      setSending(false);
      abortRef.current = null;
    }
  }

  const filterLabel =
    active && active.doc_ids.length
      ? `${active.doc_ids.length} document${active.doc_ids.length === 1 ? "" : "s"}`
      : "All documents";

  return (
    <div className="flex h-[calc(100vh-6rem)] gap-6">
      <aside className="hidden w-64 shrink-0 flex-col gap-2 md:flex">
        <Button
          onClick={newConversation}
          variant="outline"
          className="justify-start"
        >
          <MessageSquarePlus /> New chat
        </Button>
        <div className="flex flex-col gap-1 overflow-y-auto">
          {conversations.map((c) => (
            <div
              key={c.id}
              className={cn(
                "group flex items-center gap-1 rounded-md px-2 py-1.5 text-sm hover:bg-muted",
                active?.id === c.id && "bg-muted font-medium",
              )}
            >
              <button
                type="button"
                className="flex-1 truncate text-left"
                onClick={() => open(c)}
              >
                {c.title}
              </button>
              <button
                type="button"
                title="Delete"
                className="opacity-0 group-hover:opacity-100"
                onClick={() => remove(c)}
              >
                <Trash2 className="size-3.5 text-muted-foreground" />
              </button>
            </div>
          ))}
        </div>
      </aside>

      <section className="flex min-w-0 flex-1 flex-col gap-3">
        <div className="flex items-center justify-between gap-2">
          <h1 className="truncate text-xl font-semibold">
            {active?.title ?? "Chat with your documents"}
          </h1>
          <Button
            variant="outline"
            size="sm"
            disabled={!active}
            onClick={() => setFilterOpen(true)}
          >
            <Filter /> {filterLabel}
          </Button>
        </div>

        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}

        <div className="flex flex-1 flex-col gap-4 overflow-y-auto rounded-xl border p-4">
          {messages.length === 0 && (
            <p className="m-auto max-w-sm text-center text-sm text-muted-foreground">
              Ask a question about your uploaded documents. Answers cite their
              sources as [n]; click a number to see the passage.
            </p>
          )}
          {messages.map((m) => (
            <div
              key={m.id}
              className={cn(
                "flex max-w-[85%] flex-col gap-2 rounded-xl px-4 py-3 text-sm",
                m.role === "user"
                  ? "self-end bg-primary text-primary-foreground"
                  : "self-start bg-muted",
              )}
            >
              {m.role === "user" ? (
                <div className="whitespace-pre-wrap">{m.content}</div>
              ) : (
                <>
                  {m.content ? (
                    <AnswerText
                      text={m.content}
                      citations={m.citations}
                      onCite={setSource}
                    />
                  ) : (
                    <span className="flex items-center gap-2 text-muted-foreground">
                      <Loader2 className="size-4 animate-spin" /> Searching
                      documents…
                    </span>
                  )}
                  {m.error && (
                    <p className="text-xs text-destructive">
                      Stream interrupted: {m.error}
                    </p>
                  )}
                  {m.citations.length > 0 && (
                    <div className="flex flex-col gap-1 border-t pt-2">
                      {m.citations.map((c) => (
                        <button
                          key={c.n}
                          type="button"
                          onClick={() => setSource(c)}
                          className="flex items-start gap-2 text-left text-xs hover:underline"
                        >
                          <span className="font-medium">[{c.n}]</span>
                          <span
                            className={cn(
                              "truncate",
                              c.missing && "line-through",
                            )}
                          >
                            {c.doc_title ?? "Source"}
                            {c.page ? `, p. ${c.page}` : ""}
                            {c.section ? ` · ${c.section}` : ""}
                          </span>
                        </button>
                      ))}
                    </div>
                  )}
                  {m.usage && !m.streaming && <UsageLine usage={m.usage} />}
                </>
              )}
            </div>
          ))}
          <div ref={bottomRef} />
        </div>

        <div className="flex items-end gap-2">
          <Textarea
            rows={2}
            value={input}
            placeholder="Ask about your documents… (Enter to send, Shift+Enter for a new line)"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
          />
          {sending ? (
            <Button variant="outline" onClick={() => abortRef.current?.abort()}>
              <Square /> Stop
            </Button>
          ) : (
            <Button onClick={send} disabled={!input.trim()}>
              <Send /> Send
            </Button>
          )}
        </div>
      </section>

      <Sheet open={source !== null} onOpenChange={(o) => !o && setSource(null)}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-lg">
          <SheetHeader>
            <SheetTitle>
              [{source?.n}] {source?.doc_title ?? "Source"}
            </SheetTitle>
            <SheetDescription>
              {[source?.page ? `Page ${source.page}` : null, source?.section]
                .filter(Boolean)
                .join(" · ") || "Cited passage"}
            </SheetDescription>
          </SheetHeader>
          <div className="px-4 pb-4 text-sm whitespace-pre-wrap">
            {source?.missing
              ? "This passage no longer exists (its document was deleted or re-ingested)."
              : (source?.text ?? source?.snippet)}
          </div>
        </SheetContent>
      </Sheet>

      <Sheet open={filterOpen} onOpenChange={setFilterOpen}>
        <SheetContent className="w-full overflow-y-auto sm:max-w-md">
          <SheetHeader>
            <SheetTitle>Documents for this chat</SheetTitle>
            <SheetDescription>
              Answers only use the selected documents. None selected means all.
            </SheetDescription>
          </SheetHeader>
          <div className="flex flex-col gap-1 px-4 pb-4">
            {documents.length === 0 && (
              <p className="text-sm text-muted-foreground">
                No ready documents yet.
              </p>
            )}
            {documents.map((d) => {
              const checked = active?.doc_ids.includes(d.id) ?? false;
              return (
                <Fragment key={d.id}>
                  <label className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-1.5 text-sm hover:bg-muted">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() =>
                        void setFilter(
                          checked
                            ? (active?.doc_ids ?? []).filter((x) => x !== d.id)
                            : [...(active?.doc_ids ?? []), d.id],
                        )
                      }
                    />
                    <FileText className="size-4 text-muted-foreground" />
                    <span className="truncate">{d.title ?? d.filename}</span>
                  </label>
                </Fragment>
              );
            })}
            {active && active.doc_ids.length > 0 && (
              <Button
                variant="ghost"
                size="sm"
                className="mt-2 self-start"
                onClick={() => setFilter([])}
              >
                Use all documents
              </Button>
            )}
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}
