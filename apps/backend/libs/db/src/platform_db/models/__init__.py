# Import all models so Base.metadata is complete for Alembic.
from platform_db.models.api_key import ApiKey
from platform_db.models.conversation import Conversation, Message, MessageRole
from platform_db.models.document import Document, DocumentStatus
from platform_db.models.history import ContextCapsule, ConversationMemory, ConversationSummary
from platform_db.models.llm_usage import LlmUsage
from platform_db.models.tenant import Tenant
from platform_db.models.user import User, UserRole

__all__ = [
    "ApiKey",
    "ContextCapsule",
    "Conversation",
    "ConversationMemory",
    "ConversationSummary",
    "Message",
    "MessageRole",
    "Document",
    "DocumentStatus",
    "LlmUsage",
    "Tenant",
    "User",
    "UserRole",
]
