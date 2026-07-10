"""add stalled download history event type

Issue #165's minimal fixed-cooldown self-heal writes a ``DownloadHistoryEvent
.stalled`` row when the reconcile loop auto mark-fails a download stuck in
metadata-fetching or with dead/frozen progress. ``download_history.event_type``
is a portable non-native enum (``native_enum=False, create_constraint=True``),
and ``b7e2d4f6c8a1`` already gave it a real CHECK constraint
(``ck_download_history_event_type_enum``) enumerating every value at the time --
so, unlike ``evicted``/``reported``/``cancelled`` (added before that hardening
migration, needing no migration of their own), ``stalled`` DOES need one: a
migrated database's constraint would otherwise reject the new value outright.

Revision ID: 26bc01829ae1
Revises: bfaa63130ee7
Create Date: 2026-07-07 21:58:55.593970
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "26bc01829ae1"
down_revision: str | None = "bfaa63130ee7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_download_history_event_type_enum"
_OLD_VALUES = (
    "grabbed",
    "import_started",
    "imported",
    "failed",
    "evicted",
    "reported",
    "cancelled",
)
_NEW_VALUES = (*_OLD_VALUES, "stalled")


def _check_sql(values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{value}'" for value in values)
    return f"event_type IN ({quoted})"


def _raise_on_stalled_rows() -> None:
    """Guard the downgrade: it re-narrows the CHECK to the pre-#165 value set, so
    any already-persisted ``stalled`` row would violate it immediately. Mirrors
    ``b7e2d4f6c8a1``'s ``_raise_on_invalid_enum_values`` -- a clear ``RuntimeError``
    beats an opaque CHECK-violation from the database.
    """
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT COUNT(*) AS invalid_count FROM download_history "
                "WHERE event_type = 'stalled'"
            )
        )
        .mappings()
        .one()
    )
    if rows["invalid_count"]:
        raise RuntimeError(
            "Cannot downgrade: "
            f"{rows['invalid_count']} download_history row(s) have event_type='stalled', "
            "which the pre-#165 CHECK constraint rejects"
        )


def upgrade() -> None:
    with op.batch_alter_table("download_history", schema=None) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _check_sql(_NEW_VALUES))


def downgrade() -> None:
    _raise_on_stalled_rows()
    with op.batch_alter_table("download_history", schema=None) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="check")
        batch_op.create_check_constraint(_CONSTRAINT, _check_sql(_OLD_VALUES))
