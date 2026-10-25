"""History plumbing for chat: state loading (with a Redis hot cache), semantic recall,
compaction scheduling, and context capsules."""

import base64
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from llm_client import Caller, GatewayError
from platform_api.core.resources import Resources
from platform_core.errors import ForbiddenError, NotFoundError
from platform_db.models import ContextCapsule, Conversation, Message
from platform_history import (
    CapsuleContent,
    HistoryState,
    HistoryTurn,
    Memory,
    build_capsule,
    compact_conversation,
    render_capsule,
)
from platform_history import repo as history_repo
from platform_history.builder import RecallFn
from platform_messaging import CompactConversationJob

logger = structlog.get_logger(__name__)


# ---------- state ----------
def _state_to_json(state: HistoryState) -> str:
    return json.dumps(
        {
            "turns": [[t.msg_id, t.role, t.content] for t in state.turns],
            "summary": state.summary,
            "summarized_until": state.summarized_until,
            "memory": state.memory.model_dump(),
        }
    )


def _state_from_json(raw: str, capsule_text: str | None) -> HistoryState:
    data = json.loads(raw)
    return HistoryState(
        turns=[HistoryTurn(*t) for t in data["turns"]],
        summary=data["summary"],
        summarized_until=data["summarized_until"],
        memory=Memory.model_validate(data["memory"]),
        capsule_text=capsule_text,
    )


async def load_history(
    session: AsyncSession,
    resources: Resources,
    conversation: Conversation,
    *,
    capsule_text: str | None,
) -> HistoryState:
    """Conversation state, cached in Redis under conv_id:last_msg_id:summary_version,
    so any new message or compaction naturally misses the cache."""
    last = (
        await session.execute(
            select(Message.id)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    summary = await history_repo.latest_summary(session, conversation.id)
    key = f"hist:{conversation.id}:{last}:{summary.version if summary else 0}"
    try:
        cached = await resources.redis.get(key)
        if cached:
            return _state_from_json(str(cached), capsule_text)
    except RedisError:
        pass
    state = await history_repo.load_state(session, conversation.id, capsule_text=capsule_text)
    try:
        await resources.redis.set(
            key, _state_to_json(state), ex=resources.settings.history_cache_ttl_seconds
        )
    except RedisError:
        pass
    return state


def recall_fn(resources: Resources, conversation: Conversation, caller: Caller) -> RecallFn:
    settings = resources.settings

    async def recall(question: str, exclude: set[str], k: int) -> list[HistoryTurn]:
        try:
            embedded = await resources.gateway.embed(
                f"{settings.embed_query_prefix}{question}",
                caller=caller,
                model=settings.embed_alias,
            )
        except GatewayError as exc:
            logger.warning("recall_embedding_failed", error=exc.code)
            return []
        if not embedded.data.data:
            return []
        return await resources.turns.search(
            conversation.tenant_id,
            conversation.id,
            embedded.data.data[0].embedding,
            exclude_msg_ids=exclude,
            limit=k,
        )

    return recall


async def schedule_compaction(
    resources: Resources, conversation_id: uuid.UUID, tenant_id: uuid.UUID
) -> bool:
    """Queue compaction once older messages would leave the recent window (count)
    or the unsummarized history passes the token threshold. A Redis flag keeps one
    job per conversation in flight."""
    settings = resources.settings
    async with resources.sessionmaker() as session:
        count, pending = await history_repo.unsummarized(session, conversation_id)
    if (
        count < settings.history_max_recent_messages
        and pending <= settings.history_compact_threshold
    ):
        return False
    try:
        if not await resources.redis.set(
            f"compact:pending:{conversation_id}", "1", nx=True, ex=300
        ):
            return False
    except RedisError:
        pass
    try:
        await resources.publisher.publish(
            settings.history_queue(),
            CompactConversationJob(conversation_id=conversation_id, tenant_id=tenant_id),
        )
    except Exception as exc:  # noqa: BLE001 - compaction is an optimization; chat goes on
        logger.warning("compaction_enqueue_failed", error=repr(exc))
        return False
    logger.info("compaction_queued", conversation_id=str(conversation_id), tokens=pending)
    return True


# ---------- capsules ----------
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def sign_share_token(capsule_id: uuid.UUID, secret: str) -> str:
    """`<capsule id>.<hmac>`: verifiable without a lookup, unguessable without the key."""
    mac = hmac.new(secret.encode(), b"capsule:" + capsule_id.bytes, hashlib.sha256).digest()
    return f"{_b64(capsule_id.bytes)}.{_b64(mac[:18])}"


def verify_share_token(token: str, secret: str) -> uuid.UUID | None:
    try:
        raw_id, _ = token.split(".", 1)
        capsule_id = uuid.UUID(bytes=base64.urlsafe_b64decode(raw_id + "=" * (-len(raw_id) % 4)))
    except (ValueError, TypeError):
        return None
    expected = sign_share_token(capsule_id, secret)
    return capsule_id if hmac.compare_digest(expected, token) else None


async def capsule_by_token(
    session: AsyncSession, resources: Resources, token: str, tenant_id: uuid.UUID
) -> ContextCapsule:
    capsule_id = verify_share_token(token, resources.settings.jwt_secret.get_secret_value())
    capsule = await session.get(ContextCapsule, capsule_id) if capsule_id else None
    if capsule is None or capsule.share_token != token:
        raise NotFoundError("Capsule not found")
    if capsule.tenant_id != tenant_id:
        raise ForbiddenError("This capsule belongs to another workspace")
    if capsule.expires_at is not None and capsule.expires_at <= datetime.now(UTC):
        raise NotFoundError("This capsule link has expired")
    return capsule


async def capsule_for_tenant(
    session: AsyncSession, capsule_id: uuid.UUID, tenant_id: uuid.UUID
) -> ContextCapsule:
    capsule = await session.get(ContextCapsule, capsule_id)
    if capsule is None or capsule.tenant_id != tenant_id:
        raise NotFoundError("Capsule not found")
    if capsule.expires_at is not None and capsule.expires_at <= datetime.now(UTC):
        raise NotFoundError("This capsule has expired")
    return capsule


def capsule_text(capsule: ContextCapsule) -> str:
    return render_capsule(CapsuleContent.model_validate(capsule.content))


async def create_capsule(
    session: AsyncSession,
    resources: Resources,
    *,
    conversation: Conversation,
    user_id: uuid.UUID,
    title: str | None,
    expires_in_days: int | None,
    request_id: str | None,
) -> ContextCapsule:
    settings = resources.settings
    caller = Caller(tenant_id=conversation.tenant_id, user_id=user_id, request_id=request_id)
    # Fold everything older than the recent window into the summary first, so the
    # capsule reflects the whole conversation (recent turns become key exchanges).
    try:
        await compact_conversation(
            resources.sessionmaker,
            resources.gateway,
            resources.turns,
            resources.counter,
            conversation_id=conversation.id,
            tenant_id=conversation.tenant_id,
            caller=caller,
            alias=settings.summary_alias,
            embed_alias=settings.embed_alias,
            embed_prefix=settings.embed_document_prefix,
            summary_max_tokens=settings.summary_max_tokens,
            keep_recent=settings.history_keep_recent,
        )
    except (GatewayError, ValueError) as exc:
        logger.warning("capsule_compaction_failed", error=repr(exc)[:200])

    messages = await history_repo.messages(session, conversation.id)
    summary = await history_repo.latest_summary(session, conversation.id)
    memory = await history_repo.memory(session, conversation.id)
    last_alias = next(
        (
            (m.usage or {}).get("gateway", {}).get("deployment")
            for m in reversed(messages)
            if m.role == "assistant"
        ),
        None,
    )
    content, tokens = build_capsule(
        title=title or conversation.title,
        summary=summary.summary if summary else None,
        memory=memory,
        messages=messages,
        source_model=last_alias,
        counter=resources.counter,
    )
    capsule_id = uuid.uuid4()
    days = expires_in_days if expires_in_days is not None else settings.capsule_default_ttl_days
    capsule = ContextCapsule(
        id=capsule_id,
        tenant_id=conversation.tenant_id,
        created_by=user_id,
        source_conversation_id=conversation.id,
        title=content.title[:200],
        content=content.model_dump(),
        token_count=tokens,
        source_model=last_alias,
        share_token=sign_share_token(capsule_id, settings.jwt_secret.get_secret_value()),
        expires_at=datetime.now(UTC) + timedelta(days=days) if days else None,
    )
    session.add(capsule)
    await session.commit()
    await session.refresh(capsule)
    return capsule


async def fork_capsule(
    session: AsyncSession,
    capsule: ContextCapsule,
    *,
    user_id: uuid.UUID,
    alias: str | None,
    title: str | None,
) -> Conversation:
    conversation = Conversation(
        tenant_id=capsule.tenant_id,
        user_id=user_id,
        title=(title or f"{capsule.title} (fork)")[:200],
        doc_ids=[],
        alias=alias,
        capsule_id=capsule.id,
    )
    session.add(conversation)
    await session.commit()
    await session.refresh(conversation)
    return conversation
