import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    # Restrict answers to these documents; empty means all of the tenant's documents.
    doc_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    # Answer with this gateway alias instead of the default.
    alias: str | None = Field(default=None, max_length=64)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    doc_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    alias: str | None = Field(default=None, max_length=64)


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    doc_ids: list[uuid.UUID]
    alias: str | None = None
    capsule_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class CitationRead(BaseModel):
    n: int
    chunk_id: str
    doc_id: str
    doc_title: str | None = None
    page: int | None = None
    section: str | None = None
    snippet: str | None = None
    text: str | None = None
    # The cited chunk no longer exists (its document was deleted or re-ingested).
    missing: bool = False


class MessageRead(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    token_count: int
    citations: list[CitationRead]
    usage: dict[str, Any] | None
    created_at: datetime


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    # Attach a context capsule for this request (agent-to-agent handoff).
    context_capsule_id: uuid.UUID | None = None


class CapsuleCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class CapsuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    share_token: str
    token_count: int
    source_model: str | None
    source_conversation_id: uuid.UUID | None
    content: dict[str, Any]
    expires_at: datetime | None
    created_at: datetime


class CapsuleFork(BaseModel):
    # Continue on a different alias/model; default keeps the platform default.
    alias: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=200)
