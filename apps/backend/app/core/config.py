from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-insecure-change-me-0123456789abcdef"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_name: str = "AI Platform API"
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = ["http://localhost:3000"]

    # Logging
    log_level: str = "INFO"
    log_json: bool = False

    # Data stores
    database_url: str = "postgresql+asyncpg://platform:platform@localhost:5432/platform"
    database_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://platform:platform@localhost:5672/"
    qdrant_url: str = "http://localhost:6333"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("platform-neo4j")
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "platform"
    s3_secret_key: SecretStr = SecretStr("platform-minio")

    # LLM gateway
    llm_gateway_url: str = "http://localhost:4000"
    llm_gateway_key: SecretStr = SecretStr("sk-local-dev")

    # Auth
    jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 60 * 12
    api_key_prefix: str = "aip_"

    # Rate limiting (token bucket per principal / IP)
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 30

    # Readiness
    readiness_timeout_seconds: float = 2.0

    # Seed
    seed_tenant_name: str = "default"
    seed_admin_email: str = "admin@example.com"
    seed_admin_password: SecretStr = SecretStr("admin12345")

    @model_validator(mode="after")
    def _require_real_secret_outside_local(self) -> "Settings":
        if self.environment in ("staging", "production"):
            secret = self.jwt_secret.get_secret_value()
            if secret == DEV_JWT_SECRET or len(secret) < 32:
                raise ValueError(
                    "JWT_SECRET must be set to a random value of at least 32 bytes "
                    "outside local/test environments"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
