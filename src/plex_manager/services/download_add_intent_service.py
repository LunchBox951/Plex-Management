"""Recovery and reservation lifecycle for durable pre-add torrent intents."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plex_manager.adapters.qbittorrent.adapter import QbittorrentSourceError
from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import RequestStatus
from plex_manager.ports.download_client import AddResult, DownloadStatus, PreparedAdd
from plex_manager.ports.repositories import (
    CreateDownloadAddIntent,
    DownloadAddIntentRecord,
    DownloadAddIntentScopeCreate,
    DownloadRecord,
)
from plex_manager.repositories.download_add_intents import SqlDownloadAddIntentRepository
from plex_manager.repositories.downloads import SqlDownloadRepository

if TYPE_CHECKING:
    from plex_manager.domain.release import ScoredRelease

DEFAULT_CATEGORY = "plex-manager"
_logger = logging.getLogger(__name__)
_MAX_ERROR_LENGTH = 180
_CLEANUP_LEASE_RENEWAL_SECONDS = 60


class IntentRecoveryConflictError(RuntimeError):
    """An intent cannot safely become a tracked download without operator correction."""

    def __init__(self, reason: str, *, owned_client_torrent: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.owned_client_torrent = owned_client_torrent


class ParkedIntentHashError(RuntimeError):
    """A release hash belongs to parked operator history, not an active claim."""


_TERMINAL_DOWNLOAD_STATES = frozenset(
    {
        DownloadState.Imported.value,
        DownloadState.Failed.value,
        DownloadState.NoAcceptableRelease.value,
    }
)
_PACK_TARGET_SEASON_STATUS_VALUES = frozenset(
    {
        RequestStatus.pending.value,
        RequestStatus.searching.value,
        RequestStatus.no_acceptable_release.value,
        RequestStatus.failed.value,
    }
)


class _IntentClient(Protocol):
    async def prepare_add(self, magnet_or_url: str) -> PreparedAdd:
        raise NotImplementedError

    async def add_prepared(self, prepared: PreparedAdd, save_path: str, category: str) -> AddResult:
        raise NotImplementedError

    async def get_status(self, info_hash: str) -> DownloadStatus | None:
        raise NotImplementedError

    async def set_category(self, info_hash: str, category: str) -> None:
        raise NotImplementedError

    async def remove(self, info_hash: str, *, delete_files: bool) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class IntentRecoveryResult:
    """Counts used by the reconcile owner to publish invalidations."""

    finalized: int = 0
    removed: int = 0
    needs_attention: int = 0

    @property
    def changed(self) -> bool:
        return self.finalized > 0 or self.removed > 0 or self.needs_attention > 0


@dataclass(frozen=True)
class _SubmissionFinalization:
    """A submission's tracked download and whether this call parked its intent."""

    record: DownloadRecord | None
    parked: bool = False


def intent_category(intent_id: int) -> str:
    """Return the non-user-supplied qBittorrent category proving intent ownership."""
    return f"plex-manager-intent-{intent_id}"


def _safe_error(exc: Exception) -> str:
    return type(exc).__name__[:_MAX_ERROR_LENGTH]


def _intent_scopes(
    *,
    scored: ScoredRelease,
    tmdb_id: int,
    season: int | None,
    episodes: list[int] | None,
    scope_episodes_by_season: Mapping[int, Sequence[int] | None] | None,
) -> tuple[DownloadAddIntentScopeCreate, ...]:
    """Build the physical title footprint that an intent owns until finalization."""
    media_type = "tv" if season is not None else "movie"
    scopes: list[DownloadAddIntentScopeCreate] = []
    if season is None:
        scopes.append(
            DownloadAddIntentScopeCreate(
                tmdb_id=tmdb_id, media_type=media_type, scope_key="movie", is_target=True
            )
        )
    else:
        targets = tuple(dict.fromkeys((season, *scored.target_seasons)))
        coverage = tuple(dict.fromkeys((*targets, *scored.covered_seasons)))
        for covered_season in coverage:
            target_episodes = (
                episodes
                if covered_season == season
                else (
                    scope_episodes_by_season.get(covered_season)
                    if scope_episodes_by_season is not None and covered_season in targets
                    else None
                )
            )
            scopes.append(
                DownloadAddIntentScopeCreate(
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    scope_key=f"season:{covered_season}",
                    season_number=covered_season,
                    episodes=tuple(target_episodes) if target_episodes is not None else None,
                    is_target=covered_season in targets,
                )
            )
    return tuple(scopes)


def _intent_command(
    *,
    scored: ScoredRelease,
    prepared: PreparedAdd,
    request_id: int | None,
    tmdb_id: int,
    year: int | None,
    season: int | None,
    episodes: list[int] | None,
    save_path: str,
    observed_request_status: str | None,
    observed_season_status: str | None,
    scope_episodes_by_season: Mapping[int, Sequence[int] | None] | None,
) -> CreateDownloadAddIntent:
    media_type = "tv" if season is not None else "movie"
    candidate = scored.candidate
    return CreateDownloadAddIntent(
        torrent_hash=prepared.torrent_hash,
        source=candidate.magnet_url or candidate.download_url,
        media_request_id=request_id,
        tmdb_id=tmdb_id,
        media_type=media_type,
        year=year,
        release_title=candidate.title,
        indexer=candidate.indexer_name,
        quality_name=scored.quality.name,
        save_path=save_path,
        observed_request_status=observed_request_status,
        observed_season_status=observed_season_status,
        scopes=_intent_scopes(
            scored=scored,
            tmdb_id=tmdb_id,
            season=season,
            episodes=episodes,
            scope_episodes_by_season=scope_episodes_by_season,
        ),
    )


async def reserve_intent(
    session: AsyncSession,
    *,
    scored: ScoredRelease,
    prepared: PreparedAdd,
    request_id: int | None,
    tmdb_id: int,
    year: int | None,
    season: int | None,
    episodes: list[int] | None,
    save_path: str,
    observed_request_status: str | None,
    observed_season_status: str | None,
    scope_episodes_by_season: Mapping[int, Sequence[int] | None] | None,
) -> DownloadAddIntentRecord | None:
    """Atomically reserve a candidate footprint before its client submission.

    A committed reservation uses the same unique scope domain as every other
    durable add. A competing publisher returns ``None`` without client mutation;
    a crash leaves the prepared intent for normal recovery rather than wedging a
    scope behind an in-process lock.
    """
    repository = SqlDownloadAddIntentRepository(session)
    intent = await repository.try_create(
        _intent_command(
            scored=scored,
            prepared=prepared,
            request_id=request_id,
            tmdb_id=tmdb_id,
            year=year,
            season=season,
            episodes=episodes,
            save_path=save_path,
            observed_request_status=observed_request_status,
            observed_season_status=observed_season_status,
            scope_episodes_by_season=scope_episodes_by_season,
        )
    )
    if intent is not None:
        await session.commit()
        return intent
    existing = await repository.get_by_hash(prepared.torrent_hash)
    if existing is not None and existing.state == "needs_attention":
        raise ParkedIntentHashError(prepared.torrent_hash)
    return None


async def publish_intent(
    session: AsyncSession,
    *,
    scored: ScoredRelease,
    prepared: PreparedAdd,
    request_id: int | None,
    tmdb_id: int,
    year: int | None,
    season: int | None,
    episodes: list[int] | None,
    save_path: str,
    observed_request_status: str | None,
    observed_season_status: str | None,
    scope_episodes_by_season: Mapping[int, Sequence[int] | None] | None,
) -> DownloadAddIntentRecord:
    """Publish a hash-keyed intent; activation wires this before client submission."""

    return await SqlDownloadAddIntentRepository(session).create(
        _intent_command(
            scored=scored,
            prepared=prepared,
            request_id=request_id,
            tmdb_id=tmdb_id,
            year=year,
            season=season,
            episodes=episodes,
            save_path=save_path,
            observed_request_status=observed_request_status,
            observed_season_status=observed_season_status,
            scope_episodes_by_season=scope_episodes_by_season,
        )
    )


async def _finalize_present(
    qbt: _IntentClient,
    session: AsyncSession,
    intent: DownloadAddIntentRecord,
    *,
    actually_added: bool = False,
    owned_client_torrent: bool = False,
) -> DownloadRecord:
    """Exchange a proven owned intent for a tracked download atomically."""
    intents = SqlDownloadAddIntentRepository(session)
    current = await intents.get(intent.id, fresh=True)
    if current is None:
        record = await SqlDownloadRepository(session).get_by_hash(intent.torrent_hash)
        if record is None:
            raise LookupError("durable intent disappeared before finalization")
        return record
    if current.media_request_id is None:
        raise IntentRecoveryConflictError(
            "intent request no longer exists", owned_client_torrent=owned_client_torrent
        )

    target_scopes = tuple(scope for scope in current.scopes if scope.is_target)
    primary = target_scopes[0] if target_scopes else None
    season = primary.season_number if primary is not None else None
    episodes = list(primary.episodes) if primary is not None and primary.episodes else None
    target_seasons = tuple(
        scope.season_number for scope in target_scopes if scope.season_number is not None
    )
    active_guard_seasons = tuple(
        scope.season_number for scope in current.scopes if scope.season_number is not None
    ) or (season,)
    scope_episodes_by_season = {
        scope.season_number: scope.episodes
        for scope in target_scopes
        if scope.season_number is not None
    }
    existing = await SqlDownloadRepository(session).get_by_hash(current.torrent_hash)
    # Any active same-request hash must enter the authority's locked convergence
    # path. It re-reads and validates the full immutable media identity before
    # attachment; treating a mismatched row as an ordinary duplicate would accept
    # it via the movie early return and delete this intent.
    require_active_existing = (
        existing is not None
        and existing.status not in _TERMINAL_DOWNLOAD_STATES
        and existing.media_request_id == current.media_request_id
    )
    from plex_manager.services.grab_service import (
        RequestNotActiveError,
        TorrentAlreadyTrackedError,
        register_submitted_download,
    )

    try:
        record = await register_submitted_download(
            qbt,
            session,
            torrent_hash=current.torrent_hash,
            actually_added=actually_added,
            request_id=current.media_request_id,
            source=current.source or "",
            tmdb_id=current.tmdb_id,
            year=current.year,
            season=season,
            episodes=episodes,
            request_media_type=current.media_type,
            release_title=current.release_title,
            indexer=current.indexer or "unknown",
            target_seasons=target_seasons,
            active_guard_seasons=active_guard_seasons,
            observed_request_status=current.observed_request_status,
            observed_season_status=current.observed_season_status,
            scope_episodes_by_season=scope_episodes_by_season,
            history_message="recovered durable torrent add",
            commit=False,
            require_active_existing=require_active_existing,
            bypass_premise_on_convergence=require_active_existing,
        )
    except RequestNotActiveError as exc:
        raise IntentRecoveryConflictError(
            "intent_premise_no_longer_active", owned_client_torrent=owned_client_torrent
        ) from exc
    except TorrentAlreadyTrackedError as exc:
        # The active row may finish importing after the snapshot but before its
        # convergence lock. Re-read and delegate terminal reuse back to authority.
        refreshed = await SqlDownloadRepository(session).get_by_hash(
            current.torrent_hash, populate_existing=True
        )
        if (
            require_active_existing
            and refreshed is not None
            and refreshed.status in _TERMINAL_DOWNLOAD_STATES
        ):
            return await _finalize_present(
                qbt,
                session,
                current,
                actually_added=actually_added,
                owned_client_torrent=owned_client_torrent,
            )
        raise IntentRecoveryConflictError(
            type(exc).__name__, owned_client_torrent=owned_client_torrent
        ) from exc
    await intents.delete(current.id)
    await session.commit()
    try:
        await qbt.set_category(current.torrent_hash, DEFAULT_CATEGORY)
    except Exception as exc:
        _logger.warning("durable intent category normalization deferred (%s)", type(exc).__name__)
    return record


def _client_hash(intent: DownloadAddIntentRecord) -> str:
    """Return the real client hash for ordinary and synthetic cleanup intents."""
    return intent.cleanup_torrent_hash or intent.torrent_hash


def _owns_present_torrent(intent: DownloadAddIntentRecord, status: DownloadStatus | None) -> bool:
    """Return whether a client torrent is proven to belong to this intent."""
    if status is None:
        return False
    if intent.cleanup_category is not None:
        return status.category == intent.cleanup_category
    return intent.owns_client_torrent or status.category == intent_category(intent.id)


def _same_intent_identity(
    current: DownloadAddIntentRecord, original: DownloadAddIntentRecord
) -> bool:
    """Return whether an id re-read is still the submission's original intent."""
    return (
        current.torrent_hash == original.torrent_hash
        and current.media_request_id == original.media_request_id
        and current.tmdb_id == original.tmdb_id
        and current.media_type == original.media_type
        and current.source == original.source
    )


async def create_late_cleanup(
    session: AsyncSession, intent: DownloadAddIntentRecord
) -> DownloadAddIntentRecord:
    """Durably retain a cancellation sweep record for a late owned add."""
    intents = SqlDownloadAddIntentRepository(session)
    cleanup = await intents.try_create(
        CreateDownloadAddIntent(
            torrent_hash=intent.torrent_hash,
            media_request_id=intent.media_request_id,
            tmdb_id=intent.tmdb_id,
            media_type=intent.media_type,
            year=intent.year,
            release_title=intent.release_title,
            indexer=intent.indexer,
            quality_name=intent.quality_name,
            save_path=intent.save_path,
            observed_request_status=intent.observed_request_status,
            observed_season_status=intent.observed_season_status,
            owns_client_torrent=True,
        )
    )
    if cleanup is None:
        # A new request now owns the real hash reservation. Retain the old add's
        # category identity in a synthetic, claim-less cleanup row instead of ever
        # mutating the collision owner.
        cleanup = await intents.create(
            CreateDownloadAddIntent(
                torrent_hash=f"cleanup:{intent.id}:{intent.torrent_hash}",
                media_request_id=intent.media_request_id,
                tmdb_id=intent.tmdb_id,
                media_type=intent.media_type,
                year=intent.year,
                release_title=intent.release_title,
                indexer=intent.indexer,
                quality_name=intent.quality_name,
                save_path=intent.save_path,
                observed_request_status=intent.observed_request_status,
                observed_season_status=intent.observed_season_status,
                owns_client_torrent=True,
                cleanup_torrent_hash=intent.torrent_hash,
                cleanup_category=intent_category(intent.id),
            )
        )
    if cleanup.state == "prepared" and not await intents.mark_state(
        cleanup.id, "cancel_requested", expected_state="prepared"
    ):
        raise IntentRecoveryConflictError("late cleanup intent changed before removal")
    await session.commit()
    refreshed = await intents.get(cleanup.id, fresh=True)
    if refreshed is None:  # pragma: no cover - the cleanup row was just committed
        raise LookupError("late cleanup intent disappeared after creation")
    return refreshed


async def _renew_cleanup_lease_until_done(
    session: AsyncSession, intent_id: int, token: str
) -> None:
    """Keep a live cleanup lease fresh; a reclaimed token stops this worker."""
    async with AsyncSession(bind=session.bind) as renewal_session:
        intents = SqlDownloadAddIntentRepository(renewal_session)
        while True:
            await asyncio.sleep(_CLEANUP_LEASE_RENEWAL_SECONDS)
            if not await intents.renew_cleanup_lease(intent_id, token):
                await renewal_session.rollback()
                return
            await renewal_session.commit()


async def _await_client_removal(
    qbt: _IntentClient,
    session: AsyncSession,
    *,
    client_hash: str,
    intent_id: int,
    token: str,
) -> bool:
    """Fence and heartbeat the lease through a potentially slow client removal."""
    intents = SqlDownloadAddIntentRepository(session)
    if not await intents.renew_cleanup_lease(intent_id, token):
        await session.rollback()
        return False
    await session.commit()
    heartbeat = asyncio.create_task(_renew_cleanup_lease_until_done(session, intent_id, token))
    try:
        await qbt.remove(client_hash, delete_files=True)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
    return await intents.has_cleanup_lease(intent_id, token)


@dataclass(frozen=True)
class _CleanupRecovery:
    """The lease-guarded result of processing one cancellation cleanup record."""

    settled: bool = False
    needs_attention: bool = False


async def _retire_or_remove_late_cleanup(
    qbt: _IntentClient,
    session: AsyncSession,
    cleanup: DownloadAddIntentRecord,
) -> _CleanupRecovery:
    """Settle cancellation cleanup while holding its durable, fenced worker lease."""
    client_hash = _client_hash(cleanup)
    intents = SqlDownloadAddIntentRepository(session)
    # Every cancellation disposition, including synthetic-to-real conversion,
    # begins with a token claim on the current durable cleanup row.
    lease = await intents.acquire_cleanup_lease(cleanup.id)
    if lease is None:
        await session.rollback()
        return _CleanupRecovery()
    await session.commit()

    target = cleanup
    cleanup_lease = lease
    try:
        if cleanup.torrent_hash != client_hash:
            temporary_claim = await intents.try_create(
                CreateDownloadAddIntent(
                    torrent_hash=client_hash,
                    media_request_id=cleanup.media_request_id,
                    tmdb_id=cleanup.tmdb_id,
                    media_type=cleanup.media_type,
                    save_path=cleanup.save_path,
                    owns_client_torrent=True,
                )
            )
            if temporary_claim is None:
                # ``try_create`` also returns None for non-uniqueness failures. Only
                # a re-read real-hash owner proves a successor may own this torrent.
                owner = await intents.get_by_hash(client_hash)
                if owner is None:
                    raise RuntimeError("late cleanup claim insertion did not produce a hash owner")
                if not await intents.delete_with_cleanup_lease(cleanup.id, lease):
                    await session.rollback()
                    return _CleanupRecovery()
                await session.commit()
                return _CleanupRecovery(settled=True)
            if not await intents.mark_state(
                temporary_claim.id, "cancel_requested", expected_state="prepared"
            ):
                raise IntentRecoveryConflictError("late cleanup claim changed before removal")
            # Swap synthetic identity while still fenced by its original row. The
            # temporary real-hash claim must itself acquire a fresh lease before I/O.
            if not await intents.delete_with_cleanup_lease(cleanup.id, lease):
                await session.rollback()
                return _CleanupRecovery()
            await session.commit()
            target = temporary_claim
            lease = await intents.acquire_cleanup_lease(target.id)
            if lease is None:
                await session.rollback()
                return _CleanupRecovery()
            cleanup_lease = lease
            await session.commit()

        # Probe after the lease claim: an absence/convergence result can settle
        # only the record whose token we still own below.
        status = await qbt.get_status(client_hash)
        active = await SqlDownloadRepository(session).get_by_hash(client_hash)
        if status is None or (
            active is not None and active.status not in _TERMINAL_DOWNLOAD_STATES
        ):
            if not await intents.delete_with_cleanup_lease(target.id, lease):
                await session.rollback()
                return _CleanupRecovery()
            await session.commit()
            return _CleanupRecovery(settled=True)

        if not _owns_present_torrent(target, status):
            if not await intents.set_cleanup_error(
                target.id, lease, "client_hash_ownership_unproven"
            ):
                await session.rollback()
                return _CleanupRecovery()
            # The error update proves our fence before this non-destructive
            # disposition; release is token-matched so a reclaimer is untouched.
            if not await intents.release_cleanup_lease(target.id, lease):
                await session.rollback()
                return _CleanupRecovery()
            await session.commit()
            return _CleanupRecovery(needs_attention=True)

        if not await _await_client_removal(
            qbt,
            session,
            client_hash=client_hash,
            intent_id=target.id,
            token=lease,
        ):
            await session.rollback()
            return _CleanupRecovery()
        if not await intents.delete_with_cleanup_lease(target.id, lease):
            await session.rollback()
            return _CleanupRecovery()
        await session.commit()
        return _CleanupRecovery(settled=True)
    except Exception:
        # This covers ambiguous client errors AND a database failure after a
        # confirmed removal. Token-matched release cannot clear a newer lease.
        await session.rollback()
        await intents.release_cleanup_lease(target.id, cleanup_lease)
        await session.commit()
        raise


async def submit_and_finalize(
    qbt: _IntentClient,
    session: AsyncSession,
    *,
    intent: DownloadAddIntentRecord,
    prepared: PreparedAdd | None = None,
) -> _SubmissionFinalization:
    """Submit a prepared intent then atomically exchange it for a tracked download."""
    resolved = prepared
    if resolved is None:
        if intent.source is None:
            raise ValueError("source-less intent requires a present client torrent")
        resolved = await qbt.prepare_add(intent.source)
    if resolved.torrent_hash.lower() != intent.torrent_hash:
        raise ValueError("prepared hash differs from durable intent")
    intents = SqlDownloadAddIntentRepository(session)
    current = await intents.get(intent.id, fresh=True)
    if current is None or not _same_intent_identity(current, intent) or current.state != "prepared":
        return _SubmissionFinalization(None)
    result = await qbt.add_prepared(resolved, intent.save_path, intent_category(intent.id))
    current = await intents.get(intent.id, fresh=True)
    if current is None or not _same_intent_identity(current, intent):
        # The id can be reused after cancellation deletes the original row. Treat a
        # different immutable reservation as the old intent having disappeared.
        # Cancellation can conclusively observe absence and delete its intent while
        # this add POST is still in flight. ``created`` is proof this submission
        # owns the resulting torrent even without a remaining intent row. Recreate
        # a cancellation-only intent before removal so a transport failure remains
        # visible to the normal retry sweep instead of orphaning the torrent.
        if result.created:
            cleanup = await create_late_cleanup(session, intent)
            await _retire_or_remove_late_cleanup(qbt, session, cleanup)
        else:
            status = await qbt.get_status(intent.torrent_hash)
            if _owns_present_torrent(intent, status):
                await qbt.remove(intent.torrent_hash, delete_files=True)
        return _SubmissionFinalization(None)
    if current.state == "cancel_requested":
        # Cancellation may win while the add request is in flight. A created result
        # proves this intent owns the just-added torrent, so remove it immediately;
        # if removal fails, leave cancel_requested for normal cleanup recovery.
        if result.created:
            # The still-cancelled reservation uses the same lease-fenced cleanup
            # path as background recovery; a concurrent sweeper cannot retire it
            # while this late add is being removed.
            await _retire_or_remove_late_cleanup(qbt, session, current)
        return _SubmissionFinalization(None)
    if current.state != "prepared":
        return _SubmissionFinalization(None)
    if result.created:
        return _SubmissionFinalization(
            await _finalize_present(
                qbt,
                session,
                current,
                actually_added=True,
                owned_client_torrent=True,
            )
        )
    status = await qbt.get_status(intent.torrent_hash)
    if _owns_present_torrent(intent, status):
        return _SubmissionFinalization(
            await _finalize_present(
                qbt,
                session,
                intent,
                actually_added=False,
                owned_client_torrent=True,
            )
        )
    if status is None:
        # A duplicate only proves the hash exists. If its status cannot be read,
        # retain the prepared reservation so a later sweep can re-probe safely.
        return _SubmissionFinalization(None)
    parked = await _park_needs_attention(
        session,
        SqlDownloadAddIntentRepository(session),
        intent.id,
        "client_hash_ownership_unproven",
    )
    return _SubmissionFinalization(None, parked=parked)


async def park_intent(session: AsyncSession, intent_id: int, error: str) -> bool:
    """Park a prepared intent and release its scope claim for a fresh retry."""
    return await _park_needs_attention(
        session, SqlDownloadAddIntentRepository(session), intent_id, error
    )


async def _park_needs_attention(
    session: AsyncSession,
    intents: SqlDownloadAddIntentRepository,
    intent_id: int,
    error: str,
) -> bool:
    """Park a still-prepared intent once; a concurrent transition wins silently."""
    marked = await intents.mark_state(
        intent_id, "needs_attention", last_error=error, expected_state="prepared"
    )
    if marked:
        await session.commit()
    return marked


async def recover_all(qbt: _IntentClient, session: AsyncSession) -> IntentRecoveryResult:
    """Recover every committed intent in stable order without recreating cancellations."""
    intents = SqlDownloadAddIntentRepository(session)
    return await _recover(qbt, session, await intents.list_recoverable())


async def recover_for_request(
    qbt: _IntentClient, session: AsyncSession, *, request_id: int
) -> IntentRecoveryResult:
    """Recover only one request's durable intents after its cancellation commits."""
    intents = SqlDownloadAddIntentRepository(session)
    return await _recover(
        qbt,
        session,
        [
            intent
            for intent in await intents.list_for_request(request_id, recoverable_only=True)
            if intent.state in {"prepared", "cancel_requested"}
        ],
    )


async def _recover(
    qbt: _IntentClient,
    session: AsyncSession,
    recoverable: Sequence[DownloadAddIntentRecord],
) -> IntentRecoveryResult:
    """Recover the supplied durable intents without widening a caller's scope."""
    intents = SqlDownloadAddIntentRepository(session)
    result = IntentRecoveryResult()
    for intent in recoverable:
        client_hash = _client_hash(intent)
        status = await qbt.get_status(client_hash)
        try:
            if intent.state == "cancel_requested":
                # Every disposition (absence, active-download convergence, unproven
                # ownership, and removal) is serialized by the cleanup helper's
                # token-conditional lease. A live holder always wins unchanged.
                cleanup = await _retire_or_remove_late_cleanup(qbt, session, intent)
                if cleanup.settled:
                    result = IntentRecoveryResult(
                        result.finalized, result.removed + 1, result.needs_attention
                    )
                elif cleanup.needs_attention:
                    result = IntentRecoveryResult(
                        result.finalized, result.removed, result.needs_attention + 1
                    )
                continue
            if status is not None:
                if not _owns_present_torrent(intent, status):
                    if await _park_needs_attention(
                        session, intents, intent.id, "client_hash_ownership_unproven"
                    ):
                        result = IntentRecoveryResult(
                            result.finalized, result.removed, result.needs_attention + 1
                        )
                    continue
                await _finalize_present(
                    qbt,
                    session,
                    intent,
                    actually_added=False,
                    owned_client_torrent=_owns_present_torrent(intent, status),
                )
            elif intent.source is None:
                if await _park_needs_attention(session, intents, intent.id, "source_unavailable"):
                    result = IntentRecoveryResult(
                        result.finalized, result.removed, result.needs_attention + 1
                    )
                continue
            else:
                try:
                    prepared = await qbt.prepare_add(intent.source)
                except QbittorrentSourceError as exc:
                    if await _park_needs_attention(
                        session, intents, intent.id, f"source_error:{_safe_error(exc)}"
                    ):
                        _logger.warning(
                            "durable intent %s needs operator attention (%s)", intent.id, exc
                        )
                        result = IntentRecoveryResult(
                            result.finalized, result.removed, result.needs_attention + 1
                        )
                    continue
                if prepared.torrent_hash.lower() != intent.torrent_hash:
                    if await _park_needs_attention(
                        session, intents, intent.id, "prepared_hash_mismatch"
                    ):
                        result = IntentRecoveryResult(
                            result.finalized, result.removed, result.needs_attention + 1
                        )
                    continue
                finalization = await submit_and_finalize(
                    qbt, session, intent=intent, prepared=prepared
                )
                if finalization.record is None:
                    if finalization.parked:
                        result = IntentRecoveryResult(
                            result.finalized, result.removed, result.needs_attention + 1
                        )
                    continue
            result = IntentRecoveryResult(
                result.finalized + 1, result.removed, result.needs_attention
            )
        except IntentRecoveryConflictError as exc:
            await session.rollback()
            if (
                exc.owned_client_torrent or _owns_present_torrent(intent, status)
            ) and exc.reason == "intent_premise_no_longer_active":
                # The live intent category proves this torrent is ours, but the
                # decision premise is gone. Keep it in the existing machine-cleanup
                # lifecycle rather than parking an invisible seeding torrent.
                if await intents.mark_state(
                    intent.id, "cancel_requested", expected_state="prepared"
                ):
                    await session.commit()
                continue
            if await _park_needs_attention(session, intents, intent.id, exc.reason):
                _logger.warning("durable intent %s needs operator attention (%s)", intent.id, exc)
                result = IntentRecoveryResult(
                    result.finalized, result.removed, result.needs_attention + 1
                )
        except IntegrityError:
            await session.rollback()
            # This recovery invocation submitted under this intent's category, so a
            # persistence collision after that submit leaves a client torrent owned by
            # the intent. Retire it into the cancellation cleanup lifecycle rather
            # than retrying the same collision and starving later intents.
            if await intents.mark_state(intent.id, "cancel_requested", expected_state="prepared"):
                await session.commit()
            continue
        except Exception as exc:
            await session.rollback()
            _logger.warning(
                "durable intent %s recovery deferred by %s", intent.id, type(exc).__name__
            )
            raise
    return result
