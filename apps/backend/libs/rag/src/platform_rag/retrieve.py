"""Hybrid retrieval: query rewrite -> dense+BM25 search -> graph expansion -> RRF
fusion -> cross-encoder rerank -> token budget -> neighboring chunks."""

import asyncio
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import structlog

from llm_client import Caller, ChatCompletionRequest, ChatMessage, GatewayClient, GatewayError
from platform_graph import EntityHit, Expansion, Fact
from platform_rag.rerank import NoReranker, Reranker
from platform_rag.sparse import Bm25Encoder
from platform_rag.store import ChunkStore, StoredChunk

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Turn:
    role: Literal["user", "assistant"]
    content: str


class GraphExpander(Protocol):
    async def expand(
        self,
        tenant_id: uuid.UUID,
        question: str,
        *,
        doc_ids: Sequence[uuid.UUID] | None = None,
        hops: int = 1,
        max_seeds: int = 5,
        max_facts: int = 15,
        max_chunks: int = 30,
    ) -> Expansion: ...


@dataclass(frozen=True)
class RetrievalConfig:
    embed_alias: str = "embed"
    query_prefix: str = "search_query: "
    rewrite_alias: str = "chat-fast"
    candidates: int = 30  # per list (vector, graph)
    rerank_candidates: int = 20
    final_k: int = 8
    graph_hops: int = 1
    max_facts: int = 12
    rrf_k: int = 60
    context_token_budget: int = 3000
    neighbor_window: int = 1
    neighbors_for_top: int = 3


@dataclass
class RetrievedChunk:
    chunk: StoredChunk
    vector_rank: int | None = None
    graph_rank: int | None = None
    rrf: float = 0.0
    rerank: float | None = None
    neighbor_of: str | None = None

    @property
    def id(self) -> str:
        return self.chunk.id

    def trace(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk.id,
            "doc_id": self.chunk.doc_id,
            "chunk_index": self.chunk.chunk_index,
            "vector_rank": self.vector_rank,
            "graph_rank": self.graph_rank,
            "rrf": round(self.rrf, 5),
            "rerank": None if self.rerank is None else round(self.rerank, 4),
            "neighbor_of": self.neighbor_of,
        }


@dataclass
class RetrievalResult:
    question: str
    query: str  # standalone (rewritten) question used for search
    chunks: list[RetrievedChunk] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    seeds: list[EntityHit] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)
    reranker: str = "none"
    graph_error: str | None = None

    def trace(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "rewritten": self.query != self.question,
            "reranker": self.reranker,
            "chunks": [c.trace() for c in self.chunks],
            "graph": {
                "seeds": [{"id": s.id, "name": s.name, "type": s.type} for s in self.seeds],
                "facts": [f.text() for f in self.facts],
                "error": self.graph_error,
            },
            "timings_ms": self.timings_ms,
        }


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def rrf_fuse(rankings: Sequence[Sequence[str]], *, k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return scores


_REWRITE_SYSTEM = (
    "Rewrite the user's last message as a standalone search question, resolving pronouns "
    "and references using the conversation. Reply with the question only."
)


class Retriever:
    def __init__(
        self,
        gateway: GatewayClient,
        store: ChunkStore,
        *,
        graph: GraphExpander | None = None,
        reranker: Reranker | None = None,
        sparse: Bm25Encoder | None = None,
        config: RetrievalConfig | None = None,
    ) -> None:
        self._gateway = gateway
        self._store = store
        self._graph = graph
        self._reranker = reranker or NoReranker()
        self._sparse = sparse or Bm25Encoder()
        self.config = config or RetrievalConfig()

    async def rewrite(self, question: str, history: Sequence[Turn], caller: Caller) -> str:
        """Standalone question from the last two turns (Phase 5 replaces this with
        the compacted history). Falls back to the raw question on any failure."""
        recent = list(history)[-4:]
        if not recent:
            return question
        transcript = "\n".join(f"{t.role}: {t.content[:1000]}" for t in recent)
        try:
            result = await self._gateway.chat(
                ChatCompletionRequest(
                    model=self.config.rewrite_alias,
                    messages=[
                        ChatMessage(role="system", content=_REWRITE_SYSTEM),
                        ChatMessage(
                            role="user",
                            content=f"Conversation:\n{transcript}\n\nLast message: {question}",
                        ),
                    ],
                    temperature=0,
                    max_tokens=120,
                ),
                caller=caller,
            )
        except GatewayError as exc:
            logger.warning("query_rewrite_failed", error=exc.code)
            return question
        rewritten = result.data.text().strip().strip('"').strip()
        return rewritten if 3 <= len(rewritten) <= 500 else question

    async def retrieve(
        self,
        tenant_id: uuid.UUID,
        question: str,
        *,
        caller: Caller,
        history: Sequence[Turn] = (),
        doc_ids: Sequence[uuid.UUID] | None = None,
    ) -> RetrievalResult:
        cfg = self.config
        timings: dict[str, int] = {}

        def tick(name: str, started: float) -> None:
            timings[name] = int((time.perf_counter() - started) * 1000)

        started = time.perf_counter()
        query = await self.rewrite(question, history, caller)
        tick("rewrite", started)
        result = RetrievalResult(question=question, query=query, reranker=self._reranker.name)

        # Vector and graph lists in parallel.
        started = time.perf_counter()
        vector_hits, (expansion, result.graph_error) = await asyncio.gather(
            self._vector_search(tenant_id, query, caller, doc_ids),
            self._graph_expand(tenant_id, query, doc_ids),
        )
        tick("search", started)
        result.seeds, result.facts = expansion.seeds, expansion.facts[: cfg.max_facts]

        # Fuse, then fetch payloads for chunks only the graph found.
        vector_ids = [c.id for c in vector_hits]
        graph_ids = [cid for cid, _ in expansion.chunks]
        fused = rrf_fuse([vector_ids, graph_ids], k=cfg.rrf_k)
        ordered = sorted(fused, key=lambda i: fused[i], reverse=True)[: cfg.rerank_candidates]
        by_id = {c.id: c for c in vector_hits}
        missing = [i for i in ordered if i not in by_id]
        for chunk in await self._store.get_chunks(tenant_id, missing):
            by_id[chunk.id] = chunk
        candidates = [
            RetrievedChunk(
                chunk=by_id[i],
                vector_rank=vector_ids.index(i) + 1 if i in vector_ids else None,
                graph_rank=graph_ids.index(i) + 1 if i in graph_ids else None,
                rrf=fused[i],
            )
            for i in ordered
            if i in by_id
        ]

        started = time.perf_counter()
        if candidates:
            try:
                scores = await self._reranker.score(query, [c.chunk.text for c in candidates])
            except Exception as exc:  # noqa: BLE001 - keep the fused order
                logger.warning("rerank_failed", reranker=self._reranker.name, error=repr(exc)[:200])
                result.reranker = f"{self._reranker.name} (failed: {type(exc).__name__})"
            else:
                for candidate, score in zip(candidates, scores, strict=True):
                    candidate.rerank = score
                candidates.sort(key=lambda c: c.rerank or 0.0, reverse=True)
        tick("rerank", started)

        result.chunks = await self._fit_budget(tenant_id, candidates[: cfg.final_k], result.facts)
        timings["total"] = sum(timings.values())
        result.timings_ms = timings
        return result

    async def _vector_search(
        self,
        tenant_id: uuid.UUID,
        query: str,
        caller: Caller,
        doc_ids: Sequence[uuid.UUID] | None,
    ) -> list[StoredChunk]:
        cfg = self.config
        embedded = await self._gateway.embed(
            f"{cfg.query_prefix}{query}", caller=caller, model=cfg.embed_alias
        )
        if not embedded.data.data:
            return []
        return await self._store.hybrid_search(
            tenant_id,
            dense=embedded.data.data[0].embedding,
            sparse=self._sparse.encode_query(query),
            doc_ids=doc_ids,
            limit=cfg.candidates,
        )

    async def _graph_expand(
        self, tenant_id: uuid.UUID, query: str, doc_ids: Sequence[uuid.UUID] | None
    ) -> tuple[Expansion, str | None]:
        """Graph context is best-effort: if Neo4j is down, answer from vectors alone."""
        if self._graph is None:
            return Expansion(), None
        cfg = self.config
        try:
            expansion = await self._graph.expand(
                tenant_id,
                query,
                doc_ids=doc_ids,
                hops=cfg.graph_hops,
                max_facts=cfg.max_facts,
                max_chunks=cfg.candidates,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("graph_expansion_failed", error=repr(exc)[:300])
            return Expansion(), type(exc).__name__
        return expansion, None

    async def _fit_budget(
        self, tenant_id: uuid.UUID, ranked: list[RetrievedChunk], facts: list[Fact]
    ) -> list[RetrievedChunk]:
        """Keep chunks in rank order within the token budget, then add the neighbors
        of the top chunks (placed right after them) while room remains."""
        cfg = self.config
        budget = cfg.context_token_budget - sum(estimate_tokens(f.text()) for f in facts)
        kept: list[RetrievedChunk] = []
        for item in ranked:
            cost = estimate_tokens(item.chunk.text)
            if kept and cost > budget:
                continue
            kept.append(item)
            budget -= cost

        if cfg.neighbor_window <= 0 or budget <= 0:
            return kept
        seen = {c.id for c in kept}
        out: list[RetrievedChunk] = []
        for position, item in enumerate(kept):
            out.append(item)
            if position >= cfg.neighbors_for_top:
                continue
            for neighbor in await self._store.neighbors(
                tenant_id, item.chunk, window=cfg.neighbor_window
            ):
                cost = estimate_tokens(neighbor.text)
                if neighbor.id in seen or cost > budget:
                    continue
                seen.add(neighbor.id)
                budget -= cost
                out.append(RetrievedChunk(chunk=neighbor, neighbor_of=item.id))
        return out
