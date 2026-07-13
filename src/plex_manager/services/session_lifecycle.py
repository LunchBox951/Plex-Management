"""Browser-session lifecycle policy: sliding idle expiry + dead-row sweep.

ADR-0016 mints an :class:`~plex_manager.models.AuthSession` on Plex sign-in (or a
recovery-key exchange) whose SHA-256 token digest rides an HTTP-only cookie, with
an **absolute** 30-day ``expires_at`` stamped at creation. Two gaps, closed here
and in :mod:`plex_manager.web.deps` (issues #56):

* **Sliding idle window.** ``last_seen_at`` was written once at creation and never
  refreshed, so the only bound on a session was the absolute cap. We keep the
  absolute cap (``expires_at`` is untouched — a session can never outlive it) AND
  add an *idle* window: a session is live only while it has been *seen* within
  :data:`SESSION_IDLE_WINDOW`. :mod:`plex_manager.web.deps` refreshes
  ``last_seen_at`` on authenticated activity, throttled to at most once per
  :data:`SESSION_LAST_SEEN_REFRESH_INTERVAL` so ordinary request traffic never
  turns into a write per request.

* **Dead rows accumulate forever.** Revoked / expired / idle-timed-out rows are
  kept (never deleted inline) so revocation stays auditable, but nothing ever
  reaps them. :func:`sweep_dead_sessions` deletes rows that are BOTH permanently
  unusable AND older than :data:`SESSION_SWEEP_RETENTION`, bounding table growth
  while preserving a recent audit trail. The web layer runs it on a background
  loop (``web/app.py``'s ``_session_sweep_loop``), mirroring the log-retention
  prune.

Pure policy + one DB helper each for the sweep and for admin revocation; no web
imports, so ``deps``/routers/``app`` depend on this module and never the reverse.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.models import AuthSession

__all__ = [
    "SESSION_IDLE_WINDOW",
    "SESSION_LAST_SEEN_REFRESH_INTERVAL",
    "SESSION_SWEEP_INTERVAL_SECONDS",
    "SESSION_SWEEP_RETENTION",
    "ensure_utc",
    "revoke_recovery_sessions",
    "revoke_user_sessions",
    "session_effective_last_seen",
    "session_idle_deadline",
    "session_is_idle_expired",
    "sweep_dead_sessions",
]

# How long a session may sit idle (no authenticated request refreshing
# ``last_seen_at``) before it stops authenticating, INDEPENDENT of the absolute
# ``expires_at`` cap. Shorter than the 30-day absolute lifetime so it is the
# binding constraint for a browser that goes quiet: a tab abandoned for longer
# than this must sign in again even though the absolute cap has not yet passed.
SESSION_IDLE_WINDOW = timedelta(days=7)

# The refresh throttle: an authenticated request refreshes ``last_seen_at`` only
# when the stored value is older than this, so a busy tab does one small UPDATE
# per hour rather than one per request. Must be comfortably shorter than
# :data:`SESSION_IDLE_WINDOW` or an active session could still idle out between
# refreshes.
SESSION_LAST_SEEN_REFRESH_INTERVAL = timedelta(hours=1)

# A dead row (revoked, past its absolute expiry, or idle-timed-out) is retained
# this long before the sweep deletes it, so a just-revoked session stays visible
# as an audit record for a while rather than vanishing the instant it dies.
SESSION_SWEEP_RETENTION = timedelta(days=7)

# How often the background sweep runs. Hourly is plenty: the retention grace is
# measured in days, so the exact cadence only affects how promptly a long-dead
# row is reclaimed, never correctness.
SESSION_SWEEP_INTERVAL_SECONDS = 3600.0


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` as a UTC-aware datetime.

    Some backends (SQLite) hand back naive datetimes even for ``timezone=True``
    columns; treat a naive value as already-UTC so comparisons never raise on a
    naive/aware mismatch. Mirrors ``web.deps._normalize_dt``.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def session_effective_last_seen(auth_session: AuthSession) -> datetime:
    """The timestamp the idle window is measured from for this session.

    ``last_seen_at`` when set (every session minted since ADR-0016 stamps it at
    creation, refreshed on activity); ``created_at`` as the fallback for a legacy
    row predating the sliding-window work whose ``last_seen_at`` is NULL — so such
    a row idles out relative to when it was created, never treated as
    never-expiring.
    """
    stamp = auth_session.last_seen_at or auth_session.created_at
    return ensure_utc(stamp)


def session_idle_deadline(auth_session: AuthSession) -> datetime:
    """The instant this session idles out (last activity + the idle window)."""
    return session_effective_last_seen(auth_session) + SESSION_IDLE_WINDOW


def session_is_idle_expired(auth_session: AuthSession, *, now: datetime) -> bool:
    """Whether this session has sat idle past :data:`SESSION_IDLE_WINDOW`."""
    return session_idle_deadline(auth_session) <= now


async def revoke_user_sessions(
    session: AsyncSession,
    user_id: int,
    *,
    now: datetime | None = None,
) -> int:
    """Stamp ``revoked_at`` on every ACTIVE session for ``user_id``; return count.

    An admin lever (issue #56): a removed or demoted Plex user keeps a locally
    validated session until revocation, so this is the on-demand way to cut it
    short. Revocation stamps ``revoked_at`` (the model's auditable-revoke
    convention) rather than deleting; the sweep reclaims the row later. Only rows
    whose ``revoked_at`` is still NULL are touched, so a re-revoke is a harmless
    no-op that reports 0. The caller owns the commit.
    """
    stamp = now if now is not None else datetime.now(UTC)
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=stamp)
        ),
    )
    return result.rowcount


async def revoke_recovery_sessions(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Stamp ``revoked_at`` on every ACTIVE recovery session; return the count.

    The recovery-session counterpart to :func:`revoke_user_sessions` (issue #56).
    Recovery sessions are the cookies minted by ``POST /auth/api-key`` — admin
    authority, no Plex identity (``AuthSession.user_id IS NULL``) — so they cannot
    be targeted by ``user_id`` and are revoked as a single group instead. Only
    still-active (``revoked_at`` NULL) rows are touched, so a re-revoke is a
    harmless no-op reporting 0. Rotation of the recovery KEY is a separate concern
    (PR #319); this is the on-demand admin lever to cut existing recovery cookies.
    The caller owns the commit.
    """
    stamp = now if now is not None else datetime.now(UTC)
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(AuthSession)
            .where(AuthSession.user_id.is_(None), AuthSession.revoked_at.is_(None))
            .values(revoked_at=stamp)
        ),
    )
    return result.rowcount


async def sweep_dead_sessions(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> int:
    """Delete auth-session rows that are permanently dead AND past retention.

    A row is *dead* once it can no longer authenticate: it was revoked, it passed
    its absolute ``expires_at``, or it idled out (``effective last_seen`` +
    :data:`SESSION_IDLE_WINDOW`). We only delete once that death is at least
    :data:`SESSION_SWEEP_RETENTION` in the past, so a recently-revoked session
    survives as an audit record for a while before being reclaimed. A still-live
    session is never touched. The caller owns nothing — this commits the delete
    itself so the background loop stays a single call. Returns the row count.
    """
    moment = now if now is not None else datetime.now(UTC)
    retention_cutoff = moment - SESSION_SWEEP_RETENTION
    idle_death_cutoff = retention_cutoff - SESSION_IDLE_WINDOW
    result = cast(
        CursorResult[Any],
        await session.execute(
            delete(AuthSession).where(
                or_(
                    AuthSession.revoked_at <= retention_cutoff,
                    AuthSession.expires_at <= retention_cutoff,
                    func.coalesce(AuthSession.last_seen_at, AuthSession.created_at)
                    <= idle_death_cutoff,
                )
            )
        ),
    )
    await session.commit()
    return result.rowcount
