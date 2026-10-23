"""Document-level summary and keywords via the gateway's `extract` alias.

One call per document (on its opening text) rather than per chunk: on free models
per-chunk extraction is slow and burns quota, and a document summary/keyword set is
what retrieval and the UI actually use. Failures are non-fatal.
"""

import structlog
from pydantic import BaseModel, Field, ValidationError

from llm_client import Caller, ChatMessage, GatewayClient, GatewayError

logger = structlog.get_logger(__name__)

_MAX_INPUT_CHARS = 6000


class DocumentMetadata(BaseModel):
    summary: str = Field(description="2-3 sentence summary of the document")
    keywords: list[str] = Field(description="5-10 short keywords or key phrases")


async def extract_metadata(
    gateway: GatewayClient, caller: Caller, *, title: str, text: str, model: str = "extract"
) -> DocumentMetadata | None:
    prompt = f"Title: {title}\n\nDocument (beginning):\n{text[:_MAX_INPUT_CHARS]}"
    try:
        result = await gateway.judge(
            [ChatMessage(role="user", content=prompt)], DocumentMetadata, caller=caller, model=model
        )
    except (GatewayError, ValidationError, ValueError) as exc:
        logger.warning("metadata_extraction_failed", error=repr(exc)[:300])
        return None
    meta = result.data
    meta.keywords = [k.strip() for k in meta.keywords if k.strip()][:10]
    meta.summary = meta.summary.strip()[:1000]
    return meta
