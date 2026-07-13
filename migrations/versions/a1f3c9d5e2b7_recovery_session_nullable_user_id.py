"""recovery session: nullable auth_sessions.user_id

Lets a valid ``X-Api-Key`` be exchanged for the SAME HTTP-only session cookie the
Plex sign-in flow issues (CodeQL #263). The recovery key is an admin-authority
credential with no Plex identity, so a session it mints has no owning user —
``auth_sessions.user_id`` must therefore be nullable. A Plex sign-in session still
always carries its ``users.id``.

Revision ID: a1f3c9d5e2b7
Revises: 3d28d05107aa
Create Date: 2026-07-13 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1f3c9d5e2b7"
down_revision: str | None = "3d28d05107aa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("auth_sessions", schema=None) as batch_op:
        batch_op.alter_column(
            "user_id",
            existing_type=sa.Integer(),
            nullable=True,
        )


def downgrade() -> None:
    # Any recovery session (NULL user_id) minted under the new behaviour would
    # violate the restored NOT NULL, so purge those rows first. They are
    # revocable browser sessions, not durable data — dropping them merely forces
    # a re-exchange of the recovery key, never a lockout (Plex-session rows are
    # untouched because they always carry a user_id).
    op.execute(sa.text("DELETE FROM auth_sessions WHERE user_id IS NULL"))
    with op.batch_alter_table("auth_sessions", schema=None) as batch_op:
        batch_op.alter_column(
            "user_id",
            existing_type=sa.Integer(),
            nullable=False,
        )
