"""add durable pre-add intent tables

Revision ID: 5f2d9a8c4b71
Revises: 111b3b3c67fb
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5f2d9a8c4b71"
down_revision: str | None = "111b3b3c67fb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "download_add_intents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("torrent_hash", sa.String(), nullable=False, unique=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("state", sa.String(), nullable=False, server_default="prepared"),
        sa.Column(
            "media_request_id",
            sa.Integer(),
            sa.ForeignKey("media_requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("media_type", sa.String(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("release_title", sa.String(), nullable=True),
        sa.Column("indexer", sa.String(), nullable=True),
        sa.Column("quality_name", sa.String(), nullable=True),
        sa.Column("save_path", sa.String(), nullable=False, server_default=""),
        sa.Column("observed_request_status", sa.String(), nullable=True),
        sa.Column("observed_season_status", sa.String(), nullable=True),
        sa.Column("owns_client_torrent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_download_add_intents_state", "download_add_intents", ["state"])
    op.create_table(
        "download_add_intent_scopes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "intent_id",
            sa.Integer(),
            sa.ForeignKey("download_add_intents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "media_request_id",
            sa.Integer(),
            sa.ForeignKey("media_requests.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tmdb_id", sa.Integer(), nullable=True),
        sa.Column("media_type", sa.String(), nullable=True),
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column("season_number", sa.Integer(), nullable=True),
        sa.Column("episodes_json", sa.JSON(), nullable=True),
        sa.Column("is_target", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint(
            "intent_id", "scope_key", name="uq_download_add_intent_scopes_intent_scope"
        ),
        sa.UniqueConstraint(
            "tmdb_id", "media_type", "scope_key", name="uq_download_add_intent_scopes_title_scope"
        ),
    )
    op.create_index(
        "ix_download_add_intent_scopes_intent", "download_add_intent_scopes", ["intent_id"]
    )
    op.create_table(
        "client_only_torrents",
        sa.Column("torrent_hash", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("raw_state", sa.String(), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False, server_default="0"),
        sa.Column("save_path", sa.String(), nullable=False, server_default=""),
        sa.Column("category", sa.String(), nullable=False, server_default="plex-manager"),
        sa.Column("state", sa.String(), nullable=False, server_default="pending"),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_client_only_torrents_state", "client_only_torrents", ["state"])


def downgrade() -> None:
    op.drop_index("ix_client_only_torrents_state", table_name="client_only_torrents")
    op.drop_table("client_only_torrents")
    op.drop_index("ix_download_add_intent_scopes_intent", table_name="download_add_intent_scopes")
    op.drop_table("download_add_intent_scopes")
    op.drop_index("ix_download_add_intents_state", table_name="download_add_intents")
    op.drop_table("download_add_intents")
