"""``RequestRepository`` implementation over an :class:`AsyncSession`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select

from plex_manager.models import MediaRequest, MediaType, RequestStatus
from plex_manager.ports.repositories import RequestRecord

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = ["SqlRequestRepository"]

# Statuses at which a request is SETTLED and no longer dedup-blocking — a new
# request for the same media is allowed once the prior one reaches one of these.
# ``completed`` is deliberately NOT here: it is the in-flight "Finalizing" state
# (imported, before Plex confirms availability), so it must keep deduping a second
# request (and a second grab) for the same movie until it reaches available/failed.
# ``evicted`` (ADR-0012) belongs here for the SAME reason as available/failed: the
# disk-pressure sweep already deleted the file, so the old row must never shadow a
# fresh re-request that actually re-grabs the content. This MUST stay in sync with
# ``uq_media_requests_active``'s partial-index predicate in ``models.py`` (also
# ADR-0012), which excludes ``evicted`` from the DB backstop for the identical
# reason — see ``RequestStatus.evicted``'s docstring there.
_SETTLED_REQUEST_STATUSES: frozenset[RequestStatus] = frozenset(
    {RequestStatus.available, RequestStatus.failed, RequestStatus.evicted}
)


def _to_record(row: MediaRequest) -> RequestRecord:
    """Map a ``MediaRequest`` ORM row to its frozen read-model DTO."""
    return RequestRecord(
        id=row.id,
        tmdb_id=row.tmdb_id,
        media_type=row.media_type.value,
        title=row.title,
        status=row.status.value,
        year=row.year,
        is_anime=bool(row.is_anime),
        user_id=row.user_id,
        poster_url=row.poster_url,
        backdrop_url=row.backdrop_url,
        library_path=row.library_path,
        keep_forever=bool(row.keep_forever),
    )


class SqlRequestRepository:
    """Persist and read media requests via SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, request_id: int) -> RequestRecord | None:
        row = await self._session.get(MediaRequest, request_id)
        return _to_record(row) if row is not None else None

    async def list_by_status(self, status: str | None = None) -> list[RequestRecord]:
        stmt = select(MediaRequest)
        if status is not None:
            stmt = stmt.where(MediaRequest.status == RequestStatus(status))
        stmt = stmt.order_by(MediaRequest.id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_record(row) for row in rows]

    async def find_active(self, tmdb_id: int, media_type: str) -> RequestRecord | None:
        stmt = (
            select(MediaRequest)
            .where(
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.media_type == MediaType(media_type),
                MediaRequest.status.notin_(_SETTLED_REQUEST_STATUSES),
            )
            .order_by(MediaRequest.id)
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return _to_record(row) if row is not None else None

    async def find_in_library(self, tmdb_id: int, media_type: str) -> RequestRecord | None:
        stmt = (
            select(MediaRequest)
            .where(
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.media_type == MediaType(media_type),
                MediaRequest.status.in_([RequestStatus.available, RequestStatus.completed]),
            )
            .order_by(MediaRequest.id.desc())
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return _to_record(row) if row is not None else None

    async def find_earliest_available(self, tmdb_id: int, media_type: str) -> RequestRecord | None:
        """Return the OLDEST ``available`` request for this media (lowest id), or None.

        Anchors the in-library short-circuit race-collapse: two concurrent requests
        can each pass ``find_in_library`` (neither committed yet) and insert a separate
        ``available`` row, which the active-dedup partial UNIQUE index does NOT reject
        (it excludes terminal ``available``). After committing, ``create_request``
        re-reads the earliest available row and deletes any later duplicate of it.
        Scoped to ``available`` only (not ``completed``) so an in-flight re-acquire is
        never mistaken for a race loser.
        """
        stmt = (
            select(MediaRequest)
            .where(
                MediaRequest.tmdb_id == tmdb_id,
                MediaRequest.media_type == MediaType(media_type),
                MediaRequest.status == RequestStatus.available,
            )
            .order_by(MediaRequest.id)
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return _to_record(row) if row is not None else None

    async def delete(self, request_id: int) -> None:
        """Delete a request row (collapse a race-loser duplicate). No-op if absent."""
        row = await self._session.get(MediaRequest, request_id)
        if row is not None:
            await self._session.delete(row)
            await self._session.flush()

    async def create(
        self,
        *,
        tmdb_id: int,
        media_type: str,
        title: str,
        status: str,
        year: int | None = None,
        is_anime: bool = False,
        user_id: int | None = None,
        poster_url: str | None = None,
        backdrop_url: str | None = None,
    ) -> RequestRecord:
        row = MediaRequest(
            tmdb_id=tmdb_id,
            media_type=MediaType(media_type),
            title=title,
            status=RequestStatus(status),
            year=year,
            is_anime=is_anime,
            user_id=user_id,
            poster_url=poster_url,
            backdrop_url=backdrop_url,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.refresh(row)
        return _to_record(row)

    async def set_status(self, request_id: int, status: str) -> None:
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.status = RequestStatus(status)
        await self._session.flush()

    async def mark_completed(self, request_id: int) -> None:
        """Set ``completed`` + stamp ``completed_at`` (imported, scan triggered)."""
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.status = RequestStatus.completed
        row.completed_at = datetime.now(UTC)
        await self._session.flush()

    async def mark_available(self, request_id: int) -> None:
        """Set ``available`` + stamp ``library_verified_at`` (Plex-confirmed)."""
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        now = datetime.now(UTC)
        row.status = RequestStatus.available
        row.library_verified_at = now
        if row.completed_at is None:
            row.completed_at = now
        await self._session.flush()

    async def set_library_path(self, request_id: int, library_path: str) -> None:
        """Store the final placed path this request's import wrote into (ADR-0012)."""
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.library_path = library_path
        await self._session.flush()

    async def set_keep_forever(self, request_id: int, keep_forever: bool) -> None:
        """Set the operator's "keep forever" pin (ADR-0012)."""
        row = await self._session.get(MediaRequest, request_id)
        if row is None:
            raise LookupError(f"media request {request_id} does not exist")
        row.keep_forever = keep_forever
        await self._session.flush()
