"""add request_subscribers (user_id, request_id) page index

Issue #218 phase 1: the shared-user request-history page
(``SqlRequestRepository.list_page``) filters ``request_subscribers`` by
``user_id`` equality and range-walks ``request_id`` under it. The composite
index serves that page query; it also covers every lookup the old
single-column ``ix_request_subscribers_user_id`` served (same leading
column), so that index is dropped as redundant.

Revision ID: e91b3f7a5d24
Revises: 516a74ff0b40
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e91b3f7a5d24"
down_revision: str | None = "516a74ff0b40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_request_subscribers_user_id_request_id",
        "request_subscribers",
        ["user_id", "request_id"],
        unique=False,
    )
    op.drop_index("ix_request_subscribers_user_id", table_name="request_subscribers")


def downgrade() -> None:
    op.create_index(
        "ix_request_subscribers_user_id", "request_subscribers", ["user_id"], unique=False
    )
    op.drop_index(
        "ix_request_subscribers_user_id_request_id", table_name="request_subscribers"
    )
