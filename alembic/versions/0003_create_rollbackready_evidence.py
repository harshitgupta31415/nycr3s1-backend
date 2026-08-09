"""Create sanitized RollbackReady evidence tables.

Revision ID: 0003_rollbackready_evidence
Revises: 0002_create_users
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_rollbackready_evidence"
down_revision: str | None = "0002_create_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rollbackready_analyses",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("evidence_level", sa.String(32), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("candidate_migration", sa.String(200), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("limitations", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_rollbackready_analyses"),
        schema="app",
    )
    op.create_index("ix_app_rollbackready_analyses_status", "rollbackready_analyses", ["status"], schema="app")
    op.create_index("ix_app_rollbackready_analyses_verdict", "rollbackready_analyses", ["verdict"], schema="app")
    op.create_index("ix_app_rollbackready_analyses_expires_at", "rollbackready_analyses", ["expires_at"], schema="app")

    op.create_table(
        "rollbackready_findings",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("analysis_id", sa.String(36), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("category", sa.String(80), nullable=False),
        sa.Column("statement_index", sa.Integer(), nullable=True),
        sa.Column("statement_shape", sa.Text(), nullable=True),
        sa.Column("affected_object", sa.String(300), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_source", sa.String(32), nullable=False),
        sa.Column("remediation_hint", sa.Text(), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["app.rollbackready_analyses.id"], name="fk_rr_findings_analysis", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_rollbackready_findings"),
        schema="app",
    )
    op.create_index("ix_rr_findings_analysis_severity", "rollbackready_findings", ["analysis_id", "severity"], schema="app")

    op.create_table(
        "rollbackready_simulation_runs",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("analysis_id", sa.String(36), nullable=False),
        sa.Column("run_type", sa.String(40), nullable=False),
        sa.Column("boundary", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=True),
        sa.Column("recovery", sa.JSON(), nullable=True),
        sa.Column("sanitized_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["analysis_id"], ["app.rollbackready_analyses.id"], name="fk_rr_runs_analysis", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_rollbackready_simulation_runs"),
        schema="app",
    )

    op.create_table(
        "rollbackready_legacy_query_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_id", sa.String(36), nullable=False),
        sa.Column("query_name", sa.String(120), nullable=False),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("affected_rows", sa.Integer(), nullable=True),
        sa.Column("sanitized_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["analysis_id"], ["app.rollbackready_analyses.id"], name="fk_rr_legacy_analysis", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_rollbackready_legacy_query_results"),
        schema="app",
    )

    op.create_table(
        "rollbackready_timeline_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("analysis_id", sa.String(36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=True),
        sa.Column("statement_index", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["analysis_id"], ["app.rollbackready_analyses.id"], name="fk_rr_timeline_analysis", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_rollbackready_timeline_events"),
        schema="app",
    )
    op.create_index("uq_rr_timeline_analysis_sequence", "rollbackready_timeline_events", ["analysis_id", "sequence"], unique=True, schema="app")

    op.create_table(
        "rollbackready_recovery_plans",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("analysis_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("prompt_template_version", sa.String(80), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plan", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["analysis_id"], ["app.rollbackready_analyses.id"], name="fk_rr_plans_analysis", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_rollbackready_recovery_plans"),
        schema="app",
    )

    op.create_table(
        "rollbackready_verification_results",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("plan_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("verdict", sa.String(32), nullable=False),
        sa.Column("dimensions", sa.JSON(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sanitized_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], ["app.rollbackready_recovery_plans.id"], name="fk_rr_verification_plan", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_rollbackready_verification_results"),
        sa.UniqueConstraint("plan_id", name="uq_rr_verification_plan_id"),
        schema="app",
    )


def downgrade() -> None:
    op.drop_table("rollbackready_verification_results", schema="app")
    op.drop_table("rollbackready_recovery_plans", schema="app")
    op.drop_index("uq_rr_timeline_analysis_sequence", table_name="rollbackready_timeline_events", schema="app")
    op.drop_table("rollbackready_timeline_events", schema="app")
    op.drop_table("rollbackready_legacy_query_results", schema="app")
    op.drop_table("rollbackready_simulation_runs", schema="app")
    op.drop_index("ix_rr_findings_analysis_severity", table_name="rollbackready_findings", schema="app")
    op.drop_table("rollbackready_findings", schema="app")
    op.drop_index("ix_app_rollbackready_analyses_expires_at", table_name="rollbackready_analyses", schema="app")
    op.drop_index("ix_app_rollbackready_analyses_verdict", table_name="rollbackready_analyses", schema="app")
    op.drop_index("ix_app_rollbackready_analyses_status", table_name="rollbackready_analyses", schema="app")
    op.drop_table("rollbackready_analyses", schema="app")
