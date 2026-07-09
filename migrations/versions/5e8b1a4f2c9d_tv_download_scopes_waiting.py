"""add tv download scopes and waiting-for-air-date status

Revision ID: 5e8b1a4f2c9d
Revises: 9b7a1c5d2e4f
Create Date: 2026-07-09 00:00:00.000000
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5e8b1a4f2c9d"
down_revision: str | None = "9b7a1c5d2e4f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REQUEST_STATUS_CONSTRAINTS = (
    ("media_requests", "ck_media_requests_status_enum", "status"),
    ("season_requests", "ck_season_requests_status_enum", "status"),
)
_OLD_STATUS_VALUES = (
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
)
_NEW_STATUS_VALUES = (
    "pending",
    "searching",
    "no_acceptable_release",
    "waiting_for_air_date",
    "downloading",
    "completed",
    "available",
    "partially_available",
    "failed",
    "import_blocked",
    "evicted",
    "cancelled",
)
_OLD_ACTIVE_PREDICATE = (
    "status IN ('pending', 'searching', 'no_acceptable_release', "
    "'downloading', 'import_blocked', 'completed', 'partially_available')"
)
_NEW_ACTIVE_PREDICATE = (
    "status IN ('pending', 'searching', 'no_acceptable_release', "
    "'waiting_for_air_date', 'downloading', 'import_blocked', 'completed', "
    "'partially_available')"
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _check_sql(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({_quoted(values)})"


def _normalize_episodes(value: object) -> list[int] | None:
    if value in (None, "", "null"):
        return None
    parsed = value
    if isinstance(value, str):
        parsed = json.loads(value)
    if not isinstance(parsed, list):
        return None
    normalized = sorted({int(episode) for episode in parsed})
    return normalized or None


def _scope_key(season: int | None, episodes: object) -> str:
    normalized = _normalize_episodes(episodes)
    episode_key = json.dumps(normalized, separators=(",", ":")) if normalized is not None else "*"
    return f"season:{season if season is not None else 'null'}|episodes:{episode_key}"


def _backfill_scope_keys() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, season_number, episodes_json FROM download_scopes")
    ).mappings()
    for row in rows:
        bind.execute(
            sa.text("UPDATE download_scopes SET scope_key = :scope_key WHERE id = :id"),
            {
                "id": row["id"],
                "scope_key": _scope_key(row["season_number"], row["episodes_json"]),
            },
        )


def _recreate_active_index(predicate: str) -> None:
    op.drop_index("uq_media_requests_active", table_name="media_requests")
    op.create_index(
        "uq_media_requests_active",
        "media_requests",
        ["tmdb_id", "media_type"],
        unique=True,
        sqlite_where=sa.text(predicate),
        postgresql_where=sa.text(predicate),
    )


def _update_status_constraints(values: tuple[str, ...]) -> None:
    for table, constraint, column in _REQUEST_STATUS_CONSTRAINTS:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(constraint, type_="check")
            batch_op.create_check_constraint(constraint, _check_sql(column, values))


def upgrade() -> None:
    _update_status_constraints(_NEW_STATUS_VALUES)
    _recreate_active_index(_NEW_ACTIVE_PREDICATE)

    with op.batch_alter_table("media_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("requested_episodes_json", sa.JSON(), nullable=True))

    op.create_table(
        "download_scopes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("download_id", sa.Integer(), nullable=False),
        sa.Column("media_request_id", sa.Integer(), nullable=True),
        sa.Column("season_request_id", sa.Integer(), nullable=True),
        sa.Column("season_number", sa.Integer(), nullable=True),
        sa.Column("episodes_json", sa.JSON(), nullable=True),
        sa.Column("scope_key", sa.String(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["download_id"], ["downloads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_request_id"], ["media_requests.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["season_request_id"], ["season_requests.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO download_scopes (
                download_id,
                media_request_id,
                season_request_id,
                season_number,
                episodes_json,
                status
            )
            SELECT
                downloads.id,
                downloads.media_request_id,
                season_requests.id,
                downloads.season,
                downloads.episodes_json,
                CASE
                    WHEN downloads.status = 'imported' THEN 'imported'
                    WHEN downloads.status = 'import_blocked' THEN 'import_blocked'
                    WHEN downloads.status = 'failed' THEN 'failed'
                    WHEN downloads.status = 'no_acceptable_release' THEN 'no_acceptable_release'
                    ELSE 'active'
                END
            FROM downloads
            LEFT JOIN season_requests
              ON season_requests.media_request_id = downloads.media_request_id
             AND season_requests.season_number = downloads.season
            WHERE downloads.media_type = 'tv'
              AND downloads.season IS NOT NULL
            """
        )
    )
    _backfill_scope_keys()
    with op.batch_alter_table("download_scopes", schema=None) as batch_op:
        batch_op.alter_column("scope_key", existing_type=sa.String(), nullable=False)

    op.create_index("ix_download_scopes_download", "download_scopes", ["download_id"])
    op.create_index(
        "ix_download_scopes_request_scope",
        "download_scopes",
        ["media_request_id", "season_number"],
    )
    op.create_index(
        "uq_download_scopes_active_scope",
        "download_scopes",
        ["media_request_id", "scope_key"],
        unique=True,
        sqlite_where=sa.text("media_request_id IS NOT NULL AND status = 'active'"),
        postgresql_where=sa.text("media_request_id IS NOT NULL AND status = 'active'"),
    )
    op.create_index(op.f("ix_download_scopes_download_id"), "download_scopes", ["download_id"])
    op.create_index(
        op.f("ix_download_scopes_media_request_id"),
        "download_scopes",
        ["media_request_id"],
    )
    op.create_index(
        op.f("ix_download_scopes_season_request_id"),
        "download_scopes",
        ["season_request_id"],
    )


def downgrade() -> None:
    op.drop_index("uq_download_scopes_active_scope", table_name="download_scopes")
    op.drop_index(op.f("ix_download_scopes_season_request_id"), table_name="download_scopes")
    op.drop_index(op.f("ix_download_scopes_media_request_id"), table_name="download_scopes")
    op.drop_index(op.f("ix_download_scopes_download_id"), table_name="download_scopes")
    op.drop_index("ix_download_scopes_request_scope", table_name="download_scopes")
    op.drop_index("ix_download_scopes_download", table_name="download_scopes")
    op.drop_table("download_scopes")

    with op.batch_alter_table("media_requests", schema=None) as batch_op:
        batch_op.drop_column("requested_episodes_json")

    _recreate_active_index(_OLD_ACTIVE_PREDICATE)
    _update_status_constraints(_OLD_STATUS_VALUES)
