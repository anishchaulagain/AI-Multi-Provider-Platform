import uuid
from typing import Any

import httpx
import pytest
from qdrant_client import AsyncQdrantClient

from platform_api.core.config import Settings
from platform_graph import EntityHit, Expansion, Neighborhood
from tests.integration.test_chat import _seed


class FakeGraph:
    def __init__(self) -> None:
        self.deleted: list[uuid.UUID] = []

    async def search_entities(self, tenant_id: uuid.UUID, q: str, *, limit: int) -> list[EntityHit]:
        return [EntityHit("e1", "Qdrant", "Product", 2.5, mentions=3)]

    async def neighborhood(
        self, tenant_id: uuid.UUID, entity: str, *, hops: int, limit: int
    ) -> Neighborhood | None:
        if entity != "e1":
            return None
        return Neighborhood(
            nodes=[{"id": "e1", "name": "Qdrant", "type": "Product"}],
            edges=[{"source": "e1", "target": "e2", "type": "USES", "doc_id": "d", "weight": 1}],
        )

    async def expand(self, *args: object, **kwargs: object) -> Any:
        return Expansion()

    async def delete_document(self, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> None:
        self.deleted.append(doc_id)

    async def ensure_schema(self) -> None:
        return None

    async def ping(self) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


async def test_graph_endpoints_and_document_delete(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    qdrant: AsyncQdrantClient,
    settings: Settings,
    graph: FakeGraph,
) -> None:
    entities = (await client.get("/api/v1/graph/entities?q=qdr", headers=admin_headers)).json()
    assert entities["items"][0] == {
        "id": "e1",
        "name": "Qdrant",
        "type": "Product",
        "score": 2.5,
        "mentions": 3,
    }
    hood = await client.get("/api/v1/graph/neighborhood/e1", headers=admin_headers)
    assert hood.json()["edges"][0]["type"] == "USES"
    missing = await client.get("/api/v1/graph/neighborhood/nope", headers=admin_headers)
    assert missing.status_code == 404

    doc_id = await _seed(client, admin_headers, qdrant, settings)
    assert (
        await client.delete(f"/api/v1/documents/{doc_id}", headers=admin_headers)
    ).status_code == 204
    assert graph.deleted == [doc_id]
