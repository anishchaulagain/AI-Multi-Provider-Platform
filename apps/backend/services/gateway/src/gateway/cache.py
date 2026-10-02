"""Response cache: exact match in Redis, then semantic match via a vector index.

Responses live in Redis (with TTL). The vector index only maps
"prompt embedding -> Redis key", scoped by tenant, alias and generation params.
"""

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from llm_client.types import ChatCompletion, ChatCompletionRequest

logger = structlog.get_logger(__name__)

CacheKind = Literal["exact", "semantic"]

# Generation params that change the answer; requests only share cache entries
# when these match exactly.
_PARAM_FIELDS = ("temperature", "top_p", "max_tokens", "stop", "response_format")


def is_cacheable(request: ChatCompletionRequest) -> bool:
    return not request.tools and (request.n or 1) == 1


def prompt_text(request: ChatCompletionRequest) -> str:
    return "\n".join(f"{m.role}: {' '.join(m.text().split())}" for m in request.messages)


def params_hash(request: ChatCompletionRequest) -> str:
    params = {f: getattr(request, f) for f in _PARAM_FIELDS}
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class CacheScope:
    tenant_id: str
    alias: str
    params: str


class VectorIndex(Protocol):
    async def search(self, vector: list[float], scope: CacheScope, threshold: float) -> str | None:
        """Return the Redis key of the best match scoring >= threshold, if any."""

    async def add(self, vector: list[float], scope: CacheScope, key: str, ttl: int) -> None: ...


class InMemoryVectorIndex:
    """Process-local index (tests, or when Qdrant is unavailable)."""

    def __init__(self) -> None:
        self._items: list[tuple[list[float], CacheScope, str, float]] = []

    async def search(self, vector: list[float], scope: CacheScope, threshold: float) -> str | None:
        now = time.time()
        best: tuple[float, str] | None = None
        for vec, item_scope, key, expires in self._items:
            if item_scope != scope or expires < now:
                continue
            score = _cosine(vector, vec)
            if score >= threshold and (best is None or score > best[0]):
                best = (score, key)
        return best[1] if best else None

    async def add(self, vector: list[float], scope: CacheScope, key: str, ttl: int) -> None:
        self._items.append((vector, scope, key, time.time() + ttl))


class QdrantVectorIndex:
    """Semantic cache index in Qdrant; collection is created lazily per vector size."""

    def __init__(self, url: str, collection_prefix: str = "gateway_semantic_cache") -> None:
        from qdrant_client import AsyncQdrantClient

        self._client = AsyncQdrantClient(url=url, timeout=5)
        self._prefix = collection_prefix
        self._ready: set[str] = set()

    async def _collection(self, dim: int) -> str:
        from qdrant_client import models

        name = f"{self._prefix}_{dim}"
        if name not in self._ready:
            if not await self._client.collection_exists(name):
                await self._client.create_collection(
                    name,
                    vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
                )
                for field in ("tenant_id", "alias", "params"):
                    await self._client.create_payload_index(
                        name, field, field_schema=models.PayloadSchemaType.KEYWORD
                    )
            self._ready.add(name)
        return name

    @staticmethod
    def _filter(scope: CacheScope) -> object:
        from qdrant_client import models

        return models.Filter(
            must=[
                models.FieldCondition(
                    key="tenant_id", match=models.MatchValue(value=scope.tenant_id)
                ),
                models.FieldCondition(key="alias", match=models.MatchValue(value=scope.alias)),
                models.FieldCondition(key="params", match=models.MatchValue(value=scope.params)),
                models.FieldCondition(key="expires_at", range=models.Range(gt=time.time())),
            ]
        )

    async def search(self, vector: list[float], scope: CacheScope, threshold: float) -> str | None:
        collection = await self._collection(len(vector))
        result = await self._client.query_points(
            collection,
            query=vector,
            query_filter=self._filter(scope),  # type: ignore[arg-type]
            limit=1,
            score_threshold=threshold,
            with_payload=["key"],
        )
        if not result.points:
            return None
        payload = result.points[0].payload or {}
        key = payload.get("key")
        return str(key) if key else None

    async def add(self, vector: list[float], scope: CacheScope, key: str, ttl: int) -> None:
        from qdrant_client import models

        collection = await self._collection(len(vector))
        await self._client.upsert(
            collection,
            points=[
                models.PointStruct(
                    id=str(uuid.uuid4()),
                    vector=vector,
                    payload={
                        "tenant_id": scope.tenant_id,
                        "alias": scope.alias,
                        "params": scope.params,
                        "key": key,
                        "expires_at": time.time() + ttl,
                    },
                )
            ],
        )

    async def aclose(self) -> None:
        await self._client.close()


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class ResponseCache:
    def __init__(
        self,
        redis: Redis,
        index: VectorIndex | None,
        *,
        ttl_seconds: int,
        semantic_threshold: float,
    ) -> None:
        self._redis = redis
        self._index = index
        self._ttl = ttl_seconds
        self._threshold = semantic_threshold

    @staticmethod
    def _exact_key(scope: CacheScope, text: str) -> str:
        digest = hashlib.sha256(text.encode()).hexdigest()
        return f"cache:chat:{scope.tenant_id}:{scope.alias}:{scope.params}:{digest}"

    async def _load(self, key: str) -> ChatCompletion | None:
        raw = await self._redis.get(key)
        return ChatCompletion.model_validate_json(raw) if raw else None

    async def get_exact(self, scope: CacheScope, text: str) -> ChatCompletion | None:
        try:
            return await self._load(self._exact_key(scope, text))
        except RedisError:
            return None

    async def get_semantic(self, scope: CacheScope, vector: list[float]) -> ChatCompletion | None:
        if self._index is None:
            return None
        try:
            key = await self._index.search(vector, scope, self._threshold)
            return await self._load(key) if key else None
        except Exception as exc:  # noqa: BLE001 - cache must never fail a request
            logger.warning("semantic_cache_lookup_failed", error=repr(exc))
            return None

    async def put(
        self,
        scope: CacheScope,
        text: str,
        completion: ChatCompletion,
        vector: list[float] | None,
    ) -> None:
        key = self._exact_key(scope, text)
        try:
            await self._redis.set(key, completion.model_dump_json(), ex=self._ttl)
            if vector is not None and self._index is not None:
                await self._index.add(vector, scope, key, self._ttl)
        except Exception as exc:  # noqa: BLE001
            logger.warning("cache_store_failed", error=repr(exc))
