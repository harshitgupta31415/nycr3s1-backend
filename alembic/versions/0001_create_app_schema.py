"""Create the application schema.

Revision ID: 0001_create_app_schema
Revises:
Create Date: 2026-08-08
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_create_app_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS app")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS app RESTRICT")
