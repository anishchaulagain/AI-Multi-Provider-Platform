"""Handles CompactConversationJob: rolling summary, memory patch, recall embeddings."""

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from llm_client import Caller, GatewayClient, GatewayError
from platform_history import TokenCounter, TurnStore, compact_conversation
from platform_messaging import CompactConversationJob, Delivery, PermanentError

logger = structlog.get_logger(__name__)

_PERMANENT_GATEWAY_STATUS = {400, 401, 403, 404, 413, 422}


class Compactor:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: Redis,
        gateway: GatewayClient,
        turns: TurnStore,
        counter: TokenCounter,
        summary_alias: str,
        summary_max_tokens: int,
        keep_recent: int,
        embed_alias: str,
        embed_prefix: str,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._redis = redis
        self._gateway = gateway
        self._turns = turns
        self._counter = counter
        self._summary_alias = summary_alias
        self._summary_max_tokens = summary_max_tokens
        self._keep_recent = keep_recent
        self._embed_alias = embed_alias
        self._embed_prefix = embed_prefix

    async def handle(self, job: CompactConversationJob, delivery: Delivery) -> None:
        try:
            await compact_conversation(
                self._sessionmaker,
                self._gateway,
                self._turns,
                self._counter,
                conversation_id=job.conversation_id,
                tenant_id=job.tenant_id,
                caller=Caller(tenant_id=job.tenant_id, request_id=delivery.request_id),
                alias=self._summary_alias,
                embed_alias=self._embed_alias,
                embed_prefix=self._embed_prefix,
                summary_max_tokens=self._summary_max_tokens,
                keep_recent=self._keep_recent,
            )
        except IntegrityError:
            # The conversation was deleted while we summarized it.
            logger.info("compaction_conversation_gone", conversation_id=str(job.conversation_id))
        except GatewayError as exc:
            if exc.status_code in _PERMANENT_GATEWAY_STATUS or exc.code == "quota_exceeded":
                raise PermanentError(f"Summarization failed: {exc.message} ({exc.code})") from exc
            raise
        except ValueError as exc:
            # The model's JSON didn't validate; retrying may get a usable answer.
            raise RuntimeError(f"Invalid summary output: {exc}") from exc
        await self._release(job)

    async def on_failure(
        self,
        job: CompactConversationJob,
        delivery: Delivery,
        exc: BaseException,
        retry_in_s: float | None,
    ) -> None:
        if retry_in_s is None:
            await self._release(job)

    async def _release(self, job: CompactConversationJob) -> None:
        """Let the API queue the next compaction for this conversation."""
        try:
            await self._redis.delete(f"compact:pending:{job.conversation_id}")
        except RedisError:
            pass
