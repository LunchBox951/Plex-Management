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

    async def _reread_one(
        self, season_request_id: int, episode_number: int
    ) -> SeasonEpisodeState | None:
        """Re-read a single row by its unique key -- a FRESH query (not the stale
        bulk ``_existing_by_episode`` snapshot) used to reconcile with the winner
        of an insert race after ``_insert_or_reread`` reports one."""
        stmt = select(SeasonEpisodeState).where(
            SeasonEpisodeState.season_request_id == season_request_id,
            SeasonEpisodeState.episode_number == episode_number,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

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
        # Retire PENDING rows no longer in the aired set (P2, issue #178 review
        # round 2): TMDB can delay an episode to a future/unknown air date or
        # remove it outright after an earlier refresh seeded it. Left in place,
        # the stale pending row keeps counting toward the completion target and
        # the season searches forever for an episode that is not currently
        # aired. Only never-progressed ``pending`` rows are retired -- a
        # ``grabbed``/``imported`` row records REAL work/content (including a
        # round-1 adopted baseline, which is ``imported``) and is kept even if
        # TMDB retracts the episode: completeness counts imported rows, so a
        # kept row can never wedge the season the way a stale pending one does.
        for episode_number, row in existing.items():
            if episode_number not in aired and row.status == EpisodeState.pending:
                await self._session.delete(row)
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
            if await self._insert_or_reread(new_row) is None:
                # A concurrent ``refresh_target``/``upsert_target`` won the insert
                # race after our ``_existing_by_episode`` snapshot. Import and the
                # airing refresh run in separate background tasks, so this is a real
                # race: leaving the winner ``pending`` would let ``apply_import``
                # re-arm/search an episode that was JUST placed. Re-read the winner
                # and promote it to ``imported`` (CAS-style) so import always wins.
                winner = await self._reread_one(season_request_id, episode_number)
                if winner is not None:
                    winner.status = EpisodeState.imported
                    winner.grabbed_download_id = download_id
        await self._session.flush()

    async def adopt_baseline(self, season_request_id: int) -> None:
        """Promote every not-yet-``imported`` row for this season to ``imported``
        with NO backing download (``grabbed_download_id`` stays ``NULL``).

        Baseline adoption (ADR-0020 §6) for an already-watchable season whose
        target rows were just seeded by ``refresh_target`` but which has no real
        imported baseline -- see ``season_episode_service.reconcile_airing`` for
        the full invariant. Idempotent; only the caller (which has just confirmed
        the season had NO rows before the seed) invokes this, so every row it
        touches is a freshly-seeded ``pending`` target row.
        """
        existing = await self._existing_by_episode(season_request_id)
        for row in existing.values():
            if row.status != EpisodeState.imported:
                row.status = EpisodeState.imported
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
