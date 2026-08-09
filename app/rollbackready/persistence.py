from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.database import get_database_engine
from app.models.rollbackready import (
    AnalysisRecord,
    FindingRecord,
    IdempotencyRecord,
    LegacyQueryResultRecord,
    RateLimitRecord,
    RecoveryPlanRecord,
    SimulationRunRecord,
    TimelineEventRecord,
    VerificationResultRecord,
)
from app.rollbackready.contracts import (
    AnalysisSummary,
    ArtifactManifest,
    EvidenceDimension,
    EvidenceReport,
    LegacyQueryResult,
    RecoveryPlan,
    RiskFinding,
    SimulationRun,
    StatementExecution,
    TimelineEvent,
    VerificationResult,
)
from app.rollbackready.errors import RollbackReadyError

_UNFINISHED_STATUSES = {
    "STAGED",
    "ANALYZING",
    "SIMULATING",
    "PLANNING",
    "VERIFYING_PLAN",
}


@dataclass(frozen=True, slots=True)
class ArtifactReference:
    object_name: str
    generation: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IdempotencyDecision:
    state: str
    response: dict | None = None


class EvidenceRepository(Protocol):
    def create(
        self,
        report: EvidenceReport,
        owner_clerk_user_id: str,
        artifact: ArtifactReference | None = None,
    ) -> bool: ...

    def update(
        self,
        report: EvidenceReport,
        owner_clerk_user_id: str,
        operation_token: str | None = None,
    ) -> bool: ...

    def get(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> EvidenceReport | None: ...

    def delete(self, analysis_id: str, owner_clerk_user_id: str) -> None: ...

    def delete_expired(self, expired_before: datetime) -> None: ...

    def get_artifact(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> ArtifactReference | None: ...

    def claim_operation(
        self,
        analysis_id: str,
        owner_clerk_user_id: str,
        operation: str,
        token: str,
        started_at: datetime,
    ) -> bool: ...

    def mark_artifact_deleted(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> None: ...

    def begin_idempotency(
        self, owner_clerk_user_id: str, operation: str, key_hash: str
    ) -> IdempotencyDecision: ...

    def finish_idempotency(
        self,
        owner_clerk_user_id: str,
        operation: str,
        key_hash: str,
        analysis_id: str,
        response: dict,
    ) -> None: ...

    def abort_idempotency(
        self, owner_clerk_user_id: str, operation: str, key_hash: str
    ) -> None: ...

    def consume_rate_limit(
        self,
        scope_hash: str,
        bucket: str,
        limit: int,
        window_seconds: int,
    ) -> int | None: ...


class NullEvidenceRepository:
    authoritative = False

    def create(
        self,
        report: EvidenceReport,
        owner_clerk_user_id: str,
        artifact: ArtifactReference | None = None,
    ) -> bool:
        return True

    def update(
        self,
        report: EvidenceReport,
        owner_clerk_user_id: str,
        operation_token: str | None = None,
    ) -> bool:
        return True

    def save(self, report: EvidenceReport, owner_clerk_user_id: str) -> None:
        return None

    def get(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> EvidenceReport | None:
        return None

    def delete(self, analysis_id: str, owner_clerk_user_id: str) -> None:
        return None

    def delete_expired(self, expired_before: datetime) -> None:
        return None

    def get_artifact(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> ArtifactReference | None:
        return None

    def claim_operation(
        self,
        analysis_id: str,
        owner_clerk_user_id: str,
        operation: str,
        token: str,
        started_at: datetime,
    ) -> bool:
        return True

    def mark_artifact_deleted(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> None:
        return None

    def begin_idempotency(
        self, owner_clerk_user_id: str, operation: str, key_hash: str
    ) -> IdempotencyDecision:
        return IdempotencyDecision("NEW")

    def finish_idempotency(
        self,
        owner_clerk_user_id: str,
        operation: str,
        key_hash: str,
        analysis_id: str,
        response: dict,
    ) -> None:
        return None

    def abort_idempotency(
        self, owner_clerk_user_id: str, operation: str, key_hash: str
    ) -> None:
        return None

    def consume_rate_limit(
        self,
        scope_hash: str,
        bucket: str,
        limit: int,
        window_seconds: int,
    ) -> int | None:
        return None


class SqlAlchemyEvidenceRepository:
    """Replace sanitized evidence atomically; raw artifacts never enter this layer."""

    authoritative = True

    def __init__(self, engine_factory: Callable[[], Engine] = get_database_engine) -> None:
        self._engine_factory = engine_factory

    def save(
        self,
        report: EvidenceReport,
        owner_clerk_user_id: str,
        *,
        create_if_missing: bool = True,
        operation_token: str | None = None,
        artifact: ArtifactReference | None = None,
    ) -> bool:
        summary = report.analysis
        with Session(self._engine_factory()) as session, session.begin():
            if create_if_missing:
                lock_id = int.from_bytes(
                    hashlib.sha256(owner_clerk_user_id.encode()).digest()[:8],
                    byteorder="big",
                    signed=True,
                )
                session.execute(select(func.pg_advisory_xact_lock(lock_id)))
            existing = session.scalar(
                select(AnalysisRecord)
                .where(
                    AnalysisRecord.id == summary.id,
                    AnalysisRecord.owner_clerk_user_id == owner_clerk_user_id,
                )
                .with_for_update()
            )
            if existing is None and not create_if_missing:
                return False
            if existing is None and create_if_missing:
                unfinished = session.scalar(
                    select(func.count())
                    .select_from(AnalysisRecord)
                    .where(
                        AnalysisRecord.owner_clerk_user_id
                        == owner_clerk_user_id,
                        AnalysisRecord.status.in_(_UNFINISHED_STATUSES),
                    )
                )
                if (unfinished or 0) >= max(
                    1, settings.rollbackready_max_unfinished_per_user
                ):
                    raise RollbackReadyError(
                        "ANALYSIS_CAPACITY_REACHED",
                        "This account has reached its unfinished-analysis limit.",
                        status_code=429,
                        details={
                            "limit": settings.rollbackready_max_unfinished_per_user,
                        },
                    )
            if (
                existing is not None
                and operation_token is not None
                and existing.active_operation_token != operation_token
            ):
                return False
            analysis = existing or AnalysisRecord(id=summary.id)
            analysis.owner_clerk_user_id = owner_clerk_user_id
            analysis.status = summary.status
            analysis.evidence_level = summary.evidence_level
            analysis.verdict = summary.verdict
            analysis.input_hash = summary.manifest.archive_sha256
            analysis.provider = summary.provider
            analysis.candidate_migration = summary.candidate_migration
            analysis.manifest = summary.manifest.model_dump(mode="json")
            analysis.evidence = [item.model_dump(mode="json") for item in summary.evidence]
            analysis.limitations = summary.limitations
            analysis.created_at = summary.created_at
            analysis.updated_at = summary.updated_at
            analysis.expires_at = summary.expires_at
            analysis.row_version = (analysis.row_version or 0) + 1
            if artifact is not None:
                analysis.artifact_object_name = artifact.object_name
                analysis.artifact_generation = artifact.generation
                analysis.artifact_state = "AVAILABLE"
                analysis.artifact_expires_at = artifact.expires_at
            if existing is not None:
                analysis.findings.clear()
                analysis.simulation_runs.clear()
                analysis.legacy_results.clear()
                analysis.timeline_events.clear()
                analysis.recovery_plans.clear()
                session.flush()
            if operation_token is not None:
                analysis.active_operation = None
                analysis.active_operation_token = None
                analysis.operation_started_at = None
            analysis.findings = [
                FindingRecord(
                    id=item.id,
                    severity=item.severity,
                    category=item.category,
                    statement_index=item.statement_index,
                    statement_shape=item.statement_shape,
                    affected_object=item.affected_object,
                    reason=item.reason,
                    evidence_source=item.evidence_source,
                    remediation_hint=item.remediation_hint,
                    confirmed=item.confirmed,
                )
                for item in summary.findings
            ]
            analysis.simulation_runs = [
                SimulationRunRecord(
                    id=item.id,
                    run_type=item.run_type,
                    boundary=item.boundary,
                    status=item.status,
                    duration_ms=item.duration_ms,
                    statements=[
                        statement.model_dump(mode="json")
                        for statement in item.statements
                    ],
                    snapshot=item.snapshot.model_dump(mode="json") if item.snapshot else None,
                    recovery=item.recovery.model_dump(mode="json") if item.recovery else None,
                    sanitized_error=item.sanitized_error,
                )
                for item in summary.simulation_runs
            ]
            analysis.legacy_results = [
                LegacyQueryResultRecord(
                    query_name=item.name,
                    query_hash=item.query_hash,
                    status=item.status,
                    duration_ms=item.duration_ms,
                    affected_rows=item.affected_rows,
                    sanitized_error=item.sanitized_error,
                )
                for item in summary.legacy_query_results
            ]
            analysis.timeline_events = [
                TimelineEventRecord(
                    sequence=item.sequence,
                    occurred_at=item.occurred_at,
                    event_type=item.event_type,
                    status=item.status,
                    message=item.message,
                    run_id=item.run_id,
                    statement_index=item.statement_index,
                )
                for item in report.timeline
            ]
            verifications = {item.plan_id: item for item in summary.verification_results}
            analysis.recovery_plans = []
            for item in summary.plans:
                plan_record = RecoveryPlanRecord(
                    id=item.id,
                    state=item.state,
                    provider=item.provider,
                    model=item.model,
                    prompt_template_version=item.prompt_template_version,
                    generated_at=item.generated_at,
                    plan=item.model_dump(mode="json"),
                )
                verification = verifications.get(item.id)
                if verification:
                    plan_record.verification = VerificationResultRecord(
                        id=verification.id,
                        status=verification.status,
                        verdict=verification.verdict,
                        dimensions=[
                            dimension.model_dump(mode="json")
                            for dimension in verification.dimensions
                        ],
                        completed_at=verification.completed_at,
                        sanitized_error=verification.sanitized_error,
                    )
                analysis.recovery_plans.append(plan_record)
            session.add(analysis)
        return True

    def create(
        self,
        report: EvidenceReport,
        owner_clerk_user_id: str,
        artifact: ArtifactReference | None = None,
    ) -> bool:
        return self.save(
            report,
            owner_clerk_user_id,
            create_if_missing=True,
            artifact=artifact,
        )

    def update(
        self,
        report: EvidenceReport,
        owner_clerk_user_id: str,
        operation_token: str | None = None,
    ) -> bool:
        return self.save(
            report,
            owner_clerk_user_id,
            create_if_missing=False,
            operation_token=operation_token,
        )

    def get(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> EvidenceReport | None:
        with Session(self._engine_factory()) as session:
            record = session.scalar(
                select(AnalysisRecord)
                .where(
                    AnalysisRecord.id == analysis_id,
                    AnalysisRecord.owner_clerk_user_id == owner_clerk_user_id,
                )
                .options(
                    selectinload(AnalysisRecord.findings),
                    selectinload(AnalysisRecord.simulation_runs),
                    selectinload(AnalysisRecord.legacy_results),
                    selectinload(AnalysisRecord.timeline_events),
                    selectinload(AnalysisRecord.recovery_plans).selectinload(
                        RecoveryPlanRecord.verification
                    )
                )
            )
            if record is None:
                return None
            plans = [RecoveryPlan.model_validate(item.plan) for item in record.recovery_plans]
            verifications = [
                VerificationResult(
                    id=item.verification.id,
                    plan_id=item.id,
                    status=item.verification.status,
                    verdict=item.verification.verdict,
                    dimensions=[
                        EvidenceDimension.model_validate(dimension)
                        for dimension in item.verification.dimensions
                    ],
                    completed_at=item.verification.completed_at,
                    sanitized_error=item.verification.sanitized_error,
                )
                for item in record.recovery_plans
                if item.verification is not None
            ]
            summary = AnalysisSummary(
                id=record.id,
                status=record.status,
                evidence_level=record.evidence_level,
                verdict=record.verdict,
                provider=record.provider,
                candidate_migration=record.candidate_migration,
                created_at=record.created_at,
                updated_at=record.updated_at,
                expires_at=record.expires_at,
                raw_artifacts_available=record.artifact_state == "AVAILABLE",
                manifest=ArtifactManifest.model_validate(record.manifest),
                findings=[
                    RiskFinding(
                        id=item.id,
                        severity=item.severity,
                        category=item.category,
                        statement_index=item.statement_index,
                        statement_shape=item.statement_shape,
                        affected_object=item.affected_object,
                        reason=item.reason,
                        evidence_source=item.evidence_source,
                        remediation_hint=item.remediation_hint,
                        confirmed=item.confirmed,
                    )
                    for item in record.findings
                ],
                evidence=[
                    EvidenceDimension.model_validate(item) for item in record.evidence
                ],
                simulation_runs=[
                    SimulationRun(
                        id=item.id,
                        run_type=item.run_type,
                        boundary=item.boundary,
                        status=item.status,
                        duration_ms=item.duration_ms,
                        statements=[
                            StatementExecution.model_validate(statement)
                            for statement in item.statements
                        ],
                        snapshot=item.snapshot,
                        recovery=item.recovery,
                        sanitized_error=item.sanitized_error,
                    )
                    for item in record.simulation_runs
                ],
                legacy_query_results=[
                    LegacyQueryResult(
                        name=item.query_name,
                        query_hash=item.query_hash,
                        status=item.status,
                        duration_ms=item.duration_ms,
                        affected_rows=item.affected_rows,
                        sanitized_error=item.sanitized_error,
                    )
                    for item in record.legacy_results
                ],
                plans=plans,
                verification_results=verifications,
                limitations=record.limitations,
            )
            timeline = [
                TimelineEvent(
                    sequence=item.sequence,
                    occurred_at=item.occurred_at,
                    event_type=item.event_type,
                    status=item.status,
                    message=item.message,
                    run_id=item.run_id,
                    statement_index=item.statement_index,
                )
                for item in sorted(record.timeline_events, key=lambda event: event.sequence)
            ]
            return EvidenceReport(
                analysis=summary,
                timeline=timeline,
                generated_at=record.updated_at,
            )

    def get_artifact(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> ArtifactReference | None:
        with Session(self._engine_factory()) as session:
            record = session.scalar(
                select(AnalysisRecord).where(
                    AnalysisRecord.id == analysis_id,
                    AnalysisRecord.owner_clerk_user_id == owner_clerk_user_id,
                )
            )
            if (
                record is None
                or record.artifact_state != "AVAILABLE"
                or not record.artifact_object_name
                or not record.artifact_generation
                or record.artifact_expires_at is None
            ):
                return None
            return ArtifactReference(
                object_name=record.artifact_object_name,
                generation=record.artifact_generation,
                expires_at=record.artifact_expires_at,
            )

    def begin_idempotency(
        self, owner_clerk_user_id: str, operation: str, key_hash: str
    ) -> IdempotencyDecision:
        now = datetime.now(UTC)
        with Session(self._engine_factory()) as session, session.begin():
            inserted = session.scalar(
                pg_insert(IdempotencyRecord)
                .values(
                    owner_clerk_user_id=owner_clerk_user_id,
                    operation=operation,
                    key_hash=key_hash,
                    state="IN_PROGRESS",
                    created_at=now,
                    expires_at=now + timedelta(hours=24),
                )
                .on_conflict_do_nothing(
                    constraint="uq_rr_idempotency_owner_operation_key"
                )
                .returning(IdempotencyRecord.id)
            )
            if inserted is not None:
                return IdempotencyDecision("NEW")
            record = session.scalar(
                select(IdempotencyRecord)
                .where(
                    IdempotencyRecord.owner_clerk_user_id == owner_clerk_user_id,
                    IdempotencyRecord.operation == operation,
                    IdempotencyRecord.key_hash == key_hash,
                )
                .with_for_update()
            )
            if record is None:
                raise RuntimeError("idempotency record disappeared during claim")
            if record.expires_at <= now:
                record.state = "IN_PROGRESS"
                record.analysis_id = None
                record.response = None
                record.created_at = now
                record.expires_at = now + timedelta(hours=24)
                return IdempotencyDecision("NEW")
            if record.state == "COMPLETE" and record.response is not None:
                return IdempotencyDecision("COMPLETE", dict(record.response))
            return IdempotencyDecision("IN_PROGRESS")

    def finish_idempotency(
        self,
        owner_clerk_user_id: str,
        operation: str,
        key_hash: str,
        analysis_id: str,
        response: dict,
    ) -> None:
        with Session(self._engine_factory()) as session, session.begin():
            session.execute(
                update(IdempotencyRecord)
                .where(
                    IdempotencyRecord.owner_clerk_user_id == owner_clerk_user_id,
                    IdempotencyRecord.operation == operation,
                    IdempotencyRecord.key_hash == key_hash,
                    IdempotencyRecord.state == "IN_PROGRESS",
                )
                .values(
                    state="COMPLETE",
                    analysis_id=analysis_id,
                    response=response,
                )
            )

    def abort_idempotency(
        self, owner_clerk_user_id: str, operation: str, key_hash: str
    ) -> None:
        with Session(self._engine_factory()) as session, session.begin():
            session.execute(
                sql_delete(IdempotencyRecord).where(
                    IdempotencyRecord.owner_clerk_user_id == owner_clerk_user_id,
                    IdempotencyRecord.operation == operation,
                    IdempotencyRecord.key_hash == key_hash,
                    IdempotencyRecord.state == "IN_PROGRESS",
                )
            )

    def consume_rate_limit(
        self,
        scope_hash: str,
        bucket: str,
        limit: int,
        window_seconds: int,
    ) -> int | None:
        now = datetime.now(UTC)
        epoch = int(now.timestamp())
        window_epoch = epoch - (epoch % window_seconds)
        window_start = datetime.fromtimestamp(window_epoch, UTC)
        expires_at = window_start + timedelta(seconds=window_seconds * 2)
        statement = (
            pg_insert(RateLimitRecord)
            .values(
                scope_hash=scope_hash,
                bucket=bucket,
                window_start=window_start,
                count=1,
                expires_at=expires_at,
            )
            .on_conflict_do_update(
                constraint="uq_rr_rate_limit_scope_bucket_window",
                set_={"count": RateLimitRecord.count + 1},
                where=RateLimitRecord.count < max(1, limit),
            )
            .returning(RateLimitRecord.count)
        )
        with Session(self._engine_factory()) as session, session.begin():
            count = session.scalar(statement)
        if count is not None:
            return None
        return max(1, window_seconds - (epoch - window_epoch))

    def claim_operation(
        self,
        analysis_id: str,
        owner_clerk_user_id: str,
        operation: str,
        token: str,
        started_at: datetime,
    ) -> bool:
        with Session(self._engine_factory()) as session, session.begin():
            result = session.execute(
                update(AnalysisRecord)
                .where(
                    AnalysisRecord.id == analysis_id,
                    AnalysisRecord.owner_clerk_user_id == owner_clerk_user_id,
                    or_(
                        AnalysisRecord.active_operation_token.is_(None),
                        AnalysisRecord.operation_started_at
                        < started_at - timedelta(minutes=10),
                    ),
                )
                .values(
                    active_operation=operation,
                    active_operation_token=token,
                    operation_started_at=started_at,
                    row_version=AnalysisRecord.row_version + 1,
                )
            )
            return bool(result.rowcount)

    def mark_artifact_deleted(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> None:
        with Session(self._engine_factory()) as session, session.begin():
            session.execute(
                update(AnalysisRecord)
                .where(
                    AnalysisRecord.id == analysis_id,
                    AnalysisRecord.owner_clerk_user_id == owner_clerk_user_id,
                )
                .values(
                    artifact_state="DELETED",
                    artifact_object_name=None,
                    artifact_generation=None,
                    artifact_expires_at=None,
                    row_version=AnalysisRecord.row_version + 1,
                )
            )

    def delete(self, analysis_id: str, owner_clerk_user_id: str) -> None:
        with Session(self._engine_factory()) as session, session.begin():
            existing = session.scalar(
                select(AnalysisRecord).where(
                    AnalysisRecord.id == analysis_id,
                    AnalysisRecord.owner_clerk_user_id == owner_clerk_user_id,
                )
            )
            if existing is not None:
                session.delete(existing)

    def delete_expired(self, expired_before: datetime) -> None:
        with Session(self._engine_factory()) as session, session.begin():
            session.execute(
                sql_delete(AnalysisRecord).where(
                    AnalysisRecord.expires_at <= expired_before
                )
            )


def build_evidence_repository() -> EvidenceRepository:
    if settings.rollbackready_persist_reports:
        return SqlAlchemyEvidenceRepository()
    return NullEvidenceRepository()
