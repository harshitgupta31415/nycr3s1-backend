from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AnalysisStatus(StrEnum):
    STAGED = "STAGED"
    VALIDATING = "VALIDATING"
    INVALID = "INVALID"
    ANALYZING = "ANALYZING"
    STATIC_ONLY = "STATIC_ONLY"
    SIMULATING = "SIMULATING"
    UNSAFE = "UNSAFE"
    CONDITIONAL = "CONDITIONAL"
    VERIFIED = "VERIFIED"
    PLANNING = "PLANNING"
    PLAN_REJECTED = "PLAN_REJECTED"
    VERIFYING_PLAN = "VERIFYING_PLAN"
    VERIFIED_PLAN = "VERIFIED_PLAN"
    ERROR = "ERROR"
    EXPIRED = "EXPIRED"


class Verdict(StrEnum):
    UNSAFE = "UNSAFE"
    CONDITIONALLY_VERIFIED = "CONDITIONALLY_VERIFIED"
    VERIFIED_FOR_REVIEW = "VERIFIED_FOR_REVIEW"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    ERROR = "ERROR"


class EvidenceLevel(StrEnum):
    STATIC_ANALYSIS_ONLY = "STATIC_ANALYSIS_ONLY"
    SANDBOX_SIMULATED = "SANDBOX_SIMULATED"


class EvidenceStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    NOT_TESTED = "NOT_TESTED"


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class EvidenceSource(StrEnum):
    STATIC = "STATIC"
    FIXTURE_EXECUTION = "FIXTURE_EXECUTION"
    LEGACY_REPLAY = "LEGACY_REPLAY"
    FAILURE_INJECTION = "FAILURE_INJECTION"
    HEURISTIC = "HEURISTIC"
    PLAN_VERIFICATION = "PLAN_VERIFICATION"


class RecoveryClassification(StrEnum):
    FULLY_REVERSIBLE = "FULLY_REVERSIBLE"
    SCHEMA_REVERSIBLE = "SCHEMA_REVERSIBLE"
    FORWARD_FIX_REQUIRED = "FORWARD_FIX_REQUIRED"
    EXTERNALLY_RECOVERABLE_ONLY = "EXTERNALLY_RECOVERABLE_ONLY"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class RetryOutcome(StrEnum):
    SUCCESSFUL = "SUCCESSFUL"
    IDEMPOTENT = "IDEMPOTENT"
    DUPLICATE_OR_CONFLICTING = "DUPLICATE_OR_CONFLICTING"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_ATTEMPTED = "NOT_ATTEMPTED"


class PlanState(StrEnum):
    UNVERIFIED_CANDIDATE = "UNVERIFIED_CANDIDATE"
    VERIFYING = "VERIFYING"
    VERIFIED_FOR_REVIEW = "VERIFIED_FOR_REVIEW"
    REJECTED = "REJECTED"


class ArtifactDigest(BaseModel):
    path: str
    sha256: str
    byte_count: int = Field(ge=0)


class MigrationArtifact(BaseModel):
    folder: str
    sha256: str
    byte_count: int = Field(ge=0)
    statement_count: int = Field(ge=0)
    candidate: bool = False


class ArtifactManifest(BaseModel):
    archive_sha256: str
    archive_byte_count: int = Field(ge=0)
    provider: str | None = None
    candidate_migration: str
    migrations: list[MigrationArtifact] = Field(default_factory=list)
    artifacts: list[ArtifactDigest] = Field(default_factory=list)
    has_schema: bool
    has_lockfile: bool
    has_seed: bool
    legacy_query_count: int = Field(ge=0)


class LegacyQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    sql: str = Field(min_length=1, max_length=50_000)
    expected_outcome: Literal["success", "failure"] = "success"
    expected_affected_rows: int | None = Field(default=None, ge=0)


class RiskFinding(BaseModel):
    id: str
    severity: Severity
    category: str
    statement_index: int | None = Field(default=None, ge=1)
    statement_shape: str | None = None
    affected_object: str | None = None
    reason: str
    evidence_source: EvidenceSource
    remediation_hint: str
    confirmed: bool = False


class EvidenceDimension(BaseModel):
    key: str
    label: str
    status: EvidenceStatus
    source: EvidenceSource
    summary: str


class StatementExecution(BaseModel):
    statement_index: int
    statement_shape: str
    status: EvidenceStatus
    duration_ms: int = Field(ge=0)
    affected_rows: int | None = None
    sanitized_error: str | None = None
    rolled_back: bool = False


class LegacyQueryResult(BaseModel):
    name: str
    query_hash: str
    status: EvidenceStatus
    duration_ms: int = Field(ge=0)
    affected_rows: int | None = None
    sanitized_error: str | None = None


class SnapshotSummary(BaseModel):
    schema_hash: str
    compatibility_schema_hash: str = ""
    table_count: int = Field(ge=0)
    row_counts: dict[str, int] = Field(default_factory=dict)
    content_hashes: dict[str, str] = Field(default_factory=dict)
    content_columns: dict[str, list[str]] = Field(default_factory=dict)


class RecoveryAssessment(BaseModel):
    classification: RecoveryClassification
    retry_outcome: RetryOutcome
    schema_matches_target: bool
    data_loss_possible: bool
    summary: str


class SimulationRun(BaseModel):
    id: str
    run_type: str
    boundary: int | None = None
    status: EvidenceStatus
    duration_ms: int = Field(ge=0)
    statements: list[StatementExecution] = Field(default_factory=list)
    snapshot: SnapshotSummary | None = None
    recovery: RecoveryAssessment | None = None
    sanitized_error: str | None = None


class TimelineEvent(BaseModel):
    sequence: int = Field(ge=1)
    occurred_at: datetime
    event_type: str
    status: str
    message: str
    run_id: str | None = None
    statement_index: int | None = None


class PlanPhase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=500)
    sql: list[str] = Field(default_factory=list, max_length=25)
    application_changes: list[str] = Field(default_factory=list, max_length=20)
    verification_sql: list[str] = Field(default_factory=list, max_length=20)
    rollback_guidance: str = Field(min_length=1, max_length=1000)


class GeneratedPlanPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=1000)
    assumptions: list[str] = Field(default_factory=list, max_length=20)
    phases: list[PlanPhase] = Field(min_length=1, max_length=8)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class RecoveryPlan(GeneratedPlanPayload):
    id: str
    analysis_id: str
    state: PlanState
    provider: str
    model: str
    prompt_template_version: str
    generated_at: datetime
    deterministic_fallback: bool = False


class VerificationResult(BaseModel):
    id: str
    plan_id: str
    status: EvidenceStatus
    verdict: Verdict
    dimensions: list[EvidenceDimension]
    completed_at: datetime
    sanitized_error: str | None = None


class AnalysisSummary(BaseModel):
    id: str
    status: AnalysisStatus
    evidence_level: EvidenceLevel
    verdict: Verdict
    provider: str | None
    candidate_migration: str
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    raw_artifacts_available: bool = False
    manifest: ArtifactManifest
    findings: list[RiskFinding] = Field(default_factory=list)
    evidence: list[EvidenceDimension] = Field(default_factory=list)
    simulation_runs: list[SimulationRun] = Field(default_factory=list)
    legacy_query_results: list[LegacyQueryResult] = Field(default_factory=list)
    plans: list[RecoveryPlan] = Field(default_factory=list)
    verification_results: list[VerificationResult] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class EvidenceReport(BaseModel):
    report_version: str = "1.0"
    safety_promise: str = "Verified for human review; never safe to deploy."
    analysis: AnalysisSummary
    timeline: list[TimelineEvent]
    generated_at: datetime
    production_execution_performed: bool = False


class ErrorDetail(BaseModel):
    code: str
    message: str
    analysis_id: str | None = None
    request_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(BaseModel):
    error: ErrorDetail
