"""Evaluation (Phase 6): LLM-judge answer metrics, retrieval metrics, and loading and
storing evaluations of persisted answers."""

from platform_evals.evaluator import METRICS, AnswerEvaluator, EvalSample, EvalScores
from platform_evals.metrics import Source, is_abstention
from platform_evals.retrieval import first_hit_rank, hit_at_k, reciprocal_rank
from platform_evals.samples import (
    LoadedSample,
    SampleUnavailableError,
    load_sample,
    save_result,
)

__all__ = [
    "METRICS",
    "AnswerEvaluator",
    "EvalSample",
    "EvalScores",
    "LoadedSample",
    "SampleUnavailableError",
    "Source",
    "first_hit_rank",
    "hit_at_k",
    "is_abstention",
    "load_sample",
    "reciprocal_rank",
    "save_result",
]
