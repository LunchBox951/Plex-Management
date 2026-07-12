"""``SeasonEpisodeStateRepository`` implementation over an :class:`AsyncSession`.

Mirrors :mod:`repositories.season_requests`: frozen read-model DTOs, ``flush``
(never ``commit``) so the repository composes inside a caller-owned unit of
work, and the ``IntegrityError``-catch-and-reread pattern for races on the
``(season_request_id, episode_number)`` unique index.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError

from plex_manager.models import EpisodeState, SeasonEpisodeState
from plex_manager.ports.repositories import SeasonEpisodeStateRecord

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SqlSeasonEpisodeStateRepository"]


def _to_record(row: SeasonEpisodeState) -> SeasonEpisodeStateRecord:
    return SeasonEpisodeStateRecord(
        id=row.id,
        season_request_id=row.season_request_id,
        episode_number=row.episode_number,
        status=row.status.value,
        air_date=row.air_date,
        grabbed_download_id=row.grabbed_download_id,
    )


class SqlSeasonEpisodeStateRepository:
    """Persist and read per-episode fallback-collection state via SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _existing_by_episode(self, season_request_id: int) -> dict[int, SeasonEpisodeState]:
        stmt = select(SeasonEpisodeState).where(
            SeasonEpisodeState.season_request_id == season_request_id
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.episode_number: row for row in rows}

    async def _insert_or_reread(self, row: SeasonEpisodeState) -> SeasonEpisodeState | None:
        """Insert ``row`` under a SAVEPOINT; on a unique-index race, return ``None``
        (the caller re-reads the winner) rather than aborting the whole unit of
        work -- mirrors ``SqlSeasonRequestRepository.ensure``'s race handling.
        """
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError:
            return None
        return row

    async def list_for_season(self, season_request_id: int) -> list[SeasonEpisodeStateRecord]:
        stmt = (
            select(SeasonEpisodeState)
            .where(SeasonEpisodeState.season_request_id == season_request_id)
            .order_by(SeasonEpisodeState.episode_number)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]

    async def upsert_target(self, season_request_id: int, aired: Mapping[int, date | None]) -> None:
        existing = await self._existing_by_episode(season_request_id)
        for episode_number, air_date in aired.items():
            row = existing.get(episode_number)
            if row is not None:
                # Never downgrade progress already made; only refresh the date.
                if row.air_date != air_date:
                    row.air_date = air_date
                continue
            new_row = SeasonEpisodeState(
                season_request_id=season_request_id,
                episode_number=episode_number,
                status=EpisodeState.pending,
                air_date=air_date,
            )
            if await self._insert_or_reread(new_row) is None:
                # A concurrent upsert_target for the same episode won the race;
                # the winner's row already carries an (equal-or-fresher) target.
                continue
        await self._session.flush()

    async def mark_grabbed(
        self, season_request_id: int, episode_numbers: Sequence[int], download_id: int
    ) -> None:
        existing = await self._existing_by_episode(season_request_id)
        for episode_number in episode_numbers:
            row = existing.get(episode_number)
            if row is not None:
                # Never regress a terminal ``imported`` row back to ``grabbed``.
                if row.status != EpisodeState.imported:
                    row.status = EpisodeState.grabbed
                    row.grabbed_download_id = download_id
                continue
            new_row = SeasonEpisodeState(
                season_request_id=season_request_id,
                episode_number=episode_number,
                status=EpisodeState.grabbed,
                grabbed_download_id=download_id,
            )
            await self._insert_or_reread(new_row)
        await self._session.flush()

    async def mark_imported(
        self, season_request_id: int, episode_numbers: Sequence[int], download_id: int
    ) -> None:
        existing = await self._existing_by_episode(season_request_id)
        for episode_number in episode_numbers:
            row = existing.get(episode_number)
            if row is not None:
                row.status = EpisodeState.imported
                row.grabbed_download_id = download_id
                continue
            new_row = SeasonEpisodeState(
                season_request_id=season_request_id,
                episode_number=episode_number,
                status=EpisodeState.imported,
                grabbed_download_id=download_id,
            )
            await self._insert_or_reread(new_row)
        await self._session.flush()

    async def counts_for_seasons(
        self, season_request_ids: Sequence[int]
    ) -> dict[int, tuple[int, int]]:
        if not season_request_ids:
            return {}
        stmt = (
            select(
                SeasonEpisodeState.season_request_id,
                func.sum(case((SeasonEpisodeState.status == EpisodeState.imported, 1), else_=0)),
                func.count(),
            )
            .where(SeasonEpisodeState.season_request_id.in_(season_request_ids))
            .group_by(SeasonEpisodeState.season_request_id)
        )
        rows = (await self._session.execute(stmt)).all()
        return {row[0]: (int(row[1] or 0), int(row[2])) for row in rows}
