"""add watchlist_items tmdb_media index

``watchlist_service.is_watchlisted`` looks a title up by ``(tmdb_id,
media_type)`` with no ``user_id`` predicate (any user's watchlist protects a
title from eviction), but the table's only key is the ``user_id``-first
composite primary key, which cannot serve that leading-column-omitting lookup.
Eviction runs it once per candidate during assembly and once per selected
candidate in the pre-claim re-read, so a disk-pressure sweep drove up to 2N full
scans of ``watchlist_items``. This secondary index gives that query a seek path.

Expand-only: adds an index, no data change; safe to leave in place if a prior
release image is restored.

Revision ID: 516a74ff0b40
Revises: a2c4e6f8b0d1
Create Date: 2026-07-13 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "516a74ff0b40"
down_revision: str | None = "9db7096d4e6c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_watchlist_items_tmdb_media",
        "watchlist_items",
        ["tmdb_id", "media_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_watchlist_items_tmdb_media", table_name="watchlist_items")
