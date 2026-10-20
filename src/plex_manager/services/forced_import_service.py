"""Admin "Import anyway" for a blocked import (UI overhaul) -- NOT IMPLEMENTED.

Today ``POST /queue/{id}/import`` only RETRIES an import; it never overrides the
validation that blocked it. "Import anyway" is a new admin-only correction: place
the files despite the mismatch (e.g. a season mismatch), mark the request done,
and record an audit history row so Report a problem can undo it later. The UI
confirms first and lists those side effects. Until it lands, the endpoint raises
``NotImplementedError`` -> 501 ``not_implemented``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["force_import"]


async def force_import(session: AsyncSession, *, download_id: int) -> NoReturn:
    """Import ``download_id``'s files despite the validation that blocked them."""
    raise NotImplementedError("Import anyway isn't available yet.")
