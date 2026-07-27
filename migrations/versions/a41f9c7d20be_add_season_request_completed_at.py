"""add season_requests.completed_at completion generation

Issue #494: the availability promotion CAS binds to the completion its Plex
answer described. Movies already persist that generation
(``media_requests.completed_at``); seasons had no mirror, so this adds one.

Nullable with NO backfill by design: the generation of a season completed
before this migration was never recorded, and inventing one (``created_at``, a
download's ``completed_at``) would fabricate a completion instant. ``NULL`` is
the honest value and is safe in both directions -- a snapshotted ``NULL``
matches only its own still-``NULL`` row (``IS NOT DISTINCT FROM``), and any
re-completion after this point stamps a value that no longer matches it.

Revision ID: a41f9c7d20be
Revises: 111b3b3c67fb
Create Date: 2026-07-27 16:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a41f9c7d20be"
down_revision: str | None = "111b3b3c67fb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("season_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("season_requests", schema=None) as batch_op:
        batch_op.drop_column("completed_at")
