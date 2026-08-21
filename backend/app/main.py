"""FastAPI application factory.

Kept deliberately thin: it wires middleware, mounts the versioned router, and
owns the startup/shutdown lifespan. No business logic, no route handlers.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.csrf import CSRFMiddleware
from app.core.db import engine

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown hooks. Disposing the engine on shutdown returns pooled
    connections cleanly instead of leaving them for the DB to time out."""
    logger.info("Starting %s (env=%s)", settings.project_name, settings.environment)
    yield
    await engine.dispose()
    logger.info("Shutdown complete")


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.project_name,
        version="0.1.0",
        description="AI clinic scheduling: forecast demand, optimize schedules, explain decisions.",
        # Hide interactive docs in production; they leak the full API surface.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=lifespan,
    )

    # Middleware runs in reverse registration order, so CSRF is added first and
    # therefore runs *after* CORS. That ordering matters: a rejected
    # cross-origin request must still carry CORS headers, or the browser reports
    # an opaque network error instead of the 403 the server actually sent.
    app.add_middleware(CSRFMiddleware, allowed_origins=settings.cors_origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        # Required for cookie auth: without it the browser neither sends the
        # session cookie cross-origin nor stores the one we set.
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
