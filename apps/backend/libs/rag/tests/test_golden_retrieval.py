"""Retrieval quality on the sample corpus (evals/corpus) with the real ingestion
pipeline: parsing, markdown chunking, BM25 and hybrid search. Embeddings are a
bag-of-words stand-in, so this checks the retrieval plumbing, not model quality."""

import hashlib
import json
import math
import re
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import httpx
import pytest
from qdrant_client import AsyncQdrantClient

from llm_client import Caller, GatewayClient
from platform_rag.ingest import IngestionPipeline, PipelineConfig
from platform_rag.ingest.pipeline import Stage
from platform_rag.retrieve import RetrievalConfig, Retriever
from platform_rag.store import ChunkStore

ROOT = Path(__file__).resolve().parents[5]
TENANT = uuid.uuid4()
CALLER = Caller(tenant_id=TENANT)
GOLDEN = [
    json.loads(line)
    for line in (ROOT / "evals" / "datasets" / "golden.jsonl").read_text().splitlines()
    if json.loads(line)["expected_doc"]
]


def embed(text: str, dim: int = 256) -> list[float]:
    vec = [0.0] * dim
    for word in re.findall(r"\w+", text.lower()):
        if word in {"search_query", "search_document"}:
            continue
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
    return httpx.Response(
        200, json={"data": [{"index": i, "embedding": embed(t)} for i, t in enumerate(inputs)]}
    )


@pytest.fixture(scope="module")
async def corpus() -> AsyncGenerator[tuple[Retriever, dict[str, str]]]:
    qdrant = AsyncQdrantClient(":memory:")
    store = ChunkStore(qdrant, "golden")
    gateway = GatewayClient("http://gw", "t", transport=httpx.MockTransport(handler))
    pipeline = IngestionPipeline(
        gateway,
        store,
        config=PipelineConfig(extract_metadata=False, chunk_size=128, chunk_overlap=16),
    )

    async def on_stage(stage: Stage) -> None:
        return None

    doc_names: dict[str, str] = {}
    for path in sorted((ROOT / "evals" / "corpus").glob("*.md")):
        doc_id = uuid.uuid4()
        doc_names[str(doc_id)] = path.name
        await pipeline.run(
            tenant_id=TENANT,
            doc_id=doc_id,
            filename=path.name,
            data=path.read_bytes(),
            caller=CALLER,
            on_stage=on_stage,
        )
    retriever = Retriever(gateway, store, config=RetrievalConfig(final_k=4, neighbor_window=0))
    yield retriever, doc_names
    await qdrant.close()


@pytest.mark.parametrize("case", GOLDEN, ids=[c["id"] for c in GOLDEN])
async def test_golden_question_retrieves_the_right_passage(
    corpus: tuple[Retriever, dict[str, str]], case: dict[str, object]
) -> None:
    retriever, doc_names = corpus
    result = await retriever.retrieve(TENANT, str(case["question"]), caller=CALLER)
    assert result.chunks, "nothing retrieved"
    top = result.chunks[0].chunk
    assert doc_names[top.doc_id] == case["expected_doc"]
    # The answer's key fact is in the retrieved context (top 4).
    context = " ".join(c.chunk.text for c in result.chunks).lower()
    for term in case["expected_terms"]:  # type: ignore[attr-defined]
        assert str(term).lower() in context
