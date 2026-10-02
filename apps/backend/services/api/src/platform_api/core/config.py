from functools import lru_cache

from pydantic import SecretStr, model_validator

from platform_core.config import BaseServiceSettings

DEV_JWT_SECRET = "dev-insecure-change-me-0123456789abcdef"


class Settings(BaseServiceSettings):
    app_name: str = "AI Platform API"
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = ["http://localhost:3000"]

    # Data stores used by later phases
    rabbitmq_url: str = "amqp://platform:platform@localhost:5672/"
    qdrant_url: str = "http://localhost:6333"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("platform-neo4j")
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "platform"
    s3_secret_key: SecretStr = SecretStr("platform-minio")

    # AI Gateway (internal)
    gateway_url: str = "http://localhost:8100"
    gateway_service_token: SecretStr = SecretStr("dev-gateway-token-change-me")
    gateway_timeout_seconds: float = 120.0

    # Auth
    jwt_secret: SecretStr = SecretStr(DEV_JWT_SECRET)
    jwt_algorithm: str = "HS256"
    jwt_expires_minutes: int = 60 * 12
    api_key_prefix: str = "aip_"

    # Rate limiting (token bucket per principal / IP)
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 120
    rate_limit_burst: int = 30

    # Seed
    seed_tenant_name: str = "default"
    seed_admin_email: str = "admin@example.com"
    seed_admin_password: SecretStr = SecretStr("admin12345")

    @model_validator(mode="after")
    def _require_real_secret_outside_local(self) -> "Settings":
        if self.is_deployed:
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
