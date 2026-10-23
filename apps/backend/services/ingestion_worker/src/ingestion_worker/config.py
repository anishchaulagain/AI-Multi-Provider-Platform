from functools import lru_cache

from pydantic import SecretStr

from platform_core.config import BaseServiceSettings
from platform_messaging import WorkQueue, ingest_documents_queue


class WorkerSettings(BaseServiceSettings):
    app_name: str = "Ingestion Worker"

    rabbitmq_url: str = "amqp://platform:platform@localhost:5672/"
    # Must match between API and worker (they both declare the topology).
    ingest_exchange: str = "ingest"
    ingest_retry_delays_ms: tuple[int, ...] = (10_000, 60_000, 600_000)
    prefetch: int = 1
    job_timeout_seconds: float = 900.0

    gateway_url: str = "http://localhost:8100"
    gateway_service_token: SecretStr = SecretStr("dev-gateway-token-change-me")
    gateway_timeout_seconds: float = 120.0

    qdrant_url: str = "http://localhost:6333"

    # Knowledge graph. GRAPH_EXTRACTION=false skips the graphing stage entirely.
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = SecretStr("platform-neo4j")
    graph_extraction: bool = True
    graph_alias: str = "extract"
    graph_batch_chars: int = 6000
    graph_max_chunks: int = 300

    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "platform"
    s3_secret_key: SecretStr = SecretStr("platform-minio")
    s3_bucket: str = "documents"

    # Pipeline. EMBED_MODEL_NAME names the Qdrant collection and must match the API's;
    # change it (and re-ingest) when switching the embed alias to another model.
    embed_model_name: str = "nomic-embed-text"
    embed_alias: str = "embed"
    embed_batch_size: int = 32
    embed_document_prefix: str = "search_document: "
    chunk_size: int = 512
    chunk_overlap: int = 64
    extract_metadata: bool = True
    extract_alias: str = "extract"
    max_chunks: int = 5000

    def ingest_queue(self) -> WorkQueue:
        return ingest_documents_queue(self.ingest_exchange, self.ingest_retry_delays_ms)


@lru_cache
def get_settings() -> WorkerSettings:
    return WorkerSettings()
