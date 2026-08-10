"""add user entitlement and share-state columns

Issue #391 / #484 auth-revalidation design, PR-1 (schema only): six nullable-ish
columns on ``users`` that a later PR's sweep will populate via
``plex_access_service.check_share``. Nothing in this revision or the app writes
or reads them yet -- no loop, no enforcement, no user-visible behavior change.

``upgrade`` uses plain ``op.add_column``/``op.create_index`` deliberately: all
six columns are additive (nullable, or ``NOT NULL DEFAULT 0``), which SQLite
supports as a true ``ALTER TABLE ADD COLUMN`` -- no table rebuild, so parent
rows of ``audit_log``/``media_requests``/``auth_sessions`` are never dropped
and recreated. (Batch mode would rebuild here: Alembic's SQLite impl forces a
move-and-copy whenever an added column carries a ``sa.text`` server default.)
``downgrade`` keeps batch mode because column *drops* are where older SQLite
needs the rebuild path.

Revision ID: c8198009583d
Revises: a41f9c7d20be
Create Date: 2026-08-09 23:13:26.038779
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8198009583d"
down_revision: str | None = "a41f9c7d20be"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("entitled_section_keys", sa.JSON(), nullable=True))
    op.add_column("users", sa.Column("entitlements_machine_id", sa.String(), nullable=True))
    op.add_column("users", sa.Column("share_state", sa.String(), nullable=True))
    op.add_column("users", sa.Column("share_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "share_check_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "users", sa.Column("share_check_failed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(op.f("ix_users_share_state"), "users", ["share_state"], unique=False)
    op.create_index(op.f("ix_users_share_checked_at"), "users", ["share_checked_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_users_share_checked_at"))
        batch_op.drop_index(batch_op.f("ix_users_share_state"))
        batch_op.drop_column("share_check_failed_at")
        batch_op.drop_column("share_check_failures")
        batch_op.drop_column("share_checked_at")
        batch_op.drop_column("share_state")
        batch_op.drop_column("entitlements_machine_id")
        batch_op.drop_column("entitled_section_keys")
