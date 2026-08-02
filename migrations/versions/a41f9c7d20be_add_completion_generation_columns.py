"""add completion generation counters (+ season completed_at)

Issue #494: the availability promotion CAS binds to the completion its Plex
answer described, so it needs a value that identifies ONE completion.

``completion_generation`` (on both request levels) is that value: a counter
bumped in SQL on every entry into ``completed`` and never reset. A timestamp
cannot serve -- two completions can share a reading under a coarse-resolution,
frozen, or backward-adjusted clock, and a stale CAS bound to that reading would
promote the replacement, which is the bug itself.
``season_requests.completed_at`` is added alongside as the per-season "when
Finalizing began" metadata that movies already had.

All columns are nullable with NO backfill by design: the generation of a request
completed before this migration was never recorded, and inventing one would
fabricate history. ``NULL`` is the honest value and is safe in both directions --
a snapshotted ``NULL`` matches only a row that is still ``NULL`` (``IS NOT
DISTINCT FROM``), and the first bump after the upgrade lands on 1, which no
earlier snapshot can match.

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
    with op.batch_alter_table("media_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("completion_generation", sa.Integer(), nullable=True))
    with op.batch_alter_table("season_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("completion_generation", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("season_requests", schema=None) as batch_op:
        batch_op.drop_column("completion_generation")
        batch_op.drop_column("completed_at")
    with op.batch_alter_table("media_requests", schema=None) as batch_op:
        batch_op.drop_column("completion_generation")
