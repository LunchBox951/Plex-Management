"""plex oauth login state and sessions

Revision ID: 7bcbce2c2e2b
Revises: b7e2d4f6c8a1
Create Date: 2026-07-04 11:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7bcbce2c2e2b"
down_revision: str | None = "b7e2d4f6c8a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column(
            "permissions",
            existing_type=sa.Integer(),
            server_default=sa.text("0"),
            existing_nullable=False,
        )

    op.create_table(
        "plex_login_states",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("pin_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("client_identifier", sa.String(), nullable=False),
        sa.Column("browser_token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("plex_login_states", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_plex_login_states_expires_at"), ["expires_at"])
        batch_op.create_index(batch_op.f("ix_plex_login_states_pin_id"), ["pin_id"])
        batch_op.create_index(batch_op.f("ix_plex_login_states_state"), ["state"], unique=True)

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("auth_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_auth_sessions_expires_at"), ["expires_at"])
        batch_op.create_index(batch_op.f("ix_auth_sessions_revoked_at"), ["revoked_at"])
        batch_op.create_index(batch_op.f("ix_auth_sessions_token_hash"), ["token_hash"], unique=True)
        batch_op.create_index(batch_op.f("ix_auth_sessions_user_id"), ["user_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_auth_sessions_user_id"), table_name="auth_sessions")
    op.drop_index(op.f("ix_auth_sessions_token_hash"), table_name="auth_sessions")
    op.drop_index(op.f("ix_auth_sessions_revoked_at"), table_name="auth_sessions")
    op.drop_index(op.f("ix_auth_sessions_expires_at"), table_name="auth_sessions")
    op.drop_table("auth_sessions")

    op.drop_index(op.f("ix_plex_login_states_state"), table_name="plex_login_states")
    op.drop_index(op.f("ix_plex_login_states_pin_id"), table_name="plex_login_states")
    op.drop_index(op.f("ix_plex_login_states_expires_at"), table_name="plex_login_states")
    op.drop_table("plex_login_states")

    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.alter_column(
            "permissions",
            existing_type=sa.Integer(),
            server_default=sa.text("1"),
            existing_nullable=False,
        )
