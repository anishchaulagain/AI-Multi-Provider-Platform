"""Judge prompts, their JSON schemas, and how verdicts turn into 0..1 scores.

Every metric is one `judge` call (temperature 0, validated JSON). Scores are None when
a metric doesn't apply: no sources retrieved, no cited sentences, or the answer is the
"I don't know" abstention (relevance would just measure the abstention).
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, Field

from platform_rag.answer import NO_ANSWER, parse_citations

# Long sources are cut for the judge; the start of a chunk carries its gist.
_SOURCE_CHARS = 2000
_SENTENCE = re.compile(r"(?<=[.!?。])\s+|\n+")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")
# "...25 days. [1]" -> "...25 days [1]." so the citation stays with its sentence.
_TRAILING_CITATIONS = re.compile(r"([.!?。])[ \t]*((?:[\[【]\d[\d,，、\s]*[\]】])+)")


@dataclass(frozen=True)
class Source:
    n: int
    text: str
    chunk_id: str = ""
    doc_id: str = ""
    doc_title: str = ""


def render_sources(sources: Sequence[Source]) -> str:
    return "\n\n".join(f"[{s.n}] {s.text[:_SOURCE_CHARS]}" for s in sources) or "(none)"


def is_abstention(answer: str) -> bool:
    normalized = " ".join(answer.split()).lower().strip(" .\"'")
    return not normalized or NO_ANSWER.lower().rstrip(".") in normalized


# ---------- faithfulness ----------
class Claim(BaseModel):
    claim: str
    supported: bool


class FaithfulnessVerdict(BaseModel):
    claims: list[Claim] = Field(default_factory=list)


FAITHFULNESS_PROMPT = """You check whether an ANSWER is grounded in its SOURCES.
1. Split the answer into short, atomic factual claims. Ignore citation markers like [1], \
greetings, and statements that something is unknown.
2. For each claim, set supported=true only if the SOURCES state or directly imply it.
Use the sources only, never your own knowledge."""


def faithfulness_score(verdict: FaithfulnessVerdict) -> float:
    if not verdict.claims:
        return 1.0  # nothing asserted, nothing unsupported
    return sum(c.supported for c in verdict.claims) / len(verdict.claims)


# ---------- answer relevance ----------
class RelevanceVerdict(BaseModel):
    score: int = Field(ge=1, le=5)
    reason: str = ""


RELEVANCE_PROMPT = """Rate how well the ANSWER addresses the QUESTION, from 1 to 5:
5 = answers exactly what was asked, completely and concisely
4 = answers it, with minor gaps or extra material
3 = partly answers it
2 = mostly off-topic or evasive
1 = does not answer it at all
Judge relevance only, not correctness."""


def relevance_score(verdict: RelevanceVerdict) -> float:
    return (verdict.score - 1) / 4


# ---------- context precision ----------
class SourceRelevance(BaseModel):
    n: int
    relevant: bool


class ContextVerdict(BaseModel):
    sources: list[SourceRelevance] = Field(default_factory=list)


CONTEXT_PROMPT = """For each numbered SOURCE, decide whether it contains information \
useful for answering the QUESTION (relevant=true) or not (relevant=false). \
Return one entry per source number."""


def context_precision_score(verdict: ContextVerdict, count: int) -> float:
    """Average precision over the ranked sources: relevant ones near the top score
    higher. 0 when nothing retrieved is relevant."""
    relevant = {v.n for v in verdict.sources if v.relevant}
    hits = 0
    total = 0.0
    for rank in range(1, count + 1):
        if rank in relevant:
            hits += 1
            total += hits / rank
    return total / hits if hits else 0.0


# ---------- citation accuracy ----------
@dataclass(frozen=True)
class CitedStatement:
    id: int
    text: str
    sources: list[int]


class StatementVerdict(BaseModel):
    id: int
    supported: bool


class CitationVerdict(BaseModel):
    statements: list[StatementVerdict] = Field(default_factory=list)


CITATION_PROMPT = """Each STATEMENT from an answer cites source numbers. For each one, \
set supported=true only if the cited sources, taken together, support the statement. \
A statement citing the wrong source is unsupported even if another source would support it."""


def cited_statements(answer: str) -> list[CitedStatement]:
    """Sentences (or list items) that carry [n] citations, with the numbers they cite."""
    out: list[CitedStatement] = []
    for part in _SENTENCE.split(_TRAILING_CITATIONS.sub(r" \2\1", answer)):
        text = _BULLET.sub("", part).strip()
        numbers = parse_citations(text)
        if text and numbers:
            out.append(CitedStatement(id=len(out) + 1, text=text, sources=numbers))
    return out


def render_statements(statements: Sequence[CitedStatement]) -> str:
    return "\n".join(
        f"{s.id}. (cites {', '.join(f'[{n}]' for n in s.sources)}) {s.text}" for s in statements
    )


def citation_score(
    verdict: CitationVerdict, statements: Sequence[CitedStatement], source_count: int
) -> float:
    """Share of cited statements the judge found supported. Statements citing a number
    outside the provided sources count as unsupported whatever the judge said."""
    supported = {v.id for v in verdict.statements if v.supported}
    ok = sum(
        1
        for s in statements
        if s.id in supported and all(1 <= n <= source_count for n in s.sources)
    )
    return ok / len(statements)
