"""Gateway tests run without any services: fake LiteLLM (httpx.MockTransport),
fakeredis, an in-memory vector index, usage recorder and quota lookup."""

import hashlib
import json
import math
import re
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

import fakeredis
import httpx
import pytest

from gateway.cache import InMemoryVectorIndex
from gateway.config import GatewaySettings
from gateway.main import create_app
from gateway.resources import Overrides
from gateway.routing import RouteTable
from gateway.usage import InMemoryUsageRecorder

TOKEN = "test-service-token"
TENANT = str(uuid.uuid4())

ROUTES = {
    "deployments": {
        "a-small": {"provider": "prov-a"},
        "b-small": {"provider": "prov-b"},
        "c-small": {"provider": "prov-c"},
        "keyed": {"provider": "prov-k", "requires_env": "GATEWAY_TEST_KEY"},
        "limited": {"provider": "prov-l", "rpm": 1},
        "embedder": {"provider": "prov-e", "kind": "embedding"},
    },
    "aliases": {
        "chat-fast": {"deployments": ["a-small", "b-small", "c-small"]},
        "chat-smart": {"deployments": ["c-small"]},
        "keyed-chat": {"deployments": ["keyed", "b-small"]},
        "limited-chat": {"deployments": ["limited", "b-small"]},
        "embed": {"kind": "embedding", "deployments": ["embedder"]},
    },
}

# A behaviour is "ok", an HTTP status code, or "drop" (connection error).
Behaviour = str | int


@dataclass
class FakeLiteLLM:
    behaviours: dict[str, list[Behaviour]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def set(self, deployment: str, *behaviours: Behaviour) -> None:
        """Queue behaviours; the last one repeats forever."""
        self.behaviours[deployment] = list(behaviours)

    def _next(self, deployment: str) -> Behaviour:
        queue = self.behaviours.get(deployment, ["ok"])
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def calls_to(self, deployment: str) -> int:
        return sum(1 for c in self.calls if c["model"] == deployment)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health/liveliness":
            return httpx.Response(200, json="I'm alive!")
        body = json.loads(request.content)
        model = body["model"]
        self.calls.append(body)
        behaviour = self._next(model)
        if behaviour == "drop":
            raise httpx.ConnectError("connection refused", request=request)
        if isinstance(behaviour, int):
            return httpx.Response(behaviour, json={"error": {"message": f"{model} failed"}})

        if request.url.path == "/v1/embeddings":
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": model,
                    "data": [
                        {"object": "embedding", "index": i, "embedding": fake_embedding(t)}
                        for i, t in enumerate(inputs)
                    ],
                    "usage": {"prompt_tokens": 3, "total_tokens": 3},
                },
            )

        content = f"hello from {model}"
        if body.get("stream"):
            words = content.split(" ")
            chunks = [
                {
                    "id": "c1",
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": w + " "}}],
                }
                for w in words
            ]
            chunks.append(
                {
                    "id": "c1",
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                }
            )
            sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(
                200, content=sse.encode(), headers={"content-type": "text/event-stream"}
            )

        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            },
        )


def fake_embedding(text: str, dim: int = 64) -> list[float]:
    """Bag-of-words hashing: same words (any case/spacing) -> identical vector."""
    vec = [0.0] * dim
    for word in re.findall(r"\w+", text.lower()):
        vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@dataclass
class Harness:
    client: httpx.AsyncClient
    upstream: FakeLiteLLM
    usage: InMemoryUsageRecorder
    quotas: dict[str, int | None]

    async def chat(
        self, content: str = "hi", *, model: str = "chat-fast", **kw: Any
    ) -> httpx.Response:
        headers = {
            "Authorization": f"Bearer {TOKEN}",
            "X-Tenant-ID": TENANT,
            **kw.pop("headers", {}),
        }
        body = {"model": model, "messages": [{"role": "user", "content": content}], **kw}
        return await self.client.post("/v1/chat/completions", json=body, headers=headers)


def make_settings(**overrides: Any) -> GatewaySettings:
    values: dict[str, Any] = {
        "environment": "test",
        "gateway_service_token": TOKEN,
        "database_url": "postgresql+asyncpg://x:x@127.0.0.1:1/x",
        "breaker_failure_threshold": 2,
        "breaker_window_seconds": 60,
        "breaker_cooldown_seconds": 1,
        "default_daily_token_quota": 0,
        "guardrail_injection_mode": "flag",
    }
    values.update(overrides)
    return GatewaySettings(_env_file=None, **values)


HarnessFactory = Callable[..., Any]


@pytest.fixture
def make_harness() -> Callable[..., Any]:
    async def factory(**settings_overrides: Any) -> AsyncGenerator[Harness]:
        upstream = FakeLiteLLM()
        usage = InMemoryUsageRecorder()
        quotas: dict[str, int | None] = {}

        async def quota_lookup(tenant_id: str) -> int | None:
            return quotas.get(tenant_id)

        app = create_app(
            make_settings(**settings_overrides),
            Overrides(
                routes=RouteTable.from_dict(ROUTES),
                upstream_transport=httpx.MockTransport(upstream.handler),
                redis=fakeredis.FakeAsyncRedis(decode_responses=True),
                vector_index=InMemoryVectorIndex(),
                usage=usage,
                quota_lookup=quota_lookup,
            ),
        )
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gw") as client:
                yield Harness(client, upstream, usage, quotas)

    return factory


@pytest.fixture
async def gw(make_harness: Callable[..., Any]) -> AsyncGenerator[Harness]:
    async for harness in make_harness():
        yield harness
