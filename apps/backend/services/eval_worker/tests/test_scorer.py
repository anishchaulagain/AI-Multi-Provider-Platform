"""AnswerScorer against a real Postgres (skipped if unreachable), in-memory Qdrant and a
fake judge. Uses its own database, created and migrated on first use."""

import argparse
import asyncio
import json
import os
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
from alembic import command
from alembic.config import Config
from qdrant_client import AsyncQdrantClient
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eval_worker.scorer import AnswerScorer
from llm_client import GatewayClient
from platform_db.models import (
    Conversation,
    EvalResult,
    Message,
    MessageRole,
    Tenant,
    User,
)
from platform_db.session import create_engine, create_sessionmaker
from platform_evals import AnswerEvaluator
from platform_messaging import Delivery, EvaluateAnswerJob, PermanentError
from platform_rag.sparse import Bm25Encoder
from platform_rag.store import ChunkRecord, ChunkStore, chunk_point_id

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[3]
DATABASE_URL = os.getenv(
    "TEST_EVAL_DATABASE_URL",
    "postgresql+asyncpg://platform:platform@localhost:5432/platform_test_eval",
)
DELIVERY = Delivery(attempt=0, max_retries=3, redelivered=False, request_id=None, traceparent=None)


def _dsn(database: str | None = None) -> str:
    url = make_url(DATABASE_URL).set(drivername="postgresql")
    if database:
        url = url.set(database=database)
    return url.render_as_string(hide_password=False)


@pytest.fixture(scope="session")
async def sessionmaker() -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    try:
        admin = await asyncpg.connect(_dsn("postgres"), timeout=3)
    except Exception as exc:  # noqa: BLE001
        if os.getenv("CI"):
            pytest.fail(f"Postgres unavailable in CI: {exc!r}")
        pytest.skip(f"Postgres unavailable: {exc!r}")
    database = make_url(DATABASE_URL).database
    try:
        if not await admin.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database):
            await admin.execute(f'CREATE DATABASE "{database}"')
    finally:
        await admin.close()
    cfg = Config(str(BACKEND_DIR / "libs" / "db" / "alembic.ini"))
    cfg.cmd_opts = argparse.Namespace(x=[f"database_url={DATABASE_URL}"])
    await asyncio.to_thread(command.upgrade, cfg, "head")

    engine = create_engine(DATABASE_URL)
    yield create_sessionmaker(engine)
    await engine.dispose()


class FakeJudge:
    def __init__(self) -> None:
        self.fail_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with:
            return httpx.Response(
                self.fail_with, json={"error": {"code": "all_deployments_failed", "message": "x"}}
            )
        system = json.loads(request.content)["messages"][1]["content"]
        if "grounded in its SOURCES" in system:
            reply: dict[str, Any] = {"claims": [{"claim": "Rust", "supported": True}]}
        elif "Rate how well" in system:
            reply = {"score": 3}
        elif "For each numbered SOURCE" in system:
            reply = {"sources": [{"n": 1, "relevant": True}]}
        else:
            reply = {"statements": [{"id": 1, "supported": True}]}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": json.dumps(reply)}}]},
            headers={"X-Gateway-Deployment": "judge-x"},
        )


@pytest.fixture
async def setup(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[tuple[AnswerScorer, FakeJudge, Message]]:
    qdrant = AsyncQdrantClient(":memory:")
    chunks = ChunkStore(qdrant, "test")
    judge = FakeJudge()
    gateway = GatewayClient("http://gw", "t", transport=httpx.MockTransport(judge.handler))

    async with sessionmaker() as session:
        tenant = Tenant(name=f"eval-{uuid.uuid4().hex[:6]}")
        session.add(tenant)
        await session.flush()
        user = User(tenant_id=tenant.id, email=f"{uuid.uuid4().hex[:8]}@x.io", password_hash="x")
        session.add(user)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, user_id=user.id, title="t", doc_ids=[])
        session.add(conversation)
        await session.flush()
        doc_id = uuid.uuid4()
        text = "Qdrant is written in Rust."
        await chunks.replace_document(
            tenant.id,
            doc_id,
            [
                ChunkRecord(
                    tenant_id=tenant.id,
                    doc_id=doc_id,
                    chunk_index=0,
                    text=text,
                    metadata={"doc_title": "KB"},
                    dense=[1.0, 0.0, 0.0],
                    sparse=Bm25Encoder().encode_document(text),
                )
            ],
        )
        [stored] = await chunks.get_chunks(tenant.id, [chunk_point_id(doc_id, 0)])
        session.add(
            Message(
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                role=MessageRole.USER,
                content="What is Qdrant written in?",
                citations=[],
            )
        )
        await session.flush()
        answer = Message(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content="Qdrant is written in Rust [1].",
            citations=[{"n": 1, "chunk_id": stored.id, "doc_id": str(doc_id)}],
            retrieval={"chunks": [{"chunk_id": stored.id, "doc_id": str(doc_id)}]},
            usage={"gateway": {"alias": "chat-smart", "deployment": "dep-a", "provider": "prov"}},
        )
        session.add(answer)
        await session.commit()

    scorer = AnswerScorer(
        sessionmaker=sessionmaker, chunks=chunks, evaluator=AnswerEvaluator(gateway)
    )
    yield scorer, judge, answer
    await gateway.aclose()
    await qdrant.close()


async def test_scores_are_stored(
    setup: tuple[AnswerScorer, FakeJudge, Message], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    scorer, _, answer = setup
    await scorer.handle(
        EvaluateAnswerJob(message_id=answer.id, tenant_id=answer.tenant_id), DELIVERY
    )
    async with sessionmaker() as session:
        result = (
            await session.execute(select(EvalResult).where(EvalResult.message_id == answer.id))
        ).scalar_one()
    assert result.source == "online"
    assert (result.faithfulness, result.answer_relevance) == (1.0, 0.5)
    assert (result.context_precision, result.citation_accuracy) == (1.0, 1.0)
    assert (result.answer_deployment, result.judge_deployment) == ("dep-a", "judge-x")


async def test_missing_message_is_skipped(setup: tuple[AnswerScorer, FakeJudge, Message]) -> None:
    scorer, _, answer = setup
    await scorer.handle(
        EvaluateAnswerJob(message_id=uuid.uuid4(), tenant_id=answer.tenant_id), DELIVERY
    )


async def test_wrong_tenant_is_permanent(setup: tuple[AnswerScorer, FakeJudge, Message]) -> None:
    scorer, _, answer = setup
    with pytest.raises(PermanentError):
        await scorer.handle(
            EvaluateAnswerJob(message_id=answer.id, tenant_id=uuid.uuid4()), DELIVERY
        )


async def test_judge_outage_is_retried(setup: tuple[AnswerScorer, FakeJudge, Message]) -> None:
    scorer, judge, answer = setup
    judge.fail_with = 503
    job = EvaluateAnswerJob(message_id=answer.id, tenant_id=answer.tenant_id)
    with pytest.raises(Exception) as caught:
        await scorer.handle(job, DELIVERY)
    assert not isinstance(caught.value, PermanentError)

    judge.fail_with = 400
    with pytest.raises(PermanentError):
        await scorer.handle(job, DELIVERY)
