"""Recovery and reservation lifecycle for durable pre-add torrent intents."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from sqlalchemy.exc import IntegrityError

from plex_manager.adapters.qbittorrent.adapter import QbittorrentSourceError
from plex_manager.domain.state_machine import DownloadState
from plex_manager.models import DownloadHistory, DownloadHistoryEvent, RequestStatus
from plex_manager.ports.download_client import AddResult, DownloadStatus, PreparedAdd
from plex_manager.ports.repositories import (
    CreateDownloadAddIntent,
    DownloadAddIntentRecord,
    DownloadAddIntentScopeCreate,
    DownloadRecord,
)
from plex_manager.repositories.download_add_intents import SqlDownloadAddIntentRepository
from plex_manager.repositories.downloads import SqlDownloadRepository
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.repositories.season_requests import SqlSeasonRequestRepository
from plex_manager.services import season_request_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.domain.release import ScoredRelease

DEFAULT_CATEGORY = "plex-manager"
_logger = logging.getLogger(__name__)
_MAX_ERROR_LENGTH = 180


class IntentRecoveryConflictError(RuntimeError):
    """An intent cannot safely become a tracked download without operator correction."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


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
    qbt: _IntentClient, session: AsyncSession, intent: DownloadAddIntentRecord
) -> DownloadRecord:
    intents = SqlDownloadAddIntentRepository(session)
    downloads = SqlDownloadRepository(session)
    current = await intents.get(intent.id, fresh=True)
    if current is None:
        record = await downloads.get_by_hash(intent.torrent_hash)
        if record is None:
            raise LookupError("durable intent disappeared before finalization")
        return record
    if current.media_request_id is None:
        raise IntentRecoveryConflictError("intent request no longer exists")
    converged = False
    while True:
        existing = await downloads.get_by_hash(current.torrent_hash, populate_existing=True)
        if existing is None:
            record = await downloads.create(
                torrent_hash=current.torrent_hash,
                status=DownloadState.Downloading.value,
                media_request_id=current.media_request_id,
                magnet_link=current.source,
                tmdb_id=current.tmdb_id,
                year=current.year,
                season=next(
                    (scope.season_number for scope in current.scopes if scope.is_target), None
                ),
                episodes=next(
                    (
                        list(scope.episodes)
                        for scope in current.scopes
                        if scope.is_target and scope.episodes
                    ),
                    None,
                ),
                media_type=current.media_type,
                release_title=current.release_title,
                timeout_at=datetime.now(UTC),
            )
            break
        if existing.status in _TERMINAL_DOWNLOAD_STATES:
            # The live client category proves this intent owns the hash, so a terminal
            # historical row is safe to resurrect exactly as a fresh grab would.
            now = datetime.now(UTC)
            claimed = await downloads.update_status_if_in(
                existing.id,
                DownloadState.Downloading.value,
                _TERMINAL_DOWNLOAD_STATES,
                progress=0.0,
                seed_ratio=0.0,
                clear_failed_reason=True,
                clear_first_seen_at=True,
                clear_download_path=True,
                media_request_id=current.media_request_id,
                replace_grab_metadata=True,
                magnet_link=current.source,
                tmdb_id=current.tmdb_id,
                year=current.year,
                season=next(
                    (scope.season_number for scope in current.scopes if scope.is_target), None
                ),
                episodes=next(
                    (
                        list(scope.episodes)
                        for scope in current.scopes
                        if scope.is_target and scope.episodes
                    ),
                    None,
                ),
                media_type=current.media_type,
                release_title=current.release_title,
                added_at=now,
                timeout_at=now,
                retry_count=0,
            )
            if not claimed:
                await session.rollback()
                continue
            refreshed = await downloads.get_by_hash(current.torrent_hash)
            if refreshed is None:  # pragma: no cover - the row was just updated
                raise LookupError("same-hash download disappeared during finalization")
            record = refreshed
            break
        # A same-request/hash convergence still needs the active-row lock before it
        # may attach new scopes. Otherwise an import can finish after the snapshot.
        if not await downloads.lock_if_active(existing.id):
            await session.rollback()
            continue
        refreshed = await downloads.get_by_hash(current.torrent_hash, populate_existing=True)
        if refreshed is None or refreshed.status in _TERMINAL_DOWNLOAD_STATES:
            await session.rollback()
            continue
        if (
            refreshed.media_request_id != current.media_request_id
            or refreshed.tmdb_id != current.tmdb_id
            or refreshed.media_type != current.media_type
        ):
            await session.rollback()
            raise IntentRecoveryConflictError("same-hash download cannot be safely re-owned")
        record = refreshed
        converged = refreshed.media_request_id == current.media_request_id
        break
    for scope in current.scopes:
        if scope.is_target and scope.season_number is not None:
            await downloads.ensure_scope(
                record.id,
                media_request_id=current.media_request_id,
                season=scope.season_number,
                episodes=list(scope.episodes) if scope.episodes else None,
            )
        if scope.season_number is not None:
            await downloads.ensure_coverage_claim(
                record.id, media_request_id=current.media_request_id, season=scope.season_number
            )
    if current.media_type == "tv":
        moved = True
        for target in (scope for scope in current.scopes if scope.is_target):
            if target.season_number is None:
                continue
            season_row = await SqlSeasonRequestRepository(session).ensure(
                current.media_request_id,
                target.season_number,
                status=RequestStatus.pending.value,
            )
            allowed_from = (
                frozenset({current.observed_season_status or season_row.status})
                if target.season_number
                == next(
                    (
                        scope.season_number
                        for scope in current.scopes
                        if scope.is_target and scope.season_number is not None
                    ),
                    None,
                )
                else _PACK_TARGET_SEASON_STATUS_VALUES
            )
            moved = await season_request_service.set_status_if_in(
                session,
                media_request_id=current.media_request_id,
                season_request_id=season_row.id,
                status=RequestStatus.downloading.value,
                allowed_from=allowed_from,
            )
            if not moved:
                break
    else:
        moved = converged or await SqlRequestRepository(session).set_status_if_in(
            current.media_request_id,
            RequestStatus.downloading.value,
            frozenset({current.observed_request_status or RequestStatus.pending.value}),
        )
    if not moved:
        await session.rollback()
        raise IntentRecoveryConflictError("intent_premise_no_longer_active")
    session.add(
        DownloadHistory(
            tmdb_id=current.tmdb_id,
            torrent_hash=current.torrent_hash,
            event_type=DownloadHistoryEvent.grabbed,
            source_title=current.release_title,
            indexer=current.indexer,
            message="recovered durable torrent add",
        )
    )
    await intents.delete(current.id)
    await session.commit()
    try:
        await qbt.set_category(current.torrent_hash, DEFAULT_CATEGORY)
    except Exception as exc:
        _logger.warning("durable intent category normalization deferred (%s)", type(exc).__name__)
    refreshed = await downloads.get_by_hash(current.torrent_hash)
    return refreshed if refreshed is not None else record


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
            await qbt.remove(intent.torrent_hash, delete_files=True)
            await intents.delete(cleanup.id)
            await session.commit()
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
            await qbt.remove(intent.torrent_hash, delete_files=True)
            await intents.delete(intent.id)
            await session.commit()
        return _SubmissionFinalization(None)
    if current.state != "prepared":
        return _SubmissionFinalization(None)
    if result.created:
        return _SubmissionFinalization(await _finalize_present(qbt, session, current))
    status = await qbt.get_status(intent.torrent_hash)
    if _owns_present_torrent(intent, status):
        return _SubmissionFinalization(await _finalize_present(qbt, session, intent))
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
            for intent in await intents.list_for_request(request_id)
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
                # Confirmed absence retires the cleanup record. A live active Download
                # means a newer workflow has converged on this physical torrent, so
                # retain its data and retire only the obsolete cleanup record. An
                # owned, untracked torrent is removed; an unproven live torrent stays
                # durable for a later probe rather than being silently forgotten.
                active = await SqlDownloadRepository(session).get_by_hash(client_hash)
                if status is None or (
                    active is not None and active.status not in _TERMINAL_DOWNLOAD_STATES
                ):
                    await intents.delete(intent.id)
                    await session.commit()
                    result = IntentRecoveryResult(
                        result.finalized, result.removed + 1, result.needs_attention
                    )
                    continue
                if _owns_present_torrent(intent, status):
                    await qbt.remove(client_hash, delete_files=True)
                    await intents.delete(intent.id)
                    await session.commit()
                    result = IntentRecoveryResult(
                        result.finalized, result.removed + 1, result.needs_attention
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
                await _finalize_present(qbt, session, intent)
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
                _owns_present_torrent(intent, status)
                and exc.reason == "intent_premise_no_longer_active"
            ):
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
