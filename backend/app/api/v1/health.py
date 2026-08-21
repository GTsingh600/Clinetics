"""Liveness and readiness endpoints.

Two distinct checks, because they answer different questions:

* `/health`  — liveness. Is this process up? Never touches dependencies, so an
  orchestrator does not restart the API just because Postgres blipped.
* `/health/ready` — readiness. Can this process actually serve traffic? Probes
  the database, and reports degraded rather than throwing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": settings.project_name,
        "environment": settings.environment,
    }


@router.get("/health/ready")
async def readiness(response: Response, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    checks: dict[str, str] = {}
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"

    ready = all(v == "ok" for v in checks.values())
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready else "degraded", "checks": checks}
