import aio_pika
import httpx
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from platform_api.api.deps import ResourcesDep
from platform_core.health import HealthResponse, ReadinessResponse, run_readiness_checks

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz(resources: ResourcesDep) -> HealthResponse:
    """Liveness: the process is up. Never checks dependencies."""
    settings = resources.settings
    return HealthResponse(service=settings.app_name, environment=settings.environment)


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
async def readyz(resources: ResourcesDep, response: Response) -> ReadinessResponse:
    """Readiness: hard dependencies are reachable. The gateway is reported but optional,
    so an LLM outage doesn't take the whole API out of rotation."""

    async def database() -> None:
        async with resources.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    async def redis() -> None:
        await resources.redis.ping()

    async def rabbitmq() -> None:
        connection = await aio_pika.connect(resources.settings.rabbitmq_url)
        await connection.close()

    async def gateway() -> None:
        async with httpx.AsyncClient(timeout=2.0) as client:
            (await client.get(f"{resources.settings.gateway_url}/healthz")).raise_for_status()

    result = await run_readiness_checks(
        {"database": database, "redis": redis, "rabbitmq": rabbitmq},
        optional={"gateway": gateway},
        timeout=resources.settings.readiness_timeout_seconds,
    )
    if result.status != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
