import json
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import aio_pika
import asyncpg
import httpx
import pytest

from llm_client import Caller
from platform_api.core.config import Settings
from platform_history import compact_conversation
from tests.conftest import TEST_RABBITMQ_URL, make_settings
from tests.fake_gateway import FakeGateway
from tests.integration.conftest import MEMBER_EMAIL, MEMBER_PASSWORD, _asyncpg_dsn, login
from tests.integration.test_chat import _chat

QUEUE = "test-history.compact"


@pytest.fixture
def settings() -> Settings:
    # A low threshold so a few short turns trigger compaction.
    return make_settings(history_compact_threshold=40)


@pytest.fixture
async def compact_queue() -> AsyncGenerator[aio_pika.abc.AbstractQueue]:
    connection = await aio_pika.connect(TEST_RABBITMQ_URL)
    channel = await connection.channel()
    try:
        queue = await channel.get_queue(QUEUE)
        await queue.purge()
    except aio_pika.exceptions.ChannelClosed:
        channel = await connection.channel()
        queue = None  # declared by the API's publisher on first use
    yield queue  # type: ignore[misc]
    await connection.close()


def _resources(client: httpx.AsyncClient) -> Any:
    return client._transport.app.state.resources  # type: ignore[attr-defined]


async def _new_conversation(client: httpx.AsyncClient, headers: dict[str, str], **body: Any) -> str:
    response = await client.post("/api/v1/conversations", json=body, headers=headers)
    assert response.status_code == 201, response.text
    cid: str = response.json()["id"]
    return cid


async def _compact(client: httpx.AsyncClient, cid: str, tenant: str) -> Any:
    """Run the worker's compaction step in-process."""
    r = _resources(client)
    return await compact_conversation(
        r.sessionmaker,
        r.gateway,
        r.turns,
        r.counter,
        conversation_id=uuid.UUID(cid),
        tenant_id=uuid.UUID(tenant),
        caller=Caller(tenant_id=tenant),
        keep_recent=4,
    )


async def _tenant(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    me: dict[str, str] = (await client.get("/api/v1/auth/me", headers=headers)).json()
    return me["tenant_id"]


def _system_text(body: dict[str, Any]) -> str:
    return "\n".join(m["content"] for m in body["messages"] if m["role"] == "system")


async def test_usage_reports_history_tokens(
    client: httpx.AsyncClient, admin_headers: dict[str, str], fake_gateway: FakeGateway
) -> None:
    cid = await _new_conversation(client, admin_headers)
    for q in ("first question", "second question", "third question"):
        events = await _chat(client, admin_headers, cid, q)
    history = dict(events)["usage"]["history"]
    assert history["raw_history_tokens"] > 0
    assert history["recent_turns"] == 4  # two previous exchanges, verbatim
    # Previous answers are replayed without their citation markup.
    sent = fake_gateway.rag_bodies[-1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user", "assistant", "user"]
    assert "[1]" not in sent[2]["content"] and "Rust" in sent[2]["content"]

    summary = (await client.get("/api/v1/usage/summary", headers=admin_headers)).json()
    assert summary["history_tokens_raw"] > 0 and summary["history_tokens_sent"] > 0


async def test_compaction_is_queued_once_past_threshold(
    client: httpx.AsyncClient, admin_headers: dict[str, str], compact_queue: Any
) -> None:
    cid = await _new_conversation(client, admin_headers)
    for i in range(3):
        await _chat(client, admin_headers, cid, f"question number {i} about warehouse robots")
    connection = await aio_pika.connect(TEST_RABBITMQ_URL)
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(QUEUE, passive=True)
        assert queue.declaration_result.message_count == 1  # Redis flag: one job in flight
        message = await queue.get(no_ack=True)
        assert json.loads(message.body)["conversation_id"] == cid


async def test_summary_memory_and_recall_reach_the_prompt(
    client: httpx.AsyncClient, admin_headers: dict[str, str], fake_gateway: FakeGateway
) -> None:
    tenant = await _tenant(client, admin_headers)
    cid = await _new_conversation(client, admin_headers)
    await _chat(client, admin_headers, cid, "Remember the code FACT-alpha for the Atlas robot.")
    for i in range(3):
        await _chat(client, admin_headers, cid, f"Unrelated filler question {i} about vacations.")

    result = await _compact(client, cid, tenant)
    assert result is not None and "FACT-alpha" in result.summary
    assert result.covered_messages == 4  # 8 messages, newest 4 stay verbatim
    assert await _compact(client, cid, tenant) is None  # nothing new to fold in

    await _chat(client, admin_headers, cid, "What was the code for the Atlas robot FACT-alpha?")
    system = _system_text(fake_gateway.rag_bodies[-1])
    assert "Mentioned FACT-alpha" in system  # memory facts
    assert "Summary of the earlier conversation:\nSummary: FACT-alpha" in system
    assert "Earlier exchanges relevant to the new question" in system  # semantic recall
    assert "User: Remember the code FACT-alpha" in system

    messages = (
        await client.get(f"/api/v1/conversations/{cid}/messages", headers=admin_headers)
    ).json()
    stats = messages[-1]["usage"]["history"]
    assert stats["recalled_turns"] >= 1 and stats["sent_history_tokens"] > 0


async def test_capsule_round_trip_and_fork(
    client: httpx.AsyncClient, admin_headers: dict[str, str], fake_gateway: FakeGateway
) -> None:
    cid = await _new_conversation(client, admin_headers)
    await _chat(client, admin_headers, cid, "Our project codename is FACT-beta.")
    # Five exchanges: compaction keeps the newest six messages verbatim and
    # summarizes the rest, so the fact reaches the capsule through the summary.
    for i in range(4):
        await _chat(client, admin_headers, cid, f"Follow-up {i} on the plan.")

    created = await client.post(
        f"/api/v1/conversations/{cid}/capsules", json={"title": "Plan"}, headers=admin_headers
    )
    assert created.status_code == 201, created.text
    capsule = created.json()
    assert capsule["title"] == "Plan" and capsule["source_conversation_id"] == cid
    assert "FACT-beta" in capsule["content"]["summary"]
    assert capsule["content"]["key_exchanges"][-1]["question"] == "Follow-up 3 on the plan."
    assert 0 < capsule["token_count"] < 600

    # Read-only view: signed token, same workspace (any member), tampering rejected.
    token = capsule["share_token"]
    member = {"Authorization": f"Bearer {await login(client, MEMBER_EMAIL, MEMBER_PASSWORD)}"}
    view = await client.get(f"/api/v1/capsules/{token}", headers=member)
    assert view.status_code == 200 and view.json()["id"] == capsule["id"]
    bad = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    assert (await client.get(f"/api/v1/capsules/{bad}", headers=member)).status_code == 404

    # Fork onto another alias: the new conversation's prompts carry the capsule.
    bad_alias = await client.post(
        f"/api/v1/capsules/{capsule['id']}/fork", json={"alias": "nope"}, headers=member
    )
    assert bad_alias.status_code == 400
    fork = await client.post(
        f"/api/v1/capsules/{capsule['id']}/fork", json={"alias": "chat-fast"}, headers=member
    )
    assert fork.status_code == 201, fork.text
    forked = fork.json()
    assert forked["alias"] == "chat-fast" and forked["capsule_id"] == capsule["id"]

    await _chat(client, member, forked["id"], "What is our project codename?")
    body = fake_gateway.rag_bodies[-1]
    assert body["model"] == "chat-fast"
    assert "Shared context capsule:\nSummary: Summary: FACT-beta" in _system_text(body)

    listed = (await client.get("/api/v1/capsules", headers=admin_headers)).json()
    assert [c["id"] for c in listed] == [capsule["id"]]


async def test_capsule_attached_to_a_single_request(
    client: httpx.AsyncClient, admin_headers: dict[str, str], fake_gateway: FakeGateway
) -> None:
    source = await _new_conversation(client, admin_headers)
    await _chat(client, admin_headers, source, "The handoff token is FACT-gamma.")
    capsule = (
        await client.post(
            f"/api/v1/conversations/{source}/capsules", json={}, headers=admin_headers
        )
    ).json()
    other = await _new_conversation(client, admin_headers)
    response = await client.post(
        f"/api/v1/conversations/{other}/chat",
        json={"message": "What is the handoff token?", "context_capsule_id": capsule["id"]},
        headers=admin_headers,
    )
    assert response.status_code == 200
    system = _system_text(fake_gateway.rag_bodies[-1])
    assert "Shared context capsule" in system and "FACT-gamma" in system


async def test_expired_capsule_link(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    cid = await _new_conversation(client, admin_headers)
    await _chat(client, admin_headers, cid, "hello")
    capsule = (
        await client.post(
            f"/api/v1/conversations/{cid}/capsules",
            json={"expires_in_days": 1},
            headers=admin_headers,
        )
    ).json()
    assert capsule["expires_at"] is not None
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        await conn.execute(
            "UPDATE context_capsules SET expires_at = now() - interval '1 minute' WHERE id = $1",
            uuid.UUID(capsule["id"]),
        )
    finally:
        await conn.close()
    response = await client.get(f"/api/v1/capsules/{capsule['share_token']}", headers=admin_headers)
    assert response.status_code == 404
    assert "expired" in response.json()["error"]["message"]


async def test_concurrent_compactions_do_not_conflict(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    """The worker and capsule creation may compact the same conversation at once."""
    import asyncio

    tenant = await _tenant(client, admin_headers)
    cid = await _new_conversation(client, admin_headers)
    for i in range(5):
        await _chat(client, admin_headers, cid, f"Question {i} with FACT-delta{i}.")
    results = await asyncio.gather(_compact(client, cid, tenant), _compact(client, cid, tenant))
    assert sum(r is not None for r in results) == 1  # one wins, the other stands down
