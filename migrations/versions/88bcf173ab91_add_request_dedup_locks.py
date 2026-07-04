"""add request dedup locks

Revision ID: 88bcf173ab91
Revises: 088a027cb4ec
Create Date: 2026-07-01 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "88bcf173ab91"
down_revision: str | None = "088a027cb4ec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "request_dedup_locks",
        sa.Column("tmdb_id", sa.Integer(), nullable=False),
        sa.Column(
            "media_type",
            sa.Enum("movie", "tv", name="mediatype", native_enum=False),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tmdb_id", "media_type"),
    )


def downgrade() -> None:
    op.drop_table("request_dedup_locks")
