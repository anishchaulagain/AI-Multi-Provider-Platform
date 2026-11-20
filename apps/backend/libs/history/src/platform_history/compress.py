"""Compression rules applied to past turns before they go back into a prompt.

- citation markup is stripped from assistant turns (the sources aren't replayed)
- long code blocks collapse to a reference: [code: python, 42 lines, msg#1a2b3c4d]
- RAG context is never part of history (messages store only the answer text)
- near-identical turns are deduplicated, keeping the most recent
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace

_CITATION = re.compile(r"\s?[\[【]\d{1,3}(?:\s*[,，、]\s*\d{1,3})*[\]】]")
_CODE_BLOCK = re.compile(r"```([\w+-]*)\n(.*?)```", re.DOTALL)
_WORD = re.compile(r"\w+")


@dataclass(frozen=True)
class HistoryTurn:
    msg_id: str
    role: str  # user | assistant
    content: str


def short_id(msg_id: str) -> str:
    return msg_id.replace("-", "")[-8:]


def strip_citations(text: str) -> str:
    return _CITATION.sub("", text)


def collapse_code(text: str, msg_id: str, *, max_lines: int = 12) -> str:
    def repl(match: re.Match[str]) -> str:
        lang = match.group(1) or "text"
        lines = match.group(2).rstrip("\n").count("\n") + 1
        if lines <= max_lines:
            return match.group(0)
        return f"[code: {lang}, {lines} lines, msg#{short_id(msg_id)}]"

    return _CODE_BLOCK.sub(repl, text)


def compress_turn(turn: HistoryTurn, *, max_code_lines: int = 12) -> HistoryTurn:
    text = collapse_code(turn.content, turn.msg_id, max_lines=max_code_lines)
    if turn.role == "assistant":
        text = strip_citations(text)
    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    return replace(turn, content=text)


def _shingles(text: str) -> set[str]:
    words = _WORD.findall(text.lower())
    if len(words) < 3:
        return {" ".join(words)}
    return {" ".join(words[i : i + 3]) for i in range(len(words) - 2)}


def similarity(a: str, b: str) -> float:
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def dedupe(turns: Sequence[HistoryTurn], *, threshold: float = 0.9) -> list[HistoryTurn]:
    """Drop an exchange (a question plus its answer) when the same question is asked
    again later: the newer exchange supersedes it. Works on whole exchanges so the
    prompt keeps alternating user/assistant turns. Order is preserved."""
    exchanges: list[list[HistoryTurn]] = []
    for turn in turns:
        if turn.role == "user" or not exchanges:
            exchanges.append([turn])
        else:
            exchanges[-1].append(turn)

    kept: list[HistoryTurn] = []
    for index, exchange in enumerate(exchanges):
        question = exchange[0]
        repeated = question.role == "user" and any(
            later[0].role == "user" and similarity(later[0].content, question.content) >= threshold
            for later in exchanges[index + 1 :]
        )
        if not repeated:
            kept.extend(exchange)
    return kept
