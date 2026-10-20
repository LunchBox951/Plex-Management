"""Notifications router -- the bell / inbox (UI overhaul).

Not implemented yet: every endpoint answers 501 ``not_implemented`` (see
``services.notification_service``) and the SPA shows an honest "not available
yet" state. The response models are the contract the backend will fill.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.services import notification_service
from plex_manager.web.deps import AuthContext, get_session, require_api_key
from plex_manager.web.schemas import (
    ErrorEnvelope,
    MarkNotificationsReadBody,
    NotificationListResponse,
)

__all__ = ["router"]

router = APIRouter(
    prefix="/api/v1/notifications",
    tags=["notifications"],
)

_NOT_IMPLEMENTED_RESPONSES: dict[int | str, dict[str, Any]] = {
    501: {"model": ErrorEnvelope, "description": "Not available yet"},
}


@router.get("", responses=_NOT_IMPLEMENTED_RESPONSES)
async def list_notifications_endpoint(
    auth: Annotated[AuthContext, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NotificationListResponse:
    """The current user's notifications, newest first, plus the unread count."""
    await notification_service.list_notifications(session, user_id=auth.user_id)


@router.post("/read", responses=_NOT_IMPLEMENTED_RESPONSES)
async def mark_notifications_read_endpoint(
    body: MarkNotificationsReadBody,
    auth: Annotated[AuthContext, Depends(require_api_key)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NotificationListResponse:
    """Mark the listed notifications (or every one, when ``ids`` is omitted) read."""
    await notification_service.mark_read(session, user_id=auth.user_id, ids=body.ids)
