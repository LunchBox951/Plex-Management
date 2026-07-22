"""add download coverage claims for ride-along seasons

Persists a multi-season pack's PHYSICAL coverage of covered-but-untargeted
"ride-along" seasons as a first-class atomic claim (issue #456, the deferred
remainder of #409 / PR #454). A pack imports only its ``target_seasons`` (which
carry a ``download_scopes`` row keyed by ``uq_download_scopes_active_scope``) yet
downloads every season in its ``covered_seasons`` footprint; a ride-along season
therefore keyed neither active unique index, leaving a narrow window in which a
concurrent cycle could grab that season between one pack's ``qbt.add`` and its
download registration. ``download_coverage_claims`` closes it: every grab claims
each season its torrent physically fetches, and the partial unique index
``uq_download_coverage_claims_active`` makes "at most one active download covering
``(request, season)``" a database invariant -- the atomic backstop the
``grab_service._active_guard_seasons`` read guard was previously the sole line
for. Kept out of ``download_scopes`` deliberately: ``import_service`` imports every
non-``imported`` scope, so a ride-along scope would import the very season the pack
skips. New table only -- no backfill; an in-flight pack that predates this migration
simply gains claims on its next grab, and the ``covered_season_in_flight`` +
read-guard lines from PR #454 still apply meanwhile.

Revision ID: 45f1f9145e95
Revises: ec826d3aa951
Create Date: 2026-07-22 16:32:37.686616
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "45f1f9145e95"
down_revision: str | None = "ec826d3aa951"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "download_coverage_claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("download_id", sa.Integer(), nullable=False),
        sa.Column("media_request_id", sa.Integer(), nullable=True),
        sa.Column("season_number", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'active'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["download_id"], ["downloads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["media_request_id"], ["media_requests.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_download_coverage_claims_download", "download_coverage_claims", ["download_id"]
    )
    op.create_index(
        op.f("ix_download_coverage_claims_download_id"),
        "download_coverage_claims",
        ["download_id"],
    )
    op.create_index(
        op.f("ix_download_coverage_claims_media_request_id"),
        "download_coverage_claims",
        ["media_request_id"],
    )
    # At most one ACTIVE physical-coverage claim per (request, season); the
    # ``released`` status is excluded so a fresh grab after a torrent terminates is
    # allowed, and the predicate is supplied per-dialect so the partial index is
    # honoured on Postgres too (mirrors uq_download_scopes_active_scope).
    op.create_index(
        "uq_download_coverage_claims_active",
        "download_coverage_claims",
        ["media_request_id", "season_number"],
        unique=True,
        sqlite_where=sa.text("media_request_id IS NOT NULL AND status = 'active'"),
        postgresql_where=sa.text("media_request_id IS NOT NULL AND status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("uq_download_coverage_claims_active", table_name="download_coverage_claims")
    op.drop_index(
        op.f("ix_download_coverage_claims_media_request_id"),
        table_name="download_coverage_claims",
    )
    op.drop_index(
        op.f("ix_download_coverage_claims_download_id"), table_name="download_coverage_claims"
    )
    op.drop_index("ix_download_coverage_claims_download", table_name="download_coverage_claims")
    op.drop_table("download_coverage_claims")
