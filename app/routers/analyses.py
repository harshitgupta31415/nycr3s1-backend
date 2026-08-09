from __future__ import annotations

import json
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse

from app.core.clerk_auth import get_clerk_user_id
from app.core.config import settings
from app.rollbackready.contracts import (
    AIInsight,
    AnalysisSummary,
    EvidenceReport,
    InsightKind,
    RecoveryPlan,
    SchemaChatRequest,
    SchemaChatResponse,
    TimelineEvent,
    VerificationResult,
)
from app.rollbackready.errors import RollbackReadyError
from app.rollbackready.intake import (
    MAX_ARCHIVE_BYTES,
    build_demo_archive,
    load_demo_bundle,
    load_project_bundle,
)
from app.rollbackready.service import TERMINAL_ANALYSIS_STATUSES, AnalysisService

ClerkUserId = Annotated[str, Depends(get_clerk_user_id)]
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=128,
        pattern=r"^[\x21-\x7E]+$",
    ),
]


def create_analyses_router(service: AnalysisService) -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["rollbackready"])

    @router.post(
        "/analyses",
        response_model=AnalysisSummary,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_analysis(
        clerk_user_id: ClerkUserId,
        request: Request,
        response: Response,
        idempotency_key: IdempotencyKey,
        project_bundle: Annotated[UploadFile | None, File()] = None,
        candidate_migration: Annotated[str | None, Form()] = None,
        use_demo: Annotated[bool, Form()] = False,
    ) -> AnalysisSummary:
        operation = "create"
        replayed, stored, key_hash = service.begin_idempotency(
            clerk_user_id, operation, idempotency_key
        )
        if replayed == "COMPLETE" and stored is not None:
            response.headers["Idempotency-Replayed"] = "true"
            return AnalysisSummary.model_validate(stored)
        if replayed == "IN_PROGRESS":
            raise RollbackReadyError(
                "OPERATION_IN_PROGRESS",
                "A request with this idempotency key is already running.",
                status_code=409,
            )
        try:
            client = request.client.host if request.client else "unknown"
            service.check_rate_limit(
                clerk_user_id,
                "create",
                settings.rollbackready_create_rate_limit_per_minute,
                60,
                client,
            )
            if use_demo and project_bundle is not None:
                raise RollbackReadyError(
                    "AMBIGUOUS_INPUT",
                    "use_demo and project_bundle are mutually exclusive.",
                )
            if use_demo:
                archive = build_demo_archive()
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
            result = service.create(bundle, clerk_user_id, archive)
        except Exception:
            service.abort_idempotency(clerk_user_id, operation, key_hash)
            raise
        service.finish_idempotency(
            clerk_user_id,
            operation,
            key_hash,
            result.id,
            result.model_dump(mode="json"),
        )
        response.headers["Idempotency-Replayed"] = "false"
        return result

    @router.post("/analyses/{analysis_id}/run", response_model=AnalysisSummary)
    def run_analysis(
        analysis_id: str,
        clerk_user_id: ClerkUserId,
        idempotency_key: IdempotencyKey,
        response: Response,
    ) -> AnalysisSummary:
        return _idempotent_model(
            service,
            clerk_user_id,
            f"run:{analysis_id}",
            idempotency_key,
            response,
            AnalysisSummary,
            lambda: service.run(analysis_id, clerk_user_id),
        )

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

    @router.get("/analyses/{analysis_id}/events")
    def stream_timeline_events(
        analysis_id: str,
        clerk_user_id: ClerkUserId,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
    ) -> StreamingResponse:
        service.get(analysis_id, clerk_user_id)

        def event_stream():
            cursor = after_sequence
            while True:
                events, analysis_status = service.wait_for_timeline_events(
                    analysis_id,
                    clerk_user_id,
                    cursor,
                )
                if not events:
                    yield ": heartbeat\n\n"
                for event in events:
                    cursor = event.sequence
                    payload = json.dumps(event.model_dump(mode="json"), separators=(",", ":"))
                    yield f"id: {event.sequence}\nevent: timeline\ndata: {payload}\n\n"
                if analysis_status in TERMINAL_ANALYSIS_STATUSES:
                    yield f"event: complete\ndata: {{\"status\":\"{analysis_status}\"}}\n\n"
                    break

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post(
        "/analyses/{analysis_id}/plans",
        response_model=RecoveryPlan,
        status_code=status.HTTP_201_CREATED,
    )
    def create_plan(
        analysis_id: str,
        clerk_user_id: ClerkUserId,
        idempotency_key: IdempotencyKey,
        response: Response,
    ) -> RecoveryPlan:
        return _idempotent_model(
            service,
            clerk_user_id,
            f"plan:{analysis_id}",
            idempotency_key,
            response,
            RecoveryPlan,
            lambda: service.create_plan(analysis_id, clerk_user_id),
            before_invoke=lambda: service.check_rate_limit(
                clerk_user_id,
                "plan",
                settings.rollbackready_plan_rate_limit_per_hour,
                3600,
            ),
        )

    @router.post(
        "/analyses/{analysis_id}/chat",
        response_model=SchemaChatResponse,
    )
    def chat_schema_change(
        analysis_id: str,
        payload: SchemaChatRequest,
        clerk_user_id: ClerkUserId,
        request: Request,
    ) -> SchemaChatResponse:
        client = request.client.host if request.client else "unknown"
        service.check_rate_limit(clerk_user_id, "chat", 20, 60, client)
        return service.chat_schema_change(analysis_id, payload, clerk_user_id)

    @router.post(
        "/analyses/{analysis_id}/insights",
        response_model=AIInsight,
        status_code=status.HTTP_201_CREATED,
    )
    def create_insight(
        analysis_id: str,
        kind: Annotated[InsightKind, Query()],
        clerk_user_id: ClerkUserId,
    ) -> AIInsight:
        return service.create_insight(analysis_id, kind, clerk_user_id)

    @router.post(
        "/analyses/{analysis_id}/plans/{plan_id}/verify",
        response_model=VerificationResult,
    )
    def verify_plan(
        analysis_id: str,
        plan_id: str,
        clerk_user_id: ClerkUserId,
        idempotency_key: IdempotencyKey,
        response: Response,
    ) -> VerificationResult:
        return _idempotent_model(
            service,
            clerk_user_id,
            f"verify:{analysis_id}:{plan_id}",
            idempotency_key,
            response,
            VerificationResult,
            lambda: service.verify_plan(analysis_id, plan_id, clerk_user_id),
            before_invoke=lambda: service.check_rate_limit(
                clerk_user_id,
                "verify",
                settings.rollbackready_verify_rate_limit_per_hour,
                3600,
            ),
        )

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


def _idempotent_model(
    service: AnalysisService,
    owner_clerk_user_id: str,
    operation: str,
    key: str,
    response: Response,
    model_type,
    invoke,
    before_invoke=None,
):
    state, stored, key_hash = service.begin_idempotency(
        owner_clerk_user_id, operation, key
    )
    if state == "COMPLETE" and stored is not None:
        response.headers["Idempotency-Replayed"] = "true"
        return model_type.model_validate(stored)
    if state == "IN_PROGRESS":
        raise RollbackReadyError(
            "OPERATION_IN_PROGRESS",
            "A request with this idempotency key is already running.",
            status_code=409,
        )
    try:
        if before_invoke is not None:
            before_invoke()
        result = invoke()
    except Exception:
        service.abort_idempotency(owner_clerk_user_id, operation, key_hash)
        raise
    analysis_id = getattr(result, "analysis_id", None) or getattr(result, "id", "")
    service.finish_idempotency(
        owner_clerk_user_id,
        operation,
        key_hash,
        analysis_id,
        result.model_dump(mode="json"),
    )
    response.headers["Idempotency-Replayed"] = "false"
    return result
