"""add enum checks and drop blocklist title index

Revision ID: b7e2d4f6c8a1
Revises: 88bcf173ab91
Create Date: 2026-07-01 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e2d4f6c8a1"
down_revision: str | None = "88bcf173ab91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUM_CHECKS = (
    ("blocklist", "media_type", ("movie", "tv"), True),
    ("blocklist", "reason", ("failed", "bad_quality", "wrong_media", "user_reported"), False),
    (
        "download_history",
        "event_type",
        ("grabbed", "import_started", "imported", "failed", "evicted", "reported", "cancelled"),
        False,
    ),
    ("media_requests", "media_type", ("movie", "tv"), False),
    (
        "media_requests",
        "status",
        (
            "pending",
            "searching",
            "no_acceptable_release",
            "downloading",
            "completed",
            "available",
            "partially_available",
            "failed",
            "import_blocked",
            "evicted",
            "cancelled",
        ),
        False,
    ),
    ("request_dedup_locks", "media_type", ("movie", "tv"), False),
    (
        "season_requests",
        "status",
        (
            "pending",
            "searching",
            "no_acceptable_release",
            "downloading",
            "completed",
            "available",
            "partially_available",
            "failed",
            "import_blocked",
            "evicted",
            "cancelled",
        ),
        False,
    ),
    ("downloads", "media_type", ("movie", "tv"), True),
)


# SQLite cannot ADD CONSTRAINT in place, so ``batch_alter_table`` recreates each
# table -- and SQLAlchemy's SQLite reflection does NOT carry partial (WHERE'd) or
# expression indexes across that recreate, silently dropping the two ACTIVE-row
# unique guards. Re-issue them (verbatim from ``models.py``) after every batch
# pass, in BOTH directions. ``IF NOT EXISTS`` makes this a no-op on PostgreSQL,
# where batch mode is a plain ALTER and the indexes were never lost.
_PARTIAL_INDEX_DDL = (
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_media_requests_active
    ON media_requests (tmdb_id, media_type)
    WHERE status IN ('pending', 'searching', 'no_acceptable_release',
                     'downloading', 'import_blocked', 'completed',
                     'partially_available')
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_downloads_active_request
    ON downloads (media_request_id, coalesce(season, -1))
    WHERE media_request_id IS NOT NULL
      AND status NOT IN ('imported', 'failed', 'no_acceptable_release')
    """,
)


def _restore_partial_indexes() -> None:
    for ddl in _PARTIAL_INDEX_DDL:
        op.execute(sa.text(ddl))


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _constraint_name(table: str, column: str) -> str:
    return f"ck_{table}_{column}_enum"


def _constraint_sql(column: str, values: tuple[str, ...], nullable: bool) -> str:
    check = f"{column} IN ({_quoted(values)})"
    if nullable:
        return f"{column} IS NULL OR {check}"
    return check


def _raise_on_invalid_enum_values() -> None:
    bind = op.get_bind()
    for table, column, values, _nullable in _ENUM_CHECKS:
        rows = (
            bind.execute(
                # Table/column names and values come only from the local
                # _ENUM_CHECKS constants above, not operator input.
                sa.text(  # noqa: S608
                    f"""
                    SELECT {column} AS value, COUNT(*) AS invalid_count
                    FROM {table}
                    WHERE {column} IS NOT NULL
                      AND {column} NOT IN ({_quoted(values)})
                    GROUP BY {column}
                    ORDER BY invalid_count DESC, value
                    LIMIT 5
                    """
                )
            )
            .mappings()
            .all()
        )
        if rows:
            examples = ", ".join(
                f"{table}.{column}={row['value']!r} count={row['invalid_count']}"
                for row in rows
            )
            raise RuntimeError(
                "Cannot add enum CHECK constraints; invalid persisted values found: "
                f"{examples}"
            )


def upgrade() -> None:
    _raise_on_invalid_enum_values()

    with op.batch_alter_table("blocklist", schema=None) as batch_op:
        batch_op.drop_index("ix_blocklist_source_title")

    for table, column, values, nullable in _ENUM_CHECKS:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.create_check_constraint(
                _constraint_name(table, column),
                _constraint_sql(column, values, nullable),
            )

    _restore_partial_indexes()


def downgrade() -> None:
    for table, column, _values, _nullable in reversed(_ENUM_CHECKS):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(_constraint_name(table, column), type_="check")

    _restore_partial_indexes()

    with op.batch_alter_table("blocklist", schema=None) as batch_op:
        batch_op.create_index("ix_blocklist_source_title", ["source_title"], unique=False)
