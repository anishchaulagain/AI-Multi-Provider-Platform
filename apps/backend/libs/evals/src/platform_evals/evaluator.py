"""Scores one answer with the `judge` alias: faithfulness, answer relevance, context
precision and citation accuracy.

The judge is asked to avoid the answering model's provider (it is tried last), so
answers aren't graded by the model family that wrote them whenever another is up.
A judge reply that fails validation leaves that one metric empty (with the error in
`details`); gateway errors propagate so the caller can retry the whole evaluation.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from llm_client import Caller, ChatMessage, GatewayClient
from platform_evals import metrics as m
from platform_evals.metrics import Source

logger = structlog.get_logger(__name__)

METRICS = ("faithfulness", "answer_relevance", "context_precision", "citation_accuracy")


@dataclass(frozen=True)
class EvalSample:
    question: str
    answer: str
    sources: list[Source]
    # Gateway routing of the answer: {"alias", "deployment", "provider"}.
    answered_by: dict[str, str] = field(default_factory=dict)


@dataclass
class EvalScores:
    faithfulness: float | None = None
    answer_relevance: float | None = None
    context_precision: float | None = None
    citation_accuracy: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    judges: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, float | None]:
        return {name: getattr(self, name) for name in METRICS}


class AnswerEvaluator:
    def __init__(self, gateway: GatewayClient, *, alias: str = "judge") -> None:
        self._gateway = gateway
        self._alias = alias

    async def evaluate(self, sample: EvalSample, caller: Caller) -> EvalScores:
        provider = sample.answered_by.get("provider")
        if provider:
            caller = replace(caller, avoid_providers=(provider,))
        scores = EvalScores()
        abstained = m.is_abstention(sample.answer)
        scores.details["abstained"] = abstained
        statements = m.cited_statements(sample.answer)

        tasks: list[Callable[[], Awaitable[None]]] = [
            lambda: self._faithfulness(sample, caller, scores)
        ]
        if not abstained:
            tasks.append(lambda: self._relevance(sample, caller, scores))
        if sample.sources:
            tasks.append(lambda: self._context(sample, caller, scores))
        if statements:
            tasks.append(lambda: self._citations(sample, statements, caller, scores))
        await asyncio.gather(*(task() for task in tasks))
        return scores

    async def _ask[M: BaseModel](
        self,
        name: str,
        system: str,
        user: str,
        schema: type[M],
        caller: Caller,
        scores: EvalScores,
    ) -> M | None:
        try:
            result = await self._gateway.judge(
                [
                    ChatMessage(role="system", content=system),
                    ChatMessage(role="user", content=user),
                ],
                schema,
                caller=caller,
                model=self._alias,
            )
        except (ValidationError, ValueError) as exc:
            logger.warning("judge_output_invalid", metric=name, error=str(exc)[:200])
            scores.details[name] = {"error": f"invalid judge output: {str(exc)[:300]}"}
            return None
        if result.meta.deployment and result.meta.deployment not in scores.judges:
            scores.judges.append(result.meta.deployment)
        return result.data

    async def _faithfulness(self, s: EvalSample, caller: Caller, scores: EvalScores) -> None:
        verdict = await self._ask(
            "faithfulness",
            m.FAITHFULNESS_PROMPT,
            f"SOURCES:\n{m.render_sources(s.sources)}\n\nANSWER:\n{s.answer}",
            m.FaithfulnessVerdict,
            caller,
            scores,
        )
        if verdict is not None:
            scores.faithfulness = m.faithfulness_score(verdict)
            scores.details["faithfulness"] = {"claims": [c.model_dump() for c in verdict.claims]}

    async def _relevance(self, s: EvalSample, caller: Caller, scores: EvalScores) -> None:
        verdict = await self._ask(
            "answer_relevance",
            m.RELEVANCE_PROMPT,
            f"QUESTION:\n{s.question}\n\nANSWER:\n{s.answer}",
            m.RelevanceVerdict,
            caller,
            scores,
        )
        if verdict is not None:
            scores.answer_relevance = m.relevance_score(verdict)
            scores.details["answer_relevance"] = verdict.model_dump()

    async def _context(self, s: EvalSample, caller: Caller, scores: EvalScores) -> None:
        verdict = await self._ask(
            "context_precision",
            m.CONTEXT_PROMPT,
            f"QUESTION:\n{s.question}\n\nSOURCES:\n{m.render_sources(s.sources)}",
            m.ContextVerdict,
            caller,
            scores,
        )
        if verdict is not None:
            scores.context_precision = m.context_precision_score(verdict, len(s.sources))
            scores.details["context_precision"] = {
                "relevant": sorted(v.n for v in verdict.sources if v.relevant)
            }

    async def _citations(
        self,
        s: EvalSample,
        statements: Sequence[m.CitedStatement],
        caller: Caller,
        scores: EvalScores,
    ) -> None:
        verdict = await self._ask(
            "citation_accuracy",
            m.CITATION_PROMPT,
            f"SOURCES:\n{m.render_sources(s.sources)}\n\n"
            f"STATEMENTS:\n{m.render_statements(statements)}",
            m.CitationVerdict,
            caller,
            scores,
        )
        if verdict is not None:
            scores.citation_accuracy = m.citation_score(verdict, statements, len(s.sources))
            supported = {v.id for v in verdict.statements if v.supported}
            scores.details["citation_accuracy"] = {
                "statements": [
                    {"text": st.text, "sources": st.sources, "supported": st.id in supported}
                    for st in statements
                ]
            }
