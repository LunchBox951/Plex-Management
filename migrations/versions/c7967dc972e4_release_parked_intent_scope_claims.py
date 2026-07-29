"""release parked intent scope claims

Revision ID: c7967dc972e4
Revises: 5f2d9a8c4b71
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7967dc972e4"
down_revision: str | None = "5f2d9a8c4b71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("download_add_intent_scopes") as batch_op:
        batch_op.add_column(sa.Column("active_scope_key", sa.String(), nullable=True))
        batch_op.drop_constraint("uq_download_add_intent_scopes_title_scope", type_="unique")
        batch_op.create_unique_constraint(
            "uq_download_add_intent_scopes_active_title_scope",
            ["tmdb_id", "media_type", "active_scope_key"],
        )
    op.execute(
        "UPDATE download_add_intent_scopes "
        "SET active_scope_key = scope_key "
        "WHERE intent_id IN ("
        "SELECT id FROM download_add_intents WHERE state IN ('prepared', 'cancel_requested')"
        ")"
    )


def downgrade() -> None:
    # The predecessor schema cannot represent any released scope claim. Remove
    # every NULL active key (not merely parked state): cancellation can transition
    # a released claim to cleanup state while a replacement owns the scope.
    op.execute("DELETE FROM download_add_intent_scopes WHERE active_scope_key IS NULL")
    op.execute(
        "DELETE FROM download_add_intents WHERE id NOT IN "
        "(SELECT DISTINCT intent_id FROM download_add_intent_scopes)"
    )
    with op.batch_alter_table("download_add_intent_scopes") as batch_op:
        batch_op.drop_constraint("uq_download_add_intent_scopes_active_title_scope", type_="unique")
        batch_op.create_unique_constraint(
            "uq_download_add_intent_scopes_title_scope",
            ["tmdb_id", "media_type", "scope_key"],
        )
        batch_op.drop_column("active_scope_key")
