"""widen update coordinator digest and build columns

Docker reports a pulled image as ``<repository>@sha256:<64 hex>`` — the accepted
image reference (repo:tag, up to 255 chars) plus a fixed 72-char digest suffix.
The original ``String(255)`` digest/build columns therefore reject a valid
long private-registry RepoDigest (327 chars), leaving a check/install outcome
stuck at 422 with the sidecar retrying forever. Widen to ``String(400)`` so the
outcome round-trips (see issue #298).

Expand-only: N-1 continues to read/write the same rows unchanged. On SQLite,
which does not enforce VARCHAR length, the batch recreate is a semantic no-op;
on PostgreSQL it issues ``ALTER COLUMN ... TYPE VARCHAR(400)``. Both are safe.

Revision ID: 9db7096d4e6c
Revises: a2c4e6f8b0d1
Create Date: 2026-07-13 17:51:50.783397
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9db7096d4e6c"
down_revision: str | None = "a2c4e6f8b0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WIDENED_COLUMNS = (
    "current_build",
    "current_digest",
    "available_build",
    "available_digest",
    "last_from_build",
    "last_to_build",
)


def upgrade() -> None:
    with op.batch_alter_table("update_coordinator_state", schema=None) as batch_op:
        for column in _WIDENED_COLUMNS:
            batch_op.alter_column(
                column,
                existing_type=sa.String(length=255),
                type_=sa.String(length=400),
                existing_nullable=True,
            )


def downgrade() -> None:
    with op.batch_alter_table("update_coordinator_state", schema=None) as batch_op:
        for column in _WIDENED_COLUMNS:
            batch_op.alter_column(
                column,
                existing_type=sa.String(length=400),
                type_=sa.String(length=255),
                existing_nullable=True,
            )
