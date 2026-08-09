from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
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
logger = logging.getLogger("rollbackready.request")


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
        version="0.4.0",
        lifespan=lifespan,
    )

    @application.middleware("http")
    async def request_context(request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request_id = (
            supplied
            if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", supplied)
            else uuid4().hex
        )
        request.state.request_id = request_id
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            json.dumps(
                {
                    "event": "request_complete",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                },
                separators=(",", ":"),
            )
        )
        return response

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
        request: Request, exc: RollbackReadyError
    ) -> JSONResponse:
        payload = ErrorEnvelope(
            error=ErrorDetail(
                code=exc.code,
                message=exc.message,
                analysis_id=exc.analysis_id,
                request_id=getattr(request.state, "request_id", None),
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

    @application.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
        code = {
            401: "AUTHENTICATION_REQUIRED",
            403: "AUTHORIZATION_FAILED",
            503: "AUTHENTICATION_UNAVAILABLE",
        }.get(exc.status_code, "HTTP_ERROR")
        payload = ErrorEnvelope(
            error=ErrorDetail(
                code=code,
                message=str(exc.detail),
                request_id=getattr(request.state, "request_id", None),
            )
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=payload.model_dump(mode="json"),
            headers=exc.headers,
        )

    return application


app = create_app()
