"""Persist per-statement simulation evidence.

Revision ID: 0005_add_statement_evidence
Revises: 0004_add_analysis_ownership
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005_add_statement_evidence"
down_revision: str | None = "0004_add_analysis_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "rollbackready_simulation_runs",
        sa.Column(
            "statements",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        schema="app",
    )
    op.alter_column(
        "rollbackready_simulation_runs",
        "statements",
        server_default=None,
        schema="app",
    )


def downgrade() -> None:
    op.drop_column(
        "rollbackready_simulation_runs",
        "statements",
        schema="app",
    )
