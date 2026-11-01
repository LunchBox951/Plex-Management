"""retain suspect movie imports for review

Revision ID: 3af8b3f3dbc5
Revises: 2efdfe4b6871
Create Date: 2026-10-31 19:57:18.638785
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3af8b3f3dbc5"
down_revision: str | None = "2efdfe4b6871"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("downloads", sa.Column("import_review_reason", sa.String(), nullable=True))
    op.add_column(
        "downloads",
        sa.Column(
            "import_review_approved", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
    )
    op.add_column("downloads", sa.Column("import_review_blocklist_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("downloads", "import_review_blocklist_id")
    op.drop_column("downloads", "import_review_approved")
    op.drop_column("downloads", "import_review_reason")
