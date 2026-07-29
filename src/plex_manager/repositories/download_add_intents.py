"""SQLAlchemy persistence for durable download add intents."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, select, update

from plex_manager.models import DownloadAddIntent, DownloadAddIntentScope
from plex_manager.ports.repositories import (
    CreateDownloadAddIntent,
    DownloadAddIntentRecord,
    DownloadAddIntentScopeRecord,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SqlDownloadAddIntentRepository"]


def _episodes(value: list[Any] | None) -> tuple[int, ...] | None:
    normalized = tuple(sorted({int(item) for item in value or ()}))
    return normalized or None


def _scope_record(row: DownloadAddIntentScope) -> DownloadAddIntentScopeRecord:
    return DownloadAddIntentScopeRecord(
        id=row.id,
        intent_id=row.intent_id,
        media_request_id=row.media_request_id,
        tmdb_id=row.tmdb_id,
        media_type=row.media_type,
        scope_key=row.scope_key,
        season_number=row.season_number,
        episodes=_episodes(row.episodes_json),
        is_target=row.is_target,
    )


def _record(
    row: DownloadAddIntent, scopes: list[DownloadAddIntentScope]
) -> DownloadAddIntentRecord:
    if row.tmdb_id is None or row.media_type is None:
        raise RuntimeError("download add intent has no title identity")
    return DownloadAddIntentRecord(
        id=row.id,
        torrent_hash=row.torrent_hash,
        source=row.source,
        state=row.state,
        media_request_id=row.media_request_id,
        tmdb_id=row.tmdb_id,
        media_type=row.media_type,
        year=row.year,
        release_title=row.release_title,
        indexer=row.indexer,
        quality_name=row.quality_name,
        save_path=row.save_path,
        observed_request_status=row.observed_request_status,
        observed_season_status=row.observed_season_status,
        owns_client_torrent=row.owns_client_torrent,
        last_error=row.last_error,
        scopes=tuple(_scope_record(scope) for scope in scopes),
    )


class SqlDownloadAddIntentRepository:
    """Persist and retrieve the workflow state that bridges an add crash window."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _scopes(self, intent_id: int) -> list[DownloadAddIntentScope]:
        return list(
            (
                await self._session.scalars(
                    select(DownloadAddIntentScope)
                    .where(DownloadAddIntentScope.intent_id == intent_id)
                    .order_by(DownloadAddIntentScope.id)
                )
            ).all()
        )

    async def _to_record(self, row: DownloadAddIntent) -> DownloadAddIntentRecord:
        return _record(row, await self._scopes(row.id))

    async def create(self, command: CreateDownloadAddIntent) -> DownloadAddIntentRecord:
        torrent_hash = command.torrent_hash.lower()
        existing = await self.get_by_hash(torrent_hash)
        if existing is not None:
            return existing
        row = DownloadAddIntent(
            torrent_hash=torrent_hash,
            source=command.source,
            media_request_id=command.media_request_id,
            tmdb_id=command.tmdb_id,
            media_type=command.media_type,
            year=command.year,
            release_title=command.release_title,
            indexer=command.indexer,
            quality_name=command.quality_name,
            save_path=command.save_path,
            observed_request_status=command.observed_request_status,
            observed_season_status=command.observed_season_status,
            owns_client_torrent=command.owns_client_torrent,
        )
        self._session.add(row)
        await self._session.flush()
        for scope in command.scopes:
            self._session.add(
                DownloadAddIntentScope(
                    intent_id=row.id,
                    media_request_id=scope.media_request_id or command.media_request_id,
                    tmdb_id=scope.tmdb_id,
                    media_type=scope.media_type,
                    scope_key=scope.scope_key,
                    season_number=scope.season_number,
                    episodes_json=list(_episodes(list(scope.episodes or ())) or ()) or None,
                    is_target=scope.is_target,
                )
            )
        await self._session.flush()
        return await self._to_record(row)

    async def get(self, intent_id: int, *, fresh: bool = False) -> DownloadAddIntentRecord | None:
        row = await self._session.get(DownloadAddIntent, intent_id, populate_existing=fresh)
        return None if row is None else await self._to_record(row)

    async def get_by_hash(self, torrent_hash: str) -> DownloadAddIntentRecord | None:
        row = await self._session.scalar(
            select(DownloadAddIntent).where(DownloadAddIntent.torrent_hash == torrent_hash.lower())
        )
        return None if row is None else await self._to_record(row)

    async def list_recoverable(self) -> list[DownloadAddIntentRecord]:
        rows = list(
            (
                await self._session.scalars(
                    select(DownloadAddIntent)
                    .where(
                        DownloadAddIntent.state.in_(
                            ("prepared", "needs_attention", "cancel_requested")
                        )
                    )
                    .order_by(DownloadAddIntent.id)
                )
            ).all()
        )
        return [await self._to_record(row) for row in rows]

    async def list_for_request(self, request_id: int) -> list[DownloadAddIntentRecord]:
        rows = list(
            (
                await self._session.scalars(
                    select(DownloadAddIntent)
                    .where(DownloadAddIntent.media_request_id == request_id)
                    .order_by(DownloadAddIntent.id)
                )
            ).all()
        )
        return [await self._to_record(row) for row in rows]

    async def mark_state(
        self,
        intent_id: int,
        state: str,
        *,
        last_error: str | None = None,
        expected_state: str | None = None,
    ) -> bool:
        predicates = [DownloadAddIntent.id == intent_id]
        if expected_state is not None:
            predicates.append(DownloadAddIntent.state == expected_state)
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(DownloadAddIntent)
                .where(*predicates)
                .values(state=state, last_error=last_error)
                .execution_options(synchronize_session="fetch")
            ),
        )
        return result.rowcount == 1

    async def delete(self, intent_id: int) -> bool:
        row = await self._session.get(DownloadAddIntent, intent_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True
