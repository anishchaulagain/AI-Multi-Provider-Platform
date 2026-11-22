"""Handles EvaluateAnswerJob: load the answer and its sources, judge it, store scores."""

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from llm_client import Caller, GatewayError
from platform_db.models import EvalSource
from platform_evals import AnswerEvaluator, SampleUnavailableError, load_sample, save_result
from platform_messaging import Delivery, EvaluateAnswerJob, PermanentError
from platform_rag.store import ChunkStore

logger = structlog.get_logger(__name__)

_PERMANENT_GATEWAY_STATUS = {400, 401, 403, 404, 413, 422}


class AnswerScorer:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        chunks: ChunkStore,
        evaluator: AnswerEvaluator,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._chunks = chunks
        self._evaluator = evaluator

    async def handle(self, job: EvaluateAnswerJob, delivery: Delivery) -> None:
        log = logger.bind(message_id=str(job.message_id))
        async with self._sessionmaker() as session:
            try:
                loaded = await load_sample(session, self._chunks, job.message_id)
            except SampleUnavailableError as exc:
                # Deleted, or nothing to judge: done, not a failure.
                log.info("eval_skipped", reason=str(exc))
                return
            if loaded.message.tenant_id != job.tenant_id:
                raise PermanentError("message belongs to another tenant")

            caller = Caller(tenant_id=job.tenant_id, request_id=delivery.request_id)
            try:
                scores = await self._evaluator.evaluate(loaded.sample, caller)
            except GatewayError as exc:
                if exc.status_code in _PERMANENT_GATEWAY_STATUS or exc.code == "quota_exceeded":
                    raise PermanentError(f"Judge failed: {exc.message} ({exc.code})") from exc
                raise
            if all(v is None for v in scores.as_dict().values()) and any(
                isinstance(d, dict) and "error" in d for d in scores.details.values()
            ):
                # Every judge reply was unusable; a retry may get valid JSON.
                raise RuntimeError("judge returned no usable verdicts")

            try:
                await save_result(session, loaded, scores, source=EvalSource.ONLINE)
            except IntegrityError:
                # The conversation was deleted while we judged it.
                log.info("eval_message_gone")
                return
        log.info("answer_evaluated", **{k: v for k, v in scores.as_dict().items() if v is not None})
