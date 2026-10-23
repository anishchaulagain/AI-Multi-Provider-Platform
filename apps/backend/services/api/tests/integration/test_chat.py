import json
import uuid
from typing import Any

import asyncpg
import httpx
from qdrant_client import AsyncQdrantClient

from platform_api.core.config import Settings
from platform_rag.sparse import Bm25Encoder
from platform_rag.store import ChunkRecord, ChunkStore
from tests.fake_gateway import FakeGateway, embed
from tests.integration.conftest import MEMBER_EMAIL, MEMBER_PASSWORD, _asyncpg_dsn, login

CORPUS = [
    "Qdrant is a vector database written in Rust.",
    "Neo4j is a graph database queried with Cypher.",
    "Employees get twenty vacation days per year.",
]


async def _me(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    me: dict[str, str] = (await client.get("/api/v1/auth/me", headers=headers)).json()
    return me


async def _seed(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    qdrant: AsyncQdrantClient,
    settings: Settings,
) -> uuid.UUID:
    """One ready document in Postgres, its chunks in (in-memory) Qdrant."""
    tenant_id = uuid.UUID((await _me(client, headers))["tenant_id"])
    doc_id = uuid.uuid4()
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(
            """INSERT INTO documents (id, tenant_id, filename, content_type, size_bytes,
               content_hash, storage_key, status, attempts, chunk_count, details, title)
               VALUES ($1, $2, 'kb.md', 'text/markdown', 10, $3, 'k', 'ready', 1, 3, '{}', 'KB')""",
            doc_id,
            tenant_id,
            uuid.uuid4().hex * 2,
        )
    finally:
        await conn.close()
    sparse = Bm25Encoder()
    await ChunkStore(qdrant, settings.embed_model_name).replace_document(
        tenant_id,
        doc_id,
        [
            ChunkRecord(
                tenant_id=tenant_id,
                doc_id=doc_id,
                chunk_index=i,
                text=text,
                page=1,
                metadata={"doc_title": "KB"},
                dense=embed(text),
                sparse=sparse.encode_document(text),
            )
            for i, text in enumerate(CORPUS)
        ],
    )
    return doc_id


def _events(text: str) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        out.append((lines["event"], json.loads(lines["data"])))
    return out


async def _chat(
    client: httpx.AsyncClient, headers: dict[str, str], conversation_id: str, message: str
) -> list[tuple[str, dict[str, Any]]]:
    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/chat", json={"message": message}, headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return _events(response.text)


async def test_grounded_chat_streams_cites_and_persists(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    qdrant: AsyncQdrantClient,
    settings: Settings,
    fake_gateway: FakeGateway,
) -> None:
    await _seed(client, admin_headers, qdrant, settings)
    conversation = (
        await client.post("/api/v1/conversations", json={}, headers=admin_headers)
    ).json()
    assert conversation["title"] == "New conversation"

    events = await _chat(client, admin_headers, conversation["id"], "What is Qdrant written in?")
    names = [name for name, _ in events]
    assert names[-3:] == ["citations", "usage", "done"]
    assert set(names[:-3]) == {"token"}
    answer = "".join(data["text"] for name, data in events if name == "token")
    assert answer == fake_gateway.rag_answer

    citations = dict(events)["citations"]
    assert [c["n"] for c in citations["citations"]] == [1]
    assert citations["invalid"] == [9]
    cited = citations["citations"][0]
    assert cited["text"] == CORPUS[0] and cited["doc_title"] == "KB" and cited["page"] == 1

    usage = dict(events)["usage"]
    assert usage["completion_tokens"] == 9 and usage["gateway"]["alias"] == "chat-smart"
    assert fake_gateway.rag_prompts[0].startswith(f"Sources:\n\n[1] (KB, page 1)\n{CORPUS[0]}")

    # Persisted turns; citations resolve to chunk text, context text isn't stored.
    messages = (
        await client.get(
            f"/api/v1/conversations/{conversation['id']}/messages", headers=admin_headers
        )
    ).json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["id"] == dict(events)["done"]["message_id"]
    assert messages[1]["citations"][0]["text"] == CORPUS[0]
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        row = await conn.fetchrow(
            "SELECT citations, retrieval FROM messages WHERE role = 'assistant'"
        )
    finally:
        await conn.close()
    assert json.loads(row["citations"]) == [
        {"n": 1, "chunk_id": cited["chunk_id"], "doc_id": cited["doc_id"]}
    ]
    trace = json.loads(row["retrieval"])
    assert trace["invalid_citations"] == [9] and trace["chunks"][0]["chunk_id"] == cited["chunk_id"]

    listed = (await client.get("/api/v1/conversations", headers=admin_headers)).json()
    assert listed[0]["title"] == "What is Qdrant written in?"


async def test_followup_uses_rewritten_question(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    qdrant: AsyncQdrantClient,
    settings: Settings,
    fake_gateway: FakeGateway,
) -> None:
    await _seed(client, admin_headers, qdrant, settings)
    cid = (await client.post("/api/v1/conversations", json={}, headers=admin_headers)).json()["id"]
    await _chat(client, admin_headers, cid, "Tell me about Qdrant")
    assert fake_gateway.rewrites == 0
    await _chat(client, admin_headers, cid, "What is it written in?")
    assert fake_gateway.rewrites == 1
    assert "Question: What is it written in?" in fake_gateway.rag_prompts[-1]


async def test_document_filter(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    qdrant: AsyncQdrantClient,
    settings: Settings,
    fake_gateway: FakeGateway,
) -> None:
    doc_id = await _seed(client, admin_headers, qdrant, settings)
    bad = await client.post(
        "/api/v1/conversations", json={"doc_ids": [str(uuid.uuid4())]}, headers=admin_headers
    )
    assert bad.status_code == 400

    other = uuid.uuid4()
    conversation = await client.post(
        "/api/v1/conversations", json={"doc_ids": [str(doc_id)]}, headers=admin_headers
    )
    assert conversation.status_code == 201
    patched = await client.patch(
        f"/api/v1/conversations/{conversation.json()['id']}",
        json={"doc_ids": [str(other)]},
        headers=admin_headers,
    )
    assert patched.status_code == 400  # unknown document

    # A conversation scoped to a document with no chunks gets no sources.
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        tenant = (await _me(client, admin_headers))["tenant_id"]
        await conn.execute(
            """INSERT INTO documents (id, tenant_id, filename, content_type, size_bytes,
               content_hash, storage_key, status, attempts, chunk_count, details)
               VALUES ($1, $2, 'e.txt', 'text/plain', 1, 'h2', 'k2', 'ready', 1, 0, '{}')""",
            other,
            uuid.UUID(tenant),
        )
    finally:
        await conn.close()
    scoped = (
        await client.post(
            "/api/v1/conversations", json={"doc_ids": [str(other)]}, headers=admin_headers
        )
    ).json()
    events = await _chat(client, admin_headers, scoped["id"], "What is Qdrant?")
    assert dict(events)["citations"]["citations"] == []
    assert "(no sources found)" in fake_gateway.rag_prompts[-1]


async def test_conversations_are_private(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    cid = (await client.post("/api/v1/conversations", json={}, headers=admin_headers)).json()["id"]
    member = {"Authorization": f"Bearer {await login(client, MEMBER_EMAIL, MEMBER_PASSWORD)}"}
    assert (await client.get(f"/api/v1/conversations/{cid}", headers=member)).status_code == 404
    response = await client.post(
        f"/api/v1/conversations/{cid}/chat", json={"message": "hi"}, headers=member
    )
    assert response.status_code == 404
    assert (await client.get("/api/v1/conversations", headers=member)).json() == []

    assert (
        await client.delete(f"/api/v1/conversations/{cid}", headers=admin_headers)
    ).status_code == 204
    assert (
        await client.get(f"/api/v1/conversations/{cid}", headers=admin_headers)
    ).status_code == 404


async def test_gateway_failure_before_streaming_is_an_http_error(
    client: httpx.AsyncClient, admin_headers: dict[str, str], fake_gateway: FakeGateway
) -> None:
    cid = (await client.post("/api/v1/conversations", json={}, headers=admin_headers)).json()["id"]
    fake_gateway.fail_with = (429, "quota_exceeded")
    response = await client.post(
        f"/api/v1/conversations/{cid}/chat", json={"message": "hi"}, headers=admin_headers
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "quota_exceeded"
    # Nothing persisted for a request that never started.
    messages = await client.get(f"/api/v1/conversations/{cid}/messages", headers=admin_headers)
    assert messages.json() == []


async def test_graph_endpoints_disabled(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    response = await client.get("/api/v1/graph/entities?q=qdrant", headers=admin_headers)
    assert response.status_code == 503
