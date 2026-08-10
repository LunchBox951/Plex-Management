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

Issue #556 adds the first READ path: :func:`list_automatic_sign_outs`. The
share-revalidation sweep (#391) signs users out on its own, and until now the
only web-visible answer to "why was I signed out?" was a log line subject to
``log_retention_days`` / ``log_max_rows`` trimming -- the durable ``AuditLog``
row behind it was reachable only from a terminal, which north star #2 forbids
as the answer path. The read is deliberately NOT a generic log browser: it
selects exactly the automatic-sign-out action family this module names, so the
audit table's other rows (and any future ones) do not silently become a web
resource nobody vetted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import select

from plex_manager.models import AuditLog, User
from plex_manager.services.session_lifecycle import ensure_utc

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "AUTOMATIC_SIGN_OUT_ACTION_TYPES",
    "PLEX_SIGN_IN_EXPIRED_ACTION",
    "SHARE_REVOKED_ACTION",
    "USER_ENTITY_TYPE",
    "SignOutAuditRecord",
    "admin_exempt_action",
    "list_automatic_sign_outs",
    "record",
]

#: ``entity_type`` every automatic-sign-out row is written against; the subject
#: user is the row's ``entity_id`` (``user_id`` is the ACTOR, and an automatic
#: sweep has none).
USER_ENTITY_TYPE: Final = "user"

#: plex.tv answered authoritatively that the account no longer reaches the
#: configured server.
SHARE_REVOKED_ACTION: Final = "user.share_revoked"

#: plex.tv rejected the stored credential, so the share could not be checked at
#: all. Access was NOT established to be gone -- different words, on purpose.
PLEX_SIGN_IN_EXPIRED_ACTION: Final = "user.plex_sign_in_expired"

#: Suffix marking a verdict that was RECORDED for an owner/admin but deliberately
#: not acted on (ADR-0005 never-locked-out; see ``plex_access_service``).
_ADMIN_EXEMPT_SUFFIX: Final = "_admin_exempt"


def admin_exempt_action(action_type: str) -> str:
    """Return the admin-exempt variant of an automatic-sign-out action type."""
    return f"{action_type}{_ADMIN_EXEMPT_SUFFIX}"


#: The complete, closed set this read surface exposes. Adding a row type here is
#: a deliberate act, not something a new audit caller inherits by accident.
AUTOMATIC_SIGN_OUT_ACTION_TYPES: Final[tuple[str, ...]] = (
    SHARE_REVOKED_ACTION,
    admin_exempt_action(SHARE_REVOKED_ACTION),
    PLEX_SIGN_IN_EXPIRED_ACTION,
    admin_exempt_action(PLEX_SIGN_IN_EXPIRED_ACTION),
)


@dataclass(frozen=True)
class SignOutAuditRecord:
    """One automatic sign-out, read back from the durable audit trail.

    Flattened out of the row's JSON payloads here rather than in the router so
    the shape of what was written stays knowledge of this module. Nothing
    secret-bearing is carried: the sweep's rows hold share states, counts, and
    the account's display name — never a token, a session id, or an IP.
    """

    id: int
    occurred_at: datetime
    action_type: str
    user_id: int | None
    username: str | None
    previous_share_state: str | None
    share_state: str | None
    sessions_revoked: int
    admin_exempt: bool
    description: str | None

    @property
    def signed_out(self) -> bool:
        """Whether this verdict actually cut the user's sessions.

        An admin-exempt row records a share loss that was deliberately NOT acted
        on, so reporting it as a sign-out would be a lie in the operator's face.
        """
        return not self.admin_exempt


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


def _json_str(payload: dict[str, Any] | None, key: str) -> str | None:
    """Read a string field out of a stored JSON payload, or ``None``.

    The audit payloads are ``sa.JSON``: typed as ``dict[str, Any]``, so every
    field has to be re-narrowed on the way out. A value of an unexpected type is
    reported as absent rather than coerced -- a wrong-looking row must not
    render as a confident wrong answer.
    """
    if payload is None:
        return None
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _json_int(payload: dict[str, Any] | None, key: str, *, default: int = 0) -> int:
    if payload is None:
        return default
    value = payload.get(key)
    # ``bool`` is an ``int`` subclass; a boolean here is a wrong-typed field.
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def _json_bool(payload: dict[str, Any] | None, key: str, *, default: bool = False) -> bool:
    if payload is None:
        return default
    value = payload.get(key)
    return value if isinstance(value, bool) else default


async def list_automatic_sign_outs(
    session: AsyncSession, *, limit: int
) -> list[SignOutAuditRecord]:
    """Return the most recent automatic sign-out audit rows, newest first.

    Scoped to :data:`AUTOMATIC_SIGN_OUT_ACTION_TYPES` on purpose: this is the
    answer to "why was I signed out?", not a general audit browser. ``limit`` is
    the caller's already-validated bound.

    ``username`` prefers the name STAMPED into the row when it was written, and
    only falls back to an OUTER join on ``entity_id``. That order matters: the
    audit trail deliberately outlives the ``users`` row it describes
    (``AuditLog.user_id`` is ``ON DELETE SET NULL`` and ``entity_id`` carries no
    FK at all), so a join is not an authority for "who was this" -- a primary key
    freed by a deletion and later reused would make the join confidently name the
    WRONG person. Rows written before the stamp existed still resolve through the
    join, and an account deleted since simply reports ``username: None`` rather
    than dropping the record of what happened.
    """
    rows = (
        await session.execute(
            select(AuditLog, User.username)
            .outerjoin(User, User.id == AuditLog.entity_id)
            .where(
                AuditLog.entity_type == USER_ENTITY_TYPE,
                AuditLog.action_type.in_(AUTOMATIC_SIGN_OUT_ACTION_TYPES),
            )
            # ``created_at`` is a server default with second-ish resolution, so
            # ``id`` breaks ties: two sign-outs inside the same tick must still
            # come back in the order they happened.
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
    ).all()
    return [
        SignOutAuditRecord(
            id=entry.id,
            occurred_at=ensure_utc(entry.created_at),
            action_type=entry.action_type,
            user_id=entry.entity_id,
            username=_json_str(entry.new_value, "username") or username,
            previous_share_state=_json_str(entry.old_value, "share_state"),
            share_state=_json_str(entry.new_value, "share_state"),
            sessions_revoked=_json_int(entry.new_value, "sessions_revoked"),
            admin_exempt=_json_bool(entry.new_value, "admin_exempt"),
            description=entry.description,
        )
        for entry, username in rows
    ]
