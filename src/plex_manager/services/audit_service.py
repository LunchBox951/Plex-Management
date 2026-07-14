"""Durable audit trail (honesty over silence) for state-changing actions that
are not obviously visible elsewhere.

``AuditLog`` (:class:`~plex_manager.models.AuditLog`) has existed since the
initial schema but was, until issue #314, written by no service -- only
FK-tested. Subscriber withdrawal / ownership handoff (ADR-pending, issue #314)
is the first caller: a handoff silently reassigning ``MediaRequest.user_id``
would otherwise be an invisible mutation with no record of WHO the request
belonged to before. :func:`record` gives it a durable, queryable trail naming
both the withdrawing user and (on handoff) the incoming owner, alongside the
SSE broadcast that tells connected clients to refetch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from plex_manager.models import AuditLog

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["record"]


async def record(
    session: AsyncSession,
    *,
    actor_user_id: int | None,
    action_type: str,
    entity_type: str,
    entity_id: int | None,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
    description: str | None = None,
) -> None:
    """Append one immutable audit row.

    FLUSH-ONLY (mirrors ``season_request_service``'s module-wide convention):
    never commits or rolls back. The caller owns the commit boundary so this
    row lands atomically alongside whatever state change it documents, never
    as a separate, potentially-inconsistent transaction.
    """
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type=action_type,
            entity_type=entity_type,
            entity_id=entity_id,
            old_value=old_value,
            new_value=new_value,
            description=description,
        )
    )
    await session.flush()
