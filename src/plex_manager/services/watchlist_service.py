"""Synchronize one Plex account watchlist into requests and a safe snapshot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, cast

from sqlalchemy import CursorResult, delete, select

from plex_manager.adapters.plex.oauth import (
    CODE_TOKEN_INVALID,
    PlexTvClient,
    PlexVerifyError,
    account_server_resource,
)
from plex_manager.models import MediaType, User, WatchlistItem
from plex_manager.services import request_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.metadata import MetadataPort
    from plex_manager.ports.watchlist import WatchlistPort

__all__ = [
    "SyncUserAuthorization",
    "WatchlistSyncResult",
    "WatchlistWorkerStatus",
    "clear_snapshots",
    "clear_user_snapshot",
    "is_watchlisted",
    "list_sync_users",
    "revalidate_sync_user",
    "sync_user",
]


class SyncUserAuthorization(Enum):
    """Whether a stored Plex token still governs the configured server.

    Mirrors the sign-in authorization ladder (``auth._post_init_access``): a
    token is authorized iff plex.tv still advertises the configured server as a
    resource that account can reach.
    """

    AUTHORIZED = "authorized"
    """The token still has access to the configured server -- sync it."""

    STALE = "stale"
    """plex.tv rejected the token, or the account no longer has access to the
    configured server (e.g. after a verified repoint). Skip its sync AND clear its
    snapshot (:func:`clear_user_snapshot`) -- do NOT let its old rows keep creating
    OR protecting requests on the new server."""

    UNKNOWN = "unknown"
    """Authorization could not be determined (plex.tv unreachable). Skip the sync
    this tick but RETAIN the previous snapshot -- a transient plex.tv outage must
    not be read as "no longer authorized"."""


@dataclass
class WatchlistWorkerStatus:
    state: Literal["starting", "ok", "degraded", "disabled", "not_configured", "error"] = field(
        default="starting"
    )
    last_run_at: datetime | None = field(default=None)
    last_ok_at: datetime | None = field(default=None)
    last_error_type: str | None = field(default=None)
    last_error_at: datetime | None = field(default=None)
    fetched: int = field(default=0)
    created: int = field(default=0)
    existing: int = field(default=0)
    failed_users: int = field(default=0)
    failed_entries: int = field(default=0)
    skipped_users: int = field(default=0)
    """Users skipped this tick because their stored token no longer governs the
    configured server (stale after a repoint) or their authorization could not be
    revalidated (plex.tv unreachable). Surfaced so an operator can see WHY a
    watchlist stopped syncing rather than it silently vanishing (north star #3)."""

    def _reset_counters(self) -> None:
        self.fetched = self.created = self.existing = 0
        self.failed_users = self.failed_entries = self.skipped_users = 0

    def mark_started(self) -> None:
        self.last_run_at = datetime.now(UTC)

    def mark_skipped(self, state: Literal["disabled", "not_configured"]) -> None:
        self.state = state
        self.last_error_type = None
        self.last_error_at = None
        self._reset_counters()

    def mark_completed(
        self,
        *,
        fetched: int,
        created: int,
        existing: int,
        failed_users: int,
        failed_entries: int,
        error: str | None,
        skipped_users: int = 0,
    ) -> None:
        self.state = "degraded" if failed_users else "ok"
        if not failed_users:
            self.last_ok_at = datetime.now(UTC)
        self.fetched = fetched
        self.created = created
        self.existing = existing
        self.failed_users = failed_users
        self.failed_entries = failed_entries
        self.skipped_users = skipped_users
        self.last_error_type = error
        self.last_error_at = datetime.now(UTC) if error is not None else None

    def mark_error(self, exc: BaseException) -> None:
        self.state = "error"
        self.last_error_type = type(exc).__name__
        self.last_error_at = datetime.now(UTC)
        self._reset_counters()


@dataclass(frozen=True)
class WatchlistSyncResult:
    fetched: int
    created: int
    existing: int
    failed: int


async def list_sync_users(session: AsyncSession) -> list[User]:
    """Return users with reusable Plex account credentials.

    This is only the candidate set: a stored token does NOT prove the account is
    still authorized for the currently-configured Plex server. The worker
    revalidates each candidate against the configured server
    (:func:`revalidate_sync_user`) before syncing, so a token left behind by a
    verified server repoint cannot keep creating/protecting requests.
    """
    stmt = select(User).where(User.encrypted_plex_token.is_not(None)).order_by(User.id)
    return list((await session.execute(stmt)).scalars().all())


async def revalidate_sync_user(
    plex_tv: PlexTvClient,
    machine_identifier: str,
    *,
    token: str,
) -> SyncUserAuthorization:
    """Re-confirm one stored token still governs the configured Plex server.

    A stored ``User.encrypted_plex_token`` survives a verified server repoint
    (which only revokes browser sessions), so without this check a user from the
    OLD server keeps having their Universal Watchlist create and protect requests
    on the NEW server they can no longer sign into. This applies the same
    plex.tv ``/resources`` + ``account_server_resource`` test the sign-in flow
    uses (``auth._post_init_access``): authorized iff the account still
    advertises the configured server as a reachable resource.

    Distinguishes a rejected/unauthorized token (:attr:`SyncUserAuthorization.
    STALE` -- skip it) from a transient plex.tv failure (:attr:`SyncUserAuthorization.
    UNKNOWN` -- skip this tick but keep the snapshot), so a plex.tv outage is
    never mistaken for a revoked account.
    """
    try:
        resources = await plex_tv.fetch_resources(token)
    except PlexVerifyError as exc:
        # A token plex.tv rejected outright (401/403) can never be re-authorized
        # for the configured server, so its owner is STALE rather than
        # "unknown/retry". Keyed off the oauth adapter's shared error code so the
        # coupling is compile-time, not a hand-copied literal.
        if exc.code == CODE_TOKEN_INVALID:
            return SyncUserAuthorization.STALE
        return SyncUserAuthorization.UNKNOWN
    if account_server_resource(resources, machine_identifier) is None:
        return SyncUserAuthorization.STALE
    return SyncUserAuthorization.AUTHORIZED


async def clear_snapshots(session: AsyncSession) -> int:
    """Delete every stored watchlist snapshot row; return how many were removed.

    Used when ``watchlist_sync_enabled`` is turned off: eviction consults
    :func:`is_watchlisted` unconditionally, so leaving the last snapshot in place
    would keep its titles protected from disk-pressure eviction indefinitely
    even though the operator disabled the feature. Clearing the rows ends that
    protection immediately -- an operator who disables watchlist sync expects
    watchlist-based protection to end, not to silently persist (north star #3).
    Idempotent: a no-op (returns 0) once already cleared.
    """
    result = cast("CursorResult[Any]", await session.execute(delete(WatchlistItem)))
    return result.rowcount or 0


async def clear_user_snapshot(session: AsyncSession, *, user_id: int) -> int:
    """Delete one user's stored watchlist snapshot rows; return how many.

    Used when a candidate token revalidates as :attr:`SyncUserAuthorization.STALE`
    (rejected by plex.tv, or no longer reaches the configured server after a
    verified repoint): eviction protection is an unfiltered ``EXISTS`` over
    :class:`WatchlistItem` by ``(tmdb_id, media_type)`` with no ``user_id``
    predicate (:func:`is_watchlisted`), so a stale user's retained rows would keep
    protecting those titles from disk-pressure eviction indefinitely on the new
    server. Clearing them stops the stale account from both CREATING (skipped
    sync) and PROTECTING (deleted rows) requests -- issue #296 finding 1 requires
    both. Not called for :attr:`SyncUserAuthorization.UNKNOWN`: a transient
    plex.tv outage must not be read as a revoked account, so its snapshot is
    retained. Idempotent (returns 0 once already cleared). Does not commit.
    """
    result = cast(
        "CursorResult[Any]",
        await session.execute(delete(WatchlistItem).where(WatchlistItem.user_id == user_id)),
    )
    return result.rowcount or 0


async def is_watchlisted(session: AsyncSession, tmdb_id: int, media_type: str) -> bool:
    stmt = select(WatchlistItem.user_id).where(
        WatchlistItem.tmdb_id == tmdb_id,
        WatchlistItem.media_type == MediaType(media_type),
    )
    return (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None


async def sync_user(
    session: AsyncSession,
    watchlist: WatchlistPort,
    tmdb: MetadataPort,
    *,
    user_id: int,
    library: LibraryPort | None = None,
) -> WatchlistSyncResult:
    """Replace one complete snapshot, then idempotently request every title."""
    entries = await watchlist.list_entries()
    await session.execute(delete(WatchlistItem).where(WatchlistItem.user_id == user_id))
    session.add_all(
        WatchlistItem(
            user_id=user_id,
            tmdb_id=entry.tmdb_id,
            media_type=MediaType(entry.media_type),
        )
        for entry in entries
    )
    await session.commit()

    created = 0
    existing = 0
    failed = 0
    for entry in entries:
        # TODO(#199 follow-up): apply request quotas/approval policy here through
        # the shared request-policy boundary once that policy exists. Do not put
        # watchlist-only limits in this worker.
        try:
            result = await request_service.create_request_result(
                session,
                tmdb,
                tmdb_id=entry.tmdb_id,
                media_type=entry.media_type,
                user_id=user_id,
                actor_is_admin=False,
                library=library,
                expand_shared_tv=True,
            )
        except Exception:
            await session.rollback()
            failed += 1
            continue
        created += int(result.created)
        existing += int(not result.created)
    return WatchlistSyncResult(
        fetched=len(entries), created=created, existing=existing, failed=failed
    )
