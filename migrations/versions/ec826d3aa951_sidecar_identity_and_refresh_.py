"""sidecar identity and refresh observability

ADR-0025 stage 0 (issue #299): the app-side "expand" step that lets a future
updater sidecar report its OWN running image identity and lets the app persist a
durable self-refresh outcome. Every column is NULLABLE and expand-only -- an
N-1 app simply ignores them, and nothing in this release writes them from the
sidecar side, so the change is backward-compatible in both directions (the C7
forward-compatibility rule). No CHECK constraint is added, matching the
existing string-column discipline on ``update_coordinator_state``.

Revision ID: ec826d3aa951
Revises: e91b3f7a5d24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ec826d3aa951"
down_revision: str | None = "e91b3f7a5d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("update_coordinator_state", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("updater_observed_build", sa.String(length=400), nullable=True)
        )
        batch_op.add_column(
            sa.Column("updater_observed_digest", sa.String(length=400), nullable=True)
        )
        batch_op.add_column(sa.Column("last_refresh_result", sa.String(length=32), nullable=True))
        batch_op.add_column(
            sa.Column("last_refresh_detail_code", sa.String(length=128), nullable=True)
        )
        batch_op.add_column(
            sa.Column("last_refresh_from_build", sa.String(length=400), nullable=True)
        )
        batch_op.add_column(
            sa.Column("last_refresh_to_build", sa.String(length=400), nullable=True)
        )
        batch_op.add_column(sa.Column("last_refresh_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("update_coordinator_state", schema=None) as batch_op:
        batch_op.drop_column("last_refresh_at")
        batch_op.drop_column("last_refresh_to_build")
        batch_op.drop_column("last_refresh_from_build")
        batch_op.drop_column("last_refresh_detail_code")
        batch_op.drop_column("last_refresh_result")
        batch_op.drop_column("updater_observed_digest")
        batch_op.drop_column("updater_observed_build")
