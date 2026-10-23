import json
import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from conftest import FakeGateway, make_pdf
from qdrant_client import AsyncQdrantClient

from llm_client import Caller, GatewayClient, GatewayError
from platform_rag.formats import DocumentParseError
from platform_rag.ingest import IngestionPipeline, PipelineConfig
from platform_rag.ingest.pipeline import Stage
from platform_rag.store import ChunkStore, chunk_point_id, collection_name

TENANT = uuid.uuid4()


@pytest.fixture
async def qdrant() -> AsyncGenerator[AsyncQdrantClient]:
    client = AsyncQdrantClient(":memory:")
    yield client
    await client.close()


@pytest.fixture
def fake() -> FakeGateway:
    return FakeGateway()


def _pipeline(fake: FakeGateway, store: ChunkStore, **cfg: object) -> IngestionPipeline:
    gateway = GatewayClient(
        "http://gw", "tok", transport=httpx.MockTransport(fake.handler), max_retries=0
    )
    return IngestionPipeline(gateway, store, config=PipelineConfig(**cfg))  # type: ignore[arg-type]


async def _run(
    pipeline: IngestionPipeline, doc_id: uuid.UUID, data: bytes, name: str
) -> list[Stage]:
    stages: list[Stage] = []

    async def on_stage(stage: Stage) -> None:
        stages.append(stage)

    await pipeline.run(
        tenant_id=TENANT,
        doc_id=doc_id,
        filename=name,
        data=data,
        caller=Caller(tenant_id=TENANT),
        on_stage=on_stage,
    )
    return stages


def test_collection_name() -> None:
    assert collection_name("nomic-embed-text") == "chunks_nomic_embed_text"


async def test_pipeline_stores_chunks_with_vectors_and_metadata(
    qdrant: AsyncQdrantClient, fake: FakeGateway
) -> None:
    store = ChunkStore(qdrant, "fake-embed")
    pipeline = _pipeline(fake, store, embed_batch_size=2, chunk_size=64, chunk_overlap=0)
    doc_id = uuid.uuid4()
    text = "\n\n".join(f"## Part {i}\n\n" + "Retrieval needs good chunks. " * 12 for i in range(4))

    stages = await _run(pipeline, doc_id, f"# Manual\n\n{text}".encode(), "manual.md")

    assert stages == ["parsing", "chunking", "embedding"]
    count = await store.count(TENANT, doc_id)
    assert count >= 4
    assert all(len(batch) <= 2 for batch in fake.embed_calls)
    # Embedded text is prefixed and carries title + section context.
    first_input = fake.embed_calls[0][0]
    assert first_input.startswith("search_document: Manual\nManual")

    chunks = await store.list_chunks(TENANT, doc_id, offset=0, limit=100)
    assert [c.chunk_index for c in chunks] == list(range(count))
    assert chunks[1].section and chunks[1].section.startswith("Manual > Part")
    assert chunks[0].metadata["doc_title"] == "Manual"

    points, _ = await qdrant.scroll(store.collection, limit=1, with_vectors=True)
    vectors = points[0].vector
    assert isinstance(vectors, dict) and {"dense", "bm25"} <= set(vectors)


async def test_reingest_replaces_points_and_tenants_are_isolated(
    qdrant: AsyncQdrantClient, fake: FakeGateway
) -> None:
    store = ChunkStore(qdrant, "fake-embed")
    pipeline = _pipeline(fake, store, chunk_size=64, chunk_overlap=0)
    doc_id = uuid.uuid4()
    long = ("Alpha beta gamma delta. " * 40).encode()
    await _run(pipeline, doc_id, long, "a.txt")
    first = await store.count(TENANT, doc_id)
    await _run(pipeline, doc_id, b"Just one short line now.", "a.txt")
    assert first > 1
    assert await store.count(TENANT, doc_id) == 1
    assert await store.count(uuid.uuid4(), doc_id) == 0  # other tenant sees nothing

    await store.delete_document(TENANT, doc_id)
    assert await store.count(TENANT, doc_id) == 0


async def test_metadata_extraction_is_optional_and_non_fatal(
    qdrant: AsyncQdrantClient, fake: FakeGateway
) -> None:
    store = ChunkStore(qdrant, "fake-embed")
    stages: list[Stage] = []

    async def on_stage(stage: Stage) -> None:
        stages.append(stage)

    fake.chat_reply = "not json at all"
    result = await _pipeline(fake, store).run(
        tenant_id=TENANT,
        doc_id=uuid.uuid4(),
        filename="x.pdf",
        data=make_pdf(["Some readable PDF text."]),
        caller=Caller(tenant_id=TENANT),
        on_stage=on_stage,
    )
    assert "summary" not in result.details
    assert result.page_count == 1

    fake.chat_reply = '{"summary": "About PDFs.", "keywords": ["pdf", " ", "text"]}'
    result = await _pipeline(fake, store).run(
        tenant_id=TENANT,
        doc_id=uuid.uuid4(),
        filename="y.txt",
        data=b"Other text",
        caller=Caller(tenant_id=TENANT),
        on_stage=on_stage,
    )
    assert result.details["summary"] == "About PDFs."
    assert result.details["keywords"] == ["pdf", "text"]

    calls = fake.chat_calls
    await _pipeline(fake, store, extract_metadata=False).run(
        tenant_id=TENANT,
        doc_id=uuid.uuid4(),
        filename="z.txt",
        data=b"No extraction",
        caller=Caller(tenant_id=TENANT),
        on_stage=on_stage,
    )
    assert fake.chat_calls == calls


async def test_errors_propagate_for_the_worker_to_classify(
    qdrant: AsyncQdrantClient, fake: FakeGateway
) -> None:
    store = ChunkStore(qdrant, "fake-embed")
    with pytest.raises(DocumentParseError):
        await _run(_pipeline(fake, store), uuid.uuid4(), b"%PDF-1.4 broken", "bad.pdf")

    fake.embed_status = 503
    with pytest.raises(GatewayError) as exc_info:
        await _run(_pipeline(fake, store), uuid.uuid4(), b"hello world", "a.txt")
    assert exc_info.value.status_code == 503

    with pytest.raises(DocumentParseError, match="too large"):
        await _run(
            _pipeline(fake, store, max_chunks=1, chunk_size=32, chunk_overlap=0),
            uuid.uuid4(),
            ("word " * 500).encode(),
            "big.txt",
        )


class RecordingGraph:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def replace_document(self, **kwargs: object) -> None:
        if self.fail:
            raise ConnectionError("neo4j down")
        self.calls.append(kwargs)


async def test_graph_stage_writes_extraction(qdrant: AsyncQdrantClient, fake: FakeGateway) -> None:
    fake.chat_reply = json.dumps(
        {
            "chunks": [
                {
                    "chunk": 0,
                    "entities": [
                        {"name": "Qdrant", "type": "Product"},
                        {"name": "Rust", "type": "Technology"},
                    ],
                    "relations": [{"source": "Qdrant", "type": "USES", "target": "Rust"}],
                }
            ],
            # The metadata call reuses this reply; extra keys are ignored by it.
            "summary": "About Qdrant.",
            "keywords": ["qdrant"],
        }
    )
    graph = RecordingGraph()
    gateway = GatewayClient(
        "http://gw", "t", transport=httpx.MockTransport(fake.handler), max_retries=0
    )
    pipeline = IngestionPipeline(gateway, ChunkStore(qdrant, "fake-embed"), graph=graph)
    doc_id = uuid.uuid4()
    stages = await _run(pipeline, doc_id, b"Qdrant is written in Rust.", "q.txt")

    assert stages == ["parsing", "chunking", "embedding", "graphing"]
    [call] = graph.calls
    assert call["doc_id"] == doc_id
    chunks = call["chunks"]
    assert [c.chunk_id for c in chunks] == [chunk_point_id(doc_id, 0)]  # type: ignore[attr-defined]
    extraction = call["extraction"]
    assert extraction.entity_count == 2  # type: ignore[attr-defined]


async def test_graph_failure_does_not_fail_the_document(
    qdrant: AsyncQdrantClient, fake: FakeGateway
) -> None:
    gateway = GatewayClient(
        "http://gw", "t", transport=httpx.MockTransport(fake.handler), max_retries=0
    )
    pipeline = IngestionPipeline(
        gateway, ChunkStore(qdrant, "fake-embed"), graph=RecordingGraph(fail=True)
    )
    stages: list[Stage] = []

    async def on_stage(stage: Stage) -> None:
        stages.append(stage)

    result = await pipeline.run(
        tenant_id=TENANT,
        doc_id=uuid.uuid4(),
        filename="a.txt",
        data=b"Some text.",
        caller=Caller(tenant_id=TENANT),
        on_stage=on_stage,
    )
    assert result.details["graph"]["status"] == "failed"
    assert "neo4j down" in result.details["graph"]["error"]
    assert result.chunk_count == 1
