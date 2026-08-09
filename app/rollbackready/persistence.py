from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.database import get_database_engine
from app.models.rollbackready import (
    AnalysisRecord,
    FindingRecord,
    LegacyQueryResultRecord,
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


class EvidenceRepository(Protocol):
    def save(self, report: EvidenceReport, owner_clerk_user_id: str) -> None: ...

    def get(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> EvidenceReport | None: ...

    def delete(self, analysis_id: str, owner_clerk_user_id: str) -> None: ...

    def delete_expired(self, expired_before: datetime) -> None: ...


class NullEvidenceRepository:
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


class SqlAlchemyEvidenceRepository:
    """Replace sanitized evidence atomically; raw artifacts never enter this layer."""

    def __init__(self, engine_factory: Callable[[], Engine] = get_database_engine) -> None:
        self._engine_factory = engine_factory

    def save(self, report: EvidenceReport, owner_clerk_user_id: str) -> None:
        summary = report.analysis
        with Session(self._engine_factory()) as session, session.begin():
            existing = session.scalar(
                select(AnalysisRecord).where(
                    AnalysisRecord.id == summary.id,
                    AnalysisRecord.owner_clerk_user_id == owner_clerk_user_id,
                )
            )
            if existing is not None:
                session.delete(existing)
                session.flush()
            analysis = AnalysisRecord(
                id=summary.id,
                owner_clerk_user_id=owner_clerk_user_id,
                status=summary.status,
                evidence_level=summary.evidence_level,
                verdict=summary.verdict,
                input_hash=summary.manifest.archive_sha256,
                provider=summary.provider,
                candidate_migration=summary.candidate_migration,
                manifest=summary.manifest.model_dump(mode="json"),
                evidence=[item.model_dump(mode="json") for item in summary.evidence],
                limitations=summary.limitations,
                created_at=summary.created_at,
                updated_at=summary.updated_at,
                expires_at=summary.expires_at,
            )
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
