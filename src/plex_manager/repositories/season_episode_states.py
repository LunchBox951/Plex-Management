"""``SeasonEpisodeStateRepository`` implementation over an :class:`AsyncSession`.

Mirrors :mod:`repositories.season_requests`: frozen read-model DTOs, ``flush``
(never ``commit``) so the repository composes inside a caller-owned unit of
work, and the ``IntegrityError``-catch-and-reread pattern for races on the
``(season_request_id, episode_number)`` unique index.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import CursorResult, case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError

from plex_manager.models import Download, EpisodeState, SeasonEpisodeState
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
        #
        # The retirement is a GUARDED, DB-side delete (round-4 P2): import and
        # the airing refresh run in separate background tasks, so a concurrent
        # import can promote this row to ``imported`` between the snapshot read
        # above and this delete -- an ORM instance/PK delete would then remove a
        # just-imported row, violating the invariant. ``WHERE status = pending``
        # makes the DATABASE re-verify at write time (the same CAS discipline as
        # every other race guard in this table); a lost race deletes nothing and
        # the promoted row survives. ``synchronize_session=False``: the stale
        # in-session snapshot instance must NOT be evaluated against the
        # criteria (its stale ``pending`` would wrongly expunge the surviving
        # row from the identity map). On a WIN (rowcount 1) nothing re-reads
        # these instances afterward and the caller's commit expires them -- but
        # on a LOST race (rowcount 0, issue #228) the stale ``pending`` instance
        # stays in the identity map, and with ``expire_on_commit=False`` a
        # same-session follow-up read (``reconcile_airing`` -> ``list_for_season``,
        # ``compute_missing``) would serve that stale ``pending`` instead of the
        # promoted winner's row, re-arming/re-searching a just-imported episode.
        # Explicitly expire ONLY the lost-CAS instance so the next read reloads
        # it from the database.
        for episode_number, row in existing.items():
            if episode_number not in aired and row.status == EpisodeState.pending:
                result = cast(
                    CursorResult[Any],
                    await self._session.execute(
                        delete(SeasonEpisodeState)
                        .where(
                            SeasonEpisodeState.id == row.id,
                            SeasonEpisodeState.status == EpisodeState.pending,
                        )
                        .execution_options(synchronize_session=False)
                    ),
                )
                if result.rowcount == 0:
                    # Lost the retirement CAS to a concurrent import promotion:
                    # expire ONLY this instance (not on a win -- the row is
                    # deleted there, and expiring a deleted instance risks
                    # ObjectDeletedError on any later attribute access in this
                    # same session before commit).
                    self._session.expire(row)
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

    async def adopt_baseline(
        self, season_request_id: int, *, episodes: Sequence[int] | None = None
    ) -> None:
        """Promote PENDING rows for this season to ``imported`` with NO backing
        download (``grabbed_download_id`` stays ``NULL``).

        Baseline adoption (ADR-0020 §6) for an already-watchable season whose
        content predates per-episode tracking -- see ``season_episode_service.
        reconcile_airing`` for the full invariant. ``episodes=None`` adopts every
        pending row (the zero-row-season case, where every row is a just-seeded
        pending target row); a sequence restricts adoption to those episode
        numbers (the partial-baseline case, round 3: only episodes provably
        covered by an imported pack). PENDING only, deliberately: a ``grabbed``
        row is evidence of OUR OWN in-flight/failed attempt to fetch the episode
        -- i.e. evidence the library did NOT already own it -- so adopting it
        would silently drop a legitimately wanted episode. Idempotent.
        """
        wanted = None if episodes is None else set(episodes)
        existing = await self._existing_by_episode(season_request_id)
        for row in existing.values():
            if row.status != EpisodeState.pending:
                continue
            if wanted is not None and row.episode_number not in wanted:
                continue
            row.status = EpisodeState.imported
        await self._session.flush()

    async def stale_grabbed_episodes(self, season_request_id: int) -> frozenset[int]:
        """Episode numbers whose row is ``grabbed`` but whose backing download is
        gone (``grabbed_download_id`` NULL -- the FK's ``SET NULL``) or terminally
        DEAD (``failed``/``no_acceptable_release``) -- i.e. the grab breadcrumb no
        longer represents in-flight work.

        Import completeness (P2, issue #178 review round 3) subtracts these from
        its completion target: a fallback grab that failed, for an episode TMDB
        then retracted, would otherwise wedge the season incomplete forever (the
        retraction in :meth:`upsert_target` retires only ``pending`` rows). A
        ``grabbed`` row whose download is LIVE (or ``imported`` -- content
        arrived somewhere even if this episode's file was filtered) still counts:
        excluding real in-flight/delivered work could complete a season whose
        aired episode is genuinely still missing. If the excluded episode IS
        genuinely aired, nothing is lost: the airing refresh sees the TMDB target
        exceed what is imported and re-arms the season to collect it.
        """
        stmt = select(SeasonEpisodeState.episode_number).where(
            SeasonEpisodeState.season_request_id == season_request_id,
            SeasonEpisodeState.status == EpisodeState.grabbed,
            or_(
                SeasonEpisodeState.grabbed_download_id.is_(None),
                select(Download.id)
                .where(
                    Download.id == SeasonEpisodeState.grabbed_download_id,
                    Download.status.in_(["failed", "no_acceptable_release"]),
                )
                .exists(),
            ),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return frozenset(rows)

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
