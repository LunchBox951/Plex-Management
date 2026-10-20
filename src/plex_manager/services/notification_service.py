"""In-app notifications and "Let the admin know" (UI overhaul) -- NOT IMPLEMENTED.

The overhauled UI ships its notification bell, inbox and the requester's
"Let the admin know" button ahead of the backend. Every function here raises
``NotImplementedError``; the web layer turns that into a 501 ``not_implemented``
envelope and the SPA shows an honest "not available yet" state.

What the real implementation must do (tracked as a GitHub issue):

* Persist notifications per recipient (new table + Alembic migration):
  kind, message, optional request/title link, optional note, created/read times.
* Emit them from the existing lifecycle points: help requested/seen, a fix
  started (report-issue, grab), import succeeded/failed, ready to watch, a
  followed show's new episode, a service going down/recovering, a new request.
* ``notify_admin`` records the requester's ask once per open problem, notifies
  every admin, and stamps ``seen_at`` when an admin first opens it.
* Publish a realtime invalidation (``notifications``) so open clients refresh.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["list_notifications", "mark_read", "notify_admin"]

_NOTIFICATIONS_UNAVAILABLE = "Notifications aren't available yet."
_NOTIFY_ADMIN_UNAVAILABLE = "Letting the admin know isn't available yet."


async def list_notifications(session: AsyncSession, *, user_id: int | None) -> NoReturn:
    """Return ``user_id``'s notifications, newest first, with the unread count."""
    raise NotImplementedError(_NOTIFICATIONS_UNAVAILABLE)


async def mark_read(
    session: AsyncSession, *, user_id: int | None, ids: Sequence[int] | None
) -> NoReturn:
    """Mark the given notifications (or all, when ``ids`` is ``None``) as read."""
    raise NotImplementedError(_NOTIFICATIONS_UNAVAILABLE)


async def notify_admin(
    session: AsyncSession, *, request_id: int, user_id: int | None, note: str | None
) -> NoReturn:
    """Record the requester's "Let the admin know" and notify every admin."""
    raise NotImplementedError(_NOTIFY_ADMIN_UNAVAILABLE)
