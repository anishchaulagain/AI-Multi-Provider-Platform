"""Chunk storage in Qdrant: one collection per embedding model, filtered by tenant.

Collection `chunks_<embed_model>` has a named dense vector ("dense", cosine) and a
named sparse vector ("bm25", IDF applied server-side). Point ids are derived from
(doc_id, chunk_index), so re-ingesting a document overwrites rather than duplicates.
"""

import re
import uuid
from typing import Any

import structlog
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient, models

logger = structlog.get_logger(__name__)

DENSE = "dense"
SPARSE = "bm25"
_UPSERT_BATCH = 128


def collection_name(embed_model: str, prefix: str = "chunks") -> str:
    return f"{prefix}_{re.sub(r'[^a-z0-9]+', '_', embed_model.lower()).strip('_')}"


def chunk_point_id(doc_id: uuid.UUID, chunk_index: int) -> str:
    return str(uuid.uuid5(doc_id, f"chunk:{chunk_index}"))


class SparseVector(BaseModel):
    indices: list[int]
    values: list[float]


class ChunkRecord(BaseModel):
    tenant_id: uuid.UUID
    doc_id: uuid.UUID
    chunk_index: int
    text: str
    page: int | None = None
    section: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    dense: list[float]
    sparse: SparseVector | None = None

    @property
    def id(self) -> str:
        return chunk_point_id(self.doc_id, self.chunk_index)

    def payload(self) -> dict[str, Any]:
        return {
            "tenant_id": str(self.tenant_id),
            "doc_id": str(self.doc_id),
            "chunk_index": self.chunk_index,
            "text": self.text,
            "page": self.page,
            "section": self.section,
            **self.metadata,
        }


class StoredChunk(BaseModel):
    id: str
    chunk_index: int
    text: str
    page: int | None = None
    section: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def _scope(tenant_id: uuid.UUID, doc_id: uuid.UUID | None = None) -> models.Filter:
    must: list[models.Condition] = [
        models.FieldCondition(key="tenant_id", match=models.MatchValue(value=str(tenant_id)))
    ]
    if doc_id is not None:
        must.append(models.FieldCondition(key="doc_id", match=models.MatchValue(value=str(doc_id))))
    return models.Filter(must=must)


class ChunkStore:
    def __init__(self, client: AsyncQdrantClient, embed_model: str) -> None:
        self._client = client
        self.collection = collection_name(embed_model)
        self._ready = False

    async def _exists(self) -> bool:
        if self._ready:
            return True
        self._ready = await self._client.collection_exists(self.collection)
        return self._ready

    async def ensure_collection(self, dim: int) -> None:
        if await self._exists():
            return
        await self._client.create_collection(
            self.collection,
            vectors_config={DENSE: models.VectorParams(size=dim, distance=models.Distance.COSINE)},
            sparse_vectors_config={SPARSE: models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
        for field, schema in (
            ("tenant_id", models.PayloadSchemaType.KEYWORD),
            ("doc_id", models.PayloadSchemaType.KEYWORD),
            ("chunk_index", models.PayloadSchemaType.INTEGER),
        ):
            await self._client.create_payload_index(self.collection, field, field_schema=schema)
        self._ready = True
        logger.info("collection_created", collection=self.collection, dim=dim)

    async def replace_document(
        self, tenant_id: uuid.UUID, doc_id: uuid.UUID, chunks: list[ChunkRecord]
    ) -> None:
        """Idempotent write: drop the document's existing points, then upsert."""
        if chunks:
            await self.ensure_collection(len(chunks[0].dense))
        await self.delete_document(tenant_id, doc_id)
        for start in range(0, len(chunks), _UPSERT_BATCH):
            batch = chunks[start : start + _UPSERT_BATCH]
            await self._client.upsert(
                self.collection,
                points=[
                    models.PointStruct(
                        id=c.id,
                        vector={
                            DENSE: c.dense,
                            **(
                                {SPARSE: models.SparseVector(**c.sparse.model_dump())}
                                if c.sparse and c.sparse.indices
                                else {}
                            ),
                        },
                        payload=c.payload(),
                    )
                    for c in batch
                ],
                wait=True,
            )

    async def delete_document(self, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> None:
        if not await self._exists():
            return
        await self._client.delete(
            self.collection,
            points_selector=models.FilterSelector(filter=_scope(tenant_id, doc_id)),
            wait=True,
        )

    async def count(self, tenant_id: uuid.UUID, doc_id: uuid.UUID) -> int:
        if not await self._exists():
            return 0
        result = await self._client.count(
            self.collection, count_filter=_scope(tenant_id, doc_id), exact=True
        )
        return result.count

    async def list_chunks(
        self, tenant_id: uuid.UUID, doc_id: uuid.UUID, *, offset: int = 0, limit: int = 20
    ) -> list[StoredChunk]:
        """Chunks [offset, offset+limit) in document order."""
        if not await self._exists():
            return []
        scope = _scope(tenant_id, doc_id)
        assert scope.must is not None and isinstance(scope.must, list)
        scope.must.append(
            models.FieldCondition(
                key="chunk_index", range=models.Range(gte=offset, lt=offset + limit)
            )
        )
        points, _ = await self._client.scroll(
            self.collection, scroll_filter=scope, limit=limit, with_payload=True
        )
        out = []
        for point in points:
            payload = dict(point.payload or {})
            out.append(
                StoredChunk(
                    id=str(point.id),
                    chunk_index=int(payload.pop("chunk_index")),
                    text=str(payload.pop("text", "")),
                    page=payload.pop("page", None),
                    section=payload.pop("section", None),
                    metadata={k: v for k, v in payload.items() if k not in ("tenant_id", "doc_id")},
                )
            )
        return sorted(out, key=lambda c: c.chunk_index)

    async def ping(self) -> None:
        await self._client.get_collections()
