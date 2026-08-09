"""Persist cached, sanitized AI insights.

Revision ID: 0006_add_ai_insights
Revises: 0005_add_statement_evidence
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_add_ai_insights"
down_revision: str | None = "0005_add_statement_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rollbackready_analyses",
        sa.Column(
            "ai_insights",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        schema="app",
    )
    op.alter_column(
        "rollbackready_analyses",
        "ai_insights",
        server_default=None,
        schema="app",
    )


def downgrade() -> None:
    op.drop_column("rollbackready_analyses", "ai_insights", schema="app")
