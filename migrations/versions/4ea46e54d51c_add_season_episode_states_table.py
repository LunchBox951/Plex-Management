"""add season_episode_states table

Revision ID: 4ea46e54d51c
Revises: c86212dad733
Create Date: 2026-07-11 20:28:25.542538
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4ea46e54d51c"
down_revision: str | None = "c86212dad733"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalize_episodes(value: object) -> list[int] | None:
    """Copied from ``5e8b1a4f2c9d``'s helper of the same name: a raw ``sa.text()``
    read of a JSON column returns the stored TEXT verbatim (no type decoding), so
    this handles both a JSON string and (defensively) an already-decoded list.
    """
    if value in (None, "", "null"):
        return None
    parsed = value
    if isinstance(value, str):
        parsed = json.loads(value)
    if not isinstance(parsed, list):
        return None
    normalized = sorted({int(episode) for episode in parsed})
    return normalized or None


def _backfill_imported_episode_states() -> None:
    """Seed ``imported`` rows for episodes already covered by a prior imported TV
    download/scope for that season (spec point 4) -- e.g. "The Last Man on Earth"
    S4E07 counts as imported without a re-download. A whole-season pack import
    (``episodes_json IS NULL``) enumerates no specific episode numbers and seeds
    NO rows for that contribution -- the season reads as "target unknown" until
    the first auto-grab cycle refreshes the target from TMDB, never guessed here
    (this migration runs offline, no network).
    """
    bind = op.get_bind()
    season_rows = (
        bind.execute(sa.text("SELECT id, media_request_id, season_number FROM season_requests"))
        .mappings()
        .all()
    )

    for season in season_rows:
        episodes: set[int] = set()

        download_rows = bind.execute(
            sa.text(
                "SELECT episodes_json FROM downloads "
                "WHERE media_request_id = :media_request_id "
                "AND season = :season_number AND status = 'imported'"
            ),
            {
                "media_request_id": season["media_request_id"],
                "season_number": season["season_number"],
            },
        ).mappings()
        for row in download_rows:
            normalized = _normalize_episodes(row["episodes_json"])
            if normalized:
                episodes.update(normalized)

        scope_rows = bind.execute(
            sa.text(
                "SELECT episodes_json FROM download_scopes "
                "WHERE media_request_id = :media_request_id "
                "AND season_number = :season_number AND status = 'imported'"
            ),
            {
                "media_request_id": season["media_request_id"],
                "season_number": season["season_number"],
            },
        ).mappings()
        for row in scope_rows:
            normalized = _normalize_episodes(row["episodes_json"])
            if normalized:
                episodes.update(normalized)

        for episode_number in sorted(episodes):
            bind.execute(
                sa.text(
                    "INSERT INTO season_episode_states "
                    "(season_request_id, episode_number, status, air_date, grabbed_download_id) "
                    "VALUES (:season_request_id, :episode_number, 'imported', NULL, NULL)"
                ),
                {"season_request_id": season["id"], "episode_number": episode_number},
            )


def upgrade() -> None:
    op.create_table(
        "season_episode_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("season_request_id", sa.Integer(), nullable=False),
        sa.Column("episode_number", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "grabbed",
                "imported",
                name="ck_season_episode_states_status_enum",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("air_date", sa.Date(), nullable=True),
        sa.Column("grabbed_download_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["grabbed_download_id"], ["downloads.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["season_request_id"], ["season_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_season_episode_states_season_request_id",
        "season_episode_states",
        ["season_request_id"],
    )
    op.create_index(
        "ix_season_episode_states_status",
        "season_episode_states",
        ["status"],
    )
    op.create_index(
        "uq_season_episode_states_season_episode",
        "season_episode_states",
        ["season_request_id", "episode_number"],
        unique=True,
    )

    _backfill_imported_episode_states()


def downgrade() -> None:
    op.drop_index("uq_season_episode_states_season_episode", table_name="season_episode_states")
    op.drop_index("ix_season_episode_states_status", table_name="season_episode_states")
    op.drop_index("ix_season_episode_states_season_request_id", table_name="season_episode_states")
    op.drop_table("season_episode_states")
