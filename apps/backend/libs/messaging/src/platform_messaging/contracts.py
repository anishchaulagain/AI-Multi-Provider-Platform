"""Message bodies exchanged between services. Bump `version` on breaking changes."""

import uuid
from typing import Literal

from pydantic import BaseModel


class IngestDocumentJob(BaseModel):
    version: Literal[1] = 1
    doc_id: uuid.UUID
    tenant_id: uuid.UUID
    storage_key: str
    filename: str
    content_type: str
    content_hash: str
