"""auto-grab: search_attempts + next_search_at scheduling columns

Adds the auto-grab worker's per-scope backoff bookkeeping (ADR-0013) to BOTH
``media_requests`` (movie scope) and ``season_requests`` (TV scope -- a grab is
always per-season):

* ``search_attempts INT NOT NULL DEFAULT 0`` -- how many auto-grab searches have
  returned nothing acceptable so far; drives the escalating backoff ladder.
* ``next_search_at`` (nullable, tz-aware) -- the earliest instant the worker may
  search this scope again; ``NULL`` means "due now" (a freshly created request is
  searched on the next tick).

``server_default=sa.text("0")`` gives every EXISTING row a concrete
``search_attempts`` so the worker treats a pre-migration pending request as
never-searched (backoff starts fresh); ``next_search_at`` defaults to ``NULL`` so
those rows are immediately due.

Revision ID: 088a027cb4ec
Revises: 6c7fca1436d8
Create Date: 2026-07-01 23:32:26.655169
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "088a027cb4ec"
down_revision: str | None = "6c7fca1436d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_requests", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "search_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
            )
        )
        batch_op.add_column(sa.Column("next_search_at", sa.DateTime(timezone=True), nullable=True))

    with op.batch_alter_table("season_requests", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "search_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False
            )
        )
        batch_op.add_column(sa.Column("next_search_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("season_requests", schema=None) as batch_op:
        batch_op.drop_column("next_search_at")
        batch_op.drop_column("search_attempts")

    with op.batch_alter_table("media_requests", schema=None) as batch_op:
        batch_op.drop_column("next_search_at")
        batch_op.drop_column("search_attempts")
