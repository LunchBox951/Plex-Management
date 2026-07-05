"""persist tv request intent and season quality breadcrumbs

Revision ID: 9b7a1c5d2e4f
Revises: b7e2d4f6c8a1
Create Date: 2026-07-04 13:55:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b7a1c5d2e4f"
down_revision: str | None = "b7e2d4f6c8a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("tv_request_mode", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("requested_seasons_json", sa.JSON(), nullable=True))

    with op.batch_alter_table("season_requests", schema=None) as batch_op:
        batch_op.add_column(sa.Column("installed_quality_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("installed_profile_index", sa.Integer(), nullable=True))

    # Backfill request intent for TV requests that predate these columns.
    #
    # The intent bit -- whole-show ("every tracked season, including ones that
    # air later") vs. a finite named season set -- was never recorded before
    # this revision, and it CANNOT be recovered from the surviving rows: a
    # whole-show request and an explicit "seasons 1..N" request leave IDENTICAL
    # ``season_requests`` rows (``request_service._season_numbers`` expands an
    # omitted season list to 1..season_count either way). We therefore stamp
    # every legacy TV request with the mode the live app itself uses whenever no
    # finite season set was named: ``whole_show`` with a NULL season set
    # (``request_service._tv_request_intent`` returns ``("whole_show", None)``
    # for an omitted/empty ``seasons``). This makes a migrated legacy request
    # bit-identical to the same request re-created after the migration, and
    # ``whole_show`` never over-grabs -- ``domain.season_pack.plan_multi_season_pack``
    # caps eligibility to the seasons already tracked by live ``season_requests``
    # rows (``eligible = tracked``), so extra seasons carried by a pack are
    # ignored, never newly grabbed.
    #
    # The rejected alternative -- ``explicit_seasons`` frozen to the
    # migration-time season set -- fabricates an intent the operator never
    # expressed and misbehaves two ways once the (currently inert) planner is
    # wired to intent: (a) a legacy TV row with zero ``season_requests`` (a
    # schema-valid state since the initial revision permitted ``media_type='tv'``)
    # becomes an explicit request for NO seasons and would reject EVERY
    # multi-season pack -- an intent no operator can hold; and (b) a legacy
    # whole-show request for a still-airing show is frozen to today's season set
    # and would permanently, invisibly reject a later season's pack, diverging
    # from an identically-intended request created after the migration.
    media_requests = sa.table(
        "media_requests",
        sa.column("media_type", sa.String()),
        sa.column("tv_request_mode", sa.String()),
        sa.column("requested_seasons_json", sa.JSON()),
    )
    op.get_bind().execute(
        media_requests.update()
        .where(media_requests.c.media_type == "tv")
        .values(tv_request_mode="whole_show", requested_seasons_json=None)
    )


def downgrade() -> None:
    with op.batch_alter_table("season_requests", schema=None) as batch_op:
        batch_op.drop_column("installed_profile_index")
        batch_op.drop_column("installed_quality_id")

    with op.batch_alter_table("media_requests", schema=None) as batch_op:
        batch_op.drop_column("requested_seasons_json")
        batch_op.drop_column("tv_request_mode")
