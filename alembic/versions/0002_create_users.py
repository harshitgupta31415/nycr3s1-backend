"""Create the application users table.

Revision ID: 0002_create_users
Revises: 0001_create_app_schema
Create Date: 2026-08-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_create_users"
down_revision: str | None = "0001_create_app_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("clerk_user_id", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("first_name", sa.String(length=100), nullable=True),
        sa.Column("last_name", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("clerk_user_id", name="uq_users_clerk_user_id"),
        schema="app",
    )
    op.create_index(
        "ix_app_users_clerk_user_id",
        "users",
        ["clerk_user_id"],
        unique=False,
        schema="app",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_app_users_clerk_user_id",
        table_name="users",
        schema="app",
    )
    op.drop_table("users", schema="app")
