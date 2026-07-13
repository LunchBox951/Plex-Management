"""add container update coordination and maintenance leases

This is an expand-only migration. The immediately previous application release
does not know about either table and continues to operate unchanged if a failed
container update restores its image. Automatic rollback never runs downgrade.

Revision ID: a2c4e6f8b0d1
Revises: 3d28d05107aa
Create Date: 2026-07-12 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2c4e6f8b0d1"
down_revision: str | None = "3d28d05107aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "update_coordinator_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "requested_action", sa.String(length=32), server_default=sa.text("'none'"), nullable=False
        ),
        sa.Column("action_generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "acknowledged_generation", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "phase", sa.String(length=32), server_default=sa.text("'idle'"), nullable=False
        ),
        sa.Column("current_build", sa.String(length=255), nullable=True),
        sa.Column("current_digest", sa.String(length=255), nullable=True),
        sa.Column("available_build", sa.String(length=255), nullable=True),
        sa.Column("available_digest", sa.String(length=255), nullable=True),
        sa.Column("updater_last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result", sa.String(length=32), nullable=True),
        sa.Column("last_error_code", sa.String(length=128), nullable=True),
        sa.Column("last_from_build", sa.String(length=255), nullable=True),
        sa.Column("last_to_build", sa.String(length=255), nullable=True),
        sa.Column("last_outcome_token_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("id = 1", name="ck_update_coordinator_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_update_coordinator_state_updater_last_seen_at",
        "update_coordinator_state",
        ["updater_last_seen_at"],
        unique=False,
    )
    op.bulk_insert(
        sa.table(
            "update_coordinator_state",
            sa.column("id", sa.Integer()),
            sa.column("requested_action", sa.String()),
            sa.column("action_generation", sa.Integer()),
            sa.column("acknowledged_generation", sa.Integer()),
            sa.column("phase", sa.String()),
        ),
        [
            {
                "id": 1,
                "requested_action": "none",
                "action_generation": 0,
                "acknowledged_generation": 0,
                "phase": "idle",
            }
        ],
    )

    op.create_table(
        "maintenance_leases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=True),
        sa.Column("action_generation", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_maintenance_leases_expires_at", "maintenance_leases", ["expires_at"], unique=False
    )
    op.create_index("ix_maintenance_leases_kind", "maintenance_leases", ["kind"], unique=False)
    op.create_index(
        "ix_maintenance_leases_kind_expires",
        "maintenance_leases",
        ["kind", "expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_maintenance_leases_token_hash",
        "maintenance_leases",
        ["token_hash"],
        unique=True,
    )
    op.create_index(
        "uq_maintenance_leases_drain",
        "maintenance_leases",
        ["kind"],
        unique=True,
        sqlite_where=sa.text("kind = 'drain'"),
        postgresql_where=sa.text("kind = 'drain'"),
    )


def downgrade() -> None:
    op.drop_index("uq_maintenance_leases_drain", table_name="maintenance_leases")
    op.drop_index("ix_maintenance_leases_token_hash", table_name="maintenance_leases")
    op.drop_index("ix_maintenance_leases_kind_expires", table_name="maintenance_leases")
    op.drop_index("ix_maintenance_leases_kind", table_name="maintenance_leases")
    op.drop_index("ix_maintenance_leases_expires_at", table_name="maintenance_leases")
    op.drop_table("maintenance_leases")
    op.drop_index(
        "ix_update_coordinator_state_updater_last_seen_at",
        table_name="update_coordinator_state",
    )
    op.drop_table("update_coordinator_state")
