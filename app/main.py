from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import check_database, close_database
from app.routers.health import create_health_router
from app.routers.root import router as root_router

DatabaseCheck = Callable[[], dict[str, Any]]


def create_app(database_check: DatabaseCheck = check_database) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        close_database()

    application = FastAPI(
        title="NYCR3S1 Backend",
        description="Python FastAPI foundation for the NYCR3S1 project.",
        version="0.2.0",
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

    return application


app = create_app()
