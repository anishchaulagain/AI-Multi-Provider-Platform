"""Online-eval sampling, answer feedback, and the eval results API.

A sample of answers (EVAL_SAMPLE_RATE) is queued for the eval worker; admins can also
score any answer on demand (the offline runner uses this). Both paths share
platform_evals, so live and offline scores mean the same thing.
"""

import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import and_, case, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from llm_client import Caller, GatewayError
from platform_api.core.resources import Resources
from platform_api.schemas.evals import (
    EvalResultDetail,
    EvalResultItem,
    EvalResultPage,
    EvalSourceRead,
    EvalSummary,
    FeedbackCreate,
    FeedbackRead,
    MetricStat,
    ModelStats,
    TrendPoint,
)
from platform_core.errors import AppError, ConflictError, NotFoundError
from platform_db.models import (
    Conversation,
    EvalResult,
    EvalSource,
    Message,
    MessageFeedback,
    MessageRole,
)
from platform_evals import METRICS, SampleUnavailableError, load_sample, save_result
from platform_evals.samples import MISSING_SOURCE
from platform_messaging import EvaluateAnswerJob

logger = structlog.get_logger(__name__)


# ---------- online sampling ----------
async def maybe_schedule_evaluation(
    resources: Resources, message_id: uuid.UUID, tenant_id: uuid.UUID
) -> bool:
    rate = resources.settings.eval_sample_rate
    if rate <= 0 or random.random() >= rate:
        return False
    try:
        await resources.publisher.publish(
            resources.settings.eval_queue(),
            EvaluateAnswerJob(message_id=message_id, tenant_id=tenant_id),
        )
    except Exception as exc:  # noqa: BLE001 - evaluation never blocks chat
        logger.warning("eval_enqueue_failed", error=repr(exc))
        return False
    return True


# ---------- feedback ----------
async def _own_answer(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, message_id: uuid.UUID
) -> Message:
    message = (
        await session.execute(
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Message.tenant_id == tenant_id,
                Message.role == MessageRole.ASSISTANT,
                Conversation.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if message is None:
        raise NotFoundError("Answer not found")
    return message


async def set_feedback(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    message_id: uuid.UUID,
    body: FeedbackCreate,
) -> FeedbackRead:
    await _own_answer(session, tenant_id=tenant_id, user_id=user_id, message_id=message_id)
    values = {"rating": body.rating, "comment": body.comment, "updated_at": func.now()}
    row = (
        await session.execute(
            insert(MessageFeedback)
            .values(tenant_id=tenant_id, message_id=message_id, user_id=user_id, **values)
            .on_conflict_do_update(constraint="uq_message_feedback_message_user", set_=values)
            .returning(MessageFeedback.rating, MessageFeedback.comment, MessageFeedback.updated_at)
        )
    ).one()
    await session.commit()
    return FeedbackRead(
        message_id=message_id, rating=row.rating, comment=row.comment, updated_at=row.updated_at
    )


async def delete_feedback(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID, message_id: uuid.UUID
) -> None:
    await _own_answer(session, tenant_id=tenant_id, user_id=user_id, message_id=message_id)
    await session.execute(
        delete(MessageFeedback).where(
            MessageFeedback.message_id == message_id, MessageFeedback.user_id == user_id
        )
    )
    await session.commit()


# ---------- summary ----------
def _metric_columns() -> dict[str, Any]:
    return {name: getattr(EvalResult, name) for name in METRICS}


def _means(row: Any) -> dict[str, float | None]:
    return {name: _round(getattr(row, name)) for name in METRICS}


def _round(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


async def summary(session: AsyncSession, *, tenant_id: uuid.UUID, days: int) -> EvalSummary:
    since = datetime.now(UTC) - timedelta(days=days)
    scope = and_(
        EvalResult.tenant_id == tenant_id,
        EvalResult.created_at >= since,
        EvalResult.error.is_(None),
    )
    columns = _metric_columns()
    abstained = case((EvalResult.details["abstained"].as_boolean(), 1.0), else_=0.0)

    overall = (
        await session.execute(
            select(
                func.count().label("count"),
                func.avg(abstained).label("abstained"),
                *(func.avg(c).label(n) for n, c in columns.items()),
                *(func.count(c).label(f"{n}_n") for n, c in columns.items()),
            ).where(scope)
        )
    ).one()

    day = func.date_trunc("day", EvalResult.created_at)
    trend = (
        await session.execute(
            select(
                day.label("day"),
                func.count().label("count"),
                *(func.avg(c).label(n) for n, c in columns.items()),
            )
            .where(scope)
            .group_by(day)
            .order_by(day)
        )
    ).all()

    by_model_rows = (
        await session.execute(
            select(
                EvalResult.answer_deployment,
                func.max(EvalResult.answer_provider).label("provider"),
                func.count().label("count"),
                *(func.avg(c).label(n) for n, c in columns.items()),
            )
            .where(scope)
            .group_by(EvalResult.answer_deployment)
            .order_by(func.count().desc())
        )
    ).all()

    # Feedback covers every answer in the window, evaluated or not.
    deployment = Message.usage["gateway"]["deployment"].astext
    feedback_rows = (
        await session.execute(
            select(
                deployment.label("deployment"),
                func.count().filter(MessageFeedback.rating > 0).label("up"),
                func.count().filter(MessageFeedback.rating < 0).label("down"),
            )
            .join(Message, Message.id == MessageFeedback.message_id)
            .where(MessageFeedback.tenant_id == tenant_id, MessageFeedback.updated_at >= since)
            .group_by(deployment)
        )
    ).all()
    feedback = {r.deployment: (r.up, r.down) for r in feedback_rows}

    by_model = [
        ModelStats(
            deployment=r.answer_deployment,
            provider=r.provider,
            count=r.count,
            feedback_up=feedback.get(r.answer_deployment, (0, 0))[0],
            feedback_down=feedback.get(r.answer_deployment, (0, 0))[1],
            **_means(r),
        )
        for r in by_model_rows
    ]
    evaluated = {m.deployment for m in by_model}
    by_model += [
        ModelStats(deployment=d, provider=None, count=0, feedback_up=up, feedback_down=down)
        for d, (up, down) in feedback.items()
        if d not in evaluated
    ]

    return EvalSummary(
        days=days,
        count=overall.count,
        metrics={
            n: MetricStat(mean=_round(getattr(overall, n)), count=getattr(overall, f"{n}_n"))
            for n in METRICS
        },
        abstention_rate=_round(overall.abstained),
        trend=[TrendPoint(day=r.day.date(), count=r.count, **_means(r)) for r in trend],
        by_model=by_model,
        feedback={
            "up": sum(up for up, _ in feedback.values()),
            "down": sum(down for _, down in feedback.values()),
        },
    )


# ---------- results ----------
def _question_column() -> ColumnElement[str | None]:
    """The user message right before each evaluated answer."""
    question = aliased(Message)
    return (
        select(question.content)
        .where(
            question.conversation_id == Message.conversation_id,
            question.role == MessageRole.USER,
            question.created_at <= Message.created_at,
            question.id != Message.id,
        )
        .order_by(question.created_at.desc(), question.id.desc())
        .limit(1)
        .correlate(Message)
        .scalar_subquery()
    )


def _feedback_column() -> ColumnElement[int]:
    return (
        select(func.coalesce(func.sum(MessageFeedback.rating), 0))
        .where(MessageFeedback.message_id == EvalResult.message_id)
        .correlate(EvalResult)
        .scalar_subquery()
    )


def _item(result: EvalResult, message: Message, question: str | None, net: int) -> EvalResultItem:
    return EvalResultItem(
        id=result.id,
        message_id=result.message_id,
        conversation_id=message.conversation_id,
        source=result.source,
        run_id=result.run_id,
        question=question,
        answer=message.content,
        answer_alias=result.answer_alias,
        answer_deployment=result.answer_deployment,
        judge_deployment=result.judge_deployment,
        feedback=net or None,
        error=result.error,
        created_at=result.created_at,
        **{n: getattr(result, n) for n in METRICS},
    )


async def list_results(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    metric: str | None,
    model: str | None,
    source: str | None,
    order: str,
    limit: int,
    offset: int,
) -> EvalResultPage:
    filters: list[ColumnElement[bool]] = [EvalResult.tenant_id == tenant_id]
    if metric:
        filters.append(getattr(EvalResult, metric).is_not(None))
    if model:
        filters.append(EvalResult.answer_deployment == model)
    if source:
        filters.append(EvalResult.source == source)

    if order == "worst":
        # LEAST ignores NULLs: an answer is as bad as its worst applicable metric.
        key = getattr(EvalResult, metric) if metric else func.least(*_metric_columns().values())
        ordering = [key.asc().nulls_last(), EvalResult.created_at.desc()]
    else:
        ordering = [EvalResult.created_at.desc()]

    total = (
        await session.execute(select(func.count()).select_from(EvalResult).where(*filters))
    ).scalar_one()
    rows = (
        await session.execute(
            select(EvalResult, Message, _question_column(), _feedback_column())
            .join(Message, Message.id == EvalResult.message_id)
            .where(*filters)
            .order_by(*ordering)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    return EvalResultPage(items=[_item(*row) for row in rows], total=total)


async def get_result(
    session: AsyncSession, resources: Resources, *, tenant_id: uuid.UUID, result_id: uuid.UUID
) -> EvalResultDetail:
    row = (
        await session.execute(
            select(EvalResult, Message, _question_column(), _feedback_column())
            .join(Message, Message.id == EvalResult.message_id)
            .where(EvalResult.id == result_id, EvalResult.tenant_id == tenant_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("Evaluation not found")
    result, message = row[0], row[1]

    ids = [c["chunk_id"] for c in (message.retrieval or {}).get("chunks", [])]
    found = {c.id: c for c in await resources.chunks.get_chunks(tenant_id, ids)}
    sources = []
    for n, chunk_id in enumerate(ids, start=1):
        chunk = found.get(chunk_id)
        sources.append(
            EvalSourceRead(
                n=n,
                chunk_id=chunk_id,
                doc_id=chunk.doc_id if chunk else "",
                doc_title=chunk.doc_title if chunk else "",
                text=chunk.text if chunk else MISSING_SOURCE,
                missing=chunk is None,
            )
        )
    comments = (
        await session.execute(
            select(MessageFeedback)
            .where(MessageFeedback.message_id == message.id)
            .order_by(MessageFeedback.updated_at.desc())
        )
    ).scalars()
    return EvalResultDetail(
        **_item(*row).model_dump(),
        sources=sources,
        details=result.details,
        comments=[
            FeedbackRead(
                message_id=f.message_id, rating=f.rating, comment=f.comment, updated_at=f.updated_at
            )
            for f in comments
        ],
    )


async def evaluate_now(
    session: AsyncSession,
    resources: Resources,
    *,
    tenant_id: uuid.UUID,
    message_id: uuid.UUID,
    source: EvalSource,
    run_id: str | None,
    request_id: str | None,
) -> EvalResultDetail:
    """Judge one answer synchronously (admins, and the offline runner)."""
    try:
        loaded = await load_sample(session, resources.chunks, message_id)
    except SampleUnavailableError as exc:
        raise ConflictError(f"This message can't be evaluated: {exc}") from exc
    if loaded.message.tenant_id != tenant_id:
        raise NotFoundError("Answer not found")
    try:
        scores = await resources.evaluator.evaluate(
            loaded.sample, Caller(tenant_id=tenant_id, request_id=request_id)
        )
    except GatewayError as exc:
        err = AppError(f"Judge failed: {exc.message}")
        err.status_code = exc.status_code
        err.code = exc.code
        raise err from exc
    result_id = await save_result(session, loaded, scores, source=source, run_id=run_id)
    return await get_result(session, resources, tenant_id=tenant_id, result_id=result_id)
