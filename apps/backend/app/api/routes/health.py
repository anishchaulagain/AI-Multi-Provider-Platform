import asyncio
from collections.abc import Awaitable, Callable

import aio_pika
import structlog
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.api.deps import ResourcesDep
from app.core.resources import Resources
from app.schemas.health import HealthResponse, ReadinessResponse

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz(resources: ResourcesDep) -> HealthResponse:
    """Liveness: the process is up. Never checks dependencies."""
    settings = resources.settings
    return HealthResponse(service=settings.app_name, environment=settings.environment)


async def _check_database(resources: Resources) -> None:
    async with resources.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _check_redis(resources: Resources) -> None:
    await resources.redis.ping()


async def _check_rabbitmq(resources: Resources) -> None:
    connection = await aio_pika.connect(resources.settings.rabbitmq_url)
    await connection.close()


_CHECKS: dict[str, Callable[[Resources], Awaitable[None]]] = {
    "database": _check_database,
    "redis": _check_redis,
    "rabbitmq": _check_rabbitmq,
}


async def _run_check(name: str, resources: Resources) -> tuple[str, str]:
    try:
        await asyncio.wait_for(
            _CHECKS[name](resources), timeout=resources.settings.readiness_timeout_seconds
        )
        return name, "ok"
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        logger.warning("readiness_check_failed", check=name, error=repr(exc))
        return name, "error"


@router.get(
    "/readyz",
    response_model=ReadinessResponse,
    responses={503: {"model": ReadinessResponse}},
)
async def readyz(resources: ResourcesDep, response: Response) -> ReadinessResponse:
    """Readiness: all hard dependencies are reachable."""
    results = await asyncio.gather(*(_run_check(name, resources) for name in _CHECKS))
    checks = dict(results)
    ready = all(v == "ok" for v in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadinessResponse(status="ready" if ready else "not_ready", checks=checks)
