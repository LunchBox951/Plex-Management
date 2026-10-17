"""Track held payloads and durable automatic rejection reasons.

Revision ID: 609a01
Revises: c5cf0a125f5f
"""

import sqlalchemy as sa
from alembic import op

revision = "609a01"
down_revision = "c5cf0a125f5f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("downloads", sa.Column("payload_pending", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("downloads", sa.Column("payload_rejection", sa.String(), nullable=True))
    op.add_column("downloads", sa.Column("payload_resume", sa.Boolean(), nullable=False, server_default=sa.false()))
    # An importing row with a download_path breadcrumb may already have placed its
    # library file; crash recovery owns it, and a rejection would orphan that file.
    op.execute("UPDATE downloads SET payload_pending = true WHERE status IN "
               "('downloading', 'metadata_fetching', 'client_missing', 'import_pending', 'import_blocked') "
               "OR (status = 'importing' AND download_path IS NULL)")


def downgrade() -> None:
    op.drop_column("downloads", "payload_resume")
    op.drop_column("downloads", "payload_rejection")
    op.drop_column("downloads", "payload_pending")
