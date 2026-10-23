"""Against a real Neo4j (skipped when unreachable). Uses random tenant ids and
deletes everything it wrote."""

import os
import uuid
from collections.abc import AsyncGenerator

import pytest

from platform_graph import Entity, GraphChunk, GraphExtraction, GraphStore, Relation

pytestmark = pytest.mark.integration

URI = os.getenv("TEST_NEO4J_URI", "bolt://localhost:7687")
AUTH = (os.getenv("TEST_NEO4J_USER", "neo4j"), os.getenv("TEST_NEO4J_PASSWORD", "platform-neo4j"))


@pytest.fixture(scope="session")
async def graph() -> AsyncGenerator[GraphStore]:
    store = GraphStore.connect(URI, *AUTH)
    try:
        await store.ping()
        await store.ensure_schema()
    except Exception as exc:  # noqa: BLE001
        await store.close()
        if os.getenv("CI"):
            pytest.fail(f"Neo4j unavailable in CI: {exc!r}")
        pytest.skip(f"Neo4j unavailable: {exc!r}")
    yield store
    await store.close()


@pytest.fixture
async def tenant(graph: GraphStore) -> AsyncGenerator[uuid.UUID]:
    tenant_id = uuid.uuid4()
    yield tenant_id
    await graph._run("MATCH (n {tenant_id: $t}) DETACH DELETE n", t=str(tenant_id))


QDRANT = Entity("Qdrant", "Product")
RUST = Entity("Rust", "Technology")
NEO4J = Entity("Neo4j", "Product")
CYPHER = Entity("Cypher", "Technology")


def _extraction() -> GraphExtraction:
    return GraphExtraction(
        mentions={0: [QDRANT, RUST], 1: [NEO4J, CYPHER], 2: [QDRANT, NEO4J]},
        relations=[
            (Relation(QDRANT, "USES", RUST), 0),
            (Relation(NEO4J, "USES", CYPHER), 1),
            (Relation(QDRANT, "COMPETES_WITH", NEO4J), 2),
        ],
    )


def _chunks(prefix: str) -> list[GraphChunk]:
    return [GraphChunk(f"{prefix}-{i}", i, page=i + 1) for i in range(3)]


async def _write(graph: GraphStore, tenant: uuid.UUID, doc: uuid.UUID, prefix: str) -> None:
    await graph.replace_document(
        tenant_id=tenant,
        doc_id=doc,
        title="Stores",
        chunks=_chunks(prefix),
        extraction=_extraction(),
    )


async def test_write_expand_search_and_neighborhood(graph: GraphStore, tenant: uuid.UUID) -> None:
    doc = uuid.uuid4()
    await _write(graph, tenant, doc, str(doc))
    assert await graph.stats(tenant, doc) == {"entities": 4, "relations": 3}

    expansion = await graph.expand(tenant, "What language is Qdrant written in?", hops=1)
    assert expansion.seeds[0].name == "Qdrant"
    assert {f.text() for f in expansion.facts} >= {"Qdrant uses Rust", "Qdrant competes with Neo4j"}
    ranked = [cid for cid, _ in expansion.chunks]
    # Chunks mentioning the seed come first; neighbor-only chunks (Neo4j/Cypher) later.
    assert set(ranked[:2]) == {f"{doc}-0", f"{doc}-2"}
    assert f"{doc}-1" in ranked

    hits = await graph.search_entities(tenant, "neo")
    assert [h.name for h in hits] == ["Neo4j"] and hits[0].mentions == 2

    hood = await graph.neighborhood(tenant, hits[0].id, hops=1)
    assert hood is not None
    assert {n["name"] for n in hood.nodes} == {"Neo4j", "Cypher", "Qdrant"}
    assert len(hood.edges) == 2

    # Other tenants see nothing.
    assert (await graph.expand(uuid.uuid4(), "Qdrant")).seeds == []
    assert await graph.neighborhood(uuid.uuid4(), hits[0].id) is None


async def test_reingest_replaces_and_delete_removes_orphans(
    graph: GraphStore, tenant: uuid.UUID
) -> None:
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    await _write(graph, tenant, doc_a, str(doc_a))
    await _write(graph, tenant, doc_a, str(doc_a))  # idempotent re-ingest
    assert await graph.stats(tenant, doc_a) == {"entities": 4, "relations": 3}

    # A second document mentioning only Qdrant shares the entity (entity resolution).
    await graph.replace_document(
        tenant_id=tenant,
        doc_id=doc_b,
        title="Other",
        chunks=[GraphChunk(f"{doc_b}-0", 0)],
        extraction=GraphExtraction(mentions={0: [Entity("qdrant", "Product")]}),
    )
    [qdrant] = await graph.search_entities(tenant, "qdrant")
    assert qdrant.mentions == 3

    await graph.delete_document(tenant, doc_a)
    remaining = await graph._run(
        "MATCH (e:Entity {tenant_id: $t}) RETURN e.name AS name ORDER BY name", t=str(tenant)
    )
    assert [r["name"] for r in remaining] == ["Qdrant"]  # orphans gone, shared entity kept
    assert await graph.stats(tenant, doc_a) == {"entities": 0, "relations": 0}

    # Doc filter: facts from a deleted document no longer appear.
    assert (await graph.expand(tenant, "Qdrant", doc_ids=[doc_b])).facts == []
