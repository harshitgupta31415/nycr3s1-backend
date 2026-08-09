from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
DatabaseCheck = Callable[[], dict[str, Any]]


def create_health_router(database_check: DatabaseCheck) -> APIRouter:
    router = APIRouter(tags=["health"])

    @router.get("/health")
    async def service_health() -> dict[str, str]:
        """Liveness check; deliberately does not depend on the database."""
        return {"status": "healthy", "service": "nycr3s1-backend"}

    def database_status():
        try:
            return database_check()
        except Exception:
            logger.exception("Database health check failed")
            return JSONResponse(
                status_code=503,
                content={
                    "status": "unhealthy",
                    "database": {"connected": False},
                },
            )

    @router.get("/health/ready")
    def database_readiness():
        """Readiness check for platforms that route only to DB-ready instances."""
        database = database_status()
        if isinstance(database, JSONResponse):
            return database
        return {"status": "ready", "database": {"connected": True}}

    @router.get("/health/database")
    def database_health():
        """Detailed database check retained for hosted-foundation verification."""
        database = database_status()
        if isinstance(database, JSONResponse):
            return database
        return {"status": "healthy", "database": database}

    return router
