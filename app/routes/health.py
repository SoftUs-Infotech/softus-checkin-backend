"""Liveness and readiness.

``/health`` answers without touching anything, so it stays honest when the
database is down. ``/health/ready`` is the one an orchestrator should gate on.
"""

from fastapi import APIRouter, Response, status

from app.core.config import settings
from app.core.datetime import org_today, utc_now
from app.db import mongo
from app.utils.response import success

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict:
    return success(
        {
            "status": "ok",
            "app": settings.APP_NAME,
            "timestamp": utc_now(),
            "timezone": settings.ORG_TIMEZONE,
            "org_date": org_today(),
        },
        message="Service is running.",
    )


@router.get("/health/ready", summary="Readiness probe")
async def readiness(response: Response) -> dict:
    database_ok = await mongo.ping()
    if not database_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return success(
        {"status": "ready" if database_ok else "degraded", "database": database_ok},
        message="All dependencies reachable." if database_ok else "Database unreachable.",
    )
