from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Condition, RLock
from uuid import uuid4

from app.core.config import settings
from app.rollbackready.advisory import SchemaChangeAdvisor
from app.rollbackready.artifacts import ArtifactStore, build_artifact_store
from app.rollbackready.contracts import (
    AIInsight,
    AnalysisStatus,
    AnalysisSummary,
    ArtifactManifest,
    EvidenceDimension,
    EvidenceLevel,
    EvidenceReport,
    EvidenceStatus,
    InsightKind,
    PlanState,
    RecoveryPlan,
    RiskFinding,
    SchemaChatRequest,
    SchemaChatResponse,
    Severity,
    SimulationRun,
    TimelineEvent,
    Verdict,
    VerificationResult,
)
from app.rollbackready.errors import RollbackReadyError, not_found
from app.rollbackready.intake import ProjectBundle, load_project_bundle
from app.rollbackready.persistence import (
    ArtifactReference,
    EvidenceRepository,
    IdempotencyDecision,
    build_evidence_repository,
)
from app.rollbackready.planning import RecoveryPlanner
from app.rollbackready.risk import analyze_risks, confirm_findings_from_execution
from app.rollbackready.simulation import SimulationEngine, empty_dimensions
from app.rollbackready.sql import validate_sql_policy

ANALYSIS_RETENTION = timedelta(hours=24)
logger = logging.getLogger(__name__)
TERMINAL_ANALYSIS_STATUSES = frozenset(
    {
        AnalysisStatus.INVALID,
        AnalysisStatus.STATIC_ONLY,
        AnalysisStatus.UNSAFE,
        AnalysisStatus.CONDITIONAL,
        AnalysisStatus.VERIFIED,
        AnalysisStatus.PLAN_REJECTED,
        AnalysisStatus.VERIFIED_PLAN,
        AnalysisStatus.ERROR,
        AnalysisStatus.EXPIRED,
    }
)


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
    artifact_object_name: str | None = None
    artifact_generation: str | None = None
    artifact_expires_at: datetime | None = None
    findings: list[RiskFinding] = field(default_factory=list)
    evidence: list[EvidenceDimension] = field(default_factory=empty_dimensions)
    runs: list[SimulationRun] = field(default_factory=list)
    legacy_results: list = field(default_factory=list)
    timeline: list[TimelineEvent] = field(default_factory=list)
    plans: list[RecoveryPlan] = field(default_factory=list)
    verifications: list[VerificationResult] = field(default_factory=list)
    insights: list[AIInsight] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


class AnalysisService:
    """Synchronous orchestrator with durable sanitized and short-lived raw state."""

    def __init__(
        self,
        *,
        simulator: SimulationEngine | None = None,
        planner: RecoveryPlanner | None = None,
        advisor: SchemaChangeAdvisor | None = None,
        repository: EvidenceRepository | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._analyses: dict[str, _Analysis] = {}
        self._lock = RLock()
        self._event_condition = Condition(self._lock)
        self._simulator = simulator or SimulationEngine()
        self._planner = planner or RecoveryPlanner()
        self._advisor = advisor or SchemaChangeAdvisor()
        self._repository = repository or build_evidence_repository()
        self._artifact_store = artifact_store or build_artifact_store()
        self._local_idempotency: dict[
            tuple[str, str, str], IdempotencyDecision
        ] = {}
        self._local_rate_windows: dict[
            tuple[str, str], deque[float]
        ] = {}

    def create(
        self,
        bundle: ProjectBundle,
        owner_clerk_user_id: str,
        archive: bytes | None = None,
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
            limitations=_input_limitations(bundle),
        )
        artifact: ArtifactReference | None = None
        if archive is not None:
            stored = self._artifact_store.put(analysis_id, archive)
            record.artifact_object_name = stored.object_name
            record.artifact_generation = stored.generation
            record.artifact_expires_at = stored.expires_at
            artifact = ArtifactReference(
                object_name=stored.object_name,
                generation=stored.generation,
                expires_at=stored.expires_at,
            )
        self._append_event(
            record,
            "BUNDLE_STAGED",
            "PASS",
            "Project bundle validated and staged in private short-lived artifact storage.",
        )
        with self._lock:
            unfinished = sum(
                item.owner_clerk_user_id == owner_clerk_user_id
                and item.status in {
                    AnalysisStatus.STAGED,
                    AnalysisStatus.ANALYZING,
                    AnalysisStatus.SIMULATING,
                    AnalysisStatus.PLANNING,
                    AnalysisStatus.VERIFYING_PLAN,
                }
                for item in self._analyses.values()
            )
            if (
                not settings.is_privileged_clerk_user(owner_clerk_user_id)
                and unfinished
                >= max(1, settings.rollbackready_max_unfinished_per_user)
            ):
                self._delete_artifact(record, mark_persisted=False)
                raise RollbackReadyError(
                    "ANALYSIS_CAPACITY_REACHED",
                    "This account has reached its unfinished-analysis limit.",
                    status_code=429,
                    details={
                        "limit": settings.rollbackready_max_unfinished_per_user,
                    },
                )
            self._analyses[analysis_id] = record
        try:
            self._create_persisted(record, artifact)
        except Exception:
            with self._lock:
                self._analyses.pop(analysis_id, None)
            self._delete_artifact(record, mark_persisted=False)
            raise
        return self._summary(record)

    def run(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> AnalysisSummary:
        record = self._get_record(analysis_id, owner_clerk_user_id)
        if record.status not in {AnalysisStatus.STAGED, AnalysisStatus.ERROR}:
            return self._summary(record)
        self._ensure_bundle(record)
        operation_token = self._claim_operation(record, "run")

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
            record.limitations = _input_limitations(record.bundle)
            self._append_event(
                record,
                "SIMULATION_NOT_TESTED",
                "NOT_TESTED",
                "Complete PostgreSQL history, fixtures, and legacy queries are required for verified evidence.",
            )
            self._touch(record)
            self._persist(record, operation_token)
            self._delete_artifact(record)
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
                self._persist(record, operation_token)
                raise
            record.status = AnalysisStatus.ERROR
            record.verdict = Verdict.INSUFFICIENT_EVIDENCE
            record.limitations.append(exc.message)
            self._append_event(record, "SANDBOX_ERROR", "ERROR", exc.message)
            self._touch(record)
            self._persist(record, operation_token)
            return self._summary(record)
        except Exception as exc:
            record.status = AnalysisStatus.ERROR
            record.verdict = Verdict.ERROR
            record.limitations.append("The sandbox failed before complete evidence could be collected.")
            self._append_event(record, "SANDBOX_ERROR", "ERROR", "The sandbox failed before complete evidence could be collected.")
            self._touch(record)
            self._persist(record, operation_token)
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
        self._persist(record, operation_token)
        if not record.findings:
            self._delete_artifact(record)
        return self._summary(record)

    def get(self, analysis_id: str, owner_clerk_user_id: str) -> AnalysisSummary:
        return self._summary(self._get_record(analysis_id, owner_clerk_user_id))

    def timeline(
        self, analysis_id: str, owner_clerk_user_id: str
    ) -> list[TimelineEvent]:
        record = self._get_record(analysis_id, owner_clerk_user_id)
        with self._lock:
            return list(record.timeline)

    def wait_for_timeline_events(
        self,
        analysis_id: str,
        owner_clerk_user_id: str,
        after_sequence: int,
        timeout_seconds: float = 15.0,
    ) -> tuple[list[TimelineEvent], AnalysisStatus]:
        """Replay and wait for owner-scoped timeline events without busy polling."""
        record = self._get_record(analysis_id, owner_clerk_user_id)
        with self._event_condition:
            self._event_condition.wait_for(
                lambda: len(record.timeline) > after_sequence
                or record.status in TERMINAL_ANALYSIS_STATUSES,
                timeout=max(0.1, timeout_seconds),
            )
            return list(record.timeline[after_sequence:]), record.status

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
        if (
            not settings.is_privileged_clerk_user(owner_clerk_user_id)
            and len(record.plans) >= settings.rollbackready_max_plans_per_analysis
        ):
            raise RollbackReadyError(
                "PLAN_LIMIT_REACHED",
                "This analysis has reached its recovery-plan limit.",
                status_code=429,
                analysis_id=analysis_id,
                details={"limit": settings.rollbackready_max_plans_per_analysis},
            )
        operation_token = self._claim_operation(record, "plan")
        record.status = AnalysisStatus.PLANNING
        self._append_event(record, "PLAN_GENERATION_STARTED", "RUNNING", "Normalized findings were sent to the constrained recovery planner.")
        try:
            plan = self._planner.generate(analysis_id, record.findings)
        except RollbackReadyError:
            record.status = AnalysisStatus.PLAN_REJECTED
            self._append_event(record, "PLAN_REJECTED", "FAIL", "No plan passed schema and deterministic policy validation.")
            self._touch(record)
            self._persist(record, operation_token)
            raise
        record.plans.append(plan)
        self._append_event(record, "PLAN_GENERATED", "PASS", "A structured plan passed schema and SQL-policy validation; it remains unverified.")
        self._touch(record)
        self._persist(record, operation_token)
        return plan

    def chat_schema_change(
        self,
        analysis_id: str,
        request: SchemaChatRequest,
        owner_clerk_user_id: str,
    ) -> SchemaChatResponse:
        record = self._get_record(analysis_id, owner_clerk_user_id)
        if not record.findings:
            raise RollbackReadyError(
                "CHAT_CONTEXT_UNAVAILABLE",
                "Run an analysis with at least one finding before starting a schema conversation.",
                status_code=409,
                analysis_id=analysis_id,
            )
        plan = record.plans[-1] if record.plans else None
        return self._advisor.reply(analysis_id, request, record.findings, plan)

    def create_insight(
        self,
        analysis_id: str,
        kind: InsightKind,
        owner_clerk_user_id: str,
    ) -> AIInsight:
        record = self._get_record(analysis_id, owner_clerk_user_id)
        cached = next((item for item in record.insights if item.kind is kind), None)
        if cached is not None:
            return cached
        if not record.findings:
            raise RollbackReadyError(
                "INSIGHT_CONTEXT_UNAVAILABLE",
                "Run an analysis with findings before requesting AI insights.",
                status_code=409,
                analysis_id=analysis_id,
            )

        plan = record.plans[-1] if record.plans else None
        if kind is InsightKind.FINDING_EXPLANATIONS:
            message = (
                "Explain each deterministic finding in plain language, preserving its finding ID, "
                "and state the practical application impact without inventing database facts."
            )
            derived_from = [item.id for item in record.findings]
        elif kind is InsightKind.MIGRATION_SUMMARY:
            message = (
                f"Write a concise pull-request summary for this migration. The verdict is {record.verdict}. "
                "Use the safety phrase verified for human review and never say safe to deploy."
            )
            derived_from = [item.id for item in record.findings] + [
                item.key for item in record.evidence
            ]
        else:
            verification = next(
                (
                    item
                    for item in reversed(record.verifications)
                    if item.status is EvidenceStatus.FAIL
                ),
                None,
            )
            if verification is None:
                raise RollbackReadyError(
                    "PLAN_REJECTION_UNAVAILABLE",
                    "A failed deterministic plan verification is required for this insight.",
                    status_code=409,
                    analysis_id=analysis_id,
                )
            failed = [
                item for item in verification.dimensions if item.status is EvidenceStatus.FAIL
            ]
            message = (
                "Explain why the recovery plan failed deterministic verification. Ground the answer "
                "only in these failed evidence dimensions: "
                + "; ".join(f"{item.key}: {item.summary}" for item in failed)
            )
            derived_from = [item.key for item in failed]

        response = self._advisor.reply(
            analysis_id,
            SchemaChatRequest(message=message),
            record.findings,
            plan,
        )
        insight = AIInsight(
            id=str(uuid4()),
            analysis_id=analysis_id,
            kind=kind,
            content=_sanitize_insight_content(response.answer, set(derived_from)),
            derived_from=derived_from,
            provider=response.provider,
            model=response.model,
            prompt_template_version=response.prompt_template_version,
            generated_at=datetime.now(UTC),
        )
        record.insights.append(insight)
        self._append_event(
            record,
            "AI_INSIGHT_GENERATED",
            "PASS",
            f"A sanitized {kind.value} insight was generated and cached.",
        )
        self._touch(record)
        self._persist(record)
        return insight

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
        self._ensure_bundle(record)
        operation_token = self._claim_operation(record, "verify")
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
            self._restore_after_verification_error(
                record, plan, exc.message, operation_token
            )
            raise
        except Exception as exc:
            message = "Fresh-sandbox plan verification could not complete. Retry the verification."
            self._restore_after_verification_error(
                record, plan, message, operation_token
            )
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
        self._touch(record)
        self._persist(record, operation_token)
        self._delete_artifact(record)
        return result

    def _restore_after_verification_error(
        self,
        record: _Analysis,
        plan: RecoveryPlan,
        message: str,
        operation_token: str,
    ) -> None:
        """Keep a transient verifier failure retryable and accurately reported."""
        plan.state = PlanState.UNVERIFIED_CANDIDATE
        record.status = _status_for_verdict(record.verdict)
        self._append_event(record, "PLAN_VERIFICATION_ERROR", "ERROR", message)
        self._touch(record)
        self._persist(record, operation_token)

    def report(self, analysis_id: str, owner_clerk_user_id: str) -> EvidenceReport:
        record = self._get_record(analysis_id, owner_clerk_user_id)
        return EvidenceReport(
            analysis=self._summary(record),
            timeline=list(record.timeline),
            generated_at=datetime.now(UTC),
        )

    def delete(self, analysis_id: str, owner_clerk_user_id: str) -> None:
        persisted = self._repository.get(analysis_id, owner_clerk_user_id)
        with self._lock:
            record = self._analyses.get(analysis_id)
            if record is not None and record.owner_clerk_user_id != owner_clerk_user_id:
                raise not_found(analysis_id)
            if record is not None:
                self._analyses.pop(analysis_id, None)
        if record is None:
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
            )
            self._load_artifact_reference(record)
        self._repository.delete(analysis_id, owner_clerk_user_id)
        self._delete_artifact(record, mark_persisted=False)

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
            self._delete_artifact(record, mark_persisted=False)
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
            cached = self._analyses.get(analysis_id)
        record = cached
        if record is not None and record.owner_clerk_user_id != owner_clerk_user_id:
            raise not_found(analysis_id)
        authoritative = bool(getattr(self._repository, "authoritative", False))
        persisted = (
            self._repository.get(analysis_id, owner_clerk_user_id)
            if authoritative or record is None
            else None
        )

        if authoritative and persisted is None:
            with self._lock:
                self._analyses.pop(analysis_id, None)
            raise not_found(analysis_id)
        if persisted is not None:
            summary = persisted.analysis
            cached_bundle = (
                cached.bundle
                if cached is not None and summary.raw_artifacts_available
                else None
            )
            record = _Analysis(
                id=summary.id,
                owner_clerk_user_id=owner_clerk_user_id,
                manifest=summary.manifest,
                bundle=cached_bundle,
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
            self._load_artifact_reference(record)
            with self._lock:
                self._analyses[analysis_id] = record
        elif record is None:
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
                insights=list(summary.insights),
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

    def begin_idempotency(
        self, owner_clerk_user_id: str, operation: str, key: str
    ) -> tuple[str, dict | None, str]:
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        if not bool(getattr(self._repository, "authoritative", False)):
            local_key = (owner_clerk_user_id, operation, key_hash)
            with self._lock:
                decision = self._local_idempotency.get(local_key)
                if decision is None:
                    decision = IdempotencyDecision("IN_PROGRESS")
                    self._local_idempotency[local_key] = decision
                    return "NEW", None, key_hash
                return decision.state, decision.response, key_hash
        begin = getattr(self._repository, "begin_idempotency", None)
        decision = (
            begin(owner_clerk_user_id, operation, key_hash)
            if begin is not None
            else IdempotencyDecision("NEW")
        )
        return decision.state, decision.response, key_hash

    def finish_idempotency(
        self,
        owner_clerk_user_id: str,
        operation: str,
        key_hash: str,
        analysis_id: str,
        response: dict,
    ) -> None:
        if not bool(getattr(self._repository, "authoritative", False)):
            with self._lock:
                self._local_idempotency[
                    (owner_clerk_user_id, operation, key_hash)
                ] = IdempotencyDecision("COMPLETE", response)
            return
        finish = getattr(self._repository, "finish_idempotency", None)
        if finish is not None:
            finish(
                owner_clerk_user_id,
                operation,
                key_hash,
                analysis_id,
                response,
            )

    def abort_idempotency(
        self, owner_clerk_user_id: str, operation: str, key_hash: str
    ) -> None:
        if not bool(getattr(self._repository, "authoritative", False)):
            with self._lock:
                self._local_idempotency.pop(
                    (owner_clerk_user_id, operation, key_hash), None
                )
            return
        abort = getattr(self._repository, "abort_idempotency", None)
        if abort is not None:
            abort(owner_clerk_user_id, operation, key_hash)

    def check_rate_limit(
        self,
        owner_clerk_user_id: str,
        bucket: str,
        limit: int,
        window_seconds: int,
        client: str = "",
    ) -> None:
        del client
        if settings.is_privileged_clerk_user(owner_clerk_user_id):
            return
        scope = hashlib.sha256(owner_clerk_user_id.encode()).hexdigest()
        if not bool(getattr(self._repository, "authoritative", False)):
            now = time.monotonic()
            key = (scope, bucket)
            with self._lock:
                entries = self._local_rate_windows.setdefault(key, deque())
                cutoff = now - window_seconds
                while entries and entries[0] <= cutoff:
                    entries.popleft()
                if len(entries) >= limit:
                    retry_after = max(1, int(entries[0] + window_seconds - now) + 1)
                else:
                    entries.append(now)
                    retry_after = None
            if retry_after is not None:
                raise RollbackReadyError(
                    "RATE_LIMITED",
                    "This operation has reached its account quota. Retry later.",
                    status_code=429,
                    details={"retry_after_seconds": retry_after},
                )
            return
        consume = getattr(self._repository, "consume_rate_limit", None)
        retry_after = (
            consume(scope, bucket, limit, window_seconds)
            if consume is not None
            else None
        )
        if retry_after is not None:
            raise RollbackReadyError(
                "RATE_LIMITED",
                "This operation has reached its account quota. Retry later.",
                status_code=429,
                details={"retry_after_seconds": retry_after},
            )

    @staticmethod
    def _summary(record: _Analysis) -> AnalysisSummary:
        return AnalysisSummary(
            id=record.id,
            auth_mode=settings.clerk_auth_mode,
            status=record.status,
            evidence_level=record.evidence_level,
            verdict=record.verdict,
            provider=record.manifest.provider,
            candidate_migration=record.manifest.candidate_migration,
            created_at=record.created_at,
            updated_at=record.updated_at,
            expires_at=record.expires_at,
            raw_artifacts_available=bool(
                (record.bundle is not None or record.artifact_object_name)
                and (
                    record.artifact_expires_at is None
                    or record.artifact_expires_at > datetime.now(UTC)
                )
            ),
            manifest=record.manifest,
            findings=list(record.findings),
            evidence=list(record.evidence),
            simulation_runs=list(record.runs),
            legacy_query_results=list(record.legacy_results),
            plans=list(record.plans),
            verification_results=list(record.verifications),
            insights=list(record.insights),
            limitations=list(record.limitations),
        )

    @staticmethod
    def _touch(record: _Analysis) -> None:
        record.updated_at = datetime.now(UTC)

    def _persist(
        self, record: _Analysis, operation_token: str | None = None
    ) -> None:
        report = EvidenceReport(
            analysis=self._summary(record),
            timeline=list(record.timeline),
            generated_at=datetime.now(UTC),
        )
        try:
            update_record = getattr(self._repository, "update", None)
            if update_record is None:
                self._repository.save(report, record.owner_clerk_user_id)
                saved = True
            else:
                saved = update_record(
                    report,
                    record.owner_clerk_user_id,
                    operation_token,
                )
            if not saved:
                with self._lock:
                    self._analyses.pop(record.id, None)
                raise not_found(record.id)
        except RollbackReadyError:
            raise
        except Exception as exc:
            if bool(getattr(self._repository, "authoritative", False)):
                raise RollbackReadyError(
                    "PERSISTENCE_UNAVAILABLE",
                    "The analysis result could not be stored durably. Retry later.",
                    status_code=503,
                    analysis_id=record.id,
                ) from exc
            message = "Sanitized report persistence is temporarily unavailable."
            if message not in record.limitations:
                record.limitations.append(message)

    def _create_persisted(
        self,
        record: _Analysis,
        artifact: ArtifactReference | None,
    ) -> None:
        report = EvidenceReport(
            analysis=self._summary(record),
            timeline=list(record.timeline),
            generated_at=datetime.now(UTC),
        )
        create_record = getattr(self._repository, "create", None)
        if create_record is None:
            self._repository.save(report, record.owner_clerk_user_id)
            return
        if not create_record(report, record.owner_clerk_user_id, artifact):
            self._delete_artifact(record, mark_persisted=False)
            raise RollbackReadyError(
                "ANALYSIS_CONFLICT",
                "The analysis could not be created safely.",
                status_code=409,
                analysis_id=record.id,
            )

    def _claim_operation(self, record: _Analysis, operation: str) -> str:
        token = uuid4().hex
        claim = getattr(self._repository, "claim_operation", None)
        if claim is not None and not claim(
            record.id,
            record.owner_clerk_user_id,
            operation,
            token,
            datetime.now(UTC),
        ):
            if self._repository.get(record.id, record.owner_clerk_user_id) is None:
                raise not_found(record.id)
            raise RollbackReadyError(
                "OPERATION_IN_PROGRESS",
                "Another operation is already running for this analysis.",
                status_code=409,
                analysis_id=record.id,
            )
        return token

    def _load_artifact_reference(self, record: _Analysis) -> None:
        getter = getattr(self._repository, "get_artifact", None)
        reference = (
            getter(record.id, record.owner_clerk_user_id)
            if getter is not None
            else None
        )
        if reference is not None:
            record.artifact_object_name = reference.object_name
            record.artifact_generation = reference.generation
            record.artifact_expires_at = reference.expires_at

    def _ensure_bundle(self, record: _Analysis) -> None:
        if record.bundle is not None:
            return
        self._load_artifact_reference(record)
        if (
            record.artifact_expires_at is not None
            and record.artifact_expires_at <= datetime.now(UTC)
        ):
            self._delete_artifact(record)
            raise RollbackReadyError(
                "RAW_ARTIFACTS_DELETED",
                "The raw analysis artifacts are no longer available.",
                status_code=409,
                analysis_id=record.id,
            )
        if not record.artifact_object_name or not record.artifact_generation:
            raise RollbackReadyError(
                "RAW_ARTIFACTS_DELETED",
                "The raw analysis artifacts are no longer available.",
                status_code=409,
                analysis_id=record.id,
            )
        archive = self._artifact_store.get(
            record.artifact_object_name,
            record.artifact_generation,
        )
        record.bundle = load_project_bundle(
            archive,
            record.manifest.candidate_migration,
        )

    def _delete_artifact(
        self, record: _Analysis, *, mark_persisted: bool = True
    ) -> None:
        if record.artifact_object_name:
            self._artifact_store.delete(
                record.artifact_object_name,
                record.artifact_generation,
            )
        record.bundle = None
        record.artifact_object_name = None
        record.artifact_generation = None
        record.artifact_expires_at = None
        marker = getattr(self._repository, "mark_artifact_deleted", None)
        if mark_persisted and marker is not None:
            marker(record.id, record.owner_clerk_user_id)

    def _append_event(
        self,
        record: _Analysis,
        event_type: str,
        status: object,
        message: str,
        *,
        run_id: str | None = None,
        statement_index: int | None = None,
    ) -> None:
        with self._event_condition:
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
            self._event_condition.notify_all()


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


def _sanitize_insight_content(content: str, allowed_references: set[str]) -> str:
    safe_language = re.sub(
        r"\bsafe\s+to\s+deploy\b",
        "verified for human review",
        content,
        flags=re.IGNORECASE,
    )
    cited_uuids = set(
        re.findall(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            safe_language,
            flags=re.IGNORECASE,
        )
    )
    if cited_uuids - allowed_references:
        return (
            "The provider response cited evidence outside this analysis and was rejected. "
            "Review the deterministic findings and evidence dimensions directly."
        )
    return safe_language


def _status_for_verdict(verdict: Verdict) -> AnalysisStatus:
    return {
        Verdict.UNSAFE: AnalysisStatus.UNSAFE,
        Verdict.CONDITIONALLY_VERIFIED: AnalysisStatus.CONDITIONAL,
        Verdict.VERIFIED_FOR_REVIEW: AnalysisStatus.VERIFIED,
        Verdict.INSUFFICIENT_EVIDENCE: AnalysisStatus.ERROR,
        Verdict.ERROR: AnalysisStatus.ERROR,
    }[verdict]


def _input_limitations(bundle: ProjectBundle) -> list[str]:
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
    if bundle.manifest.fixture_source == "synthesized":
        limitations.append(
            "Fixtures were synthesized deterministically from the pre-migration schema; business semantics remain unverified."
        )
    if bundle.manifest.legacy_query_source == "synthesized":
        limitations.append(
            "Legacy queries were synthesized from the pre-migration schema rather than captured from an application workload."
        )
    return limitations


def _run_event_message(run: SimulationRun) -> str:
    if run.run_type == "NORMAL_APPLICATION":
        return "Candidate migration applied on the normal path." if run.status is EvidenceStatus.PASS else "Candidate migration failed on the normal path."
    if run.boundary is not None:
        return f"Interruption boundary {run.boundary} completed with {run.status}."
    return f"{run.run_type} completed with {run.status}."
