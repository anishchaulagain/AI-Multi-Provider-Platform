"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { FileText, Loader2, RotateCw, Trash2, Upload } from "lucide-react";

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
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import {
  ACCEPTED_UPLOADS,
  api,
  ApiError,
  type ChunkPage,
  type DocumentEvent,
  type DocumentInfo,
  type DocumentStatus,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn } from "@/lib/utils";

const IN_PROGRESS: DocumentStatus[] = [
  "queued",
  "parsing",
  "chunking",
  "embedding",
];
const CHUNK_PAGE = 20;

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function StatusBadge({ doc }: { doc: DocumentInfo }) {
  const busy = IN_PROGRESS.includes(doc.status);
  const variant =
    doc.status === "ready"
      ? "secondary"
      : doc.status === "failed"
        ? "destructive"
        : "outline";
  return (
    <Badge variant={variant} className="gap-1">
      {busy && <Loader2 className="size-3 animate-spin" />}
      {doc.status}
      {doc.attempts > 1 && busy && ` (try ${doc.attempts})`}
    </Badge>
  );
}

export default function DocumentsPage() {
  const auth = useAuth();
  const token = auth.status === "authenticated" ? auth.token : null;

  const [docs, setDocs] = useState<DocumentInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [uploading, setUploading] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [preview, setPreview] = useState<DocumentInfo | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    if (!token) return;
    try {
      setDocs((await api.listDocuments(token)).items);
    } catch (err) {
      setError(
        err instanceof ApiError ? err.message : "Failed to load documents",
      );
    }
  }, [token]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- initial data fetch
    void load();
  }, [load]);

  // Live status: apply events to the list; refetch a row we don't know yet.
  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    const apply = (event: DocumentEvent) => {
      setDocs((current) => {
        if (!current) return current;
        if (event.deleted) return current.filter((d) => d.id !== event.doc_id);
        if (!current.some((d) => d.id === event.doc_id)) {
          void load();
          return current;
        }
        return current.map((d) =>
          d.id === event.doc_id
            ? {
                ...d,
                status: event.status as DocumentStatus,
                error: event.error,
                chunk_count: event.chunk_count ?? d.chunk_count,
                attempts: event.attempts ?? d.attempts,
              }
            : d,
        );
      });
      // Terminal states carry fields (title, pages, summary) the event doesn't.
      if (event.status === "ready") void load();
    };
    void api.watchDocuments(token, apply, controller.signal);
    return () => controller.abort();
  }, [token, load]);

  async function upload(files: FileList | File[]) {
    if (!token) return;
    setError(null);
    setNotice(null);
    const list = Array.from(files);
    setUploading((n) => n + list.length);
    const duplicates: string[] = [];
    for (const file of list) {
      try {
        const { document, duplicate } = await api.uploadDocument(token, file);
        if (duplicate) duplicates.push(file.name);
        setDocs((current) =>
          current && !current.some((d) => d.id === document.id)
            ? [document, ...current]
            : current,
        );
      } catch (err) {
        setError(
          `${file.name}: ${err instanceof ApiError ? err.message : "upload failed"}`,
        );
      } finally {
        setUploading((n) => n - 1);
      }
    }
    if (duplicates.length)
      setNotice(`Already uploaded: ${duplicates.join(", ")}`);
  }

  async function remove(doc: DocumentInfo) {
    if (
      !token ||
      !confirm(`Delete "${doc.title ?? doc.filename}" and its chunks?`)
    )
      return;
    try {
      await api.deleteDocument(token, doc.id);
      setDocs((current) => current?.filter((d) => d.id !== doc.id) ?? null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Delete failed");
    }
  }

  async function reingest(doc: DocumentInfo) {
    if (!token) return;
    try {
      const updated = await api.reingestDocument(token, doc.id);
      setDocs(
        (current) =>
          current?.map((d) => (d.id === doc.id ? updated : d)) ?? null,
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Re-ingest failed");
    }
  }

  return (
    <div className="flex max-w-5xl flex-col gap-6">
      <div>
        <h1 className="text-2xl font-semibold">Documents</h1>
        <p className="text-sm text-muted-foreground">
          Uploads are parsed, chunked and embedded in the background. Status
          updates live.
        </p>
      </div>

      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          if (e.dataTransfer.files.length) void upload(e.dataTransfer.files);
        }}
        className={cn(
          "flex flex-col items-center gap-2 rounded-xl border-2 border-dashed p-8 text-center transition-colors",
          dragging ? "border-primary bg-primary/5" : "hover:bg-muted/50",
        )}
      >
        {uploading ? (
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        ) : (
          <Upload className="size-6 text-muted-foreground" />
        )}
        <span className="font-medium">
          {uploading
            ? `Uploading ${uploading}…`
            : "Drop files here or click to upload"}
        </span>
        <span className="text-xs text-muted-foreground">
          PDF, DOCX, Markdown, HTML or TXT
        </span>
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ACCEPTED_UPLOADS}
          className="hidden"
          onChange={(e) => {
            if (e.target.files?.length) void upload(e.target.files);
            e.target.value = "";
          }}
        />
      </button>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {notice && (
        <Alert>
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Library</CardTitle>
          <CardDescription>
            {docs
              ? `${docs.length} document${docs.length === 1 ? "" : "s"}`
              : "Loading…"}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col divide-y">
          {docs?.length === 0 && (
            <p className="py-6 text-center text-sm text-muted-foreground">
              No documents yet.
            </p>
          )}
          {docs?.map((doc) => (
            <div key={doc.id} className="flex items-start gap-3 py-3">
              <FileText className="mt-0.5 size-5 shrink-0 text-muted-foreground" />
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    className="truncate text-left font-medium hover:underline disabled:no-underline"
                    disabled={doc.status !== "ready"}
                    onClick={() => setPreview(doc)}
                  >
                    {doc.title ?? doc.filename}
                  </button>
                  <StatusBadge doc={doc} />
                </div>
                <div className="text-xs text-muted-foreground">
                  {doc.filename} · {formatBytes(doc.size_bytes)}
                  {doc.page_count ? ` · ${doc.page_count} pages` : ""}
                  {doc.status === "ready" ? ` · ${doc.chunk_count} chunks` : ""}
                </div>
                {doc.details.summary && (
                  <p className="text-sm text-muted-foreground">
                    {doc.details.summary}
                  </p>
                )}
                {doc.error && (
                  <p
                    className={cn(
                      "text-xs",
                      doc.status === "failed"
                        ? "text-destructive"
                        : "text-muted-foreground",
                    )}
                  >
                    {doc.error}
                  </p>
                )}
              </div>
              <div className="flex shrink-0 gap-1">
                {(doc.status === "failed" || doc.status === "ready") && (
                  <Button
                    variant="ghost"
                    size="icon"
                    title="Re-ingest"
                    onClick={() => reingest(doc)}
                  >
                    <RotateCw />
                  </Button>
                )}
                <Button
                  variant="ghost"
                  size="icon"
                  title="Delete"
                  onClick={() => remove(doc)}
                >
                  <Trash2 />
                </Button>
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      <ChunkSheet
        doc={preview}
        token={token}
        onClose={() => setPreview(null)}
      />
    </div>
  );
}

function ChunkSheet({
  doc,
  token,
  onClose,
}: {
  doc: DocumentInfo | null;
  token: string | null;
  onClose: () => void;
}) {
  const [page, setPage] = useState<ChunkPage | null>(null);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!doc || !token) return;
    let cancelled = false;
    api
      .documentChunks(token, doc.id, offset, CHUNK_PAGE)
      .then((p) => !cancelled && setPage(p))
      .catch(
        (err) =>
          !cancelled &&
          setError(err instanceof ApiError ? err.message : "Failed"),
      );
    return () => {
      cancelled = true;
    };
  }, [doc, token, offset]);

  return (
    <Sheet
      open={doc !== null}
      onOpenChange={(open) => {
        if (!open) {
          onClose();
          setPage(null);
          setOffset(0);
          setError(null);
        }
      }}
    >
      <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>{doc?.title ?? doc?.filename}</SheetTitle>
          <SheetDescription>
            {page
              ? `Chunks ${offset + 1}–${offset + page.items.length} of ${page.total}`
              : "Loading…"}
          </SheetDescription>
          {doc?.details.keywords && (
            <div className="flex flex-wrap gap-1 pt-1">
              {doc.details.keywords.map((k) => (
                <Badge key={k} variant="outline">
                  {k}
                </Badge>
              ))}
            </div>
          )}
        </SheetHeader>
        <div className="flex flex-col gap-3 px-4 pb-4">
          {error && <p className="text-sm text-destructive">{error}</p>}
          {page?.items.map((chunk) => (
            <div key={chunk.id} className="rounded-lg border p-3 text-sm">
              <div className="mb-1 flex flex-wrap gap-2 text-xs text-muted-foreground">
                <span>#{chunk.chunk_index + 1}</span>
                {chunk.page && <span>page {chunk.page}</span>}
                {chunk.section && (
                  <span className="truncate">{chunk.section}</span>
                )}
              </div>
              <p className="whitespace-pre-wrap">{chunk.text}</p>
            </div>
          ))}
          {page && page.total > CHUNK_PAGE && (
            <div className="flex justify-between">
              <Button
                variant="outline"
                size="sm"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - CHUNK_PAGE))}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={offset + CHUNK_PAGE >= page.total}
                onClick={() => setOffset(offset + CHUNK_PAGE)}
              >
                Next
              </Button>
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
