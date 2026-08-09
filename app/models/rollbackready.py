from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class AnalysisRecord(Base):
    __tablename__ = "rollbackready_analyses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_clerk_user_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    evidence_level: Mapped[str] = mapped_column(String(32), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32))
    candidate_migration: Mapped[str] = mapped_column(String(200), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    limitations: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    artifact_object_name: Mapped[str | None] = mapped_column(String(512))
    artifact_generation: Mapped[str | None] = mapped_column(String(64))
    artifact_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="UNAVAILABLE"
    )
    artifact_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active_operation: Mapped[str | None] = mapped_column(String(32))
    active_operation_token: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    findings: Mapped[list[FindingRecord]] = relationship(
        cascade="all, delete-orphan", back_populates="analysis"
    )
    simulation_runs: Mapped[list[SimulationRunRecord]] = relationship(
        cascade="all, delete-orphan", back_populates="analysis"
    )
    legacy_results: Mapped[list[LegacyQueryResultRecord]] = relationship(
        cascade="all, delete-orphan", back_populates="analysis"
    )
    timeline_events: Mapped[list[TimelineEventRecord]] = relationship(
        cascade="all, delete-orphan", back_populates="analysis"
    )
    recovery_plans: Mapped[list[RecoveryPlanRecord]] = relationship(
        cascade="all, delete-orphan", back_populates="analysis"
    )


class FindingRecord(Base):
    __tablename__ = "rollbackready_findings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("app.rollbackready_analyses.id", ondelete="CASCADE"), nullable=False
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    statement_index: Mapped[int | None] = mapped_column(Integer)
    statement_shape: Mapped[str | None] = mapped_column(Text)
    affected_object: Mapped[str | None] = mapped_column(String(300))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_source: Mapped[str] = mapped_column(String(32), nullable=False)
    remediation_hint: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed: Mapped[bool] = mapped_column(nullable=False, default=False)

    analysis: Mapped[AnalysisRecord] = relationship(back_populates="findings")

    __table_args__ = (
        Index("ix_rr_findings_analysis_severity", "analysis_id", "severity"),
    )


class SimulationRunRecord(Base):
    __tablename__ = "rollbackready_simulation_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("app.rollbackready_analyses.id", ondelete="CASCADE"), nullable=False
    )
    run_type: Mapped[str] = mapped_column(String(40), nullable=False)
    boundary: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    statements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    recovery: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    sanitized_error: Mapped[str | None] = mapped_column(Text)

    analysis: Mapped[AnalysisRecord] = relationship(back_populates="simulation_runs")


class LegacyQueryResultRecord(Base):
    __tablename__ = "rollbackready_legacy_query_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("app.rollbackready_analyses.id", ondelete="CASCADE"), nullable=False
    )
    query_name: Mapped[str] = mapped_column(String(120), nullable=False)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    affected_rows: Mapped[int | None] = mapped_column(Integer)
    sanitized_error: Mapped[str | None] = mapped_column(Text)

    analysis: Mapped[AnalysisRecord] = relationship(back_populates="legacy_results")


class TimelineEventRecord(Base):
    __tablename__ = "rollbackready_timeline_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("app.rollbackready_analyses.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(36))
    statement_index: Mapped[int | None] = mapped_column(Integer)

    analysis: Mapped[AnalysisRecord] = relationship(back_populates="timeline_events")

    __table_args__ = (
        Index("uq_rr_timeline_analysis_sequence", "analysis_id", "sequence", unique=True),
    )


class RecoveryPlanRecord(Base):
    __tablename__ = "rollbackready_recovery_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    analysis_id: Mapped[str] = mapped_column(
        ForeignKey("app.rollbackready_analyses.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_template_version: Mapped[str] = mapped_column(String(80), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    analysis: Mapped[AnalysisRecord] = relationship(back_populates="recovery_plans")
    verification: Mapped[VerificationResultRecord | None] = relationship(
        cascade="all, delete-orphan", back_populates="plan", uselist=False
    )


class VerificationResultRecord(Base):
    __tablename__ = "rollbackready_verification_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("app.rollbackready_recovery_plans.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    dimensions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sanitized_error: Mapped[str | None] = mapped_column(Text)

    plan: Mapped[RecoveryPlanRecord] = relationship(back_populates="verification")


class IdempotencyRecord(Base):
    __tablename__ = "rollbackready_idempotency"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner_clerk_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str] = mapped_column(String(100), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    analysis_id: Mapped[str | None] = mapped_column(String(36))
    response: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint(
            "owner_clerk_user_id",
            "operation",
            "key_hash",
            name="uq_rr_idempotency_owner_operation_key",
        ),
    )


class RateLimitRecord(Base):
    __tablename__ = "rollbackready_rate_limits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket: Mapped[str] = mapped_column(String(32), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint(
            "scope_hash",
            "bucket",
            "window_start",
            name="uq_rr_rate_limit_scope_bucket_window",
        ),
    )
