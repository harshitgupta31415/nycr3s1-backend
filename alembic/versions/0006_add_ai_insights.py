"""Compatibility placeholder for the earlier AI-insights migration.

Revision ID: 0006_add_ai_insights
Revises: 0005_add_statement_evidence
Create Date: 2026-08-09
"""

from collections.abc import Sequence

revision: str = "0006_add_ai_insights"
down_revision: str | None = "0005_add_statement_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
