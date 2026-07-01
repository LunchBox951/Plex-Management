"""``SeasonRequestRepository`` implementation over an :class:`AsyncSession`.

Mirrors :mod:`repositories.requests` (``SqlRequestRepository``): frozen read-model
DTOs, ``flush`` (never ``commit``) so the repository composes inside a
caller-owned unit of work. ``tmdb_id`` on :class:`SeasonRequestRecord` is
denormalized from the parent ``MediaRequest`` via a join -- ``season_requests``
itself carries no ``tmdb_id`` column.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from plex_manager.models import MediaRequest, RequestStatus, SeasonRequest
from plex_manager.ports.repositories import SeasonRequestRecord

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SqlSeasonRequestRepository"]


def _to_record(row: SeasonRequest, tmdb_id: int) -> SeasonRequestRecord:
    """Map a ``SeasonRequest`` ORM row (+ its parent's ``tmdb_id``) to the DTO."""
    return SeasonRequestRecord(
        id=row.id,
        media_request_id=row.media_request_id,
        season_number=row.season_number,
        status=row.status.value,
        tmdb_id=tmdb_id,
    )


class SqlSeasonRequestRepository:
    """Persist and read per-season TV requests via SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _find(self, media_request_id: int, season_number: int) -> SeasonRequest | None:
        """Return the raw ORM row for ``(media_request_id, season_number)``, or ``None``."""
        stmt = select(SeasonRequest).where(
            SeasonRequest.media_request_id == media_request_id,
            SeasonRequest.season_number == season_number,
        )
        return (await self._session.execute(stmt)).scalars().first()

    async def _tmdb_id_for(self, media_request_id: int) -> int:
        """Return the parent show's ``tmdb_id`` (the FK guarantees it exists)."""
        tmdb_id = await self._session.scalar(
            select(MediaRequest.tmdb_id).where(MediaRequest.id == media_request_id)
        )
        if tmdb_id is None:  # pragma: no cover - FK guarantees the parent row exists
            raise LookupError(f"media request {media_request_id} does not exist")
        return tmdb_id

    async def get(self, season_request_id: int) -> SeasonRequestRecord | None:
        row = await self._session.get(SeasonRequest, season_request_id)
        if row is None:
            return None
        return _to_record(row, await self._tmdb_id_for(row.media_request_id))

    async def list_for_request(self, media_request_id: int) -> list[SeasonRequestRecord]:
        stmt = (
            select(SeasonRequest)
            .where(SeasonRequest.media_request_id == media_request_id)
            .order_by(SeasonRequest.season_number)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        if not rows:
            return []
        tmdb_id = await self._tmdb_id_for(media_request_id)
        return [_to_record(row, tmdb_id) for row in rows]

    async def list_for_requests(
        self, media_request_ids: Sequence[int]
    ) -> dict[int, list[SeasonRequestRecord]]:
        if not media_request_ids:
            return {}
        # JOIN straight to the parent's tmdb_id in ONE query -- avoids both an N+1
        # per request row AND the per-distinct-show follow-up lookup
        # ``list_by_status`` needs (there ``tmdb_id`` isn't otherwise in hand).
        stmt = (
            select(SeasonRequest, MediaRequest.tmdb_id)
            .join(MediaRequest, MediaRequest.id == SeasonRequest.media_request_id)
            .where(SeasonRequest.media_request_id.in_(media_request_ids))
            .order_by(SeasonRequest.media_request_id, SeasonRequest.season_number)
        )
        grouped: dict[int, list[SeasonRequestRecord]] = {}
        for row, tmdb_id in (await self._session.execute(stmt)).all():
            grouped.setdefault(row.media_request_id, []).append(_to_record(row, tmdb_id))
        return grouped

    async def list_by_status(self, status: str | None = None) -> list[SeasonRequestRecord]:
        stmt = select(SeasonRequest)
        if status is not None:
            stmt = stmt.where(SeasonRequest.status == RequestStatus(status))
        stmt = stmt.order_by(SeasonRequest.id)
        rows = (await self._session.execute(stmt)).scalars().all()
        # One tmdb_id lookup per distinct show, not one per row -- a show with
        # several tracked seasons only needs its tmdb_id resolved once.
        tmdb_ids: dict[int, int] = {}
        records: list[SeasonRequestRecord] = []
        for row in rows:
            if row.media_request_id not in tmdb_ids:
                tmdb_ids[row.media_request_id] = await self._tmdb_id_for(row.media_request_id)
            records.append(_to_record(row, tmdb_ids[row.media_request_id]))
        return records

    async def ensure(
        self, media_request_id: int, season_number: int, *, status: str
    ) -> SeasonRequestRecord:
        existing = await self._find(media_request_id, season_number)
        if existing is not None:
            return _to_record(existing, await self._tmdb_id_for(media_request_id))

        row = SeasonRequest(
            media_request_id=media_request_id,
            season_number=season_number,
            status=RequestStatus(status),
        )
        try:
            # A SAVEPOINT (not a full transaction rollback): on IntegrityError only
            # THIS insert is undone. ``request_service.create_request`` /
            # ``grab_service.grab`` can get away with a plain ``session.rollback()``
            # because their insert is the FIRST write of their transaction; ``ensure()``
            # has no such guarantee -- it is meant to be called repeatedly, once per
            # season, inside a single caller-owned transaction (``ensure_seasons``), so
            # losing one season's race must never wipe out sibling seasons already
            # flushed earlier in the SAME transaction.
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError:
            # A concurrent ensure() for the SAME (show, season) won the race: the
            # unconditional ``uq_season_requests_media_season`` unique index
            # rejected this insert. Resolve to the winner's row instead of
            # crashing (idempotent dedup, honesty over silence) -- mirrors the
            # IntegrityError-catch-and-reread pattern at
            # ``request_service.py:159-184``.
            winner = await self._find(media_request_id, season_number)
            if winner is None:  # pragma: no cover - the conflicting row must exist
                raise
            return _to_record(winner, await self._tmdb_id_for(media_request_id))
        await self._session.refresh(row)
        return _to_record(row, await self._tmdb_id_for(media_request_id))

    async def set_status(self, season_request_id: int, status: str) -> None:
        row = await self._session.get(SeasonRequest, season_request_id)
        if row is None:
            raise LookupError(f"season request {season_request_id} does not exist")
        row.status = RequestStatus(status)
        await self._session.flush()

    async def mark_completed(self, season_request_id: int) -> None:
        """Set ``completed`` (imported, scan triggered) -- the honest pre-``available`` state."""
        await self.set_status(season_request_id, RequestStatus.completed.value)

    async def mark_available(self, season_request_id: int) -> None:
        """Set ``available`` (Plex-confirmed: ``leafCount>0`` for this season)."""
        await self.set_status(season_request_id, RequestStatus.available.value)
