"""`model: "auto"` -> pick an alias from cheap request heuristics."""

import re

from llm_client.types import ChatCompletionRequest

_HARD_TASK = re.compile(
    r"\b(step[- ]by[- ]step|prove|derive|analy[sz]e|compare|trade-?offs?|architect|refactor|"
    r"debug|optimi[sz]e|reason|plan)\b",
    re.IGNORECASE,
)


def choose_alias(request: ChatCompletionRequest, *, smart_min_chars: int) -> str:
    text = "\n".join(m.text() for m in request.messages)
    last_user = next((m.text() for m in reversed(request.messages) if m.role == "user"), "")
    if (
        len(text) >= smart_min_chars
        or "```" in text
        or request.tools
        or request.response_format
        or _HARD_TASK.search(last_user)
    ):
        return "chat-smart"
    return "chat-fast"
