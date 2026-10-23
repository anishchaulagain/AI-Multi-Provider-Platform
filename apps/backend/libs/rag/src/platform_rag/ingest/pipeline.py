"""Ingestion pipeline: parse -> chunk -> metadata -> embed (dense + BM25) -> store.

Pure orchestration: no queue or database code here, so it is testable with fakes.
The caller reports stage changes (`on_stage`) and classifies failures.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from llm_client import Caller, GatewayClient
from platform_rag.formats import DocumentParseError
from platform_rag.ingest.chunking import Chunk, chunk_document
from platform_rag.ingest.metadata import extract_metadata
from platform_rag.ingest.parsing import parse
from platform_rag.ingest.sparse import Bm25Encoder
from platform_rag.store import ChunkRecord, ChunkStore

logger = structlog.get_logger(__name__)

Stage = Literal["parsing", "chunking", "embedding"]
StageCallback = Callable[[Stage], Awaitable[None]]


@dataclass(frozen=True)
class PipelineConfig:
    chunk_size: int = 512
    chunk_overlap: int = 64
    embed_alias: str = "embed"
    embed_batch_size: int = 32
    # nomic-embed-text expects task prefixes; queries use "search_query: ".
    embed_document_prefix: str = "search_document: "
    extract_metadata: bool = True
    extract_alias: str = "extract"
    max_chunks: int = 5000


@dataclass
class IngestResult:
    title: str
    format: str
    page_count: int | None
    chunk_count: int
    details: dict[str, Any] = field(default_factory=dict)


class EmbeddingMismatchError(Exception):
    pass


def embedding_text(chunk: Chunk, *, title: str, prefix: str) -> str:
    """Prepend document title and section so each chunk embeds with its context."""
    header = "\n".join(p for p in (title, chunk.section) if p)
    return f"{prefix}{header}\n\n{chunk.text}" if header else f"{prefix}{chunk.text}"


class IngestionPipeline:
    def __init__(
        self,
        gateway: GatewayClient,
        store: ChunkStore,
        *,
        sparse: Bm25Encoder | None = None,
        config: PipelineConfig | None = None,
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._sparse = sparse or Bm25Encoder()
        self.config = config or PipelineConfig()

    async def run(
        self,
        *,
        tenant_id: uuid.UUID,
        doc_id: uuid.UUID,
        filename: str,
        data: bytes,
        caller: Caller,
        on_stage: StageCallback,
    ) -> IngestResult:
        cfg = self.config
        log = logger.bind(doc_id=str(doc_id))

        await on_stage("parsing")
        parsed = await asyncio.to_thread(parse, data, filename)
        title = parsed.title or filename

        await on_stage("chunking")
        chunks = await asyncio.to_thread(
            chunk_document, parsed, chunk_size=cfg.chunk_size, chunk_overlap=cfg.chunk_overlap
        )
        if not chunks:
            raise DocumentParseError("Document produced no text chunks")
        if len(chunks) > cfg.max_chunks:
            raise DocumentParseError(
                f"Document is too large ({len(chunks)} chunks, limit {cfg.max_chunks})"
            )
        log.info("document_chunked", format=parsed.format, chunks=len(chunks))

        details: dict[str, Any] = {"format": parsed.format, **parsed.metadata}
        if cfg.extract_metadata:
            meta = await extract_metadata(
                self._gateway, caller, title=title, text=parsed.text, model=cfg.extract_alias
            )
            if meta is not None:
                details.update(summary=meta.summary, keywords=meta.keywords)

        await on_stage("embedding")
        texts = [embedding_text(c, title=title, prefix=cfg.embed_document_prefix) for c in chunks]
        dense = await self._embed(texts, caller)
        sparse = await asyncio.to_thread(lambda: [self._sparse.encode_document(t) for t in texts])

        records = [
            ChunkRecord(
                tenant_id=tenant_id,
                doc_id=doc_id,
                chunk_index=c.index,
                text=c.text,
                page=c.page,
                section=c.section,
                metadata={"doc_title": title, "filename": filename},
                dense=vector,
                sparse=sparse_vector,
            )
            for c, vector, sparse_vector in zip(chunks, dense, sparse, strict=True)
        ]
        await self._store.replace_document(tenant_id, doc_id, records)
        log.info("document_stored", collection=self._store.collection, chunks=len(records))

        return IngestResult(
            title=title,
            format=parsed.format,
            page_count=parsed.page_count,
            chunk_count=len(records),
            details=details,
        )

    async def _embed(self, texts: list[str], caller: Caller) -> list[list[float]]:
        cfg = self.config
        vectors: list[list[float]] = []
        for start in range(0, len(texts), cfg.embed_batch_size):
            batch = texts[start : start + cfg.embed_batch_size]
            result = await self._gateway.embed(batch, caller=caller, model=cfg.embed_alias)
            data = sorted(result.data.data, key=lambda d: d.index)
            if len(data) != len(batch):
                raise EmbeddingMismatchError(f"Expected {len(batch)} embeddings, got {len(data)}")
            vectors.extend(d.embedding for d in data)
        dims = {len(v) for v in vectors}
        if len(dims) != 1 or 0 in dims:
            raise EmbeddingMismatchError(f"Inconsistent embedding dimensions: {sorted(dims)}")
        return vectors
