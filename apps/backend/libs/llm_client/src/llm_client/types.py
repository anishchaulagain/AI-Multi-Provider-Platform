"""OpenAI-compatible wire types shared by the gateway and its clients.

Models allow extra fields so provider-specific parameters pass through untouched.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant", "tool", "developer"]


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow")


class ChatMessage(_Open):
    role: Role
    content: str | list[dict[str, Any]] | None = None

    def text(self) -> str:
        """Plain-text view of the content (multi-part content is concatenated)."""
        if self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        return "".join(str(part.get("text", "")) for part in self.content)


class ChatCompletionRequest(_Open):
    model: str = "auto"
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, ge=1)
    stop: str | list[str] | None = None
    response_format: dict[str, Any] | None = None
    tools: list[dict[str, Any]] | None = None
    n: int | None = Field(default=None, ge=1)


class Usage(_Open):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatChoice(_Open):
    index: int = 0
    message: ChatMessage
    finish_reason: str | None = None


class ChatCompletion(_Open):
    id: str = ""
    object: str = "chat.completion"
    created: int = 0
    model: str = ""
    choices: list[ChatChoice] = []
    usage: Usage | None = None

    def text(self) -> str:
        return self.choices[0].message.text() if self.choices else ""


class ChunkDelta(_Open):
    role: Role | None = None
    content: str | None = None


class ChunkChoice(_Open):
    index: int = 0
    delta: ChunkDelta = ChunkDelta()
    finish_reason: str | None = None


class ChatCompletionChunk(_Open):
    id: str = ""
    object: str = "chat.completion.chunk"
    created: int = 0
    model: str = ""
    choices: list[ChunkChoice] = []
    usage: Usage | None = None


class EmbeddingRequest(_Open):
    model: str = "embed"
    input: str | list[str]


class EmbeddingData(_Open):
    object: str = "embedding"
    index: int = 0
    embedding: list[float]


class EmbeddingResponse(_Open):
    object: str = "list"
    model: str = ""
    data: list[EmbeddingData] = []
    usage: Usage | None = None


class ModelInfo(BaseModel):
    id: str
    object: Literal["model"] = "model"
    kind: Literal["chat", "embedding"]
    description: str = ""
    deployments: list[str] = []
