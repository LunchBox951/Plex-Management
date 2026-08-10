"""add users.entitlements_captured_at

Issue #484 PR-3 (Codex review): one nullable timestamp recording when the
section read behind ``entitled_section_keys`` COMPLETED, so
``plex_access_service.store_entitlements`` can refuse a capture older than the
one already stored. Two captures for the same user can be in flight at once
(the share sweep's, and a sign-in's detached task); when both used the same
credential and the same anchor, the ciphertext/anchor guards on that write hold
for either ordering, so whichever finished its WRITE last won even if it had
READ first -- leaving a stale section set as the snapshot PR-5 enforcement
would act on.

``NULL`` (every row written before this revision) is treated as "older than
anything", so the next capture always lands and no backfill is needed.

Plain ``op.add_column`` deliberately, exactly as the sibling revision
``c8198009583d`` explains: a nullable added column is a true SQLite
``ALTER TABLE ADD COLUMN`` with no table rebuild, so the parent rows of
``audit_log``/``media_requests``/``auth_sessions`` are never dropped and
recreated. ``downgrade`` keeps batch mode because column *drops* are where
older SQLite needs the move-and-copy path.

Revision ID: c5cf0a125f5f
Revises: c8198009583d
Create Date: 2026-08-10 15:14:17.697676
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5cf0a125f5f"
down_revision: str | None = "c8198009583d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("entitlements_captured_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("entitlements_captured_at")
