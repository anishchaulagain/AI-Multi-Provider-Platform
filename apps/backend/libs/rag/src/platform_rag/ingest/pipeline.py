"""Ingestion pipeline: parse -> chunk -> metadata -> embed (dense + BM25) -> store
-> graph (entities/relations into Neo4j).

Pure orchestration: no queue or database code here, so it is testable with fakes.
The caller reports stage changes (`on_stage`) and classifies failures.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import structlog

from llm_client import Caller, GatewayClient
from platform_graph import GraphChunk, GraphExtraction, extract_graph
from platform_rag.formats import DocumentParseError
from platform_rag.ingest.chunking import Chunk, chunk_document
from platform_rag.ingest.metadata import extract_metadata
from platform_rag.ingest.parsing import parse
from platform_rag.sparse import Bm25Encoder
from platform_rag.store import ChunkRecord, ChunkStore, chunk_point_id

logger = structlog.get_logger(__name__)

Stage = Literal["parsing", "chunking", "embedding", "graphing"]
StageCallback = Callable[[Stage], Awaitable[None]]


class GraphSink(Protocol):
    async def replace_document(
        self,
        *,
        tenant_id: uuid.UUID,
        doc_id: uuid.UUID,
        title: str,
        chunks: list[GraphChunk],
        extraction: GraphExtraction,
    ) -> None: ...


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
    # Knowledge graph (needs a graph sink). Extraction sends several chunks per LLM
    # call; documents longer than graph_max_chunks only get their first chunks graphed.
    graph_extraction: bool = True
    graph_alias: str = "extract"
    graph_batch_chars: int = 6000
    graph_max_chunks: int = 300


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
        graph: GraphSink | None = None,
        config: PipelineConfig | None = None,
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._graph = graph
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

        if self._graph is not None and cfg.graph_extraction:
            await on_stage("graphing")
            details["graph"] = await self._build_graph(tenant_id, doc_id, title, chunks, caller)

        return IngestResult(
            title=title,
            format=parsed.format,
            page_count=parsed.page_count,
            chunk_count=len(records),
            details=details,
        )

    async def _build_graph(
        self,
        tenant_id: uuid.UUID,
        doc_id: uuid.UUID,
        title: str,
        chunks: list[Chunk],
        caller: Caller,
    ) -> dict[str, Any]:
        """Extract and write the document's graph. Never fails the document: vector
        retrieval works without it, and re-ingesting repairs a failed graph."""
        assert self._graph is not None
        cfg = self.config
        graphed = chunks[: cfg.graph_max_chunks]
        try:
            extraction = await extract_graph(
                self._gateway,
                caller,
                [(c.index, c.text) for c in graphed],
                model=cfg.graph_alias,
                batch_chars=cfg.graph_batch_chars,
            )
            await self._graph.replace_document(
                tenant_id=tenant_id,
                doc_id=doc_id,
                title=title,
                chunks=[
                    GraphChunk(chunk_point_id(doc_id, c.index), c.index, c.page) for c in chunks
                ],
                extraction=extraction,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph_build_failed", doc_id=str(doc_id), error=repr(exc)[:300])
            return {"status": "failed", "error": str(exc)[:500] or type(exc).__name__}
        return {
            "status": "partial" if extraction.failed_batches else "ok",
            "entities": extraction.entity_count,
            "relations": len({r for r, _ in extraction.relations}),
            "chunks_graphed": len(graphed),
            "failed_batches": extraction.failed_batches,
        }

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
