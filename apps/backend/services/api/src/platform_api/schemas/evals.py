import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Metric = Literal["faithfulness", "answer_relevance", "context_precision", "citation_accuracy"]


class FeedbackCreate(BaseModel):
    rating: Literal[1, -1]
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackRead(BaseModel):
    message_id: uuid.UUID
    rating: int
    comment: str | None
    updated_at: datetime


class Scores(BaseModel):
    faithfulness: float | None = None
    answer_relevance: float | None = None
    context_precision: float | None = None
    citation_accuracy: float | None = None


class MetricStat(BaseModel):
    mean: float | None
    count: int


class TrendPoint(Scores):
    day: date
    count: int


class ModelStats(Scores):
    deployment: str | None
    provider: str | None
    count: int
    feedback_up: int = 0
    feedback_down: int = 0


class EvalSummary(BaseModel):
    days: int
    count: int
    metrics: dict[str, MetricStat]
    abstention_rate: float | None
    trend: list[TrendPoint]
    by_model: list[ModelStats]
    feedback: dict[str, int]


class EvalResultItem(Scores):
    id: uuid.UUID
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    source: str
    run_id: str | None
    question: str | None
    answer: str
    answer_alias: str | None
    answer_deployment: str | None
    judge_deployment: str | None
    feedback: int | None = None  # net thumbs (up - down)
    error: str | None = None
    created_at: datetime


class EvalResultPage(BaseModel):
    items: list[EvalResultItem]
    total: int


class EvalSourceRead(BaseModel):
    n: int
    chunk_id: str
    doc_id: str
    doc_title: str
    text: str
    missing: bool = False


class EvalResultDetail(EvalResultItem):
    sources: list[EvalSourceRead]
    details: dict[str, Any]
    comments: list[FeedbackRead] = Field(default_factory=list)


class EvaluateRequest(BaseModel):
    source: Literal["manual", "offline"] = "manual"
    run_id: str | None = Field(default=None, max_length=64)
