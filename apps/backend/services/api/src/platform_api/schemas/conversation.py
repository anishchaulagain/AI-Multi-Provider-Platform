import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    # Restrict answers to these documents; empty means all of the tenant's documents.
    doc_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    doc_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    doc_ids: list[uuid.UUID]
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
