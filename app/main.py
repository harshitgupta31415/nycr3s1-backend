from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.database import check_database, close_database
from app.rollbackready.contracts import ErrorDetail, ErrorEnvelope
from app.rollbackready.errors import RollbackReadyError
from app.rollbackready.service import AnalysisService
from app.routers.analyses import create_analyses_router
from app.routers.health import create_health_router
from app.routers.root import router as root_router

DatabaseCheck = Callable[[], dict[str, Any]]


def create_app(
    database_check: DatabaseCheck = check_database,
    analysis_service: AnalysisService | None = None,
) -> FastAPI:
    service = analysis_service or AnalysisService()

    async def purge_expired_analyses() -> None:
        while True:
            await asyncio.sleep(60)
            service.purge_expired()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        expiry_task = asyncio.create_task(purge_expired_analyses())
        try:
            yield
        finally:
            expiry_task.cancel()
            with suppress(asyncio.CancelledError):
                await expiry_task
            service.purge_expired()
            close_database()

    application = FastAPI(
        title="RollbackReady API",
        description="Verified migration recovery evidence for Prisma PostgreSQL projects.",
        version="0.3.0",
        lifespan=lifespan,
    )

    if settings.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    application.include_router(root_router)
    application.include_router(create_health_router(database_check))
    application.include_router(
        create_analyses_router(service)
    )

    @application.exception_handler(RollbackReadyError)
    async def rollbackready_error_handler(
        _: Request, exc: RollbackReadyError
    ) -> JSONResponse:
        payload = ErrorEnvelope(
            error=ErrorDetail(
                code=exc.code,
                message=exc.message,
                analysis_id=exc.analysis_id,
                details=exc.details,
            )
        )
        headers = None
        if exc.status_code == 429:
            retry_after = exc.details.get("retry_after_seconds", 30)
            headers = {"Retry-After": str(retry_after)}
        return JSONResponse(
            status_code=exc.status_code,
            content=payload.model_dump(mode="json"),
            headers=headers,
        )

    return application


app = create_app()
