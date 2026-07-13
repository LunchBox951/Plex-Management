"""add request subscribers

Revision ID: 7f4a2c91d8e3
Revises: 3d28d05107aa
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7f4a2c91d8e3"
down_revision: str | None = "3d28d05107aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "request_subscribers",
        sa.Column("request_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("subscribed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["media_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("request_id", "user_id"),
    )
    op.create_index(
        "ix_request_subscribers_user_id", "request_subscribers", ["user_id"], unique=False
    )
    op.execute(
        sa.text(
            "INSERT INTO request_subscribers (request_id, user_id) "
            "SELECT id, user_id FROM media_requests WHERE user_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_request_subscribers_user_id", table_name="request_subscribers")
    op.drop_table("request_subscribers")
