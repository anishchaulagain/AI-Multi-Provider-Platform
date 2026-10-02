import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from platform_db.models import UserRole


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    email: str
    full_name: str | None
    role: UserRole
    created_at: datetime
