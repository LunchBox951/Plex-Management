"""Request orchestration — resolve TMDB detail, dedup, persist a media request.

``create_request`` resolves the movie / TV detail (title, year, anime flag) from
the metadata port, dedups against any in-flight request for the same
``(tmdb_id, media_type)`` (the composite the model indexes), and persists a new
``media_requests`` row when none exists. The dedup is honest: an existing active
request is *returned*, not silently re-created, so a double-submit is idempotent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from plex_manager.models import RequestStatus
from plex_manager.repositories.requests import SqlRequestRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.metadata import MetadataPort
    from plex_manager.ports.repositories import RequestRecord

__all__ = ["MediaNotFoundError", "create_request", "get_request", "list_requests"]


class MediaNotFoundError(Exception):
    """The metadata port could not resolve the requested ``(tmdb_id, media_type)``.

    Surfaced as HTTP 404 — an honest "not found", never a silently empty request.
    """

    def __init__(self, tmdb_id: int, media_type: str) -> None:
        self.tmdb_id = tmdb_id
        self.media_type = media_type
        super().__init__(f"{media_type} tmdb_id={tmdb_id} not found")


async def _resolve_detail(
    tmdb: MetadataPort,
    tmdb_id: int,
    media_type: str,
) -> tuple[str, int | None, bool]:
    """Return ``(title, year, is_anime)`` for the media, or raise if unresolved."""
    if media_type == "movie":
        movie = await tmdb.get_movie(tmdb_id)
        if movie is None:
            raise MediaNotFoundError(tmdb_id, media_type)
        return movie.title, movie.year, movie.is_anime
    tv = await tmdb.get_tv_show(tmdb_id)
    if tv is None:
        raise MediaNotFoundError(tmdb_id, media_type)
    return tv.title, tv.year, tv.is_anime


async def create_request(
    session: AsyncSession,
    tmdb: MetadataPort,
    *,
    tmdb_id: int,
    media_type: str,
    user_id: int | None = None,
) -> RequestRecord:
    """Create (or return the existing active) media request for this media.

    Dedups on the ``(tmdb_id, media_type)`` composite via
    :meth:`RequestRepository.find_active`: a non-terminal request for the same
    media is returned unchanged. Otherwise the TMDB detail is resolved and a new
    ``pending`` request is persisted.
    """
    repo = SqlRequestRepository(session)
    existing = await repo.find_active(tmdb_id, media_type)
    if existing is not None:
        return existing

    title, year, is_anime = await _resolve_detail(tmdb, tmdb_id, media_type)
    record = await repo.create(
        tmdb_id=tmdb_id,
        media_type=media_type,
        title=title,
        status=RequestStatus.pending.value,
        year=year,
        is_anime=is_anime,
        user_id=user_id,
    )
    await session.commit()
    return record


async def list_requests(
    session: AsyncSession,
    status: str | None = None,
) -> list[RequestRecord]:
    """List media requests, optionally filtered by ``status``."""
    return await SqlRequestRepository(session).list_by_status(status)


async def get_request(session: AsyncSession, request_id: int) -> RequestRecord | None:
    """Return the request by id, or ``None`` if absent."""
    return await SqlRequestRepository(session).get(request_id)
