"""persist tv request intent and season quality breadcrumbs

Revision ID: 9b7a1c5d2e4f
Revises: 088a027cb4ec
Create Date: 2026-07-04 13:55:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b7a1c5d2e4f"
down_revision: str | None = "088a027cb4ec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tv_request_mode", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("requested_seasons_json", sa.JSON(), nullable=True))

    with op.batch_alter_table("season_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("installed_quality_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("installed_profile_index", sa.Integer(), nullable=True))

    bind = op.get_bind()
    media_requests = sa.table(
        "media_requests",
        sa.column("id", sa.Integer()),
        sa.column("media_type", sa.String()),
        sa.column("tv_request_mode", sa.String()),
        sa.column("requested_seasons_json", sa.JSON()),
    )
    season_requests = sa.table(
        "season_requests",
        sa.column("media_request_id", sa.Integer()),
        sa.column("season_number", sa.Integer()),
    )
    request_ids = [
        row.id
        for row in bind.execute(
            sa.select(media_requests.c.id).where(media_requests.c.media_type == "tv")
        )
    ]
    for request_id in request_ids:
        seasons = [
            row.season_number
            for row in bind.execute(
                sa.select(season_requests.c.season_number)
                .where(season_requests.c.media_request_id == request_id)
                .order_by(season_requests.c.season_number)
            )
        ]
        bind.execute(
            media_requests.update()
            .where(media_requests.c.id == request_id)
            .values(
                tv_request_mode="explicit_seasons",
                requested_seasons_json=seasons,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("season_requests", schema=None) as batch_op:
        batch_op.drop_column("installed_profile_index")
        batch_op.drop_column("installed_quality_id")

    with op.batch_alter_table("media_requests", schema=None) as batch_op:
        batch_op.drop_column("requested_seasons_json")
        batch_op.drop_column("tv_request_mode")
