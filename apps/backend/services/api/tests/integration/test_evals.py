import json
from collections.abc import AsyncGenerator
from typing import Any

import aio_pika
import httpx
import pytest
from qdrant_client import AsyncQdrantClient

from platform_api.core.config import Settings
from tests.conftest import TEST_RABBITMQ_URL, make_settings
from tests.fake_gateway import FakeGateway
from tests.integration.conftest import MEMBER_EMAIL, MEMBER_PASSWORD, login
from tests.integration.test_chat import _chat, _seed

QUEUE = "test-eval.answers"


async def _answer(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    qdrant: AsyncQdrantClient,
    settings: Settings,
) -> tuple[str, str]:
    """A grounded answer with one valid ([1]) and one invented ([9]) citation."""
    await _seed(client, headers, qdrant, settings)
    cid = (await client.post("/api/v1/conversations", json={}, headers=headers)).json()["id"]
    events = await _chat(client, headers, cid, "What is Qdrant written in?")
    done = dict(events)["done"]
    return cid, done["message_id"]


async def test_feedback_is_set_shown_and_cleared(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    qdrant: AsyncQdrantClient,
    settings: Settings,
) -> None:
    cid, message_id = await _answer(client, admin_headers, qdrant, settings)
    url = f"/api/v1/messages/{message_id}/feedback"

    response = await client.post(
        url, json={"rating": -1, "comment": "Wrong source"}, headers=admin_headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["rating"] == -1
    # Upsert: changing your mind replaces the rating.
    response = await client.post(url, json={"rating": 1}, headers=admin_headers)
    assert response.json() == {**response.json(), "rating": 1, "comment": None}

    messages = (
        await client.get(f"/api/v1/conversations/{cid}/messages", headers=admin_headers)
    ).json()
    assert [m["feedback"] for m in messages] == [None, 1]

    assert (await client.post(url, json={"rating": 0}, headers=admin_headers)).status_code == 422
    # Only the conversation's owner can rate its answers.
    member = {"Authorization": f"Bearer {await login(client, MEMBER_EMAIL, MEMBER_PASSWORD)}"}
    assert (await client.post(url, json={"rating": 1}, headers=member)).status_code == 404

    assert (await client.delete(url, headers=admin_headers)).status_code == 204
    messages = (
        await client.get(f"/api/v1/conversations/{cid}/messages", headers=admin_headers)
    ).json()
    assert messages[1]["feedback"] is None


async def test_evaluate_answer_and_browse_results(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    qdrant: AsyncQdrantClient,
    settings: Settings,
    fake_gateway: FakeGateway,
) -> None:
    _, message_id = await _answer(client, admin_headers, qdrant, settings)
    await client.post(
        f"/api/v1/messages/{message_id}/feedback", json={"rating": -1}, headers=admin_headers
    )

    response = await client.post(
        f"/api/v1/evals/messages/{message_id}",
        json={"source": "offline", "run_id": "run-1"},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["faithfulness"] == 0.5  # one of two claims supported
    assert result["answer_relevance"] == 1.0
    assert result["context_precision"] == 1.0  # the only relevant source ranks first
    assert result["citation_accuracy"] == 0.5  # [9] isn't a provided source
    assert result["answer_deployment"] == "fake-deployment"
    assert result["judge_deployment"] == "fake-judge"
    assert result["question"] == "What is Qdrant written in?"
    assert result["sources"][0]["n"] == 1 and result["sources"][0]["doc_title"] == "KB"
    statements = result["details"]["citation_accuracy"]["statements"]
    assert [s["sources"] for s in statements] == [[1], [9]]
    assert result["feedback"] == -1

    # The judge is steered away from the answering model's provider.
    metrics = sorted(metric for metric, _ in fake_gateway.judged)
    assert metrics == sorted(
        ["faithfulness", "answer_relevance", "context_precision", "citation_accuracy"]
    )
    assert {h.get("x-gateway-avoid-providers") for _, h in fake_gateway.judged} == {"fake"}

    page = (
        await client.get(
            "/api/v1/evals/results?order=worst&metric=faithfulness", headers=admin_headers
        )
    ).json()
    assert page["total"] == 1 and page["items"][0]["id"] == result["id"]
    assert page["items"][0]["source"] == "offline" and page["items"][0]["run_id"] == "run-1"
    detail = (
        await client.get(f"/api/v1/evals/results/{result['id']}", headers=admin_headers)
    ).json()
    assert detail["answer"].startswith("Qdrant is written in Rust")

    summary = (await client.get("/api/v1/evals/summary", headers=admin_headers)).json()
    assert summary["count"] == 1
    assert summary["metrics"]["faithfulness"] == {"mean": 0.5, "count": 1}
    assert summary["abstention_rate"] == 0.0
    assert summary["feedback"] == {"up": 0, "down": 1}
    [model] = summary["by_model"]
    assert model["deployment"] == "fake-deployment" and model["feedback_down"] == 1
    assert len(summary["trend"]) == 1

    # Evaluating again replaces the earlier result.
    again = await client.post(f"/api/v1/evals/messages/{message_id}", headers=admin_headers)
    assert again.json()["id"] == result["id"] and again.json()["source"] == "manual"


async def test_eval_results_are_admin_only(client: httpx.AsyncClient) -> None:
    member = {"Authorization": f"Bearer {await login(client, MEMBER_EMAIL, MEMBER_PASSWORD)}"}
    assert (await client.get("/api/v1/evals/summary", headers=member)).status_code == 403
    assert (await client.get("/api/v1/evals/results", headers=member)).status_code == 403


async def test_user_messages_cannot_be_evaluated(
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    qdrant: AsyncQdrantClient,
    settings: Settings,
) -> None:
    cid, _ = await _answer(client, admin_headers, qdrant, settings)
    messages = (
        await client.get(f"/api/v1/conversations/{cid}/messages", headers=admin_headers)
    ).json()
    response = await client.post(
        f"/api/v1/evals/messages/{messages[0]['id']}", headers=admin_headers
    )
    assert response.status_code == 409


# ---------- online sampling ----------
class TestOnlineSampling:
    @pytest.fixture
    def settings(self) -> Settings:
        return make_settings(eval_sample_rate=1.0)

    @pytest.fixture
    async def eval_queue(self) -> AsyncGenerator[Any]:
        connection = await aio_pika.connect(TEST_RABBITMQ_URL)
        channel = await connection.channel()
        try:
            await (await channel.get_queue(QUEUE)).purge()
        except aio_pika.exceptions.ChannelClosed:
            channel = await connection.channel()  # declared by the publisher on first use
        yield channel
        await connection.close()

    async def test_sampled_answers_are_queued_for_the_judge(
        self,
        client: httpx.AsyncClient,
        admin_headers: dict[str, str],
        qdrant: AsyncQdrantClient,
        settings: Settings,
        eval_queue: Any,
    ) -> None:
        _, message_id = await _answer(client, admin_headers, qdrant, settings)
        queue = await eval_queue.get_queue(QUEUE)
        message = await queue.get(no_ack=True, fail=False)
        assert message is not None
        assert json.loads(message.body)["message_id"] == message_id
