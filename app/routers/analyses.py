from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Request,
    Response,
    UploadFile,
    status,
)

from app.core.clerk_auth import get_clerk_user_id
from app.core.config import settings
from app.rollbackready.contracts import (
    AnalysisSummary,
    EvidenceReport,
    RecoveryPlan,
    TimelineEvent,
    VerificationResult,
)
from app.rollbackready.errors import RollbackReadyError
from app.rollbackready.intake import (
    MAX_ARCHIVE_BYTES,
    load_demo_bundle,
    load_project_bundle,
)
from app.rollbackready.service import AnalysisService

ClerkUserId = Annotated[str, Depends(get_clerk_user_id)]


class _WindowRateLimiter:
    def __init__(self, limit: int, window_seconds: int = 60) -> None:
        self._limit = max(1, limit)
        self._window_seconds = window_seconds
        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> int | None:
        now = time.monotonic()
        with self._lock:
            entries = self._entries[key]
            while entries and entries[0] <= now - self._window_seconds:
                entries.popleft()
            if len(entries) >= self._limit:
                return max(1, int(self._window_seconds - (now - entries[0])))
            entries.append(now)
        return None


def create_analyses_router(service: AnalysisService) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["rollbackready"])
    create_limiter = _WindowRateLimiter(
        settings.rollbackready_create_rate_limit_per_minute
    )

    @router.post(
        "/analyses",
        response_model=AnalysisSummary,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_analysis(
        clerk_user_id: ClerkUserId,
        request: Request,
        project_bundle: Annotated[UploadFile | None, File()] = None,
        candidate_migration: Annotated[str | None, Form()] = None,
        use_demo: Annotated[bool, Form()] = False,
    ) -> AnalysisSummary:
        client = request.client.host if request.client else "unknown"
        retry_after = create_limiter.check(f"{clerk_user_id}:{client}")
        if retry_after is not None:
            raise RollbackReadyError(
                "RATE_LIMITED",
                "Too many analysis uploads were staged. Retry shortly.",
                status_code=429,
                details={"retry_after_seconds": retry_after},
            )
        if use_demo and project_bundle is not None:
            raise RollbackReadyError(
                "AMBIGUOUS_INPUT",
                "use_demo and project_bundle are mutually exclusive.",
            )
        if use_demo:
            bundle = load_demo_bundle()
        else:
            if project_bundle is None or not candidate_migration:
                raise RollbackReadyError(
                    "MISSING_INPUT",
                    "project_bundle and candidate_migration are required unless use_demo is true.",
                )
            if not (project_bundle.filename or "").lower().endswith(".zip"):
                raise RollbackReadyError(
                    "INVALID_ARCHIVE_TYPE",
                    "project_bundle must be a .zip archive.",
                )
            archive = await project_bundle.read(MAX_ARCHIVE_BYTES + 1)
            await project_bundle.close()
            bundle = load_project_bundle(archive, candidate_migration)
        return service.create(bundle, clerk_user_id)

    @router.post("/analyses/{analysis_id}/run", response_model=AnalysisSummary)
    def run_analysis(analysis_id: str, clerk_user_id: ClerkUserId) -> AnalysisSummary:
        return service.run(analysis_id, clerk_user_id)

    @router.get("/analyses/{analysis_id}", response_model=AnalysisSummary)
    def get_analysis(analysis_id: str, clerk_user_id: ClerkUserId) -> AnalysisSummary:
        return service.get(analysis_id, clerk_user_id)

    @router.get(
        "/analyses/{analysis_id}/timeline", response_model=list[TimelineEvent]
    )
    def get_timeline(
        analysis_id: str, clerk_user_id: ClerkUserId
    ) -> list[TimelineEvent]:
        return service.timeline(analysis_id, clerk_user_id)

    @router.post(
        "/analyses/{analysis_id}/plans",
        response_model=RecoveryPlan,
        status_code=status.HTTP_201_CREATED,
    )
    def create_plan(analysis_id: str, clerk_user_id: ClerkUserId) -> RecoveryPlan:
        return service.create_plan(analysis_id, clerk_user_id)

    @router.post(
        "/analyses/{analysis_id}/plans/{plan_id}/verify",
        response_model=VerificationResult,
    )
    def verify_plan(
        analysis_id: str, plan_id: str, clerk_user_id: ClerkUserId
    ) -> VerificationResult:
        return service.verify_plan(analysis_id, plan_id, clerk_user_id)

    @router.get("/analyses/{analysis_id}/report", response_model=EvidenceReport)
    def get_report(analysis_id: str, clerk_user_id: ClerkUserId) -> EvidenceReport:
        return service.report(analysis_id, clerk_user_id)

    @router.delete(
        "/analyses/{analysis_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    def delete_analysis(analysis_id: str, clerk_user_id: ClerkUserId) -> Response:
        service.delete(analysis_id, clerk_user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router
