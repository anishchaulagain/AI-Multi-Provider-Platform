from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from llm_client import headers as gateway_headers
from platform_api.api.router import api_router
from platform_api.api.routes import health
from platform_api.core.config import Settings, get_settings
from platform_api.core.resources import lifespan_resources
from platform_core.errors import register_exception_handlers
from platform_core.logging import configure_logging
from platform_core.middleware import REQUEST_ID_HEADER, RequestContextMiddleware

logger = structlog.get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    gateway_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        async with lifespan_resources(settings, gateway_transport=gateway_transport) as resources:
            app.state.resources = resources
            logger.info("startup", environment=settings.environment)
            yield
        logger.info("shutdown")

    app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[
            REQUEST_ID_HEADER,
            "X-RateLimit-Limit",
            "X-RateLimit-Remaining",
            *gateway_headers.META_HEADERS,
        ],
    )
    # Added last so it runs first and request ids exist for every log/error.
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
