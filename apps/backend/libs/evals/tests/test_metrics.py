import json
from typing import Any

import httpx
import pytest

from llm_client import Caller, GatewayClient
from platform_evals import AnswerEvaluator, EvalSample, Source, hit_at_k, reciprocal_rank
from platform_evals import metrics as m

CALLER = Caller(tenant_id="00000000-0000-0000-0000-000000000001")


def test_cited_statements_keep_citations_with_their_sentence() -> None:
    answer = (
        "Employees get 25 days. [1] Up to 5 can be carried over [2].\n"
        "- Parental leave is 20 weeks【1, 3】\nThanks for asking!"
    )
    statements = m.cited_statements(answer)
    assert [(s.id, s.sources) for s in statements] == [(1, [1]), (2, [2]), (3, [1, 3])]
    assert statements[0].text == "Employees get 25 days [1]."
    assert statements[2].text.startswith("Parental leave")


def test_scores() -> None:
    claims = m.FaithfulnessVerdict(
        claims=[m.Claim(claim="a", supported=True), m.Claim(claim="b", supported=False)]
    )
    assert m.faithfulness_score(claims) == 0.5
    assert m.faithfulness_score(m.FaithfulnessVerdict()) == 1.0
    assert m.relevance_score(m.RelevanceVerdict(score=4)) == 0.75

    def precision(relevant: set[int], count: int = 3) -> float:
        verdict = m.ContextVerdict(
            sources=[m.SourceRelevance(n=n, relevant=n in relevant) for n in range(1, count + 1)]
        )
        return m.context_precision_score(verdict, count)

    assert precision({1}) == 1.0
    assert precision({2}) == 0.5
    assert precision({1, 3}) == pytest.approx((1 + 2 / 3) / 2)
    assert precision(set()) == 0.0

    statements = m.cited_statements("A [1]. B [2]. C [7].")
    verdict = m.CitationVerdict(
        statements=[m.StatementVerdict(id=i, supported=i != 2) for i in (1, 2, 3)]
    )
    # [7] is outside the 3 provided sources, whatever the judge said.
    assert m.citation_score(verdict, statements, source_count=3) == pytest.approx(1 / 3)


def test_abstention() -> None:
    assert m.is_abstention("I don't know based on the provided documents.")
    assert m.is_abstention("  ")
    assert not m.is_abstention("Employees get 25 days [1].")


def test_retrieval_metrics() -> None:
    retrieved = ["d2", "d1", "d1", "d3"]
    assert hit_at_k(retrieved, {"d1"}, 1) == 0.0
    assert hit_at_k(retrieved, {"d1"}, 2) == 1.0
    assert reciprocal_rank(retrieved, {"d1"}) == 0.5
    assert reciprocal_rank(retrieved, {"d9"}) == 0.0


# ---------- evaluator ----------
SOURCES = [Source(n=1, text="Employees get 25 vacation days."), Source(n=2, text="Unrelated.")]


class FakeJudge:
    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, dict[str, str]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][1]["content"]
        metric = next(k for k in self.replies if k in system)
        self.calls.append((metric, dict(request.headers)))
        reply = self.replies[metric]
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": content}}]},
            headers={"X-Gateway-Deployment": "judge-1"},
        )


REPLIES: dict[str, Any] = {
    "grounded in its SOURCES": {"claims": [{"claim": "25 days", "supported": True}]},
    "Rate how well": {"score": 5},
    "For each numbered SOURCE": {
        "sources": [{"n": 1, "relevant": True}, {"n": 2, "relevant": False}]
    },
    "Each STATEMENT": {"statements": [{"id": 1, "supported": True}]},
}


async def _evaluate(sample: EvalSample, replies: dict[str, Any]) -> tuple[Any, FakeJudge]:
    judge = FakeJudge(replies)
    async with GatewayClient("http://gw", "t", transport=httpx.MockTransport(judge.handler)) as gw:
        scores = await AnswerEvaluator(gw).evaluate(sample, CALLER)
    return scores, judge


async def test_evaluator_scores_every_metric_and_avoids_the_answering_provider() -> None:
    sample = EvalSample(
        question="How many vacation days?",
        answer="Employees get 25 days [1].",
        sources=SOURCES,
        answered_by={"provider": "groq", "deployment": "groq-gpt-oss-120b"},
    )
    scores, judge = await _evaluate(sample, REPLIES)
    assert scores.as_dict() == {
        "faithfulness": 1.0,
        "answer_relevance": 1.0,
        "context_precision": 1.0,
        "citation_accuracy": 1.0,
    }
    assert scores.judges == ["judge-1"]
    assert {h["x-gateway-avoid-providers"] for _, h in judge.calls} == {"groq"}


async def test_abstention_skips_relevance_and_uncited_answers_skip_citations() -> None:
    sample = EvalSample(
        question="Who founded the company?",
        answer="I don't know based on the provided documents.",
        sources=SOURCES,
    )
    scores, judge = await _evaluate(sample, {**REPLIES, "grounded in its SOURCES": {"claims": []}})
    assert scores.answer_relevance is None and scores.citation_accuracy is None
    assert scores.faithfulness == 1.0
    assert scores.details["abstained"] is True
    assert sorted(metric for metric, _ in judge.calls) == [
        "For each numbered SOURCE",
        "grounded in its SOURCES",
    ]
    assert all("x-gateway-avoid-providers" not in h for _, h in judge.calls)


async def test_invalid_judge_output_leaves_that_metric_empty() -> None:
    sample = EvalSample(question="q", answer="Employees get 25 days [1].", sources=SOURCES)
    scores, _ = await _evaluate(sample, {**REPLIES, "Rate how well": "not json at all"})
    assert scores.answer_relevance is None
    assert "invalid judge output" in scores.details["answer_relevance"]["error"]
    assert scores.faithfulness == 1.0
