"""Embedded turn pairs in Qdrant (`conversation_turns_<embed_model>`) for semantic
recall of older exchanges, always filtered by tenant and conversation."""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from qdrant_client import AsyncQdrantClient, models

from platform_history.compress import HistoryTurn


def collection_name(embed_model: str) -> str:
    return "conversation_turns_" + re.sub(r"[^a-z0-9]+", "_", embed_model.lower()).strip("_")


@dataclass(frozen=True)
class TurnPair:
    conversation_id: uuid.UUID
    user_msg_id: str
    assistant_msg_id: str | None
    text: str
    seq: int  # position in the conversation, for ordering

    @property
    def point_id(self) -> str:
        return str(uuid.uuid5(self.conversation_id, f"turn:{self.user_msg_id}"))


class TurnStore:
    def __init__(self, client: AsyncQdrantClient, embed_model: str) -> None:
        self._client = client
        self.collection = collection_name(embed_model)
        self._ready = False

    async def _exists(self) -> bool:
        if not self._ready:
            self._ready = await self._client.collection_exists(self.collection)
        return self._ready

    async def add(
        self, tenant_id: uuid.UUID, pairs: Sequence[TurnPair], vectors: Sequence[list[float]]
    ) -> None:
        if not pairs:
            return
        if not await self._exists():
            await self._client.create_collection(
                self.collection,
                vectors_config=models.VectorParams(
                    size=len(vectors[0]), distance=models.Distance.COSINE
                ),
            )
            for name in ("tenant_id", "conversation_id"):
                await self._client.create_payload_index(
                    self.collection, name, field_schema=models.PayloadSchemaType.KEYWORD
                )
            self._ready = True
        await self._client.upsert(
            self.collection,
            points=[
                models.PointStruct(
                    id=pair.point_id,
                    vector=vector,
                    payload={
                        "tenant_id": str(tenant_id),
                        "conversation_id": str(pair.conversation_id),
                        "user_msg_id": pair.user_msg_id,
                        "assistant_msg_id": pair.assistant_msg_id,
                        "text": pair.text,
                        "seq": pair.seq,
                    },
                )
                for pair, vector in zip(pairs, vectors, strict=True)
            ],
            wait=True,
        )

    async def search(
        self,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        vector: list[float],
        *,
        exclude_msg_ids: set[str],
        limit: int = 3,
    ) -> list[HistoryTurn]:
        if not await self._exists():
            return []
        result = await self._client.query_points(
            self.collection,
            query=vector,
            query_filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="tenant_id", match=models.MatchValue(value=str(tenant_id))
                    ),
                    models.FieldCondition(
                        key="conversation_id", match=models.MatchValue(value=str(conversation_id))
                    ),
                ]
            ),
            limit=limit + len(exclude_msg_ids),
            with_payload=True,
        )
        out = []
        for point in result.points:
            payload = point.payload or {}
            ids = {payload.get("user_msg_id"), payload.get("assistant_msg_id")}
            if ids & exclude_msg_ids:
                continue
            out.append(
                HistoryTurn(str(payload.get("user_msg_id")), "assistant", str(payload["text"]))
            )
            if len(out) >= limit:
                break
        return out

    async def delete_conversation(self, tenant_id: uuid.UUID, conversation_id: uuid.UUID) -> None:
        if not await self._exists():
            return
        await self._client.delete(
            self.collection,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="tenant_id", match=models.MatchValue(value=str(tenant_id))
                        ),
                        models.FieldCondition(
                            key="conversation_id",
                            match=models.MatchValue(value=str(conversation_id)),
                        ),
                    ]
                )
            ),
        )
