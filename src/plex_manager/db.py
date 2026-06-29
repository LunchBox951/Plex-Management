"""Database foundation.

The schema is owned by versioned Alembic migrations (see ADR-0007); models are
added in the v1-planning session. This module only defines the declarative base
that migrations and future models build on.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""
