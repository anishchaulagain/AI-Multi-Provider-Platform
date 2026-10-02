from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from gateway.api import health_router, router
from gateway.config import GatewaySettings, get_settings
from gateway.resources import Overrides, lifespan_resources
from llm_client import headers as h
from platform_core.errors import register_exception_handlers
from platform_core.logging import configure_logging
from platform_core.middleware import RequestContextMiddleware

logger = structlog.get_logger(__name__)


def create_app(
    settings: GatewaySettings | None = None, overrides: Overrides | None = None
) -> FastAPI:
    settings = settings or get_settings()
    overrides = overrides or Overrides()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with lifespan_resources(settings, overrides) as resources:
            app.state.resources = resources
            routes = resources.service.routes
            logger.info(
                "startup",
                environment=settings.environment,
                aliases=sorted(routes.aliases),
                unconfigured=[d.name for d in routes.deployments.values() if not d.configured],
            )
            yield
        logger.info("shutdown")

    app = FastAPI(
        title=settings.app_name,
        debug=settings.debug,
        lifespan=lifespan,
        description="Internal OpenAI-compatible gateway. Requires the service token and "
        f"an `{h.TENANT_ID}` header.",
    )
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(router)
    return app


app = create_app()
