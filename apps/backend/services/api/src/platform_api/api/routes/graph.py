from typing import Annotated, Any

from fastapi import APIRouter, Query

from platform_api.api.deps import CurrentPrincipal, ResourcesDep
from platform_core.errors import NotFoundError, ServiceUnavailableError
from platform_graph import GraphStore

router = APIRouter(prefix="/graph", tags=["graph"])


def _graph(resources: ResourcesDep) -> GraphStore:
    if resources.graph is None:
        raise ServiceUnavailableError("The knowledge graph is disabled")
    return resources.graph


@router.get("/entities")
async def search_entities(
    principal: CurrentPrincipal,
    resources: ResourcesDep,
    q: Annotated[str, Query(min_length=1, max_length=200)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    try:
        hits = await _graph(resources).search_entities(principal.tenant_id, q, limit=limit)
    except ServiceUnavailableError:
        raise
    except Exception as exc:
        raise ServiceUnavailableError("Knowledge graph is unavailable") from exc
    return {
        "items": [
            {"id": h.id, "name": h.name, "type": h.type, "score": h.score, "mentions": h.mentions}
            for h in hits
        ]
    }


@router.get("/neighborhood/{entity_id}")
async def neighborhood(
    entity_id: str,
    principal: CurrentPrincipal,
    resources: ResourcesDep,
    hops: Annotated[int, Query(ge=1, le=2)] = 1,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    try:
        hood = await _graph(resources).neighborhood(
            principal.tenant_id, entity_id, hops=hops, limit=limit
        )
    except ServiceUnavailableError:
        raise
    except Exception as exc:
        raise ServiceUnavailableError("Knowledge graph is unavailable") from exc
    if hood is None:
        raise NotFoundError("Entity not found")
    return {"nodes": hood.nodes, "edges": hood.edges}
