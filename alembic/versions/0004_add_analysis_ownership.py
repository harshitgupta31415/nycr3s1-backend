"""Add Clerk ownership to RollbackReady analyses.

Revision ID: 0004_add_analysis_ownership
Revises: 0003_rollbackready_evidence
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_add_analysis_ownership"
down_revision: str | None = "0003_rollbackready_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_OWNER = "__legacy_unowned__"


def upgrade() -> None:
    op.add_column(
        "rollbackready_analyses",
        sa.Column(
            "owner_clerk_user_id",
            sa.String(length=255),
            nullable=False,
            server_default=sa.text(f"'{LEGACY_OWNER}'"),
        ),
        schema="app",
    )
    op.alter_column(
        "rollbackready_analyses",
        "owner_clerk_user_id",
        server_default=None,
        schema="app",
    )
    op.create_index(
        "ix_app_rollbackready_analyses_owner_clerk_user_id",
        "rollbackready_analyses",
        ["owner_clerk_user_id"],
        unique=False,
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_app_rollbackready_analyses_owner_clerk_user_id",
        table_name="rollbackready_analyses",
        schema="app",
    )
    op.drop_column(
        "rollbackready_analyses",
        "owner_clerk_user_id",
        schema="app",
    )
