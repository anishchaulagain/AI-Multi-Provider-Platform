import hashlib
import json
import math
import re
import uuid
from collections.abc import AsyncGenerator, Sequence

import httpx
import pytest
from qdrant_client import AsyncQdrantClient

from llm_client import Caller, GatewayClient
from platform_graph import EntityHit, Expansion, Fact
from platform_rag.answer import (
    NO_ANSWER,
    build_messages,
    format_sources,
    parse_citations,
    resolve_citations,
)
from platform_rag.retrieve import RetrievalConfig, Retriever, Turn, rrf_fuse
from platform_rag.sparse import Bm25Encoder
from platform_rag.store import ChunkRecord, ChunkStore, chunk_point_id

TENANT = uuid.uuid4()
CALLER = Caller(tenant_id=TENANT)
DOC_DB = uuid.uuid4()
DOC_HR = uuid.uuid4()

CORPUS = {
    DOC_DB: [
        "Qdrant is a vector database written in Rust.",
        "Qdrant supports hybrid search with dense and sparse vectors.",
        "Neo4j is a graph database that uses the Cypher query language.",
        "Graph databases store entities and their relationships.",
    ],
    DOC_HR: [
        "Employees receive twenty days of paid vacation per year.",
        "Vacation requests must be approved by a manager.",
    ],
}


def embed(text: str, dim: int = 128) -> list[float]:
    vec = [0.0] * dim
    for word in re.findall(r"\w+", text.lower().replace("search_query", "")):
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class FakeGateway:
    def __init__(self) -> None:
        self.rewrite_reply = "What is Qdrant written in?"
        self.chat_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/v1/embeddings":
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return httpx.Response(
                200,
                json={"data": [{"index": i, "embedding": embed(t)} for i, t in enumerate(inputs)]},
            )
        self.chat_calls += 1
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": self.rewrite_reply}}]},
        )


class FakeGraph:
    def __init__(self, expansion: Expansion | None = None, fail: bool = False) -> None:
        self.expansion = expansion or Expansion()
        self.fail = fail
        self.calls: list[tuple[str, Sequence[uuid.UUID] | None]] = []

    async def expand(self, tenant_id: uuid.UUID, question: str, **kwargs: object) -> Expansion:
        self.calls.append((question, kwargs.get("doc_ids")))  # type: ignore[arg-type]
        if self.fail:
            raise ConnectionError("neo4j down")
        return self.expansion


@pytest.fixture
async def store() -> AsyncGenerator[ChunkStore]:
    client = AsyncQdrantClient(":memory:")
    chunk_store = ChunkStore(client, "test")
    sparse = Bm25Encoder()
    for doc_id, texts in CORPUS.items():
        await chunk_store.replace_document(
            TENANT,
            doc_id,
            [
                ChunkRecord(
                    tenant_id=TENANT,
                    doc_id=doc_id,
                    chunk_index=i,
                    text=text,
                    page=1,
                    metadata={"doc_title": "Databases" if doc_id == DOC_DB else "Handbook"},
                    dense=embed(text),
                    sparse=sparse.encode_document(text),
                )
                for i, text in enumerate(texts)
            ],
        )
    yield chunk_store
    await client.close()


def _retriever(
    store: ChunkStore, fake: FakeGateway, graph: FakeGraph | None = None, **cfg: object
) -> Retriever:
    gateway = GatewayClient(
        "http://gw", "t", transport=httpx.MockTransport(fake.handler), max_retries=0
    )
    config = RetrievalConfig(neighbor_window=0, **cfg)  # type: ignore[arg-type]
    return Retriever(gateway, store, graph=graph, config=config)


# ---------- retrieval ----------
async def test_hybrid_search_finds_relevant_chunks(store: ChunkStore) -> None:
    fake = FakeGateway()
    result = await _retriever(store, fake, final_k=3).retrieve(
        TENANT, "Which language is Qdrant written in?", caller=CALLER
    )
    assert result.query == result.question  # no history -> no rewrite call
    assert fake.chat_calls == 0
    assert result.chunks[0].chunk.text == CORPUS[DOC_DB][0]
    assert all(c.vector_rank is not None for c in result.chunks)
    assert len(result.chunks) == 3


async def test_doc_filter_and_tenant_isolation(store: ChunkStore) -> None:
    retriever = _retriever(store, FakeGateway())
    filtered = await retriever.retrieve(TENANT, "Qdrant vacation", caller=CALLER, doc_ids=[DOC_HR])
    assert {c.chunk.doc_id for c in filtered.chunks} == {str(DOC_HR)}
    other = await retriever.retrieve(uuid.uuid4(), "Qdrant", caller=Caller(tenant_id=uuid.uuid4()))
    assert other.chunks == []


async def test_graph_chunks_are_fused_with_vector_hits(store: ChunkStore) -> None:
    graph_only = chunk_point_id(DOC_DB, 3)  # "Graph databases store entities..." (no word overlap)
    graph = FakeGraph(
        Expansion(
            seeds=[EntityHit("e1", "Qdrant", "Product", 1.0)],
            facts=[Fact("Qdrant", "COMPETES_WITH", "Neo4j", str(DOC_DB), 1)],
            chunks=[(graph_only, 4.0)],
        )
    )
    result = await _retriever(store, FakeGateway(), graph, final_k=8).retrieve(
        TENANT, "Tell me about Qdrant", caller=CALLER
    )
    by_id = {c.id: c for c in result.chunks}
    assert graph_only in by_id and by_id[graph_only].graph_rank == 1
    assert result.facts[0].text() == "Qdrant competes with Neo4j"
    trace = result.trace()
    assert trace["graph"]["seeds"][0]["name"] == "Qdrant"
    assert "Graph facts:\n- Qdrant competes with Neo4j" in format_sources(result)


async def test_graph_failure_falls_back_to_vectors(store: ChunkStore) -> None:
    result = await _retriever(store, FakeGateway(), FakeGraph(fail=True)).retrieve(
        TENANT, "Qdrant hybrid search", caller=CALLER
    )
    assert result.graph_error == "ConnectionError"
    assert result.chunks


async def test_followup_is_rewritten_with_history(store: ChunkStore) -> None:
    fake = FakeGateway()
    graph = FakeGraph()
    history = [
        Turn("user", "Tell me about Qdrant"),
        Turn("assistant", "Qdrant is a vector DB [1]."),
    ]
    result = await _retriever(store, fake, graph).retrieve(
        TENANT, "What is it written in?", caller=CALLER, history=history
    )
    assert fake.chat_calls == 1
    assert result.query == "What is Qdrant written in?"
    assert graph.calls[0][0] == "What is Qdrant written in?"
    assert result.chunks[0].chunk.text == CORPUS[DOC_DB][0]


async def test_budget_and_neighbors(store: ChunkStore) -> None:
    fake = FakeGateway()
    tight = await _retriever(store, fake, final_k=8, context_token_budget=15).retrieve(
        TENANT, "vector database", caller=CALLER
    )
    assert len(tight.chunks) == 1  # always keeps the best chunk

    gateway = GatewayClient("http://gw", "t", transport=httpx.MockTransport(fake.handler))
    with_neighbors = await Retriever(
        gateway, store, config=RetrievalConfig(final_k=1, neighbor_window=1, neighbors_for_top=1)
    ).retrieve(TENANT, "approved by a manager", caller=CALLER)
    assert [c.chunk.chunk_index for c in with_neighbors.chunks] == [1, 0]
    assert with_neighbors.chunks[1].neighbor_of == with_neighbors.chunks[0].id


def test_rrf_fuse() -> None:
    scores = rrf_fuse([["a", "b", "c"], ["c", "d"]], k=60)
    assert max(scores, key=lambda k: scores[k]) == "c"  # in both lists
    assert scores["a"] > scores["d"]


# ---------- answering ----------
def test_parse_citations() -> None:
    text = "Qdrant is in Rust [2]. It supports hybrid search [1, 3][2]. Not [x] or [ 9 ]."
    assert parse_citations(text) == [2, 1, 3]


async def test_resolve_citations_drops_invented_sources(store: ChunkStore) -> None:
    result = await _retriever(store, FakeGateway(), final_k=2).retrieve(
        TENANT, "Qdrant Rust", caller=CALLER
    )
    valid, invalid = resolve_citations("Rust [1]. Also [2] and made-up [7].", result)
    assert [c.n for c in valid] == [1, 2]
    assert invalid == [7]
    assert valid[0].chunk_id == result.chunks[0].id
    assert valid[0].doc_title == "Databases"
    assert valid[0].stored() == {"n": 1, "chunk_id": result.chunks[0].id, "doc_id": str(DOC_DB)}


async def test_prompt_numbers_sources_and_includes_history(store: ChunkStore) -> None:
    result = await _retriever(store, FakeGateway(), final_k=2).retrieve(
        TENANT, "Qdrant", caller=CALLER
    )
    messages = build_messages(
        "What is Qdrant?", result, [Turn("user", "hi"), Turn("assistant", "hello")]
    )
    assert messages[0].role == "system" and NO_ANSWER in str(messages[0].content)
    assert [m.role for m in messages[1:3]] == ["user", "assistant"]
    prompt = str(messages[-1].content)
    assert prompt.startswith("Sources:\n\n[1] (Databases, page 1)")
    assert "[2] (" in prompt and prompt.endswith("Question: What is Qdrant?")
