"""Pluggable request guardrails, applied before any provider sees the prompt."""

import re
from dataclasses import dataclass, field
from typing import Literal, Protocol

from llm_client.types import ChatCompletionRequest, ChatMessage


@dataclass
class GuardrailOutcome:
    request: ChatCompletionRequest
    flags: list[str] = field(default_factory=list)
    blocked_reason: str | None = None


class Guardrail(Protocol):
    name: str

    def apply(self, outcome: GuardrailOutcome) -> GuardrailOutcome: ...


_INJECTION_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts|rules)",
    r"disregard (the |your )?(system|previous) (prompt|instructions)",
    r"(reveal|print|show|repeat) (your|the) (system prompt|hidden instructions)",
    r"you are now (in )?(dan|developer mode|jailbreak)",
    r"pretend (that )?you (have no|are free of) (rules|restrictions)",
]


class PromptInjectionGuard:
    """Heuristic detector for common injection phrasings in user messages."""

    name = "prompt_injection"

    def __init__(self, mode: Literal["flag", "block"]) -> None:
        self._mode = mode
        self._pattern = re.compile("|".join(_INJECTION_PATTERNS), re.IGNORECASE)

    def apply(self, outcome: GuardrailOutcome) -> GuardrailOutcome:
        hit = any(
            m.role == "user" and self._pattern.search(m.text()) for m in outcome.request.messages
        )
        if hit:
            outcome.flags.append(self.name)
            if self._mode == "block":
                outcome.blocked_reason = "Request blocked by prompt-injection guardrail"
        return outcome


_PII_PATTERNS = {
    "EMAIL": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "CARD": re.compile(r"\b(?:\d[ -]?){13,19}\b"),
    "PHONE": re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d\b"),
}


class PIIRedactor:
    """Replaces emails, card numbers and phone numbers in user messages."""

    name = "pii_redaction"

    def apply(self, outcome: GuardrailOutcome) -> GuardrailOutcome:
        changed = False
        messages: list[ChatMessage] = []
        for message in outcome.request.messages:
            if message.role == "user" and isinstance(message.content, str):
                text = message.content
                for label, pattern in _PII_PATTERNS.items():
                    text = pattern.sub(f"[{label}]", text)
                if text != message.content:
                    changed = True
                    message = message.model_copy(update={"content": text})
            messages.append(message)
        if changed:
            outcome.flags.append(self.name)
            outcome.request = outcome.request.model_copy(update={"messages": messages})
        return outcome


def run_guardrails(request: ChatCompletionRequest, guardrails: list[Guardrail]) -> GuardrailOutcome:
    outcome = GuardrailOutcome(request=request)
    for guardrail in guardrails:
        outcome = guardrail.apply(outcome)
        if outcome.blocked_reason:
            break
    return outcome
