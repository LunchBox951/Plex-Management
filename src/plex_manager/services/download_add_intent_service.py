"""Recovery for durable pre-add torrent intents.

This module deliberately has no grab-service call site yet: PR 1 is the N-1
reader/recovery substrate; direct grab keeps its legacy client-add path until
activation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

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


class _IntentClient(Protocol):
    async def prepare_add(self, magnet_or_url: str) -> PreparedAdd: ...

    async def add_prepared(
        self, prepared: PreparedAdd, save_path: str, category: str
    ) -> AddResult: ...

    async def get_status(self, info_hash: str) -> DownloadStatus | None: ...

    async def set_category(self, info_hash: str, category: str) -> None: ...

    async def remove(self, info_hash: str, *, delete_files: bool) -> None: ...


@dataclass(frozen=True)
class IntentRecoveryResult:
    """Counts used by the reconcile owner to publish invalidations."""

    finalized: int = 0
    removed: int = 0
    needs_attention: int = 0

    @property
    def changed(self) -> bool:
        return self.finalized > 0 or self.removed > 0 or self.needs_attention > 0


def intent_category(intent_id: int) -> str:
    """Return the non-user-supplied qBittorrent category proving intent ownership."""
    return f"plex-manager-intent-{intent_id}"


def _safe_error(exc: Exception) -> str:
    return type(exc).__name__[:_MAX_ERROR_LENGTH]


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

    media_type = "tv" if season is not None else "movie"
    scopes: list[DownloadAddIntentScopeCreate] = []
    if season is None:
        scopes.append(
            DownloadAddIntentScopeCreate(
                tmdb_id=tmdb_id, media_type=media_type, scope_key="movie", is_target=True
            )
        )
    else:
        for covered_season, covered_episodes in (
            scope_episodes_by_season or {season: episodes}
        ).items():
            scopes.append(
                DownloadAddIntentScopeCreate(
                    tmdb_id=tmdb_id,
                    media_type=media_type,
                    scope_key=f"season:{covered_season}",
                    season_number=covered_season,
                    episodes=tuple(covered_episodes) if covered_episodes is not None else None,
                    is_target=covered_season == season,
                )
            )
    candidate = scored.candidate
    return await SqlDownloadAddIntentRepository(session).create(
        CreateDownloadAddIntent(
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
            scopes=tuple(scopes),
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
    existing = await downloads.get_by_hash(current.torrent_hash)
    if existing is None:
        record = await downloads.create(
            torrent_hash=current.torrent_hash,
            status=DownloadState.Downloading.value,
            media_request_id=current.media_request_id,
            magnet_link=current.source,
            tmdb_id=current.tmdb_id,
            year=current.year,
            season=next((scope.season_number for scope in current.scopes if scope.is_target), None),
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
    else:
        record = existing
    for scope in current.scopes:
        if scope.is_target and scope.season_number is not None:
            await downloads.ensure_scope(
                record.id,
                media_request_id=current.media_request_id,
                season=scope.season_number,
                episodes=list(scope.episodes) if scope.episodes else None,
            )
        if current.media_request_id is not None and scope.season_number is not None:
            await downloads.ensure_coverage_claim(
                record.id, media_request_id=current.media_request_id, season=scope.season_number
            )
    if current.media_request_id is not None:
        if current.media_type == "tv":
            target = next((scope for scope in current.scopes if scope.is_target), None)
            if target is not None and target.season_number is not None:
                season_row = await SqlSeasonRequestRepository(session).ensure(
                    current.media_request_id,
                    target.season_number,
                    status=RequestStatus.pending.value,
                )
                moved = await season_request_service.set_status_if_in(
                    session,
                    media_request_id=current.media_request_id,
                    season_request_id=season_row.id,
                    status=RequestStatus.downloading.value,
                    allowed_from=frozenset({current.observed_season_status or season_row.status}),
                )
            else:
                moved = False
        else:
            moved = await SqlRequestRepository(session).set_status_if_in(
                current.media_request_id,
                RequestStatus.downloading.value,
                frozenset({current.observed_request_status or RequestStatus.pending.value}),
            )
        if not moved:
            await session.rollback()
            raise RuntimeError("intent premise no longer active")
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


async def submit_and_finalize(
    qbt: _IntentClient,
    session: AsyncSession,
    *,
    intent: DownloadAddIntentRecord,
    prepared: PreparedAdd | None = None,
) -> DownloadRecord:
    """Submit a prepared intent then atomically exchange it for a tracked download."""
    resolved = prepared
    if resolved is None:
        if intent.source is None:
            raise ValueError("source-less intent requires a present client torrent")
        resolved = await qbt.prepare_add(intent.source)
    if resolved.torrent_hash.lower() != intent.torrent_hash:
        raise ValueError("prepared hash differs from durable intent")
    await qbt.add_prepared(resolved, intent.save_path, intent_category(intent.id))
    return await _finalize_present(qbt, session, intent)


async def recover_all(qbt: _IntentClient, session: AsyncSession) -> IntentRecoveryResult:
    """Recover every committed intent in stable order without recreating cancellations."""
    intents = SqlDownloadAddIntentRepository(session)
    result = IntentRecoveryResult()
    for intent in await intents.list_recoverable():
        status = await qbt.get_status(intent.torrent_hash)
        try:
            if intent.state == "cancel_requested":
                if status is not None and (
                    status.category == intent_category(intent.id) or intent.owns_client_torrent
                ):
                    await qbt.remove(intent.torrent_hash, delete_files=True)
                await intents.delete(intent.id)
                await session.commit()
                result = IntentRecoveryResult(
                    result.finalized, result.removed + 1, result.needs_attention
                )
                continue
            if status is not None:
                await _finalize_present(qbt, session, intent)
            elif intent.source is None:
                await intents.mark_state(
                    intent.id, "needs_attention", last_error="source_unavailable"
                )
                await session.commit()
                result = IntentRecoveryResult(
                    result.finalized, result.removed, result.needs_attention + 1
                )
                continue
            else:
                prepared = await qbt.prepare_add(intent.source)
                if prepared.torrent_hash.lower() != intent.torrent_hash:
                    await intents.mark_state(
                        intent.id, "needs_attention", last_error="prepared_hash_mismatch"
                    )
                    await session.commit()
                    result = IntentRecoveryResult(
                        result.finalized, result.removed, result.needs_attention + 1
                    )
                    continue
                await submit_and_finalize(qbt, session, intent=intent, prepared=prepared)
            result = IntentRecoveryResult(
                result.finalized + 1, result.removed, result.needs_attention
            )
        except Exception as exc:
            await session.rollback()
            await intents.mark_state(intent.id, "needs_attention", last_error=_safe_error(exc))
            await session.commit()
            result = IntentRecoveryResult(
                result.finalized, result.removed, result.needs_attention + 1
            )
    return result
