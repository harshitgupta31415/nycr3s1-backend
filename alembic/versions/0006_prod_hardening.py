"""Add durable artifact, concurrency, idempotency, and quota state.

Revision ID: 0006_prod_hardening
Revises: 0006_add_ai_insights
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_prod_hardening"
down_revision: str | None = "0006_add_ai_insights"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = (
        sa.Column("artifact_object_name", sa.String(512)),
        sa.Column("artifact_generation", sa.String(64)),
        sa.Column("artifact_state", sa.String(32), nullable=False, server_default="UNAVAILABLE"),
        sa.Column("artifact_expires_at", sa.DateTime(timezone=True)),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active_operation", sa.String(32)),
        sa.Column("active_operation_token", sa.String(64)),
        sa.Column("operation_started_at", sa.DateTime(timezone=True)),
    )
    for column in columns:
        op.add_column("rollbackready_analyses", column, schema="app")
    op.create_index(
        "ix_app_rr_analyses_active_operation_token",
        "rollbackready_analyses",
        ["active_operation_token"],
        schema="app",
    )
    op.create_table(
        "rollbackready_idempotency",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("owner_clerk_user_id", sa.String(255), nullable=False),
        sa.Column("operation", sa.String(100), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("analysis_id", sa.String(36)),
        sa.Column("response", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "owner_clerk_user_id", "operation", "key_hash",
            name="uq_rr_idempotency_owner_operation_key",
        ),
        schema="app",
    )
    op.create_index("ix_app_rr_idempotency_expires_at", "rollbackready_idempotency", ["expires_at"], schema="app")
    op.create_table(
        "rollbackready_rate_limits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("scope_hash", sa.String(64), nullable=False),
        sa.Column("bucket", sa.String(32), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "scope_hash", "bucket", "window_start",
            name="uq_rr_rate_limit_scope_bucket_window",
        ),
        schema="app",
    )
    op.create_index("ix_app_rr_rate_limits_expires_at", "rollbackready_rate_limits", ["expires_at"], schema="app")


def downgrade() -> None:
    op.drop_index("ix_app_rr_rate_limits_expires_at", table_name="rollbackready_rate_limits", schema="app")
    op.drop_table("rollbackready_rate_limits", schema="app")
    op.drop_index("ix_app_rr_idempotency_expires_at", table_name="rollbackready_idempotency", schema="app")
    op.drop_table("rollbackready_idempotency", schema="app")
    op.drop_index("ix_app_rr_analyses_active_operation_token", table_name="rollbackready_analyses", schema="app")
    for column in (
        "operation_started_at", "active_operation_token", "active_operation",
        "row_version", "artifact_expires_at", "artifact_state",
        "artifact_generation", "artifact_object_name",
    ):
        op.drop_column("rollbackready_analyses", column, schema="app")
