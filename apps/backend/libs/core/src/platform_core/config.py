from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]


class BaseServiceSettings(BaseSettings):
    """Settings shared by every backend service. Each service subclasses this."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "AI Platform"
    environment: Environment = "local"
    debug: bool = False

    # Logging
    log_level: str = "INFO"
    log_json: bool = False

    # Shared data stores
    database_url: str = "postgresql+asyncpg://platform:platform@localhost:5432/platform"
    database_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"

    # Readiness
    readiness_timeout_seconds: float = 2.0

    @property
    def is_deployed(self) -> bool:
        return self.environment in ("staging", "production")
