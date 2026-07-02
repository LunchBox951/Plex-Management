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
from plex_manager.logsafe import safe_int
from plex_manager.models import RequestStatus
from plex_manager.repositories.requests import SqlRequestRepository
from plex_manager.services import season_request_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from plex_manager.ports.library import LibraryPort
    from plex_manager.ports.metadata import MetadataPort
    from plex_manager.ports.repositories import RequestRecord

__all__ = [
    "TERMINAL_REQUEST_STATUS_VALUES",
    "MediaNotFoundError",
    "NoAiredSeasonsError",
    "create_request",
    "get_request",
    "list_requests",
    "mark_available",
    "mark_completed",
    "mark_no_acceptable_release",
    "set_keep_forever",
]

_logger = logging.getLogger(__name__)

# Request statuses (string values) at which a request is FINISHED. A terminal
# request must never be re-armed to a non-terminal status: a newer ACTIVE request
# for the same ``(tmdb_id, media_type)`` owns the ``uq_media_requests_active``
# slot, so resurrecting an old terminal row as active would re-block dedup against
# a dead-end ghost. The canonical source for the string-valued set (the SQL-side
# enum set lives in ``repositories.requests``); ``grab_service`` reuses this.
#
# ``evicted`` (ADR-0012) belongs here too: it is terminal FOR GRAB PURPOSES --
# there is nothing left on disk for this exact row to resume -- even though it is
# re-requestable (a fresh ``POST /requests`` creates a brand-new row, since
# ``evicted`` is excluded from ``uq_media_requests_active``'s predicate, exactly
# like ``available``/``failed`` above). Without this, a stale/evicted request id
# handed to ``/queue/grab`` would pass this gate, qbt.add() a torrent, and only
# THEN fail trying to move this row to ``downloading`` -- if a fresh request for
# the same media already exists (a new active row owns the unique slot), that
# update collides with ``uq_media_requests_active`` and the just-added torrent is
# left untracked. Rejecting up front (``RequestNotActiveError``, HTTP 409) means
# nothing is ever added to the client for an evicted row.
TERMINAL_REQUEST_STATUS_VALUES: Final[frozenset[str]] = frozenset(
    s.value
    for s in (
        RequestStatus.completed,
        RequestStatus.available,
        RequestStatus.failed,
        RequestStatus.evicted,
        # ADR-0014: a cancelled request is terminal for grab purposes -- a stale
        # cancelled id handed to /queue/grab must be rejected up front (nothing
        # left to resume), exactly like available/failed/evicted above.
        RequestStatus.cancelled,
    )
)


class MediaNotFoundError(Exception):
    """The metadata port could not resolve the requested ``(tmdb_id, media_type)``.

    Surfaced as HTTP 404 — an honest "not found", never a silently empty request.
    """

    def __init__(self, tmdb_id: int, media_type: str) -> None:
        self.tmdb_id = tmdb_id
        self.media_type = media_type
        super().__init__(f"{media_type} tmdb_id={tmdb_id} not found")


class NoAiredSeasonsError(Exception):
    """A whole-series tv request resolved to ZERO trackable seasons.

    Raised when the caller named no explicit ``seasons`` (a "track the whole
    aired series" request) and TMDB's ``season_count`` is ``0`` — a TMDB data
    gap, or a specials-only show. Without this guard, ``create_request`` would
    persist a ``pending`` :class:`~plex_manager.models.MediaRequest` with ZERO
    ``SeasonRequest`` rows: nothing would ever drive search/grab for it (the
    parent's rollup has no seasons to fold), and the request would show
    ``pending`` forever — a silent dead request, the exact dishonesty
    north-star #3 forbids. Surfaced as HTTP 404, the same posture as
    :class:`MediaNotFoundError` ("resolved, but nothing to act on"), never a
    persisted ghost request.
    """

    def __init__(self, tmdb_id: int) -> None:
        self.tmdb_id = tmdb_id
        super().__init__(f"tv tmdb_id={tmdb_id} resolved to zero aired seasons")


class _Detail(NamedTuple):
    """Resolved TMDB detail needed to persist a request (incl. art for rows).

    ``season_count`` is movie-irrelevant (defaults to ``0``); for tv it feeds
    :func:`_season_numbers` when the caller omitted an explicit season list.
    """

    title: str
    year: int | None
    is_anime: bool
    poster_url: str | None
    backdrop_url: str | None
    season_count: int = 0


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
    return _Detail(tv.title, tv.year, tv.is_anime, tv.poster_url, tv.backdrop_url, tv.season_count)


def _season_numbers(seasons: list[int] | None, season_count: int) -> list[int]:
    """Resolve the season numbers a tv request should track.

    ``seasons`` verbatim when the caller named specific seasons (an out-of-range
    season is harmless -- it just tracks one Plex/TMDB doesn't have). Omitted or
    empty means "the whole aired series": every season ``1..season_count``,
    SPECIALS (season 0) excluded -- a whole-show request never auto-tracks the
    specials bucket.
    """
    return seasons if seasons else list(range(1, season_count + 1))


async def _resolve_tv_seasons(
    tmdb: MetadataPort, tmdb_id: int, seasons: list[int] | None
) -> list[int]:
    """Like :func:`_season_numbers`, but fetches ``season_count`` from TMDB itself.

    For call sites that never resolved a ``_Detail`` (the dedup / integrity-race
    paths below skip that TMDB round-trip when an explicit ``seasons`` list makes
    it unnecessary) -- fetches only when ``seasons`` is falsy.
    """
    if seasons:
        return seasons
    tv = await tmdb.get_tv_show(tmdb_id)
    if tv is None:
        raise MediaNotFoundError(tmdb_id, "tv")
    return _season_numbers(seasons, tv.season_count)


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
            "plex availability check failed (%s); proceeding with a request",
            type(exc).__name__,
            extra={"tmdb_id": safe_int(tmdb_id)},
        )
        return False


async def _collapse_available_race(
    session: AsyncSession,
    repo: SqlRequestRepository,
    record: RequestRecord,
    tmdb_id: int,
    media_type: str,
) -> RequestRecord:
    """Collapse a concurrent in-library create race, returning the surviving record.

    The active-dedup partial UNIQUE index excludes terminal ``available``, so two
    ``POST /requests`` that both saw the title as already-in-Plex (neither committed
    yet) can each insert a fresh ``available`` row with no IntegrityError backstop.
    Once ours is committed, re-read the OLDEST ``available`` row for this media; if an
    earlier one exists, THIS row is the race loser -> delete it and return the winner,
    so the list/modal shows ONE row. Movie and TV share this (TV reaches ``available``
    via the season rollup, the movie via the in-library short-circuit).
    """
    winner = await repo.find_earliest_available(tmdb_id, media_type)
    if winner is not None and winner.id != record.id:
        await repo.delete(record.id)
        await session.commit()
        return winner
    return record


async def _present_seasons_or_empty(library: LibraryPort, tmdb_id: int) -> frozenset[int]:
    """Best-effort per-season Plex presence; an error is an explicit, logged empty set.

    The TV analogue of :func:`_already_in_library`: ONE fresh crawl (never a stale
    cache) yields the seasons already in Plex so an all-present re-request can dedup
    to the existing in-library record. A transient outage / deferred check must not
    block a request, so a failure is logged and treated as "prove nothing present"
    (fall through to a normal tracked request), not a swallowed empty set.
    """
    try:
        return await library.present_seasons(tmdb_id)
    except (PlexLibraryError, PlexAuthError, NotImplementedError) as exc:
        _logger.warning(
            "plex season-presence check failed (%s); proceeding with a request",
            type(exc).__name__,
            extra={"tmdb_id": safe_int(tmdb_id)},
        )
        return frozenset()


async def create_request(
    session: AsyncSession,
    tmdb: MetadataPort,
    *,
    tmdb_id: int,
    media_type: str,
    user_id: int | None = None,
    library: LibraryPort | None = None,
    seasons: list[int] | None = None,
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

    For a tv ``media_type``, ``season_request_service.ensure_seasons`` runs on
    EVERY call -- including the dedup (early-return) path -- for ``seasons``
    verbatim, or every aired season when omitted/empty (see
    :func:`_season_numbers`). So a second POST for the same show with a NEW season
    list grows the tracked set rather than being silently dropped by the
    request-level dedup; the parent's rollup status (never a movie-style
    already-in-library short-circuit) is computed by ``ensure_seasons`` itself.

    Raises :class:`NoAiredSeasonsError` for a FRESH (not dedup) whole-series tv
    request whose resolved season list is empty (see :func:`_season_numbers`) --
    never persists a request with nothing to track. The dedup path never raises
    this: an existing request resolving no NEW seasons there just means "nothing
    to add" to an already-viable request, not a dead end.
    """
    repo = SqlRequestRepository(session)
    existing = await repo.find_active(tmdb_id, media_type)
    if existing is not None:
        if media_type == "tv":
            season_numbers = await _resolve_tv_seasons(tmdb, tmdb_id, seasons)
            await season_request_service.ensure_seasons(
                session,
                library,
                media_request_id=existing.id,
                tmdb_id=tmdb_id,
                seasons=season_numbers,
            )
            await session.commit()
            # ensure_seasons recomputed + persisted the parent rollup; re-read so the
            # returned record's top-level status matches the seasons the response will
            # embed. ``existing`` was captured by find_active BEFORE that rollup write.
            existing = await repo.get(existing.id) or existing
        return existing

    detail = await _resolve_detail(tmdb, tmdb_id, media_type)

    # Resolve the season list BEFORE anything is persisted: a whole-series
    # request (no explicit ``seasons``) that resolves to NOTHING trackable must
    # never become a 'pending' request with zero SeasonRequest rows (see
    # NoAiredSeasonsError). An explicit (even out-of-range) season list is left
    # alone -- tracking a season Plex/TMDB doesn't have yet is harmless.
    season_numbers: list[int] = []
    if media_type == "tv":
        season_numbers = _season_numbers(seasons, detail.season_count)
        if not season_numbers:
            raise NoAiredSeasonsError(tmdb_id)

    initial_status = RequestStatus.pending.value
    if (
        library is not None
        and media_type == "movie"
        and await _already_in_library(library, tmdb_id)
    ):
        # Dedup the available short-circuit: if this movie is already recorded as
        # in-library, return that row rather than accumulating duplicate 'available'
        # rows (the active-dedup partial index excludes terminal statuses, so it
        # would not catch this). A movie REMOVED from Plex reads not-available above
        # and falls through to a normal pending request, so re-requests still work.
        in_library = await repo.find_in_library(tmdb_id, media_type)
        if in_library is not None:
            return in_library
        initial_status = RequestStatus.available.value

    if media_type == "tv" and library is not None and season_numbers:
        # TV in-library dedup — the per-season analogue of the movie short-circuit
        # above. When EVERY requested season is already in Plex AND an
        # available/completed request for this show already exists, return it
        # (tracking any of these seasons it doesn't already list) instead of
        # inserting a duplicate terminal 'available' MediaRequest + season rows: the
        # active-dedup partial index excludes terminal 'available'/'completed', and
        # the movie collapse below is movie-only, so nothing else would catch the
        # duplicate. A show with a NEW (not-yet-present) season falls through to a
        # normal tracked request so the missing season is still searched/grabbed.
        present = await _present_seasons_or_empty(library, tmdb_id)
        if present.issuperset(season_numbers):
            in_library = await repo.find_in_library(tmdb_id, media_type)
            if in_library is not None:
                await season_request_service.ensure_seasons(
                    session,
                    library,
                    media_request_id=in_library.id,
                    tmdb_id=tmdb_id,
                    seasons=season_numbers,
                )
                await session.commit()
                return await repo.get(in_library.id) or in_library
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
        if media_type == "tv":
            # Always starts 'pending' above (the movie-only in-library short-circuit
            # never applies to tv); ensure_seasons' own per-season availability check
            # recomputes the honest rollup (possibly 'available'/'partially_available')
            # onto the SAME row, in the SAME transaction, before it is committed below.
            # ``season_numbers`` was already resolved (and guaranteed non-empty)
            # above, before this request row was even created.
            await season_request_service.ensure_seasons(
                session,
                library,
                media_request_id=record.id,
                tmdb_id=tmdb_id,
                seasons=season_numbers,
            )
        await session.commit()
        if media_type == "tv":
            # ``record`` was captured at repo.create (status 'pending') before
            # ensure_seasons recomputed the parent rollup onto the SAME row; re-read
            # so the returned top-level status matches the seasons the response embeds
            # (e.g. all-already-in-Plex -> 'available', a mix -> 'partially_available').
            record = await repo.get(record.id) or record
            if record.status == RequestStatus.available.value:
                # All requested seasons were already in Plex -> terminal 'available'
                # (outside the active-dedup index), so two racing creates can each
                # leave an available row. Collapse to the oldest, same as the movie
                # path below (which never runs for tv: its initial_status is pending).
                winner = await _collapse_available_race(session, repo, record, tmdb_id, media_type)
                if winner.id != record.id:
                    # THIS row (the loser) was deleted, cascading ITS SeasonRequests.
                    # The two racers may have named DIFFERENT seasons, so ensure the
                    # winner also tracks the seasons THIS request asked for -- else the
                    # caller gets back a request that doesn't track the season it just
                    # requested. Then re-read past the merged rollup.
                    await season_request_service.ensure_seasons(
                        session,
                        library,
                        media_request_id=winner.id,
                        tmdb_id=tmdb_id,
                        seasons=season_numbers,
                    )
                    await session.commit()
                    winner = await repo.get(winner.id) or winner
                record = winner
    except IntegrityError:
        # A concurrent POST /requests for the same (tmdb_id, media_type) won the
        # race: the partial UNIQUE index over active statuses rejected this insert.
        # Resolve to the existing active request instead of crashing (idempotent
        # dedup, honesty over silence). The failed transaction is rolled back first.
        await session.rollback()
        winner = await repo.find_active(tmdb_id, media_type)
        if winner is None:  # pragma: no cover - the conflicting active row must exist
            raise
        if media_type == "tv":
            season_numbers = await _resolve_tv_seasons(tmdb, tmdb_id, seasons)
            await season_request_service.ensure_seasons(
                session,
                library,
                media_request_id=winner.id,
                tmdb_id=tmdb_id,
                seasons=season_numbers,
            )
            await session.commit()
            # Re-read past the rollup ensure_seasons just persisted (``winner`` was
            # captured before it), so the returned status matches the response's seasons.
            winner = await repo.get(winner.id) or winner
        return winner
    if initial_status == RequestStatus.available.value:
        # Collapse the concurrent movie in-library race (F9) via the shared helper.
        # The remove-then-re-acquire flow is unaffected: when a movie was removed from
        # Plex, _already_in_library() reads False and this branch is skipped, so the
        # legitimate SECOND available row from the normal pending -> download ->
        # mark_available path is never reconciled away.
        record = await _collapse_available_race(session, repo, record, tmdb_id, media_type)
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


async def set_keep_forever(
    session: AsyncSession, request_id: int, *, keep_forever: bool
) -> RequestRecord | None:
    """Toggle the operator's "keep forever" pin (ADR-0012) for the WHOLE title.

    Keep-forever is a per-TITLE intent, not a per-row one: because
    ``uq_media_requests_active`` only constrains ACTIVE rows, a single
    ``(tmdb_id, media_type)`` can have several ``MediaRequest`` rows over its
    lifetime -- e.g. an older SETTLED ``available`` request covering seasons
    1-2 and a newer ACTIVE request for season 3. The UI resolves a title to
    its (visible) active row and passes that row's ``request_id`` here, but
    ``eviction_service._season_candidates`` reads ``keep_forever`` off EACH
    season's OWN parent -- so pinning only the active row would leave the
    settled sibling's seasons unpinned and still evictable even though the
    operator believes they just pinned the whole show. This resolves the
    target row first (for its ``tmdb_id``/``media_type``), then applies the
    pin to EVERY row sharing that key via
    :meth:`~plex_manager.ports.repositories.RequestRepository.
    set_keep_forever_for_title` -- symmetric for both pin and unpin.

    Returns ``None`` when the request does not exist (the router surfaces
    404); otherwise commits and returns the freshly updated TARGET record so
    the endpoint can hand back the new state without a second round trip.
    """
    repo = SqlRequestRepository(session)
    current = await repo.get(request_id)
    if current is None:
        return None
    await repo.set_keep_forever_for_title(current.tmdb_id, current.media_type, keep_forever)
    await session.commit()
    return await repo.get(request_id)
