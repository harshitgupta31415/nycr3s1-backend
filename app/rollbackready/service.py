from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import uuid4

from app.core.config import settings
from app.rollbackready.contracts import (
    AnalysisStatus,
    AnalysisSummary,
    ArtifactManifest,
    EvidenceDimension,
    EvidenceLevel,
    EvidenceReport,
    EvidenceStatus,
    PlanState,
    RecoveryPlan,
    RiskFinding,
    Severity,
    SimulationRun,
    TimelineEvent,
    Verdict,
    VerificationResult,
)
from app.rollbackready.errors import RollbackReadyError, not_found
from app.rollbackready.intake import ProjectBundle
from app.rollbackready.persistence import EvidenceRepository, build_evidence_repository
from app.rollbackready.planning import RecoveryPlanner
from app.rollbackready.risk import analyze_risks, confirm_findings_from_execution
from app.rollbackready.simulation import SimulationEngine, empty_dimensions
from app.rollbackready.sql import validate_sql_policy

ANALYSIS_RETENTION = timedelta(hours=24)
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Analysis:
    id: str
    owner_clerk_user_id: str
    manifest: ArtifactManifest
    bundle: ProjectBundle | None
    status: AnalysisStatus
    evidence_level: EvidenceLevel
    verdict: Verdict
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    findings: list[RiskFinding] = field(default_factory=list)
    evidence: list[EvidenceDimension] = field(default_factory=empty_dimensions)
    runs: list[SimulationRun] = field(default_factory=list)
    legacy_results: list = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    plans: list[RecoveryPlan] = field(default_factory=list)
    verifications: list[VerificationResult] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


class AnalysisService:
    """Synchronous MVP orchestrator; raw inputs live only in process memory."""

    def __init__(
        self,
        *,
        simulator: SimulationEngine | None = None,
        planner: RecoveryPlanner | None = None,
        repository: EvidenceRepository | None = None,
    ) -> None:
        self._analyses: dict[str, _Analysis] = {}
        self._lock = RLock()
        self._simulator = simulator or SimulationEngine()
        self._planner = planner or RecoveryPlanner()
        self._repository = repository or build_evidence_repository()

    def create(
        self, bundle: ProjectBundle, owner_clerk_user_id: str
    ) -> AnalysisSummary:
        self.purge_expired()
        now = datetime.now(UTC)
        analysis_id = str(uuid4())
        record = _Analysis(
            id=analysis_id,
            owner_clerk_user_id=owner_clerk_user_id,
            manifest=bundle.manifest,
            bundle=bundle,
            status=AnalysisStatus.STAGED,
            evidence_level=bundle.evidence_level,
            verdict=Verdict.INSUFFICIENT_EVIDENCE,
            created_at=now,
            updated_at=now,
            expires_at=now + ANALYSIS_RETENTION,
        )
        self._append_event(record, "BUNDLE_STAGED", "PASS", "Project bundle validated and staged in temporary memory.")
        with self._lock:
            active_raw_bundles = sum(
                item.bundle is not None for item in self._analyses.values()
            )
            if active_raw_bundles >= max(
                1, settings.rollbackready_max_active_analyses
            ):
                raise RollbackReadyError(
                    "ANALYSIS_CAPACITY_REACHED",
                    "This instance has reached its temporary analysis capacity. Retry shortly.",
                    status_code=429,
                    details={
                        "limit": settings.rollbackready_max_active_analyses,
                    },
                )
            self._analyses[analysis_id] = record
        self._persist(record)
        return self._summary(record)

    def run(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> AnalysisSummary:
        record = self._get_record(analysis_id, owner_clerk_user_id)
        if record.bundle is None:
            raise RollbackReadyError(
                "RAW_ARTIFACTS_DELETED",
                "The raw analysis artifacts have already been deleted.",
                status_code=409,
                analysis_id=analysis_id,
            )
        if record.status not in {AnalysisStatus.STAGED, AnalysisStatus.ERROR}:
            return self._summary(record)

        record.status = AnalysisStatus.ANALYZING
        self._touch(record)
        self._append_event(record, "STATIC_ANALYSIS_STARTED", "RUNNING", "Deterministic SQL analysis started.")
        record.findings = analyze_risks(record.bundle)
        self._append_event(
            record,
            "STATIC_ANALYSIS_COMPLETED",
            "PASS",
            f"Deterministic analysis produced {len(record.findings)} sanitized findings.",
        )

        if not record.bundle.ready_for_simulation:
            record.status = AnalysisStatus.STATIC_ONLY
            record.verdict = Verdict.INSUFFICIENT_EVIDENCE
            record.evidence = empty_dimensions()
            record.limitations = _missing_input_limitations(record.bundle)
            self._append_event(
                record,
                "SIMULATION_NOT_TESTED",
                "NOT_TESTED",
                "Complete PostgreSQL history, fixtures, and legacy queries are required for verified evidence.",
            )
            self._touch(record)
            self._persist(record)
            return self._summary(record)

        record.status = AnalysisStatus.SIMULATING
        self._touch(record)
        self._append_event(record, "SANDBOX_STARTED", "RUNNING", "Disposable PostgreSQL 18 simulation started.")
        try:
            outcome = self._simulator.run(analysis_id, record.bundle, record.findings)
        except RollbackReadyError as exc:
            if exc.code not in {
                "SANDBOX_UNAVAILABLE",
                "SANDBOX_START_TIMEOUT",
                "SIMULATOR_BUSY",
            }:
                record.status = AnalysisStatus.ERROR
                record.verdict = Verdict.ERROR
                record.limitations.append(exc.message)
                self._append_event(record, "SANDBOX_ERROR", "ERROR", exc.message)
                self._touch(record)
                self._persist(record)
                raise
            record.status = AnalysisStatus.ERROR
            record.verdict = Verdict.INSUFFICIENT_EVIDENCE
            record.limitations.append(exc.message)
            self._append_event(record, "SANDBOX_ERROR", "ERROR", exc.message)
            self._touch(record)
            self._persist(record)
            return self._summary(record)
        except Exception as exc:
            record.status = AnalysisStatus.ERROR
            record.verdict = Verdict.ERROR
            record.limitations.append("The sandbox failed before complete evidence could be collected.")
            self._append_event(record, "SANDBOX_ERROR", "ERROR", "The sandbox failed before complete evidence could be collected.")
            self._touch(record)
            self._persist(record)
            logger.exception(
                "Disposable PostgreSQL simulation failed for analysis %s",
                analysis_id,
            )
            raise RollbackReadyError(
                "SIMULATION_ERROR",
                "The sandbox failed before complete evidence could be collected.",
                status_code=500,
                analysis_id=analysis_id,
            ) from exc

        record.evidence = outcome.dimensions
        record.runs = outcome.runs
        record.legacy_results = outcome.legacy_results
        record.findings = confirm_findings_from_execution(
            record.findings, outcome.candidate_error
        )
        record.verdict = _candidate_verdict(record.findings, record.evidence)
        record.status = {
            Verdict.UNSAFE: AnalysisStatus.UNSAFE,
            Verdict.CONDITIONALLY_VERIFIED: AnalysisStatus.CONDITIONAL,
            Verdict.VERIFIED_FOR_REVIEW: AnalysisStatus.VERIFIED,
            Verdict.INSUFFICIENT_EVIDENCE: AnalysisStatus.ERROR,
            Verdict.ERROR: AnalysisStatus.ERROR,
        }[record.verdict]
        for run in record.runs:
            self._append_event(
                record,
                run.run_type,
                run.status,
                _run_event_message(run),
                run_id=run.id,
                statement_index=run.boundary,
            )
        self._append_event(
            record,
            "ANALYSIS_COMPLETED",
            record.verdict,
            f"Analysis completed with verdict {record.verdict}.",
        )
        self._touch(record)
        self._persist(record)
        return self._summary(record)

    def get(self, analysis_id: str, owner_clerk_user_id: str) -> AnalysisSummary:
        return self._summary(self._get_record(analysis_id, owner_clerk_user_id))

    def timeline(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> list[TimelineEvent]:
        return list(self._get_record(analysis_id, owner_clerk_user_id).timeline)

    def create_plan(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> RecoveryPlan:
        record = self._get_record(analysis_id, owner_clerk_user_id)
        if not record.findings:
            raise RollbackReadyError(
                "PLAN_NOT_REQUIRED",
                "This analysis has no risk findings that require a recovery plan.",
                status_code=409,
                analysis_id=analysis_id,
            )
        record.status = AnalysisStatus.PLANNING
        self._append_event(record, "PLAN_GENERATION_STARTED", "RUNNING", "Normalized findings were sent to the constrained recovery planner.")
        try:
            plan = self._planner.generate(analysis_id, record.findings)
        except RollbackReadyError:
            record.status = AnalysisStatus.PLAN_REJECTED
            self._append_event(record, "PLAN_REJECTED", "FAIL", "No plan passed schema and deterministic policy validation.")
            self._touch(record)
            raise
        record.plans.append(plan)
        self._append_event(record, "PLAN_GENERATED", "PASS", "A structured plan passed schema and SQL-policy validation; it remains unverified.")
        self._touch(record)
        self._persist(record)
        return plan

    def verify_plan(
        self, analysis_id: str, plan_id: str, owner_clerk_user_id: str
    ) -> VerificationResult:
        record = self._get_record(analysis_id, owner_clerk_user_id)
        plan = next((item for item in record.plans if item.id == plan_id), None)
        if plan is None:
            raise RollbackReadyError(
                "PLAN_NOT_FOUND",
                "The requested recovery plan does not exist.",
                status_code=404,
                analysis_id=analysis_id,
            )
        if record.bundle is None:
            raise RollbackReadyError(
                "RAW_ARTIFACTS_DELETED",
                "The verification artifacts have already been deleted.",
                status_code=409,
                analysis_id=analysis_id,
            )
        plan_sql = [statement for phase in plan.phases for statement in phase.sql]
        verification_sql = [
            statement
            for phase in plan.phases
            for statement in phase.verification_sql
        ]
        if not verification_sql:
            raise RollbackReadyError(
                "PLAN_REJECTED",
                "The recovery plan has no deterministic verification assertions.",
                status_code=422,
                analysis_id=analysis_id,
            )
        for statement in plan_sql:
            validate_sql_policy(statement, analysis_id=analysis_id)
        for statement in verification_sql:
            validated = validate_sql_policy(
                statement,
                legacy_query=True,
                analysis_id=analysis_id,
            )
            if any(item.kind != "SELECT" for item in validated):
                raise RollbackReadyError(
                    "PLAN_REJECTED",
                    "Plan verification SQL must contain read-only SELECT assertions.",
                    status_code=422,
                    analysis_id=analysis_id,
                )
        plan.state = PlanState.VERIFYING
        record.status = AnalysisStatus.VERIFYING_PLAN
        self._append_event(record, "PLAN_VERIFICATION_STARTED", "RUNNING", "Plan verification started from a fresh PostgreSQL baseline.")
        try:
            outcome = self._simulator.verify_plan(
                analysis_id,
                record.bundle,
                plan_sql,
                verification_sql,
            )
        except RollbackReadyError as exc:
            self._restore_after_verification_error(record, plan, exc.message)
            raise
        except Exception as exc:
            message = "Fresh-sandbox plan verification could not complete. Retry the verification."
            self._restore_after_verification_error(record, plan, message)
            raise RollbackReadyError(
                "PLAN_VERIFICATION_ERROR",
                message,
                status_code=500,
                analysis_id=analysis_id,
            ) from exc
        mandatory = {
            "prior_migrations",
            "fixture_load",
            "schema_application",
            "data_preservation",
            "legacy_queries",
            "failure_recovery",
            "idempotent_retry",
        }
        passed = all(
            dimension.status is EvidenceStatus.PASS
            for dimension in outcome.dimensions
            if dimension.key in mandatory
        ) and mandatory.issubset({item.key for item in outcome.dimensions})
        result = VerificationResult(
            id=str(uuid4()),
            plan_id=plan_id,
            status=EvidenceStatus.PASS if passed else EvidenceStatus.FAIL,
            verdict=(
                Verdict.VERIFIED_FOR_REVIEW if passed else Verdict.UNSAFE
            ),
            dimensions=outcome.dimensions,
            completed_at=datetime.now(UTC),
            sanitized_error=outcome.candidate_error,
        )
        record.verifications.append(result)
        record.runs.extend(outcome.runs)
        record.legacy_results.extend(outcome.legacy_results)
        if passed:
            plan.state = PlanState.VERIFIED_FOR_REVIEW
            record.status = AnalysisStatus.VERIFIED_PLAN
            event_status = "PASS"
            event_message = "The plan executed on a fresh sandbox and preserved mandatory compatibility evidence."
        else:
            plan.state = PlanState.REJECTED
            record.status = AnalysisStatus.PLAN_REJECTED
            event_status = "FAIL"
            event_message = "The plan failed deterministic fresh-sandbox verification."
        self._append_event(record, "PLAN_VERIFICATION_COMPLETED", event_status, event_message)
        # The complete analysis/verification lifecycle is terminal. Only hashes,
        # normalized findings, generated SQL, and sanitized evidence remain.
        record.bundle = None
        self._touch(record)
        self._persist(record)
        return result

    def _restore_after_verification_error(
        self,
        record: _Analysis,
        plan: RecoveryPlan,
        message: str,
    ) -> None:
        """Keep a transient verifier failure retryable and accurately reported."""
        plan.state = PlanState.UNVERIFIED_CANDIDATE
        record.status = _status_for_verdict(record.verdict)
        self._append_event(record, "PLAN_VERIFICATION_ERROR", "ERROR", message)
        self._touch(record)
        self._persist(record)

    def report(self, analysis_id: str, owner_clerk_user_id: str) -> EvidenceReport:
        record = self._get_record(analysis_id, owner_clerk_user_id)
        return EvidenceReport(
            analysis=self._summary(record),
            timeline=list(record.timeline),
            generated_at=datetime.now(UTC),
        )

    def delete(self, analysis_id: str, owner_clerk_user_id: str) -> None:
        with self._lock:
            record = self._analyses.get(analysis_id)
            if record is not None and record.owner_clerk_user_id != owner_clerk_user_id:
                raise not_found(analysis_id)
            if record is not None:
                self._analyses.pop(analysis_id, None)
        if record is None:
            if self._repository.get(analysis_id, owner_clerk_user_id) is None:
                raise not_found(analysis_id)
        else:
            record.bundle = None
        self._repository.delete(analysis_id, owner_clerk_user_id)

    def purge_expired(self, now: datetime | None = None) -> int:
        """Delete expired raw bundles and sanitized reports without an access trigger."""
        expired_before = now or datetime.now(UTC)
        with self._lock:
            expired = [
                (analysis_id, record)
                for analysis_id, record in self._analyses.items()
                if record.expires_at <= expired_before
            ]
            for analysis_id, _ in expired:
                self._analyses.pop(analysis_id, None)
        for analysis_id, record in expired:
            record.bundle = None
            try:
                self._repository.delete(
                    analysis_id,
                    record.owner_clerk_user_id,
                )
            except Exception:  # noqa: BLE001 -- expiry retries on the next sweep
                logger.warning(
                    "Sanitized expired analysis deletion will retry on the next sweep."
                )
                continue
        try:
            self._repository.delete_expired(expired_before)
        except Exception:  # noqa: BLE001 -- raw in-memory artifacts are already gone
            logger.warning(
                "Sanitized persisted expiry sweep is temporarily unavailable."
            )
        return len(expired)

    def _get_record(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> _Analysis:
        with self._lock:
            record = self._analyses.get(analysis_id)
        if record is not None and record.owner_clerk_user_id != owner_clerk_user_id:
            raise not_found(analysis_id)
        if record is None:
            persisted = self._repository.get(analysis_id, owner_clerk_user_id)
            if persisted is None:
                raise not_found(analysis_id)
            summary = persisted.analysis
            record = _Analysis(
                id=summary.id,
                owner_clerk_user_id=owner_clerk_user_id,
                manifest=summary.manifest,
                bundle=None,
                status=summary.status,
                evidence_level=summary.evidence_level,
                verdict=summary.verdict,
                created_at=summary.created_at,
                updated_at=summary.updated_at,
                expires_at=summary.expires_at,
                findings=list(summary.findings),
                evidence=list(summary.evidence),
                runs=list(summary.simulation_runs),
                legacy_results=list(summary.legacy_query_results),
                timeline=list(persisted.timeline),
                plans=list(summary.plans),
                verifications=list(summary.verification_results),
                limitations=list(summary.limitations),
            )
            with self._lock:
                self._analyses[analysis_id] = record
        if record.expires_at <= datetime.now(UTC):
            record.bundle = None
            with self._lock:
                self._analyses.pop(analysis_id, None)
            self._repository.delete(analysis_id, owner_clerk_user_id)
            raise RollbackReadyError(
                "ANALYSIS_EXPIRED",
                "The requested analysis has expired.",
                status_code=410,
                analysis_id=analysis_id,
            )
        return record

    @staticmethod
    def _summary(record: _Analysis) -> AnalysisSummary:
        return AnalysisSummary(
            id=record.id,
            status=record.status,
            evidence_level=record.evidence_level,
            verdict=record.verdict,
            provider=record.manifest.provider,
            candidate_migration=record.manifest.candidate_migration,
            created_at=record.created_at,
            updated_at=record.updated_at,
            expires_at=record.expires_at,
            manifest=record.manifest,
            findings=list(record.findings),
            evidence=list(record.evidence),
            simulation_runs=list(record.runs),
            legacy_query_results=list(record.legacy_results),
            plans=list(record.plans),
            verification_results=list(record.verifications),
            limitations=list(record.limitations),
        )

    @staticmethod
    def _touch(record: _Analysis) -> None:
        record.updated_at = datetime.now(UTC)

    def _persist(self, record: _Analysis) -> None:
        try:
            self._repository.save(
                EvidenceReport(
                    analysis=self._summary(record),
                    timeline=list(record.timeline),
                    generated_at=datetime.now(UTC),
                ),
                record.owner_clerk_user_id,
            )
        except Exception:  # noqa: BLE001 -- analysis evidence remains available in memory
            message = "Sanitized report persistence is temporarily unavailable."
            if message not in record.limitations:
                record.limitations.append(message)

    @staticmethod
    def _append_event(
        record: _Analysis,
        event_type: str,
        status: object,
        message: str,
        *,
        run_id: str | None = None,
        statement_index: int | None = None,
    ) -> None:
        record.timeline.append(
            TimelineEvent(
                sequence=len(record.timeline) + 1,
                occurred_at=datetime.now(UTC),
                event_type=event_type,
                status=str(status),
                message=message,
                run_id=run_id,
                statement_index=statement_index,
            )
        )


def _candidate_verdict(
    findings: list[RiskFinding], evidence: list[EvidenceDimension]
) -> Verdict:
    statuses = [dimension.status for dimension in evidence]
    if any(status is EvidenceStatus.FAIL for status in statuses):
        return Verdict.UNSAFE
    if any(finding.severity is Severity.CRITICAL for finding in findings):
        return Verdict.UNSAFE
    if any(
        finding.severity is Severity.HIGH and finding.confirmed
        for finding in findings
    ):
        return Verdict.UNSAFE
    if any(status is EvidenceStatus.NOT_TESTED for status in statuses):
        return Verdict.INSUFFICIENT_EVIDENCE
    critical_or_high = any(
        finding.severity in {Severity.CRITICAL, Severity.HIGH} for finding in findings
    )
    if critical_or_high or any(
        finding.evidence_source.value == "HEURISTIC" for finding in findings
    ):
        return Verdict.CONDITIONALLY_VERIFIED
    return Verdict.VERIFIED_FOR_REVIEW


def _status_for_verdict(verdict: Verdict) -> AnalysisStatus:
    return {
        Verdict.UNSAFE: AnalysisStatus.UNSAFE,
        Verdict.CONDITIONALLY_VERIFIED: AnalysisStatus.CONDITIONAL,
        Verdict.VERIFIED_FOR_REVIEW: AnalysisStatus.VERIFIED,
        Verdict.INSUFFICIENT_EVIDENCE: AnalysisStatus.ERROR,
        Verdict.ERROR: AnalysisStatus.ERROR,
    }[verdict]


def _missing_input_limitations(bundle: ProjectBundle) -> list[str]:
    limitations: list[str] = []
    if bundle.manifest.provider != "postgresql":
        limitations.append("Only PostgreSQL Prisma projects can receive sandbox verification.")
    if not bundle.manifest.has_schema:
        limitations.append("schema.prisma is missing.")
    if not bundle.manifest.has_lockfile:
        limitations.append("migration_lock.toml is missing.")
    if not bundle.manifest.has_seed:
        limitations.append("Synthetic rollbackready/seed.sql is missing.")
    if not bundle.legacy_queries:
        limitations.append("rollbackready/legacy-queries.json is missing or empty.")
    return limitations


def _run_event_message(run: SimulationRun) -> str:
    if run.run_type == "NORMAL_APPLICATION":
        return "Candidate migration applied on the normal path." if run.status is EvidenceStatus.PASS else "Candidate migration failed on the normal path."
    if run.boundary is not None:
        return f"Interruption boundary {run.boundary} completed with {run.status}."
    return f"{run.run_type} completed with {run.status}."
