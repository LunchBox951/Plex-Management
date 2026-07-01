"""Request orchestration — resolve TMDB detail, dedup, persist a media request.

``create_request`` resolves the movie / TV detail (title, year, anime flag) from
the metadata port, dedups against any in-flight request for the same
``(tmdb_id, media_type)`` (the composite the model indexes), and persists a new
``media_requests`` row when none exists. The dedup is honest: an existing active
request is *returned*, not silently re-created, so a double-submit is idempotent.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final, NamedTuple

from sqlalchemy.exc import IntegrityError

from plex_manager.adapters.plex.library import PlexAuthError, PlexLibraryError
from plex_manager.models import RequestStatus
from plex_manager.repositories.requests import SqlRequestRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.metadata import MetadataPort
    from plex_manager.ports.repositories import RequestRecord

__all__ = [
    "TERMINAL_REQUEST_STATUS_VALUES",
    "MediaNotFoundError",
    "MediaTypeDeferredError",
    "create_request",
    "get_request",
    "list_requests",
    "mark_available",
    "mark_completed",
    "mark_no_acceptable_release",
]

_logger = logging.getLogger(__name__)

# Request statuses (string values) at which a request is FINISHED. A terminal
# request must never be re-armed to a non-terminal status: a newer ACTIVE request
# for the same ``(tmdb_id, media_type)`` owns the ``uq_media_requests_active``
# slot, so resurrecting an old terminal row as active would re-block dedup against
# a dead-end ghost. The canonical source for the string-valued set (the SQL-side
# enum set lives in ``repositories.requests``); ``grab_service`` reuses this.
TERMINAL_REQUEST_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    s.value for s in (RequestStatus.completed, RequestStatus.available, RequestStatus.failed)
)


class MediaNotFoundError(Exception):
    """The metadata port could not resolve the requested ``(tmdb_id, media_type)``.

    Surfaced as HTTP 404 — an honest "not found", never a silently empty request.
    """

    def __init__(self, tmdb_id: int, media_type: str) -> None:
        self.tmdb_id = tmdb_id
        self.media_type = media_type
        super().__init__(f"{media_type} tmdb_id={tmdb_id} not found")


class MediaTypeDeferredError(Exception):
    """The app cannot safely process this media type yet."""

    def __init__(self, media_type: str) -> None:
        self.media_type = media_type
        super().__init__(f"{media_type} requests are deferred")


class _Detail(NamedTuple):
    """Resolved TMDB detail needed to persist a request (incl. art for rows)."""

    title: str
    year: int | None
    is_anime: bool
    poster_url: str | None
    backdrop_url: str | None


async def _resolve_detail(tmdb: MetadataPort, tmdb_id: int, media_type: str) -> _Detail:
    """Return the request detail (title/year/anime + art), or raise if unresolved."""
    if media_type == "movie":
        movie = await tmdb.get_movie(tmdb_id)
        if movie is None:
            raise MediaNotFoundError(tmdb_id, media_type)
        return _Detail(
            movie.title, movie.year, movie.is_anime, movie.poster_url, movie.backdrop_url
        )
    tv = await tmdb.get_tv_show(tmdb_id)
    if tv is None:
        raise MediaNotFoundError(tmdb_id, media_type)
    return _Detail(tv.title, tv.year, tv.is_anime, tv.poster_url, tv.backdrop_url)


async def _already_in_library(library: LibraryPort, tmdb_id: int) -> bool:
    """Best-effort Plex availability check; an error is an explicit, logged 'no'.

    Honesty over silence (and never strand the request): a transient Plex outage or
    the deferred-TV ``NotImplementedError`` must not block a request. The failure is
    logged and treated as "can't prove it's a dup", so the request proceeds normally
    — an explicit decision, not a swallowed ``False`` (the prototype's bug).

    ``use_cache=False``: the dedup decision must reflect Plex as it is NOW. The
    cached-presence fast path would otherwise return a stale True after an operator
    REMOVES a movie and immediately re-requests it (within the cache TTL), returning
    the old 'available' row instead of a fresh pending request (G7). The cost — one
    section-page walk per interactive create — is bounded by library size and only on
    this low-frequency path; the reconcile loop keeps the cached fast path.
    """
    try:
        return await library.is_available(tmdb_id, "movie", use_cache=False)
    except (PlexLibraryError, PlexAuthError, NotImplementedError) as exc:
        _logger.warning(
            "plex availability check failed for tmdb %s (%s); proceeding with a request",
            tmdb_id,
            type(exc).__name__,
        )
        return False


async def create_request(
    session: AsyncSession,
    tmdb: MetadataPort,
    *,
    tmdb_id: int,
    media_type: str,
    user_id: int | None = None,
    library: LibraryPort | None = None,
) -> RequestRecord:
    """Create (or return the existing active) media request for this media.

    Dedups on the ``(tmdb_id, media_type)`` composite via
    :meth:`RequestRepository.find_active`: a non-terminal request for the same
    media is returned unchanged. Otherwise the TMDB detail (incl. art) is resolved
    and a new ``pending`` request is persisted.

    When ``library`` is supplied and the movie is **already in Plex**, the request
    is recorded directly as ``available`` (and ``library_verified_at`` stamped),
    short-circuiting the search/grab — a visible "already in your library" record,
    not a wasted grab. An unconfigured/unreachable Plex skips the check (see
    :func:`_already_in_library`).
    """
    if media_type != "movie":
        raise MediaTypeDeferredError(media_type)

    repo = SqlRequestRepository(session)
    existing = await repo.find_active(tmdb_id, media_type)
    if existing is not None:
        return existing

    detail = await _resolve_detail(tmdb, tmdb_id, media_type)

    initial_status = RequestStatus.pending.value
    if (
        library is not None
        and media_type == "movie"
        and await _already_in_library(library, tmdb_id)
    ):
        # Dedup the available short-circuit: if this movie is already recorded as
        # in-library, return that row rather than accumulating duplicate 'available'
        # rows (the active-dedup partial index excludes terminal statuses, so it
        # would not catch this). Acquire a per-media DB lock first so PostgreSQL MVCC
        # cannot let two concurrent transactions both miss each other's uncommitted
        # terminal row. A movie REMOVED from Plex reads not-available above and falls
        # through to a normal pending request, so re-requests still work.
        await repo.acquire_media_lock(tmdb_id, media_type)
        in_library = await repo.find_in_library(tmdb_id, media_type)
        if in_library is not None:
            return in_library
        initial_status = RequestStatus.available.value
    try:
        record = await repo.create(
            tmdb_id=tmdb_id,
            media_type=media_type,
            title=detail.title,
            status=initial_status,
            year=detail.year,
            is_anime=detail.is_anime,
            user_id=user_id,
            poster_url=detail.poster_url,
            backdrop_url=detail.backdrop_url,
        )
        if initial_status == RequestStatus.available.value:
            # It IS in Plex — stamp library_verified_at so the record is honest.
            await repo.mark_available(record.id)
        await session.commit()
    except IntegrityError:
        # A concurrent POST /requests for the same (tmdb_id, media_type) won the
        # race: the partial UNIQUE index over active statuses rejected this insert.
        # Resolve to the existing active request instead of crashing (idempotent
        # dedup, honesty over silence). The failed transaction is rolled back first.
        await session.rollback()
        winner = await repo.find_active(tmdb_id, media_type)
        if winner is None:  # pragma: no cover - the conflicting active row must exist
            raise
        return winner
    if initial_status == RequestStatus.available.value:
        # Collapse the concurrent in-library race (F9). The active-dedup partial
        # UNIQUE index excludes terminal 'available', so two POSTs that BOTH passed
        # find_in_library above (neither had committed yet) can each insert a fresh
        # 'available' row with NO IntegrityError backstop -> duplicate rows. Now that
        # ours is committed, re-read the OLDEST available row for this media; if an
        # earlier one exists, THIS row is the race loser -> delete it and return the
        # winner, so the Requests list/modal shows ONE row.
        #
        # The remove-then-re-acquire flow is unaffected: when a movie was removed
        # from Plex, _already_in_library() reads False and this whole short-circuit
        # branch is skipped, so the (legitimate) SECOND available row produced by the
        # normal pending -> download -> mark_available path is never reconciled away.
        winner = await repo.find_earliest_available(tmdb_id, media_type)
        if winner is not None and winner.id != record.id:
            await repo.delete(record.id)
            await session.commit()
            return winner
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


async def mark_no_acceptable_release(session: AsyncSession, request_id: int) -> None:
    """Persist ``no_acceptable_release`` on the request when a grab finds nothing.

    Honesty over silence: a live grab that finds no acceptable candidate returns
    409, but without this the owning request would stay ``downloading`` /
    ``searching`` — a dishonest status asserting progress that is not happening.
    ``no_acceptable_release`` is a visible, retryable state (the operator can
    re-search later), not a silent ``failed``.

    A request that is already TERMINAL (``completed`` / ``available`` / ``failed``)
    is left untouched: ``no_acceptable_release`` is itself non-terminal and
    dedup-blocking, so writing it over a finished request would resurrect it as a
    ghost that re-blocks a fresh request for the same media. Never un-terminate a
    finished request.
    """
    repo = SqlRequestRepository(session)
    current = await repo.get(request_id)
    if current is not None and current.status in TERMINAL_REQUEST_STATUS_VALUES:
        return
    await repo.set_status(request_id, RequestStatus.no_acceptable_release.value)
    await session.commit()


async def mark_completed(session: AsyncSession, request_id: int) -> None:
    """Phase 1 of honest availability: imported + Plex scan triggered ("Finalizing").

    The file is in the library folder and a scan was triggered, but Plex has not yet
    confirmed it is indexed — so this is ``completed``, not ``available``. The
    reconcile loop later confirms via ``is_available`` and promotes it (phase 2).
    """
    await SqlRequestRepository(session).mark_completed(request_id)
    await session.commit()


async def mark_available(session: AsyncSession, request_id: int) -> None:
    """Phase 2 of honest availability: Plex has confirmed the title is in the library."""
    await SqlRequestRepository(session).mark_available(request_id)
    await session.commit()
