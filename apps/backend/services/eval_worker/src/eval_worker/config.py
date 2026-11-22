from functools import lru_cache

from pydantic import SecretStr

from platform_core.config import BaseServiceSettings
from platform_messaging import WorkQueue, eval_answers_queue


class EvalWorkerSettings(BaseServiceSettings):
    app_name: str = "Eval Worker"

    rabbitmq_url: str = "amqp://platform:platform@localhost:5672/"
    # Must match the API's EVAL_EXCHANGE (both declare the topology).
    eval_exchange: str = "eval"
    eval_retry_delays_ms: tuple[int, ...] = (10_000, 60_000, 600_000)
    prefetch: int = 1
    job_timeout_seconds: float = 300.0

    gateway_url: str = "http://localhost:8100"
    gateway_service_token: SecretStr = SecretStr("dev-gateway-token-change-me")
    gateway_timeout_seconds: float = 120.0
    judge_alias: str = "judge"

    # The answer's sources are read back from Qdrant; must match the API's.
    qdrant_url: str = "http://localhost:6333"
    embed_model_name: str = "nomic-embed-text"

    def eval_queue(self) -> WorkQueue:
        return eval_answers_queue(self.eval_exchange, self.eval_retry_delays_ms)


@lru_cache
def get_settings() -> EvalWorkerSettings:
    return EvalWorkerSettings()
