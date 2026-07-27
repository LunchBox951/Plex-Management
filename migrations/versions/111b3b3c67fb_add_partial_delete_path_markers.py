"""add durable incomplete-delete markers to request/season rows

Persists the "a destructive delete of this row's media was started and has not
been proven to have left the tree intact" fact on the eviction CLAIM ROW itself
(issues #482 and #485), replacing the process-memory set an earlier attempt used.

``shutil.rmtree`` is not atomic: it can unlink several children of a library tree
and then raise, leaving a directory that still ``os.stat``s fine but is missing
files. Eviction's crash-recovery pass reads "the path still exists" as "the media
is still watchable" and restores the row to ``available`` -- publishing gutted
media as fully playable, indefinitely. A crash, a cancelled sweep, or a process
restart mid-``rmtree`` produces the exact same on-disk shape with no in-band
result to classify at all, so no in-memory record can cover it.

``partial_delete_path`` is ARMED (set to the row's ``library_path``) in the same
commit as the eviction claim, BEFORE the delete runs, and DISARMED only once the
outcome proves the tree needs no further reclaiming -- fully removed, provably
untouched, or never started. Anything that ends WITHOUT that proof leaves it
armed, which is the honest default: recovery then routes the row through
retry/converge instead of restore. A fresh import placement clears it, so a
same-row re-import is never prejudged by the previous eviction's marker.

Nullable with no backfill, on both the movie (``media_requests``) and season
(``season_requests``) claim rows. ``NULL`` means "no outstanding delete", so every
pre-existing row keeps exactly today's behavior; an eviction interrupted by the
upgrade itself falls back to the pre-#482 stat-based recovery for that one row and
is re-armed correctly by its next claim.

Revision ID: 111b3b3c67fb
Revises: 45f1f9145e95
Create Date: 2026-07-27 11:38:16.548021
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "111b3b3c67fb"
down_revision: str | None = "45f1f9145e95"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("partial_delete_path", sa.String(), nullable=True))

    with op.batch_alter_table("season_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("partial_delete_path", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("season_requests", schema=None) as batch_op:
        batch_op.drop_column("partial_delete_path")

    with op.batch_alter_table("media_requests", schema=None) as batch_op:
        batch_op.drop_column("partial_delete_path")
