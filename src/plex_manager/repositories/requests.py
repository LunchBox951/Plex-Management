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

# Statuses at which a request is finished and no longer dedup-blocking. A new
# request for the same media is allowed once the prior one reaches one of these.
_TERMINAL_REQUEST_STATUSES: frozenset[RequestStatus] = frozenset(
    {RequestStatus.completed, RequestStatus.available, RequestStatus.failed}
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
                MediaRequest.status.notin_(_TERMINAL_REQUEST_STATUSES),
            )
            .order_by(MediaRequest.id)
            .limit(1)
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return _to_record(row) if row is not None else None

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
