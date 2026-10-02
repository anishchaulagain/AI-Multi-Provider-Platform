import uuid

import asyncpg
import httpx

from tests.fake_gateway import FakeGateway
from tests.integration.conftest import MEMBER_EMAIL, MEMBER_PASSWORD, _asyncpg_dsn, login

CHAT = {"model": "chat-fast", "messages": [{"role": "user", "content": "hello"}]}


async def test_requires_auth(client: httpx.AsyncClient) -> None:
    response = await client.post("/api/v1/llm/chat/completions", json=CHAT)
    assert response.status_code == 401


async def test_chat_forwards_identity_and_returns_meta(
    client: httpx.AsyncClient, admin_headers: dict[str, str], fake_gateway: FakeGateway
) -> None:
    me = (await client.get("/api/v1/auth/me", headers=admin_headers)).json()

    response = await client.post("/api/v1/llm/chat/completions", json=CHAT, headers=admin_headers)
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Hi there"
    assert response.headers["x-gateway-deployment"] == "fake-deployment"

    sent = fake_gateway.last
    assert sent.headers["x-tenant-id"] == me["tenant_id"]
    assert sent.headers["x-user-id"] == me["id"]
    assert sent.headers["authorization"] == "Bearer dev-gateway-token-change-me"
    assert sent.headers["x-request-id"] == response.headers["x-request-id"]


async def test_chat_with_api_key(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> None:
    key = (
        await client.post("/api/v1/api-keys", json={"name": "sdk"}, headers=admin_headers)
    ).json()["key"]
    response = await client.post(
        "/api/v1/llm/chat/completions", json=CHAT, headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 200


async def test_streaming_passthrough(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/api/v1/llm/chat/completions", json={**CHAT, "stream": True}, headers=admin_headers
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-gateway-alias"] == "chat-fast"
    assert '"content":"Hi"' in response.text
    assert response.text.rstrip().endswith("data: [DONE]")


async def test_gateway_errors_keep_status_and_code(
    client: httpx.AsyncClient, admin_headers: dict[str, str], fake_gateway: FakeGateway
) -> None:
    fake_gateway.fail_with = (429, "quota_exceeded")
    for body in (CHAT, {**CHAT, "stream": True}):
        response = await client.post(
            "/api/v1/llm/chat/completions", json=body, headers=admin_headers
        )
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "quota_exceeded"


async def test_gateway_unreachable_is_503(
    client: httpx.AsyncClient, admin_headers: dict[str, str], fake_gateway: FakeGateway
) -> None:
    fake_gateway.unreachable = True
    response = await client.post("/api/v1/llm/chat/completions", json=CHAT, headers=admin_headers)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "gateway_unavailable"


async def test_models_and_embeddings(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    models = await client.get("/api/v1/llm/models", headers=admin_headers)
    assert models.json()["data"][0]["id"] == "chat-fast"

    emb = await client.post(
        "/api/v1/llm/embeddings", json={"model": "embed", "input": "x"}, headers=admin_headers
    )
    assert emb.status_code == 200
    assert emb.headers["x-gateway-alias"] == "embed"


async def test_usage_summary(client: httpx.AsyncClient, admin_headers: dict[str, str]) -> None:
    me = (await client.get("/api/v1/auth/me", headers=admin_headers)).json()
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        for alias, status, tokens in [
            ("chat-fast", "ok", 100),
            ("chat-fast", "cache_hit", 0),
            ("chat-smart", "error", 0),
        ]:
            await conn.execute(
                """INSERT INTO llm_usage (id, tenant_id, kind, alias, status, cache, fallbacks,
                   prompt_tokens, completion_tokens, total_tokens, latency_ms)
                   VALUES ($1, $2, 'chat', $3, $4, 'miss', 0, 0, 0, $5, 50)""",
                uuid.uuid4(),
                uuid.UUID(me["tenant_id"]),
                alias,
                status,
                tokens,
            )
    finally:
        await conn.close()

    summary = (await client.get("/api/v1/usage/summary", headers=admin_headers)).json()
    assert summary["requests"] == 3
    assert summary["total_tokens"] == 100
    assert summary["cache_hits"] == 1
    assert summary["errors"] == 1
    assert [a["alias"] for a in summary["by_alias"]] == ["chat-fast", "chat-smart"]
    assert summary["tokens_used_today"] == 42
    assert summary["daily_token_limit"] == 1000


async def test_admin_providers_requires_admin(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    assert (await client.get("/api/v1/admin/providers", headers=admin_headers)).status_code == 200
    member = {"Authorization": f"Bearer {await login(client, MEMBER_EMAIL, MEMBER_PASSWORD)}"}
    assert (await client.get("/api/v1/admin/providers", headers=member)).status_code == 403
