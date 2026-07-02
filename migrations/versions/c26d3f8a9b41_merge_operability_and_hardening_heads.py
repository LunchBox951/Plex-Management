"""merge operability and hardening migration heads

Revision ID: c26d3f8a9b41
Revises: 6c7fca1436d8, b7e2d4f6c8a1
Create Date: 2026-07-02 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "c26d3f8a9b41"
down_revision: tuple[str, str] | None = ("6c7fca1436d8", "b7e2d4f6c8a1")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op merge revision."""


def downgrade() -> None:
    """No-op merge revision."""
