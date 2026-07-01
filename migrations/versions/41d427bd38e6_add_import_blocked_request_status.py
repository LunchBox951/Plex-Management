"""add import_blocked request status

Adds ``import_blocked`` to the active-request partial unique index so a request
whose download finished but failed to import keeps blocking a duplicate request
for the same media. The ``media_requests.status`` column itself needs no change:
it is a plain VARCHAR(21) (SQLAlchemy 2.0's ``native_enum=False`` creates no CHECK
constraint), and ``import_blocked`` (14 chars) fits the existing length.

Revision ID: 41d427bd38e6
Revises: f679b4c17194
Create Date: 2026-06-30 14:33:59.465628
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "41d427bd38e6"
down_revision: str | None = "f679b4c17194"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "uq_media_requests_active"
# Both import_blocked (download finished, import failed, retryable) and completed
# (imported, "Finalizing" before Plex confirms availability) are in-flight states
# that must keep blocking a duplicate request for the same media.
_NEW_PREDICATE = (
    "status IN ('pending', 'searching', 'no_acceptable_release', "
    "'downloading', 'import_blocked', 'completed')"
)
_OLD_PREDICATE = "status IN ('pending', 'searching', 'no_acceptable_release', 'downloading')"


def _recreate_active_index(predicate: str) -> None:
    op.drop_index(_INDEX, table_name="media_requests")
    op.create_index(
        _INDEX,
        "media_requests",
        ["tmdb_id", "media_type"],
        unique=True,
        sqlite_where=sa.text(predicate),
        postgresql_where=sa.text(predicate),
    )


def upgrade() -> None:
    _recreate_active_index(_NEW_PREDICATE)


def downgrade() -> None:
    _recreate_active_index(_OLD_PREDICATE)
