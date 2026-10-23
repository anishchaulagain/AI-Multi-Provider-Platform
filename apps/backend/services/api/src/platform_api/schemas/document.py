import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DocumentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    content_hash: str
    status: str
    error: str | None
    attempts: int
    title: str | None
    page_count: int | None
    chunk_count: int
    details: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    ingested_at: datetime | None


class DocumentList(BaseModel):
    items: list[DocumentRead]
    total: int


class ChunkRead(BaseModel):
    id: str
    chunk_index: int
    text: str
    page: int | None
    section: str | None


class ChunkPage(BaseModel):
    items: list[ChunkRead]
    offset: int
    limit: int
    total: int
